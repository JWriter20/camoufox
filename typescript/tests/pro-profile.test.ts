/**
 * Port of pythonlib/tests/test_pro_profile.py: what a Camoufox Pro lease
 * grants beyond the lease itself -- a profile's identity and synced state,
 * managed egress, remote rendering and the captcha solver -- and how the
 * launcher applies each.
 *
 * Every test talks to a local fake of the Camoufox Pro API and of the object
 * store its presigned URLs point at; no test reaches the network. The crypto
 * vectors are the synthetic ones the API's own code generates.
 */
import { createHash, randomBytes, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as http from "node:http";
import type { AddressInfo } from "node:net";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import {
	afterAll,
	afterEach,
	beforeAll,
	beforeEach,
	describe,
	expect,
	it,
	vi,
} from "vitest";
import {
	BUNDLE,
	configOf,
	HOME,
	quietly,
	restoreDeps,
	SCRATCH,
	stubHost,
	utils,
} from "./launch-host.js";
import { prerequisite } from "./prereq.js";

const pro = await import("../src/pro.js");
const errors = await import("../src/exceptions.js");
const state = await import("../src/pro-state.js");
const profiles = await import("../src/pro-profile.js");
const { ensureModel } = await import("../src/fpgen/index.js");

let modelReady = true;
try {
	await ensureModel();
} catch (e) {
	modelReady = prerequisite("fpgen-model", false, String(e));
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VECTORS = JSON.parse(
	fs.readFileSync(
		path.resolve(HERE, "../../pythonlib/tests/data/pro-profile-state-v1.json"),
		"utf-8",
	),
);
const BUILD_HASH = `sha256:${"ab".repeat(32)}`;
const KEY = `cfp_live_${"k".repeat(43)}`;
const BUILD = {
	build_hash: BUILD_HASH,
	version: "156.0.1-pro.1",
	target: "linux-x86_64",
};
const PROFILE_ID = "prf_0191f3a2-7c1e-7b52-9a10-3f9e6d5a1c22";
const ACCOUNT = 42;

/** The vectors' input generator: SHA-256 in counter mode. */
function stream(size: number, label: string): Buffer {
	const out: Buffer[] = [];
	for (let counter = 0n, length = 0; length < size; counter++) {
		const block = Buffer.alloc(8);
		block.writeBigUInt64BE(counter);
		const digest = createHash("sha256")
			.update(Buffer.concat([Buffer.from(label), block]))
			.digest();
		out.push(digest);
		length += digest.length;
	}
	return Buffer.concat(out).subarray(0, size);
}

const sha256 = (data: Uint8Array) =>
	createHash("sha256").update(data).digest("hex");

function tokenFor(leaseId: string) {
	const now = Math.floor(Date.now() / 1000);
	const u64 = (n: number) => {
		const b = Buffer.alloc(8);
		b.writeBigUInt64BE(BigInt(n));
		return b;
	};
	return `cfl1_${Buffer.concat([
		Buffer.from([1, 0]),
		Buffer.from(leaseId.replaceAll("-", ""), "hex"),
		u64(ACCOUNT),
		Buffer.from(PROFILE_ID.slice(4).replaceAll("-", ""), "hex"),
		Buffer.from(BUILD_HASH.replace("sha256:", ""), "hex"),
		u64(now),
		u64(now + 1800),
		Buffer.from([0, 1]),
		randomBytes(64),
	]).toString("base64url")}`;
}

interface Recorded {
	method: string;
	path: string;
	body: any;
}

/**
 * The Camoufox Pro API and its object store, as far as a lease's sections
 * reach: leases, a profile's state routes, and presigned GETs and PUTs.
 */
class FakeCloud {
	requests: Recorded[] = [];
	scripted: [RegExp, [number, Record<string, any>][]][] = [];
	/** Extra sections each mint answers with. */
	sections: Record<string, any> = {};
	heartbeat: Record<string, any> = {};
	heartbeatS = 60;
	keyClass = "account";
	transport = "direct";
	served = new Map<number, Buffer>();
	archiveCommits: Record<string, string>[] = [];
	objects = new Map<string, Buffer>();
	head = 0;
	versions = new Map<number, { manifest: string; chunks: string[] }>();
	bundle = Buffer.from("{}");
	base = "";
	server = http.createServer((req, res) => {
		const parts: Buffer[] = [];
		req.on("data", (chunk) => parts.push(chunk));
		req.on("end", () => {
			const raw = Buffer.concat(parts);
			const url = req.url as string;
			if (url.startsWith("/store/")) {
				this.requests.push({
					method: req.method as string,
					path: url,
					body: null,
				});
				if (req.method === "PUT") {
					const want = req.headers["x-amz-checksum-sha256"];
					if (
						want !== createHash("sha256").update(raw).digest("base64") ||
						Number(req.headers["content-length"]) !== raw.length
					) {
						res.writeHead(400).end();
						return;
					}
					this.objects.set(url, raw);
					res.writeHead(200).end();
					return;
				}
				const object = this.objects.get(url);
				res.writeHead(object ? 200 : 404).end(object);
				return;
			}
			if (url.includes("/state/archive")) {
				this.requests.push({
					method: req.method as string,
					path: url,
					body: null,
				});
				const [status, payload, extra] = this.archive(
					req.method as string,
					url,
					raw,
				);
				res.writeHead(status, extra).end(payload);
				return;
			}
			const body = raw.length ? JSON.parse(raw.toString()) : null;
			this.requests.push({ method: req.method as string, path: url, body });
			const [status, answer] = this.answer(req.method as string, url, body);
			res.writeHead(status, { "Content-Type": "application/json" });
			res.end(JSON.stringify(answer));
		});
	});

	/** The served-state route: the head as a cfp-dir/1 archive, and a commit of one. */
	archive(
		method: string,
		url: string,
		raw: Buffer,
	): [number, Buffer, Record<string, string>] {
		const query = Object.fromEntries(new URL(url, "http://x").searchParams);
		if (method === "GET") {
			return [
				200,
				this.served.get(this.head) ?? EMPTY_ARCHIVE,
				{ "x-cfp-state-version": String(this.head) },
			];
		}
		if (Number(query.base_version) !== this.head) {
			const refusal = {
				error: "state_conflict",
				message: "moved",
				details: { current_version: this.head },
			};
			return [409, Buffer.from(JSON.stringify(refusal)), {}];
		}
		this.head += 1;
		this.served.set(this.head, raw);
		this.archiveCommits.push(query);
		return [200, Buffer.from(JSON.stringify({ version: this.head })), {}];
	}

	script(route: string, ...answers: [number, Record<string, any>][]) {
		this.scripted.push([new RegExp(`^${route}$`), answers]);
	}

	calls(method: string, route: string) {
		const pattern = new RegExp(`^${route}$`);
		return this.requests.filter(
			(r) => r.method === method && pattern.test(r.path),
		);
	}

	setBundle(document: Record<string, any>) {
		this.bundle = Buffer.from(JSON.stringify(document));
		this.objects.set("/store/bundle", this.bundle);
	}

	stateBlock(version: number) {
		if (version === 0) {
			return {
				profile_id: PROFILE_ID,
				version: 0,
				manifest: null,
				chunk_count: 0,
				total_bytes: 0,
				chunks: [],
				next: null,
			};
		}
		const stored = this.versions.get(version) as {
			manifest: string;
			chunks: string[];
		};
		const manifest = this.objects.get(stored.manifest) as Buffer;
		return {
			profile_id: PROFILE_ID,
			version,
			manifest: {
				url: `${this.base}${stored.manifest}`,
				sha256: sha256(manifest),
				size: manifest.length,
			},
			chunk_count: stored.chunks.length,
			total_bytes: 0,
			chunks: stored.chunks.map((id) => ({
				chunk_id: id,
				size: (this.objects.get(`/store/chunks/${id}`) as Buffer).length,
				url: `${this.base}/store/chunks/${id}`,
			})),
			next: null,
		};
	}

	answer(
		method: string,
		url: string,
		body: any,
	): [number, Record<string, any>] {
		for (const [pattern, queue] of this.scripted) {
			if (pattern.test(url) && queue.length) return queue.shift() as any;
		}
		const now = new Date().toISOString();
		if (url === "/api/v1/leases") {
			const leaseId = randomUUID();
			const profile = body.profile
				? {
						id: PROFILE_ID,
						key: body.profile,
						os: body.os,
						warm_plan: this.keyClass === "account" ? "none" : "standard",
						key_class: this.keyClass,
						egress_regime: "none",
						bundle: {
							url: `${this.base}/store/bundle`,
							sha256: sha256(this.bundle),
							size: this.bundle.length,
							cache_key: `bundles/${sha256(this.bundle)}`,
						},
						bundle_version: 1,
						state:
							this.transport === "server"
								? {
										version: this.head,
										manifest: null,
										chunk_count: 0,
										total_bytes: 0,
										transport: "server",
										archive: `/api/v1/profiles/${PROFILE_ID}/state/archive`,
									}
								: this.stateBlock(this.head),
						launch_count: 1,
					}
				: null;
			return [
				201,
				{
					v: 1,
					lease_id: `lse_${leaseId}`,
					account_id: ACCOUNT,
					server_time: now,
					expires_at: now,
					token: tokenFor(leaseId),
					limits: { ttl_s: 1800, heartbeat_s: this.heartbeatS, grace_s: 180 },
					host: { fingerprint: body.host.fingerprint, fidelity: "native" },
					notices: [],
					profile,
					...this.sections,
				},
			];
		}
		const lease =
			/^\/api\/v1\/leases\/lse_([0-9a-f-]+)\/(heartbeat|release)$/.exec(url);
		if (lease?.[2] === "heartbeat") {
			return [
				200,
				{
					lease_id: `lse_${lease[1]}`,
					server_time: now,
					expires_at: now,
					token: tokenFor(lease[1]),
					notices: [],
					...this.heartbeat,
				},
			];
		}
		if (lease) return [200, { released: true }];
		const route = `/api/v1/profiles/${PROFILE_ID}/state`;
		if (method === "GET" && url.startsWith(`${route}?`)) {
			const version = Number(
				new URL(url, "http://x").searchParams.get("version"),
			);
			return [200, this.stateBlock(version)];
		}
		if (method === "POST" && url === `${route}/uploads`) {
			if (body.base_version !== this.head) {
				return [
					409,
					{
						error: "state_conflict",
						message: "moved",
						details: { current_version: this.head },
					},
				];
			}
			const missing = body.chunks
				.filter((c: any) => !this.objects.has(`/store/chunks/${c.chunk_id}`))
				.map((c: any) => ({
					chunk_id: c.chunk_id,
					url: `${this.base}/store/chunks/${c.chunk_id}`,
					headers: {
						"content-length": String(c.size),
						"x-amz-checksum-sha256": Buffer.from(c.sha256, "hex").toString(
							"base64",
						),
					},
				}));
			return [200, { next_version: this.head + 1, missing, present: [] }];
		}
		if (method === "PUT" && url === route) {
			if (body.base_version !== this.head) {
				return [
					409,
					{
						error: "state_conflict",
						message: "moved",
						details: { current_version: this.head },
					},
				];
			}
			const lost = body.manifest.chunks
				.map((c: any) => c.chunk_id)
				.filter((id: string) => !this.objects.has(`/store/chunks/${id}`));
			if (lost.length) {
				return [
					409,
					{
						error: "state_chunks_missing",
						message: "lost",
						details: { chunk_ids: lost },
					},
				];
			}
			const sealed = Buffer.from(body.manifest_body, "base64url");
			if (
				sha256(sealed) !== body.manifest.sha256 ||
				sealed.length !== body.manifest.size
			) {
				return [400, { error: "invalid_request", message: "manifest_body" }];
			}
			this.head = body.version;
			const key = `/store/manifests/${this.head}`;
			this.objects.set(key, sealed);
			this.versions.set(this.head, {
				manifest: key,
				chunks: body.manifest.chunks.map((c: any) => c.chunk_id),
			});
			return [200, { version: this.head, committed_at: now }];
		}
		return [404, { error: "not_found", message: url }];
	}
}

// gzip("cfp-dir/1\n" be32(0)): a profile with nothing in it.
const EMPTY_ARCHIVE = gzipSync(
	Buffer.concat([Buffer.from("cfp-dir/1\n"), Buffer.alloc(4)]),
);

let cloud: FakeCloud;
const TIMING = { ...pro.proTiming };
const SAVED_ENV = { ...process.env };

beforeAll(() => {
	for (const name of ["XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"]) {
		process.env[name] = path.join(SCRATCH, `profile-${name.toLowerCase()}`);
	}
});

beforeEach(async () => {
	for (const name of ["XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"]) {
		const dir = process.env[name] as string;
		fs.rmSync(dir, { recursive: true, force: true });
		fs.mkdirSync(dir, { recursive: true });
	}
	fs.rmSync(path.join(SCRATCH, "xdg-cache", "camoufox", "pro", "profiles"), {
		recursive: true,
		force: true,
	});
	cloud = new FakeCloud();
	await new Promise<void>((resolve) =>
		cloud.server.listen(0, "127.0.0.1", resolve),
	);
	const { port } = cloud.server.address() as AddressInfo;
	cloud.base = `http://127.0.0.1:${port}`;
	process.env[pro.API_ENV] = cloud.base;
	process.env[pro.KEY_ENV] = KEY;
	delete process.env[pro.LEASE_FILE_ENV];
	delete process.env[profiles.CONTENT_KEY_ENV];
	Object.assign(pro.proTiming, { mintRetryS: [0, 0, 0] });
});

afterEach(async () => {
	await pro.releaseAll();
	Object.assign(pro.proTiming, TIMING);
	restoreDeps();
	vi.restoreAllMocks();
	await new Promise((resolve) => cloud.server.close(resolve));
});

afterAll(() => {
	for (const name of [
		"XDG_CONFIG_HOME",
		"XDG_RUNTIME_DIR",
		pro.API_ENV,
		pro.KEY_ENV,
	]) {
		if (SAVED_ENV[name] === undefined) delete process.env[name];
		else process.env[name] = SAVED_ENV[name];
	}
});

const mode = (file: string) => fs.statSync(file).mode & 0o777;

// ── the vectors ─────────────────────────────────────────────────────────────

describe("the state vectors", () => {
	const keys = state.AccountKeys.derive(
		Buffer.from(VECTORS.keys.k_acct, "hex"),
		VECTORS.keys.account_id,
	);

	it("derives the account's keys", () => {
		expect(keys.kId.toString("hex")).toBe(VECTORS.keys.k_id);
		expect(keys.kChunk.toString("hex")).toBe(VECTORS.keys.k_chunk);
		expect(keys.kMan.toString("hex")).toBe(VECTORS.keys.k_man);
	});

	it("names, seals and opens a chunk", () => {
		const v = VECTORS.chunk;
		const plaintext = stream(v.plaintext.size, v.plaintext.label);
		expect(sha256(plaintext)).toBe(v.plaintext.sha256);
		const id = keys.chunkId(plaintext);
		expect(id.toString("hex")).toBe(v.chunk_id_hex);
		expect(state.chunkIdText(id)).toBe(v.chunk_id_b64url);
		const aad = state.chunkAad(ACCOUNT, id);
		expect(aad.toString("hex")).toBe(v.aad_hex);
		const sealed = state.seal(
			keys.kChunk,
			Buffer.from(v.zstd_frame_hex, "hex"),
			aad,
			Buffer.from(v.nonce_hex, "hex"),
		);
		expect(sealed.toString("hex")).toBe(v.sealed_hex);
		expect(sealed.length).toBe(v.sealed_size);
		expect(createHash("sha256").update(sealed).digest("base64")).toBe(
			v.x_amz_checksum_sha256,
		);
		expect(keys.openChunk(id, sealed, v.plaintext.size)).toEqual(plaintext);
	});

	it("refuses a chunk under another account, or with a different id", () => {
		const v = VECTORS.chunk;
		const sealed = Buffer.from(v.sealed_hex, "hex");
		const id = Buffer.from(v.chunk_id_hex, "hex");
		const other = state.AccountKeys.derive(
			Buffer.from(VECTORS.keys.k_acct, "hex"),
			ACCOUNT + 1,
		);
		expect(() => other.openChunk(id, sealed, 5000)).toThrow(
			state.StateIntegrityError,
		);
		const wrongId = Buffer.from(id);
		wrongId[0] ^= 1;
		expect(() => keys.openChunk(wrongId, sealed, 5000)).toThrow(
			state.StateIntegrityError,
		);
	});

	it("seals its own chunks with the frame's content size", () => {
		const plaintext = stream(5000, "cfp/profile-vector/chunk/1");
		const id = keys.chunkId(plaintext);
		const sealed = keys.sealChunk(id, plaintext);
		expect(keys.openChunk(id, sealed, 5000)).toEqual(plaintext);
		const frame = Buffer.from(VECTORS.chunk.zstd_frame_hex.slice(0, 8), "hex");
		expect(frame.toString("hex")).toBe("28b52ffd");
	});

	it("builds, seals and opens the manifest", () => {
		const v = VECTORS.manifest;
		const {
			format,
			total_bytes,
			file_count,
			chunk_count,
			sha256: _,
			...fields
		} = v.document;
		const manifest = state.manifestOf(fields);
		expect(manifest).toEqual(v.document);
		expect(state.canonical(manifest)).toBe(v.canonical_json);
		expect(state.selfHash(v.document)).toBe(v.self_sha256);
		const aad = state.manifestAad(ACCOUNT, v.profile_id, v.version);
		expect(aad.toString("hex")).toBe(v.aad_hex);
		const sealed = state.seal(
			keys.kMan,
			Buffer.from(v.zstd_frame_hex, "hex"),
			aad,
			Buffer.from(v.nonce_hex, "hex"),
		);
		expect(sealed.toString("hex")).toBe(v.sealed_hex);
		expect(sealed.toString("base64url")).toBe(v.manifest_body_b64url);
		const opened = keys.openManifest(v.profile_id, v.version, sealed);
		expect(opened.toString()).toBe(v.canonical_json);
		expect(state.decodeManifest(opened, v.profile_id, v.version)).toEqual(
			v.document,
		);
		expect(() =>
			state.decodeManifest(opened, v.profile_id, v.version + 1),
		).toThrow(/expected/);
		expect(() =>
			keys.openManifest(v.profile_id, v.version + 1, sealed),
		).toThrow(state.StateIntegrityError);
	});

	it("chunks with FastCDC g1", () => {
		const v = VECTORS.fastcdc_g1;
		expect(state.GEAR_SEED.toString(16)).toBe(v.seed_hex);
		const hex = (t: { hi: Uint32Array; lo: Uint32Array }, i: number) =>
			t.hi[i].toString(16).padStart(8, "0") +
			t.lo[i].toString(16).padStart(8, "0");
		expect(Array.from({ length: 256 }, (_, i) => hex(state.G1, i))).toEqual(
			v.gear_hex,
		);
		expect(Array.from({ length: 256 }, (_, i) => hex(state.G1_LS, i))).toEqual(
			v.gear_ls_hex,
		);
		expect([v.min, v.avg, v.max]).toEqual([
			state.CHUNKING.min,
			state.CHUNKING.avg,
			state.CHUNKING.max,
		]);
		expect(state.chunkLengths(stream(v.input.size, v.input.label))).toEqual(
			v.lengths,
		);
	});
});

// ── capture ─────────────────────────────────────────────────────────────────

/** A closed Firefox profile with what a real one holds: SQLite with a pending WAL, prefs, caches, locks. */
async function realisticProfile(dir: string): Promise<void> {
	fs.mkdirSync(dir, { recursive: true });
	const { DatabaseSync } = await import("node:sqlite");
	const db = new DatabaseSync(path.join(dir, "cookies.sqlite"));
	db.exec("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;");
	db.exec("CREATE TABLE moz_cookies (name TEXT, value TEXT)");
	db.prepare("INSERT INTO moz_cookies VALUES (?, ?)").run("session", "abc123");
	// Copy while the WAL still holds the rows, as a killed browser leaves it.
	fs.copyFileSync(path.join(dir, "cookies.sqlite"), path.join(dir, "c.sqlite"));
	fs.copyFileSync(
		path.join(dir, "cookies.sqlite-wal"),
		path.join(dir, "c.sqlite-wal"),
	);
	db.close();
	fs.renameSync(path.join(dir, "c.sqlite"), path.join(dir, "cookies.sqlite"));
	fs.renameSync(
		path.join(dir, "c.sqlite-wal"),
		path.join(dir, "cookies.sqlite-wal"),
	);
	fs.writeFileSync(
		path.join(dir, "prefs.js"),
		[
			'user_pref("browser.startup.homepage", "https://example.com");',
			'user_pref("webgl.force-enabled", true);',
			'user_pref("browser.download.dir", "/home/someone/Downloads");',
			"",
		].join("\n"),
	);
	const ls = path.join(dir, "storage", "default", "https+++example.com", "ls");
	fs.mkdirSync(ls, { recursive: true });
	fs.writeFileSync(path.join(ls, "data.sqlite"), stream(3 << 20, "ls"));
	fs.mkdirSync(
		path.join(
			dir,
			"storage",
			"default",
			"https+++example.com",
			"idb",
			"x.files",
		),
		{
			recursive: true,
		},
	);
	fs.mkdirSync(path.join(dir, "cache2", "entries"), { recursive: true });
	fs.writeFileSync(path.join(dir, "cache2", "entries", "A"), "cache");
	fs.writeFileSync(path.join(dir, "user.js"), 'user_pref("a", 1);');
	fs.writeFileSync(path.join(dir, ".parentlock"), "");
	fs.writeFileSync(path.join(dir, "times.json"), '{"created":1}');
	fs.writeFileSync(path.join(dir, "big-at-root.bin"), stream(2 << 20, "big"));
}

describe("capture", () => {
	it("keeps what the policy keeps and folds the WAL in", async () => {
		const dir = fs.mkdtempSync(path.join(SCRATCH, "capture-"));
		await realisticProfile(dir);
		const snapshot = await state.capture(dir);
		expect(snapshot.files.map((f) => f.path)).toEqual([
			"cookies.sqlite",
			"prefs.js",
			"storage/default/https+++example.com/ls/data.sqlite",
			"times.json",
		]);
		expect(snapshot.dirs).toEqual([
			"storage/default/https+++example.com/idb/x.files",
		]);
		expect(snapshot.suspectFiles).toEqual([]);
		// Folded: the WAL is truncated, and gone once the last connection closed.
		const wal = path.join(dir, "cookies.sqlite-wal");
		expect(fs.existsSync(wal) ? fs.statSync(wal).size : 0).toBe(0);
		const { DatabaseSync } = await import("node:sqlite");
		const db = new DatabaseSync(path.join(dir, "cookies.sqlite"), {
			readOnly: true,
		});
		expect(db.prepare("SELECT value FROM moz_cookies").get()).toEqual({
			value: "abc123",
		});
		db.close();
		const prefs = fs.readFileSync(path.join(dir, "prefs.js"), "utf-8");
		expect(prefs).toContain("browser.startup.homepage");
		expect(prefs).not.toContain("webgl.force-enabled");
		expect(prefs).not.toContain("browser.download.dir");
		expect(fs.existsSync(path.join(dir, "user.js"))).toBe(false);
		fs.rmSync(dir, { recursive: true });
	});
});

// ── restore -> capture -> restore ───────────────────────────────────────────

const BUNDLE_DOC = {
	format: "cfp-bundle/1",
	bundle_version: 1,
	profile: { id: PROFILE_ID, os: "windows" },
	config: {
		"navigator.userAgent":
			"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{FF}.0) Gecko/20100101 Firefox/{FF}.0",
		"screen.width": 1920,
	},
	browserforge_fingerprint: { screen: { width: 1920 } },
	prefs: { "webgl.enable-webgl2": true },
};

function tree(dir: string): Record<string, string> {
	const out: Record<string, string> = {};
	const walk = (sub: string, rel: string) => {
		for (const entry of fs.readdirSync(sub, { withFileTypes: true })) {
			const relpath = rel ? `${rel}/${entry.name}` : entry.name;
			if (entry.isDirectory()) {
				out[`${relpath}/`] = "";
				walk(path.join(sub, entry.name), relpath);
			} else {
				out[relpath] = sha256(fs.readFileSync(path.join(sub, entry.name)));
			}
		}
	};
	walk(dir, "");
	return out;
}

async function profileLease() {
	return pro.acquire(BUILD, "win", KEY, { profile: "linkedin-01" });
}

describe("a profile's state", () => {
	beforeEach(() => cloud.setBundle(BUNDLE_DOC));

	it("starts empty, syncs on close, and restores on the next launch", async () => {
		const first = await profileLease();
		const opened = await profiles.openProfile(first, "152.0.4");
		expect(opened.identity.config["navigator.userAgent"]).toBe(
			"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
		);
		expect(opened.identity.ff_version).toBe(152);
		expect(opened.identity.os).toBe("windows");
		expect(fs.readdirSync(opened.userDataDir)).toEqual([]);
		expect(mode(opened.userDataDir)).toBe(0o700);

		// The content key was created once, privately.
		const keyFile = profiles.contentKeyPath(ACCOUNT);
		expect(mode(keyFile)).toBe(0o600);
		expect(
			Buffer.from(fs.readFileSync(keyFile, "utf-8"), "base64url"),
		).toHaveLength(32);

		await realisticProfile(opened.userDataDir);
		await first.close();
		expect(cloud.head).toBe(1);
		const commit = cloud.calls("PUT", `/api/v1/profiles/${PROFILE_ID}/state`)[0]
			.body;
		expect(commit).toMatchObject({
			lease_id: first.leaseId,
			base_version: 0,
			version: 1,
			release: true,
			file_count: 4,
			ff_version: "152.0.4",
			integrity: "ok",
		});
		// The commit released the lease; no second release, and nothing left behind.
		expect(cloud.calls("POST", ".*/release")).toHaveLength(0);
		expect(fs.existsSync(opened.userDataDir)).toBe(false);
		expect(fs.existsSync(first.path)).toBe(false);
		// The store never saw plaintext.
		for (const [key, blob] of cloud.objects) {
			if (key.startsWith("/store/chunks/")) {
				expect(blob.includes(Buffer.from("abc123"))).toBe(false);
			}
		}

		// What the restore must reproduce: the same profile, captured, minus what does not travel.
		const expectedDir = fs.mkdtempSync(path.join(SCRATCH, "expected-"));
		await realisticProfile(expectedDir);
		await state.capture(expectedDir);
		for (const name of [
			"cache2",
			"user.js",
			"big-at-root.bin",
			"cookies.sqlite-wal",
			"cookies.sqlite-shm",
		]) {
			fs.rmSync(path.join(expectedDir, name), { recursive: true, force: true });
		}
		const expected = tree(expectedDir);

		const second = await profileLease();
		const reopened = await profiles.openProfile(second, "152.0.4");
		const restored = tree(reopened.userDataDir);
		expect(restored["cookies.sqlite"]).toBe(expected["cookies.sqlite"]);
		expect(restored).toEqual(expected);
		const ls = "storage/default/https+++example.com/ls/data.sqlite";
		expect(mode(path.join(reopened.userDataDir, ls))).toBe(
			mode(path.join(expectedDir, ls)),
		);

		// Unchanged chunks are not uploaded twice.
		const putsBefore = cloud.calls("PUT", "/store/chunks/.*").length;
		await second.close();
		expect(cloud.head).toBe(2);
		expect(cloud.calls("PUT", "/store/chunks/.*").length).toBe(putsBefore);

		const third = await profileLease();
		const again = await profiles.openProfile(third, "152.0.4");
		expect(tree(again.userDataDir)).toEqual(expected);
		await third.release();
	});

	it("never restores a profile a newer Firefox wrote", async () => {
		const first = await profileLease();
		const opened = await profiles.openProfile(first, "153.0");
		fs.writeFileSync(path.join(opened.userDataDir, "times.json"), "{}");
		await first.close();
		const second = await profileLease();
		await expect(profiles.openProfile(second, "152.0.4")).rejects.toThrow(
			state.StateNewerThanBrowser,
		);
	});

	it("keeps a capture the API calls a conflict, and releases the lease", async () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
		const lease = await profileLease();
		const opened = await profiles.openProfile(lease, "152.0.4");
		fs.writeFileSync(path.join(opened.userDataDir, "times.json"), "{}");
		cloud.head = 5; // another machine committed meanwhile
		await lease.close();
		expect(cloud.calls("POST", ".*/release")).toHaveLength(1);
		const conflicts = path.join(
			SCRATCH,
			"xdg-cache",
			"camoufox",
			"pro",
			"profiles",
			PROFILE_ID,
			"conflicts",
		);
		const [kept] = fs.readdirSync(conflicts);
		expect(
			fs.readFileSync(path.join(conflicts, kept, "times.json"), "utf-8"),
		).toBe("{}");
		expect(warn.mock.calls.flat().join(" ")).toMatch(/state_conflict|moved/);
	});

	it("keeps a capture pending while another lease holds the profile", async () => {
		vi.spyOn(console, "warn").mockImplementation(() => undefined);
		const lease = await profileLease();
		const opened = await profiles.openProfile(lease, "152.0.4");
		fs.writeFileSync(path.join(opened.userDataDir, "times.json"), "{}");
		cloud.script(`/api/v1/profiles/${PROFILE_ID}/state/uploads`, [
			409,
			{
				error: "lease_conflict",
				message: "held",
				retry_after: 30,
				details: { holder: "x" },
			},
		]);
		await lease.close();
		const pending = path.join(
			SCRATCH,
			"xdg-cache",
			"camoufox",
			"pro",
			"profiles",
			PROFILE_ID,
			"pending",
		);
		expect(fs.readdirSync(pending)).toHaveLength(1);
		expect(cloud.head).toBe(0);
	});

	it("uploads chunks the store lost, then commits", async () => {
		const lease = await profileLease();
		const opened = await profiles.openProfile(lease, "152.0.4");
		fs.writeFileSync(path.join(opened.userDataDir, "times.json"), "{}");
		let dropped = false;
		const answer = cloud.answer.bind(cloud);
		cloud.answer = (method, url, body) => {
			if (method === "PUT" && url.endsWith("/state") && !dropped) {
				dropped = true;
				for (const key of [...cloud.objects.keys()]) {
					if (key.startsWith("/store/chunks/")) cloud.objects.delete(key);
				}
			}
			return answer(method, url, body);
		};
		await lease.close();
		expect(cloud.head).toBe(1);
		expect(
			cloud.calls("PUT", `/api/v1/profiles/${PROFILE_ID}/state`),
		).toHaveLength(2);
	});

	it("launches a pool-sealed profile with its identity but no state sync", async () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
		cloud.keyClass = "pool";
		const lease = await profileLease();
		const opened = await profiles.openProfile(lease, "152.0.4");
		expect(opened.identity.os).toBe("windows");
		expect(warn.mock.calls.flat().join(" ")).toMatch(/not synced/);
		await lease.close();
		expect(cloud.calls("GET", ".*/state.*")).toHaveLength(0);
		expect(cloud.calls("POST", ".*/release")).toHaveLength(1);
		expect(fs.existsSync(opened.userDataDir)).toBe(false);
	});

	it("serves and commits a warmed profile's state through the API", async () => {
		cloud.keyClass = "pool";
		cloud.transport = "server";
		const first = await profileLease();
		const opened = await profiles.openProfile(first, "152.0.4");
		expect(fs.readdirSync(opened.userDataDir)).toEqual([]);
		await realisticProfile(opened.userDataDir);
		await first.close();
		expect(cloud.head).toBe(1);
		expect(cloud.archiveCommits).toEqual([
			{
				lease_id: first.leaseId,
				base_version: "0",
				ff_version: "152.0.4",
				release: "1",
				crashed: "0",
			},
		]);
		// Nothing sealed or uploaded here, and no content key made: this machine never holds the pool's.
		expect(cloud.calls("PUT", "/store/chunks/.*")).toHaveLength(0);
		expect(fs.existsSync(profiles.contentKeyPath(ACCOUNT))).toBe(false);
		expect(cloud.calls("POST", ".*/release")).toHaveLength(0);
		expect(fs.existsSync(opened.userDataDir)).toBe(false);

		const expectedDir = fs.mkdtempSync(path.join(SCRATCH, "expected-"));
		await realisticProfile(expectedDir);
		await state.capture(expectedDir);
		for (const name of [
			"cache2",
			"user.js",
			"big-at-root.bin",
			"cookies.sqlite-wal",
			"cookies.sqlite-shm",
		]) {
			fs.rmSync(path.join(expectedDir, name), { recursive: true, force: true });
		}
		const second = await profileLease();
		const reopened = await profiles.openProfile(second, "152.0.4");
		expect(tree(reopened.userDataDir)).toEqual(tree(expectedDir));
		await second.close();
		expect(cloud.head).toBe(2);
		expect(cloud.archiveCommits[1].base_version).toBe("1");
	});

	it("refuses served state that would write outside its directory", async () => {
		cloud.keyClass = "pool";
		cloud.transport = "server";
		const header = Buffer.from(
			JSON.stringify({
				path: "../escaped",
				type: "file",
				size: 1,
				mode: 0o600,
				mtime: 0,
			}),
		);
		const length = Buffer.alloc(4);
		length.writeUInt32BE(header.length);
		cloud.head = 1;
		cloud.served.set(
			1,
			gzipSync(
				Buffer.concat([
					Buffer.from("cfp-dir/1\n"),
					length,
					header,
					Buffer.from("x"),
					Buffer.alloc(4),
				]),
			),
		);
		const lease = await profileLease();
		await expect(profiles.openProfile(lease, "152.0.4")).rejects.toThrow(
			/cfp-dir\/1/,
		);
		const home = path.join(
			SCRATCH,
			"xdg-cache",
			"camoufox",
			"pro",
			"profiles",
			PROFILE_ID,
		);
		expect(fs.existsSync(path.join(home, "sessions", "escaped"))).toBe(false);
		expect(fs.existsSync(path.join(home, "escaped"))).toBe(false);
	});

	for (const transport of ["direct", "server"]) {
		it(`commits a ${transport} session that never closed before the next launch restores`, async () => {
			vi.spyOn(console, "warn").mockImplementation(() => undefined);
			if (transport === "server") {
				cloud.keyClass = "pool";
				cloud.transport = "server";
			}
			const crashed = await profileLease();
			const left = (await profiles.openProfile(crashed, "152.0.4")).userDataDir;
			fs.writeFileSync(path.join(left, "times.json"), '{"crashed":true}');
			// The process exits with the browser never closed.
			crashed.onAbort?.(crashed);
			await crashed.release("driver_shutdown");
			expect(fs.existsSync(left)).toBe(true);
			expect(cloud.head).toBe(0);

			const lease = await profileLease();
			const opened = await profiles.openProfile(lease, "152.0.4");
			expect(cloud.head).toBe(1);
			expect(
				fs.readFileSync(path.join(opened.userDataDir, "times.json"), "utf-8"),
			).toBe('{"crashed":true}');
			expect(fs.existsSync(left)).toBe(false);
			if (transport === "server") {
				expect(cloud.archiveCommits[0]).toMatchObject({
					release: "0",
					crashed: "1",
				});
			} else {
				const commit = cloud.calls(
					"PUT",
					`/api/v1/profiles/${PROFILE_ID}/state`,
				)[0];
				expect(commit.body).toMatchObject({ release: false, crashed: true });
			}
			await lease.close();
			expect(cloud.head).toBe(2);
		});
	}

	it("keeps a session left behind on an older version as a conflict", async () => {
		vi.spyOn(console, "warn").mockImplementation(() => undefined);
		const crashed = await profileLease();
		const left = (await profiles.openProfile(crashed, "152.0.4")).userDataDir;
		fs.writeFileSync(path.join(left, "times.json"), "{}");
		crashed.onAbort?.(crashed);
		await crashed.release("driver_shutdown");
		const home = path.join(
			SCRATCH,
			"xdg-cache",
			"camoufox",
			"pro",
			"profiles",
			PROFILE_ID,
		);
		const marks = fs
			.readdirSync(path.join(home, "marks"))
			.filter((n) => n.startsWith("sessions-"));
		expect(marks).toHaveLength(1);
		fs.writeFileSync(
			path.join(home, "marks", marks[0]),
			JSON.stringify({ base_version: 7, served: false }),
		);
		const lease = await profileLease();
		const opened = await profiles.openProfile(lease, "152.0.4");
		expect(cloud.head).toBe(0);
		const kept = fs.readdirSync(path.join(home, "conflicts"));
		expect(kept).toHaveLength(1);
		expect(
			fs.readFileSync(
				path.join(home, "conflicts", kept[0], "times.json"),
				"utf-8",
			),
		).toBe("{}");
		expect(fs.readdirSync(opened.userDataDir)).toEqual([]);
		await lease.release();
	});

	it("reports its state dirty in heartbeats", async () => {
		cloud.heartbeatS = 0.05;
		const lease = await profileLease();
		await profiles.openProfile(lease, "152.0.4");
		await vi.waitFor(() => {
			expect(cloud.calls("POST", ".*/heartbeat").length).toBeGreaterThan(0);
		});
		expect(cloud.calls("POST", ".*/heartbeat")[0].body.state_dirty).toBe(true);
		await lease.release();
	});

	it("refuses a bundle that is not the one the lease names", async () => {
		const lease = await profileLease();
		cloud.objects.set("/store/bundle", Buffer.from('{"tampered":1}'));
		await expect(profiles.openProfile(lease, "152.0.4")).rejects.toThrow(
			/sha256/,
		);
	});

	it("takes the content key from the environment", async () => {
		process.env[profiles.CONTENT_KEY_ENV] = Buffer.alloc(32, 7).toString(
			"base64url",
		);
		expect(profiles.contentKey(ACCOUNT)).toEqual(Buffer.alloc(32, 7));
		expect(fs.existsSync(profiles.contentKeyPath(ACCOUNT))).toBe(false);
	});
});

describe("refusals", () => {
	beforeEach(() => cloud.setBundle(BUNDLE_DOC));

	it("raises a profile that does not match the launch as ProfileMismatch", async () => {
		cloud.script("/api/v1/leases", [
			409,
			{
				error: "profile_mismatch",
				message: "linkedin-01 is a standard profile.",
				details: { field: "warm_plan", profile: "standard", requested: "none" },
			},
		]);
		const error = await pro
			.acquire(BUILD, "win", KEY, { profile: "linkedin-01", warm_plan: "none" })
			.catch((e) => e);
		expect(error).toBeInstanceOf(errors.ProfileMismatch);
		expect(error.details.field).toBe("warm_plan");
		expect(cloud.calls("POST", "/api/v1/leases")[0].body.warm_plan).toBe(
			"none",
		);
	});

	it("raises a farm that cannot serve as GpuUnavailable, without retrying", async () => {
		cloud.script("/api/v1/leases", [
			503,
			{
				error: "gpu_unavailable",
				message: "No GPU can serve this identity; gpu: false renders locally.",
				retry_after: 30,
				details: { reason: "no_worker" },
			},
		]);
		const error = await pro.acquire(BUILD, "win", KEY).catch((e) => e);
		expect(error).toBeInstanceOf(errors.GpuUnavailable);
		expect(error.retry_after).toBe(30);
		expect(cloud.calls("POST", "/api/v1/leases")).toHaveLength(1);
	});

	it("raises a pool-sealed profile's refused sync as StatePoolSealed, keeping the capture", async () => {
		vi.spyOn(console, "warn").mockImplementation(() => undefined);
		const lease = await profileLease();
		const opened = await profiles.openProfile(lease, "152.0.4");
		fs.writeFileSync(path.join(opened.userDataDir, "times.json"), "{}");
		cloud.script(`/api/v1/profiles/${PROFILE_ID}/state/uploads`, [
			409,
			{ error: "state_pool_sealed", message: "sealed for the pool" },
		]);
		await expect(lease.close()).rejects.toBeInstanceOf(errors.StatePoolSealed);
		expect(cloud.calls("POST", ".*/release")).toHaveLength(1);
		const conflicts = path.join(
			SCRATCH,
			"xdg-cache",
			"camoufox",
			"pro",
			"profiles",
			PROFILE_ID,
			"conflicts",
		);
		expect(fs.readdirSync(conflicts)).toHaveLength(1);
	});
});

// ── remote rendering ────────────────────────────────────────────────────────

const RENDER = {
	endpoint: "wss://render.example/firefox-webgl",
	token: `cfl1.${"a".repeat(32)}.${"b".repeat(32)}.${"c".repeat(43)}`,
	assignment: "b".repeat(32),
	profile: "d".repeat(64),
	version: "156.0",
	expires_at: 1759400000,
};
const GPU = {
	mode: "farm",
	identity_id: "d".repeat(64),
	renderer: "ANGLE (AMD, Radeon R9 200 Series Direct3D11 vs_5_0 ps_5_0)",
	render: RENDER,
	prefs: {
		"gfx.canvas.renderfarm.enabled": true,
		"privacy.resistFingerprinting": false,
	},
};

describe("a profile another session holds", () => {
	const held: [number, Record<string, any>] = [
		409,
		{
			error: "lease_conflict",
			message: "held",
			retry_after: 30,
			details: { holder: {} },
		},
	];

	it("is waited for until that session lets go, with one mint asked again", async () => {
		Object.assign(pro.proTiming, { profilePollS: 0 });
		cloud.script("/api/v1/leases", held, held);
		const lease = await profileLease();
		const mints = cloud.calls("POST", "/api/v1/leases");
		expect(mints).toHaveLength(3);
		expect(new Set(mints.map((m) => m.body.idempotency_key)).size).toBe(1);
		await lease.release();
	});

	it("is refused once the wait runs out", async () => {
		process.env[pro.PROFILE_WAIT_ENV] = "0";
		try {
			cloud.script("/api/v1/leases", held);
			await expect(profileLease()).rejects.toMatchObject({
				code: "lease_conflict",
			});
			expect(cloud.calls("POST", "/api/v1/leases")).toHaveLength(1);
		} finally {
			delete process.env[pro.PROFILE_WAIT_ENV];
		}
	});
});

describe("remote rendering", () => {
	it("writes the render document verbatim, privately, and rewrites it on each heartbeat", async () => {
		cloud.sections = { gpu: GPU };
		cloud.heartbeatS = 0.05;
		cloud.heartbeat = {
			gpu: {
				...GPU,
				render: { ...RENDER, expires_at: RENDER.expires_at + 60 },
			},
		};
		const lease = await pro.acquire(BUILD, "win", KEY);
		const file = lease.renderPath as string;
		expect(path.isAbsolute(file)).toBe(true);
		expect(mode(file)).toBe(0o600);
		expect(fs.readFileSync(file, "utf-8")).toBe(JSON.stringify(RENDER));
		await vi.waitFor(() =>
			expect(JSON.parse(fs.readFileSync(file, "utf-8")).expires_at).toBe(
				RENDER.expires_at + 60,
			),
		);
		// A heartbeat without a render section keeps the file as it is.
		cloud.heartbeat = { gpu: null };
		await vi.waitFor(() =>
			expect(cloud.calls("POST", ".*/heartbeat").length).toBeGreaterThan(2),
		);
		expect(JSON.parse(fs.readFileSync(file, "utf-8")).expires_at).toBe(
			RENDER.expires_at + 60,
		);
		await lease.release();
		expect(fs.existsSync(file)).toBe(false);
	});

	it("asks for none when told to", async () => {
		await pro.acquire(BUILD, "win", KEY, { gpu: false });
		expect(cloud.calls("POST", "/api/v1/leases")[0].body.gpu).toBe(false);
	});
});

// ── the captcha solver ──────────────────────────────────────────────────────

describe("the captcha solver", () => {
	it("is exposed on the browser, without the lease's credentials", async () => {
		const captcha = {
			endpoint: "https://solver.example/v1",
			remaining: 840,
			expires_at: "2026-10-02T21:15:07.410Z",
		};
		cloud.sections = {
			captcha,
			egress: { server: "http://127.0.0.1:1", username: "u", password: "p" },
		};
		const lease = await pro.acquire(BUILD, "win", KEY);
		const handlers: Record<string, () => void> = {};
		let closed = false;
		const browser = pro.attachLease(
			lease,
			{
				on: (event: string, handler: () => void) => (handlers[event] = handler),
				close: async () => {
					closed = true;
				},
			},
			"disconnected",
		);
		expect(browser.pro).toEqual({ leaseId: lease.leaseId, captcha });
		expect(JSON.stringify(browser.pro)).not.toContain('"p"');
		await browser.close();
		expect(closed).toBe(true);
		expect(cloud.calls("POST", ".*/release")).toHaveLength(1);
		expect(fs.existsSync(lease.path)).toBe(false);
	});
});

// ── launchOptions ───────────────────────────────────────────────────────────

describe.runIf(modelReady)("launchOptions with a lease's sections", () => {
	let exe: string;
	let lookedUp: (string | undefined)[];
	beforeEach(() => {
		stubHost();
		const dir = fs.mkdtempSync(path.join(SCRATCH, "build-"));
		fs.cpSync(BUNDLE, dir, { recursive: true });
		exe = path.join(dir, "camoufox-bin");
		fs.writeFileSync(path.join(dir, "pro-build.json"), JSON.stringify(BUILD));
		lookedUp = [];
		const deps = utils.utilsDeps;
		deps.publicIp = async (proxy?: string) => {
			lookedUp.push(proxy);
			return "203.0.113.9";
		};
		deps.getGeolocation = (async (ip: string) => ({
			asConfig: () => ({
				timezone: "America/New_York",
				"locale:language": "en",
				"locale:region": "US",
				"geolocation:latitude": 40.7,
				"geolocation:longitude": -74,
				"geolocation:accuracy": 100,
				ip,
			}),
		})) as any;
	});
	const launch = (extra: Record<string, any> = {}) =>
		quietly(() =>
			utils.launchOptions({
				env: { HOME },
				executable_path: exe,
				os: "windows",
				headless: true,
				i_know_what_im_doing: true,
				...extra,
			}),
		);
	const mint = () => cloud.calls("POST", "/api/v1/leases").at(-1)?.body;

	it("routes the browser through managed egress, located at its exit", async () => {
		cloud.sections = {
			egress: {
				server: "http://100.64.12.34:7190",
				username: "0192f0c4",
				password: "secret",
				class: "residential",
				country: "US",
				sticky: true,
				exit_ip: "98.97.12.34",
			},
		};
		const opts = await launch({ egress: { class: "isp", country: "US" } });
		expect(mint().egress).toEqual({ class: "isp", country: "US" });
		expect(opts.proxy).toEqual({
			server: "http://100.64.12.34:7190",
			username: "0192f0c4",
			password: "secret",
			bypass: "localhost,127.0.0.1,::1,*.local",
		});
		expect(configOf(opts).timezone).toBe("America/New_York");
		expect(configOf(opts)["webrtc:ipv4"]).toBe("98.97.12.34");
		expect(lookedUp).toEqual([]);
	});

	it("looks the exit up through the proxy when the API does not know it", async () => {
		cloud.sections = {
			egress: {
				server: "http://100.64.12.34:7190",
				username: "u",
				password: "p",
				exit_ip: null,
			},
		};
		await launch();
		expect("egress" in mint()).toBe(false);
		expect(lookedUp).toEqual(["http://u:p@100.64.12.34:7190"]);
		expect("profile" in mint() || "warm_plan" in mint()).toBe(false);
	});

	it("never replaces the caller's own proxy", async () => {
		const proxy = { server: "http://my.proxy:8080" };
		const opts = await launch({ proxy, geoip: "198.51.100.1" });
		expect(mint().egress).toBe(false);
		expect(opts.proxy).toEqual(proxy);
	});

	it("refuses a proxy and an egress request together", async () => {
		const proxy = { server: "http://my.proxy:8080" };
		await expect(
			launch({ proxy, egress: { provider: "evomi" } }),
		).rejects.toThrow(/proxy and egress conflict/);
		await launch({ proxy, egress: false });
		expect(mint().egress).toBe(false);
	});

	it("asks for CamouProxy with true, and never beside a proxy", async () => {
		await launch({ egress: true });
		expect(mint().egress).toBe(true);
		await expect(
			launch({ proxy: { server: "http://my.proxy:8080" }, egress: true }),
		).rejects.toThrow(/proxy and egress conflict/);
	});

	it("asks for a partner provider by name", async () => {
		await launch({ egress: { provider: "evomi", country: "US" } });
		expect(mint().egress).toEqual({ provider: "evomi", country: "US" });
	});

	it("points the browser at the render document and sets the farm prefs", async () => {
		cloud.sections = { gpu: GPU };
		const opts = await launch();
		const file = opts.env[pro.RENDER_FILE_ENV];
		expect(fs.readFileSync(file, "utf-8")).toBe(JSON.stringify(RENDER));
		expect(opts.firefoxUserPrefs["gfx.canvas.renderfarm.enabled"]).toBe(true);
		expect(opts.firefoxUserPrefs["privacy.resistFingerprinting"]).toBe(false);
	});

	it("sets none of it without a gpu section", async () => {
		const opts = await launch({ gpu: false });
		expect(mint().gpu).toBe(false);
		expect(pro.RENDER_FILE_ENV in opts.env).toBe(false);
		expect("gfx.canvas.renderfarm.enabled" in opts.firefoxUserPrefs).toBe(
			false,
		);
	});

	it("launches a profile with its identity, in its own user-data directory", async () => {
		const generated = configOf(await launch());
		const config = {
			...generated,
			"navigator.userAgent": generated["navigator.userAgent"].replaceAll(
				"152",
				"{FF}",
			),
		};
		for (const key of Object.keys(config)) {
			if (
				key === "timezone" ||
				key.startsWith("locale:") ||
				key.startsWith("geolocation:")
			) {
				delete config[key];
			}
		}
		cloud.setBundle({ ...BUNDLE_DOC, config });
		const opts = await launch({
			profile: "linkedin-01",
			warm_plan: "none",
			proxy: { server: "http://my.proxy:8080" },
		});
		expect(mint()).toMatchObject({
			profile: "linkedin-01",
			warm_plan: "none",
			os: "windows",
			egress: false,
		});
		const presented = configOf(opts);
		expect(presented["navigator.userAgent"]).toBe(
			generated["navigator.userAgent"],
		);
		expect(presented.fonts).toEqual(generated.fonts);
		expect(presented.timezone).toBe("America/New_York");
		expect(lookedUp).toEqual(["http://my.proxy:8080"]);
		expect(opts.user_data_dir).toMatch(
			new RegExp(`profiles/${PROFILE_ID}/sessions/lse_`),
		);
		expect(opts.firefoxUserPrefs["webgl.enable-webgl2"]).toBe(true);
	});

	it("refuses a profile without one OS, or with an identity of the caller's", async () => {
		await expect(
			launch({ profile: "p", os: ["windows", "linux"] }),
		).rejects.toThrow(/needs `os`/);
		await expect(launch({ profile: "p", config: { a: 1 } })).rejects.toThrow(
			/own identity/,
		);
		expect(cloud.requests).toEqual([]);
	});

	it("refuses a warm plan without a profile", async () => {
		await expect(launch({ warm_plan: "none" })).rejects.toThrow(/pass profile/);
	});

	it("refuses lease options for a stock build", async () => {
		fs.rmSync(path.join(path.dirname(exe), "pro-build.json"));
		await expect(launch({ profile: "p" })).rejects.toThrow(
			/Camoufox Pro build/,
		);
		await expect(launch({ gpu: false })).rejects.toThrow(/Camoufox Pro build/);
	});

	it("releases the lease when the rest of the launch fails", async () => {
		utils.utilsDeps.validateConfig = () => {
			throw new Error("bad config");
		};
		await expect(launch()).rejects.toThrow("bad config");
		expect(cloud.calls("POST", ".*/release").map((r) => r.body)).toEqual([
			{ reason: "error" },
		]);
	});
});

describe("capture waits for Firefox to let go of the profile", () => {
	// Playwright reports a persistent context closed while Firefox is still writing prefs.js and
	// places.sqlite on its way out, and a capture taken then failed "changed size after capture".
	it.skipIf(process.platform === "win32")(
		"until the process named by the lock symlink exits",
		async () => {
			const { spawn } = await import("node:child_process");
			const dir = fs.mkdtempSync(path.join(SCRATCH, "locked-"));
			const holder = spawn("sleep", ["30"]);
			const exited = new Promise((resolve) => holder.once("exit", resolve));
			fs.symlinkSync(`127.0.0.1:+${holder.pid}`, path.join(dir, "lock"));
			let released = false;
			const waiting = profiles
				.profileReleased(dir, 10_000, 20)
				.then(() => {
					released = true;
				});
			await new Promise((resolve) => setTimeout(resolve, 200));
			expect(released).toBe(false);
			holder.kill();
			await exited;
			await waiting;
			expect(released).toBe(true);
		},
	);

	it("not at all when the lock is stale or absent", async () => {
		const dir = fs.mkdtempSync(path.join(SCRATCH, "stale-"));
		const started = Date.now();
		await profiles.profileReleased(dir, 10_000, 20);
		fs.symlinkSync("127.0.0.1:+2147483646", path.join(dir, "lock"));
		await profiles.profileReleased(dir, 10_000, 20);
		expect(Date.now() - started).toBeLessThan(1_000);
	});
});
