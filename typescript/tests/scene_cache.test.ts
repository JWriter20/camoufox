/**
 * The scene cache on this machine, the twin of python/tests/test_scene_cache.py.
 * A real HTTP server stands in for the render gateway; no browser runs here, so
 * none of this shows the browser getting a hit.
 */
import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as http from "node:http";
import type { AddressInfo } from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as pro from "../src/pro.js";
import * as sceneCache from "../src/scene_cache.js";

const VERSION = "156.0";
const OS = "windows";
const TOKEN = "cfl1.token";

function body(fill = "a", size = 2_000_000): Buffer {
	const data = Buffer.alloc(size, fill);
	sceneCache.SQLITE_MAGIC.copy(data);
	return data;
}

function sha(data: Buffer): string {
	return createHash("sha256").update(data).digest("hex");
}

function manifestFor(data: Buffer): Record<string, unknown> {
	return {
		schema: 1,
		build: VERSION,
		os: OS,
		sha256: sha(data),
		bytes: data.length,
		path: `/v1/scene-cache/snapshot/${VERSION}/${OS}/${sha(data)}`,
	};
}

class Gateway {
	data = body();
	manifest = manifestFor(this.data);
	published = true;
	requests: { url: string; range?: string; auth?: string }[] = [];
	/** Serve this many bytes and hang up, once. */
	cutAfter: number | null = null;
	/** Serve this many bytes and go quiet, once. */
	hangAfter: number | null = null;
	onSnapshot: (() => void) | null = null;
	ignoreRange = false;
	corrupt = false;
	allowToken = TOKEN;
	server: http.Server;
	render: sceneCache.Render = { endpoint: "", token: TOKEN, version: VERSION };

	constructor() {
		this.server = http.createServer((req, res) => this.#handle(req, res));
	}

	async start(): Promise<void> {
		await new Promise<void>((resolve) =>
			this.server.listen(0, "127.0.0.1", resolve),
		);
		const { port } = this.server.address() as AddressInfo;
		this.render.endpoint = `ws://127.0.0.1:${port}/firefox-webgl`;
	}

	async stop(): Promise<void> {
		this.server.closeAllConnections();
		await new Promise((resolve) => this.server.close(resolve));
	}

	publish(data: Buffer): void {
		this.data = data;
		this.manifest = manifestFor(data);
	}

	snapshots() {
		return this.requests.filter((entry) => entry.url.includes("/snapshot/"));
	}

	#answer(res: http.ServerResponse, status: number, data: string): void {
		res.writeHead(status, { "Content-Length": Buffer.byteLength(data) });
		res.end(data);
	}

	#handle(req: http.IncomingMessage, res: http.ServerResponse) {
		const url = req.url as string;
		this.requests.push({
			url,
			range: req.headers.range,
			auth: req.headers.authorization,
		});
		if (url.includes("/snapshot/")) this.onSnapshot?.();
		if (req.headers.authorization !== `Bearer ${this.allowToken}`) {
			return this.#answer(res, 401, "{}");
		}
		if (url.startsWith("/v1/scene-cache/manifest")) {
			if (!this.published) return this.#answer(res, 404, "{}");
			return this.#answer(res, 200, JSON.stringify(this.manifest));
		}
		if (url !== this.manifest.path) return this.#answer(res, 404, "{}");
		let data = this.data;
		if (this.corrupt) {
			data = Buffer.concat([data.subarray(0, -1), Buffer.from("!")]);
		}
		let start = 0;
		const value = req.headers.range;
		if (value && !this.ignoreRange) {
			start = Number(value.replace("bytes=", "").replace("-", ""));
			if (start >= data.length) return this.#answer(res, 416, "");
		}
		const part = data.subarray(start);
		const headers: Record<string, string | number> = {
			"Content-Length": part.length,
		};
		if (start) {
			headers["Content-Range"] =
				`bytes ${start}-${data.length - 1}/${data.length}`;
		}
		res.writeHead(start ? 206 : 200, headers);
		if (this.hangAfter !== null) {
			const hang = this.hangAfter;
			this.hangAfter = null;
			res.write(part.subarray(0, hang));
			return;
		}
		if (this.cutAfter !== null) {
			const cut = this.cutAfter;
			this.cutAfter = null;
			res.write(part.subarray(0, cut), () => res.destroy());
			return;
		}
		res.end(part);
	}
}

describe("scene cache", () => {
	let home: string;
	let gateway: Gateway;
	let folder: string;
	const retryS = sceneCache.sceneCacheTiming.retryS;

	beforeEach(async () => {
		home = fs.mkdtempSync(path.join(os.tmpdir(), "scene-cache-"));
		vi.stubEnv("HOME", home);
		vi.stubEnv("XDG_CACHE_HOME", path.join(home, "cache"));
		vi.stubEnv(sceneCache.UPDATE_ENV, "");
		sceneCache.sceneCacheTiming.retryS = 0.01;
		gateway = new Gateway();
		await gateway.start();
		folder = sceneCache.directory(VERSION, "ab".repeat(32), OS);
		fs.mkdirSync(folder, { recursive: true });
		vi.spyOn(console, "warn").mockImplementation(() => {});
		vi.spyOn(console, "info").mockImplementation(() => {});
	});

	afterEach(async () => {
		await gateway.stop();
		vi.restoreAllMocks();
		vi.unstubAllEnvs();
		sceneCache.sceneCacheTiming.retryS = retryS;
		fs.rmSync(home, { recursive: true, force: true });
	});

	const run = (signal?: AbortSignal) =>
		sceneCache.update(folder, OS, () => gateway.render, signal);

	const partFile = () =>
		path.join(folder, `${gateway.manifest.sha256}.sqlite.part`);

	const glob = (suffix: string) =>
		fs.readdirSync(folder).filter((name) => name.endsWith(suffix));

	function installed(data: Buffer): string {
		const file = sceneCache.verifiedFile(folder) as string;
		expect(file).not.toBeNull();
		expect(path.basename(file)).toBe(`${sha(data)}.sqlite`);
		expect(fs.readFileSync(file).equals(data)).toBe(true);
		return file;
	}

	function backdate(): void {
		const stateFile = path.join(folder, sceneCache.STATE);
		const state = JSON.parse(fs.readFileSync(stateFile, "utf8"));
		fs.writeFileSync(stateFile, JSON.stringify({ ...state, checked_at: 0 }));
	}

	it("has an update interval that works unset and survives a bad override", () => {
		expect(sceneCache.updateSeconds()).toBe(24 * 3600);
		expect(sceneCache.DEFAULT_UPDATE_S).toBe(24 * 3600);
		vi.stubEnv(sceneCache.UPDATE_ENV, "60");
		expect(sceneCache.updateSeconds()).toBe(60);
		for (const bad of ["soon", "-1"]) {
			vi.stubEnv(sceneCache.UPDATE_ENV, bad);
			expect(sceneCache.updateSeconds()).toBe(sceneCache.DEFAULT_UPDATE_S);
		}
	});

	it("lands a download whole, with the render token", async () => {
		await run();
		installed(gateway.data);
		expect(glob(".part")).toEqual([]);
		expect(fs.existsSync(path.join(folder, sceneCache.LOCK))).toBe(false);
		expect(gateway.requests[0]?.url).toBe(
			`/v1/scene-cache/manifest?build=${VERSION}&os=${OS}`,
		);
		expect(gateway.requests[0]?.auth).toBe(`Bearer ${TOKEN}`);
	});

	it("resumes a dropped connection where it stopped", async () => {
		gateway.cutAfter = 500_000;
		await run();
		installed(gateway.data);
		const ranges = gateway.snapshots().map((entry) => entry.range);
		expect(ranges[0]).toBeUndefined();
		const resumedAt = Number(ranges[1]?.replace("bytes=", "").replace("-", ""));
		expect(resumedAt).toBeGreaterThan(0);
		expect(resumedAt).toBeLessThanOrEqual(500_000);
	});

	it("resumes a part file from an earlier launch", async () => {
		fs.writeFileSync(partFile(), gateway.data.subarray(0, 1234));
		await run();
		installed(gateway.data);
		expect(gateway.snapshots()[0]?.range).toBe("bytes=1234-");
	});

	it("restarts the file when a server ignores the range", async () => {
		fs.writeFileSync(partFile(), gateway.data.subarray(0, 1234));
		gateway.ignoreRange = true;
		await run();
		installed(gateway.data);
	});

	it("starts over from a part longer than the file", async () => {
		fs.writeFileSync(
			partFile(),
			Buffer.concat([gateway.data, Buffer.from("extra")]),
		);
		await run();
		installed(gateway.data);
	});

	it("installs nothing on a bad hash and leaves nothing to resume", async () => {
		gateway.corrupt = true;
		await run();
		expect(sceneCache.verifiedFile(folder)).toBeNull();
		expect(glob(".sqlite")).toEqual([]);
		expect(glob(".part")).toEqual([]);
		gateway.corrupt = false;
		await run();
		installed(gateway.data);
	});

	it("refuses a file that is not SQLite even with the right hash", async () => {
		gateway.publish(Buffer.from("not a database".repeat(100)));
		await run();
		expect(sceneCache.verifiedFile(folder)).toBeNull();
		expect(glob(".sqlite")).toEqual([]);
	});

	it("serves the old file until the new one is whole, then collects it", async () => {
		await run();
		const old = installed(gateway.data);
		const next = body("b");
		gateway.publish(next);
		backdate();
		gateway.corrupt = true;
		await run();
		expect(sceneCache.verifiedFile(folder)).toBe(old);
		gateway.corrupt = false;
		await run();
		installed(next);
		expect(fs.existsSync(old)).toBe(false);
		expect(glob(".sqlite")).toHaveLength(1);
	});

	it("asks nothing again inside the interval", async () => {
		await run();
		const asked = gateway.requests.length;
		await run();
		expect(gateway.requests).toHaveLength(asked);
		backdate();
		await run();
		expect(gateway.requests).toHaveLength(asked + 1);
		expect(gateway.snapshots()).toHaveLength(1);
	});

	it("remembers that nothing is published and keeps what is here", async () => {
		gateway.published = false;
		await run();
		expect(sceneCache.verifiedFile(folder)).toBeNull();
		const asked = gateway.requests.length;
		await run();
		expect(gateway.requests).toHaveLength(asked);

		gateway.published = true;
		backdate();
		await run();
		const kept = installed(gateway.data);
		gateway.published = false;
		backdate();
		await run();
		expect(sceneCache.verifiedFile(folder)).toBe(kept);
	});

	it("fetches a lost file again inside the interval", async () => {
		await run();
		fs.unlinkSync(installed(gateway.data));
		await run();
		installed(gateway.data);
	});

	it("does not hand the browser a file of the wrong size or a symlink", async () => {
		await run();
		const file = installed(gateway.data);
		fs.writeFileSync(file, "short");
		expect(sceneCache.verifiedFile(folder)).toBeNull();
		fs.unlinkSync(file);
		const target = path.join(home, "elsewhere");
		fs.writeFileSync(target, gateway.data);
		fs.symlinkSync(target, file);
		expect(sceneCache.verifiedFile(folder)).toBeNull();
	});

	it.each([
		[{ sha256: "nothex" }],
		[{ bytes: sceneCache.MAX_BYTES + 1 }],
		[{ bytes: 0 }],
		[{ bytes: true }],
		[{ build: "155.0" }],
		[{ os: "macos" }],
		[{ schema: 2 }],
		[{ path: "https://evil.example/file" }],
		[{ path: "//evil.example/file" }],
	])("ignores a manifest that does not check out: %j", async (change) => {
		gateway.manifest = { ...gateway.manifest, ...change };
		await run();
		expect(sceneCache.verifiedFile(folder)).toBeNull();
		expect(gateway.snapshots()).toEqual([]);
	});

	it("downloads nothing with too little disk", async () => {
		vi.spyOn(sceneCache.sceneCacheTiming, "freeBytes").mockReturnValue(
			gateway.data.length,
		);
		await run();
		expect(gateway.snapshots()).toEqual([]);
		expect(fs.existsSync(path.join(folder, sceneCache.LOCK))).toBe(false);
	});

	it("leaves the download to a live lock's holder and takes a stale one", async () => {
		const lock = path.join(folder, sceneCache.LOCK);
		fs.writeFileSync(lock, "");
		await run();
		expect(gateway.requests).toEqual([]);
		const old = new Date(
			Date.now() - (sceneCache.sceneCacheTiming.lockStaleS + 1) * 1000,
		);
		fs.utimesSync(lock, old, old);
		await run();
		installed(gateway.data);
	});

	it("stops the download when the lease ends and leaves it resumable", async () => {
		const stop = new AbortController();
		gateway.hangAfter = 500_000;
		const running = run(stop.signal);
		await vi.waitFor(() => {
			expect(fs.existsSync(partFile())).toBe(true);
			expect(fs.statSync(partFile()).size).toBeGreaterThan(0);
		});
		stop.abort();
		await running;
		expect(sceneCache.verifiedFile(folder)).toBeNull();
		expect(fs.existsSync(path.join(folder, sceneCache.LOCK))).toBe(false);
		expect(glob(".sqlite")).toEqual([]);
		const kept = fs.statSync(partFile()).size;
		expect(kept).toBeGreaterThan(0);
		expect(kept).toBeLessThan(gateway.data.length);
	});

	it("refuses a server that sends more than it advertised", async () => {
		gateway.manifest = { ...gateway.manifest, bytes: 1000 };
		await run();
		expect(sceneCache.verifiedFile(folder)).toBeNull();
		expect(glob(".sqlite")).toEqual([]);
		expect(glob(".part")).toEqual([]);
	});

	it("uses a renewed token for the next connection", async () => {
		const render = { ...gateway.render };
		gateway.onSnapshot = () => {
			if (gateway.snapshots().length === 1) {
				gateway.allowToken = "renewed";
				render.token = "renewed";
			}
		};
		await sceneCache.update(folder, OS, () => render);
		installed(gateway.data);
		expect(gateway.snapshots().map((entry) => entry.auth)).toEqual([
			`Bearer ${TOKEN}`,
			"Bearer renewed",
		]);
	});

	it("never rejects when the gateway is unreachable or refusing", async () => {
		gateway.allowToken = "another";
		await run();
		expect(sceneCache.verifiedFile(folder)).toBeNull();
		for (const endpoint of ["ws://127.0.0.1:1/x", "nonsense"]) {
			await sceneCache.update(folder, OS, () => ({
				endpoint,
				token: "t",
				version: VERSION,
			}));
		}
		await sceneCache.update(folder, OS, () => ({}) as sceneCache.Render);
	});

	it("keeps only this build and OS when clearing", () => {
		const base = sceneCache.root();
		const otherBuild = path.join(base, "155.0-ffffffffffffffff", OS);
		const otherOs = path.join(path.dirname(folder), "macos");
		for (const dir of [otherBuild, otherOs]) {
			fs.mkdirSync(dir, { recursive: true });
			fs.writeFileSync(path.join(dir, "x.sqlite"), "x");
		}
		fs.writeFileSync(path.join(folder, "keep.sqlite"), "x");
		sceneCache.clearOthers(folder);
		expect(fs.existsSync(path.dirname(otherBuild))).toBe(false);
		expect(fs.existsSync(otherOs)).toBe(false);
		expect(fs.existsSync(path.join(folder, "keep.sqlite"))).toBe(true);
	});

	it("keys the directory by build and OS, and a build cannot escape it", () => {
		const one = sceneCache.directory(VERSION, "ab".repeat(32), OS);
		expect(sceneCache.directory(VERSION, "cd".repeat(32), OS)).not.toBe(one);
		expect(sceneCache.directory("157.0", "ab".repeat(32), OS)).not.toBe(one);
		expect(sceneCache.directory(VERSION, "ab".repeat(32), "macos")).not.toBe(
			one,
		);
		const escaped = sceneCache.directory("../../..", "/", OS);
		expect(path.dirname(path.dirname(escaped))).toBe(sceneCache.root());
	});

	function fakeLease(directory: string): sceneCache.AttachableLease {
		return {
			sceneCacheDir: directory,
			targetOs: OS,
			onEnd: [],
			grants: { gpu: { render: gateway.render } },
		};
	}

	it("attach clears other builds, returns nothing at first, then the file", async () => {
		const stale = path.join(sceneCache.root(), "155.0-ffffffffffffffff", OS);
		fs.mkdirSync(stale, { recursive: true });
		const lease = fakeLease(folder);
		expect(sceneCache.attach(lease)).toBeNull();
		expect(fs.existsSync(stale)).toBe(false);
		await vi.waitFor(
			() => {
				expect(sceneCache.verifiedFile(folder)).not.toBeNull();
				expect(fs.existsSync(path.join(folder, sceneCache.LOCK))).toBe(false);
			},
			{ timeout: 10_000 },
		);
		for (const end of lease.onEnd) await end();
		expect(sceneCache.attach(fakeLease(folder))).toBe(
			sceneCache.verifiedFile(folder),
		);
	});

	it("attach never fails a launch when the directory cannot be made", () => {
		const blocker = path.join(home, "file");
		fs.writeFileSync(blocker, "x");
		expect(sceneCache.attach(fakeLease(path.join(blocker, "sub")))).toBeNull();
	});
});

describe("the lease file", () => {
	it("names the directory only when given one", () => {
		const claims = Buffer.from(
			JSON.stringify({ lid: randomUUID(), bh: "ab".repeat(32), exp: 2 }),
		).toString("base64url");
		const token = `cfl1.${claims}.sig`;
		const bare = pro.leaseFileBytes(token, OS, "native", 1);
		expect(JSON.parse(bare).paths.scene_cache_dir).toBeNull();
		const written = pro.leaseFileBytes(
			token,
			OS,
			"native",
			1,
			"/home/üser/cache",
		);
		expect(JSON.parse(written).paths.scene_cache_dir).toBe("/home/üser/cache");
		expect(written).toContain("/home/üser/cache");
	});
});
