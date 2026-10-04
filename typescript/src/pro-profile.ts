/**
 * Launching a Camoufox Pro profile: its identity bundle, and its browser
 * state restored before the launch and synced back when the browser closes.
 * A `warm_plan: none` profile's state is sealed with the account's content
 * key, which never leaves the account's machines. A warmed profile's state is
 * sealed with the warm pool's key, which never reaches this machine: the API
 * restores it and serves the directory, and takes it back the same way.
 * TypeScript twin of pythonlib/camoufox/pro_profile.py; see docs/pro.md,
 * "Profiles".
 */
import { createHash, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { DefaultAddons } from "./addons.js";
import { ProError, StatePoolSealed } from "./exceptions.js";
import { OS_NAME, userCacheDir, userConfigDir } from "./paths.js";
import {
	clientName,
	type Lease,
	LIVE,
	proTiming,
	sleepS,
	transient,
	writePrivate,
} from "./pro.js";
import {
	AccountKeys,
	ArchiveError,
	type CapturedFile,
	CHUNKING,
	canonical,
	capture,
	chunkIdBytes,
	chunkIdText,
	chunkLengths,
	decodeManifest,
	HARD_CAP,
	manifestOf,
	POLICY_VERSION,
	readArchive,
	restore,
	StateIntegrityError,
	StateTooLarge,
	WORKERS,
	writeArchive,
} from "./pro-state.js";

export const CONTENT_KEY_ENV = "CAMOUFOX_PRO_CONTENT_KEY";
/** The most chunks one uploads call may list. */
const UPLOAD_BATCH = 2000;
const FF_PLACEHOLDER = "{FF}";
const ARCHIVE_TYPE = "application/x-cfp-dir+gzip";

// ── the content key ─────────────────────────────────────────────────────────

export function contentKeyPath(accountId: number): string {
	return path.join(
		userConfigDir("camoufox"),
		"pro-content-keys",
		`${accountId}.key`,
	);
}

function decodeKey(text: string, where: string): Buffer {
	const key = Buffer.from(text.trim(), "base64url");
	if (key.length !== 32) {
		throw new Error(`${where} is not a 32-byte base64url content key`);
	}
	return key;
}

/**
 * The account's content key: CAMOUFOX_PRO_CONTENT_KEY, else the key file,
 * which is created the first time. It is never sent anywhere; without it the
 * account's synced state cannot be read.
 */
export function contentKey(accountId: number): Buffer {
	const fromEnv = (process.env[CONTENT_KEY_ENV] ?? "").trim();
	if (fromEnv) return decodeKey(fromEnv, CONTENT_KEY_ENV);
	const file = contentKeyPath(accountId);
	if (!fs.existsSync(file)) {
		const created = writePrivate(file, randomBytes(32).toString("base64url"), {
			replace: false,
		});
		if (created) {
			console.warn(
				`camoufox-pro: created this account's content key at ${file}. Copy it to every machine ` +
					`that launches the account's profiles (or set ${CONTENT_KEY_ENV}); ` +
					"state synced with a lost key cannot be read again.",
			);
		}
	}
	if (OS_NAME !== "win" && fs.statSync(file).mode & 0o077) {
		throw new Error(
			`${file} can be read by other users, so it is not used. Run: chmod 600 ${file}`,
		);
	}
	return decodeKey(fs.readFileSync(file, "utf-8"), file);
}

// ── presigned transfers ─────────────────────────────────────────────────────

async function transfer(
	url: string,
	init: RequestInit,
	what: string,
): Promise<Buffer> {
	for (const delay of [...proTiming.mintRetryS, null]) {
		let failure: string;
		try {
			const response = await fetch(url, {
				...init,
				signal: AbortSignal.timeout(120_000),
			});
			const body = Buffer.from(await response.arrayBuffer());
			if (response.ok) return body;
			failure = `HTTP ${response.status}`;
			if (response.status < 500 && response.status !== 429) {
				throw new ProError(`${what} failed: ${failure}`, {
					status: response.status,
				});
			}
		} catch (error) {
			if (error instanceof ProError) throw error;
			failure = (error as Error).message;
		}
		if (delay === null) throw new ProError(`${what} failed: ${failure}`);
		await sleepS(delay);
	}
	throw new Error("unreachable");
}

const download = (url: string, what: string) =>
	transfer(url, { method: "GET" }, what);

const sha256 = (data: Uint8Array) => createHash("sha256").update(data).digest();

// ── the identity ────────────────────────────────────────────────────────────

/** The launch options that present a profile's identity, from its bundle. */
export interface ProfileIdentity {
	config: Record<string, any>;
	fingerprint: Record<string, any>;
	os: string;
	ff_version: number;
	firefox_user_prefs: Record<string, any>;
	exclude_addons: string[];
}

/** Download the profile's identity bundle and check it is the one the lease names. */
export async function fetchBundle(
	section: Record<string, any>,
): Promise<Record<string, any>> {
	const body = await download(
		section.bundle.url,
		"downloading the identity bundle",
	);
	if (sha256(body).toString("hex") !== section.bundle.sha256) {
		throw new StateIntegrityError(
			"the identity bundle does not match the sha256 its lease names",
		);
	}
	return JSON.parse(body.toString("utf-8"));
}

/** The bundle as launch options, for a browser whose Firefox major is `ffMajor`. */
export function identityOptions(
	bundle: Record<string, any>,
	ffMajor: string,
): ProfileIdentity {
	const config: Record<string, any> = {};
	for (const [key, value] of Object.entries(bundle.config)) {
		config[key] =
			typeof value === "string"
				? value.replaceAll(FF_PLACEHOLDER, ffMajor)
				: value;
	}
	return {
		config,
		fingerprint: bundle.browserforge_fingerprint,
		os: bundle.profile.os,
		ff_version: Number(ffMajor),
		firefox_user_prefs: { ...bundle.prefs },
		exclude_addons: Object.keys(DefaultAddons),
	};
}

// ── where a profile's directories live ──────────────────────────────────────

function profileHome(profileId: string): string {
	return path.join(userCacheDir("camoufox"), "pro", "profiles", profileId);
}

/**
 * Each session directory and kept capture has a mark under the profile's
 * marks/ folder: the version it was restored from, which decides whether it
 * can still be committed.
 */
function marker(dir: string): string {
	return path.join(
		path.dirname(path.dirname(dir)),
		"marks",
		`${path.basename(path.dirname(dir))}-${path.basename(dir)}.json`,
	);
}

function mark(dir: string, baseVersion: number, served: boolean): void {
	fs.mkdirSync(path.dirname(marker(dir)), { recursive: true, mode: 0o700 });
	writePrivate(
		marker(dir),
		JSON.stringify({ base_version: baseVersion, served }),
	);
}

function readMark(dir: string): { base_version: number } | null {
	try {
		const found = JSON.parse(fs.readFileSync(marker(dir), "utf-8"));
		return Number.isInteger(found?.base_version) ? found : null;
	} catch {
		return null;
	}
}

function move(dir: string, destination: string): void {
	fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
	fs.renameSync(dir, destination);
	if (fs.existsSync(marker(dir))) {
		fs.mkdirSync(path.dirname(marker(destination)), {
			recursive: true,
			mode: 0o700,
		});
		fs.renameSync(marker(dir), marker(destination));
	}
}

/** Move a capture that was not committed aside, where it is kept, and say so. */
function keep(
	dir: string,
	profileId: string,
	kind: "conflicts" | "pending",
	why: string,
): void {
	const kept = path.join(
		profileHome(profileId),
		kind,
		new Date().toISOString().replace(/[:.]/g, "-"),
	);
	move(dir, kept);
	console.warn(
		`camoufox-pro: profile ${profileId}'s state was not synced (${why}); this session's state is kept at ${kept}`,
	);
}

// ── restore ─────────────────────────────────────────────────────────────────

/** What is already stored of a chunk: its sealed size and the sha256 of its sealed bytes. */
interface Stored {
	stored: number;
	sha256: Buffer;
}

async function restoreState(
	lease: Lease,
	section: Record<string, any>,
	keys: AccountKeys,
	target: string,
	ffVersion: string,
): Promise<Map<string, Stored>> {
	const known = new Map<string, Stored>();
	const version: number = section.state.version;
	if (version === 0) {
		fs.mkdirSync(target, { mode: 0o700 });
		return known;
	}
	const route = `/api/v1/profiles/${section.id}/state?version=${version}`;
	let state = await lease.api("GET", route);
	const urls = new Map<string, string>();
	const manifestRef = state.manifest;
	for (;;) {
		for (const chunk of state.chunks) urls.set(chunk.chunk_id, chunk.url);
		if (!state.next) break;
		state = await lease.api(
			"GET",
			`${route}&cursor=${encodeURIComponent(state.next)}`,
		);
	}
	const sealed = await download(
		manifestRef.url,
		"downloading the state manifest",
	);
	if (sha256(sealed).toString("hex") !== manifestRef.sha256) {
		throw new StateIntegrityError(
			"the state manifest does not match the sha256 the API names",
		);
	}
	const manifest = decodeManifest(
		keys.openManifest(section.id, version, sealed),
		section.id,
		version,
	);
	await restore(
		manifest,
		keys,
		async (chunkId) => {
			const url = urls.get(chunkId);
			if (!url) throw new Error(`the API listed no chunk ${chunkId}`);
			const blob = await download(url, "downloading a state chunk");
			known.set(chunkId, { stored: blob.length, sha256: sha256(blob) });
			return blob;
		},
		target,
		ffVersion,
	);
	return known;
}

// ── capture and commit ──────────────────────────────────────────────────────

interface Claim {
	chunk_id: string;
	size: number;
	sha256: string;
}

async function withLimit<T>(
	items: T[],
	run: (item: T) => Promise<void>,
): Promise<void> {
	let next = 0;
	await Promise.all(
		Array.from({ length: Math.min(WORKERS, items.length) }, async () => {
			while (next < items.length) await run(items[next++]);
		}),
	);
}

/**
 * Chunk and seal a captured profile, upload what the store does not hold, and
 * commit it as the next version, releasing the lease with the commit.
 */
/**
 * Resolve once Firefox has let go of `dir`, or after `timeoutMs`. Playwright reports a persistent
 * context closed while Firefox is still writing prefs.js and places.sqlite on its way out, and a
 * capture taken then reads files mid-write. If it times out the capture's own size check still
 * refuses a moving file, and the session is kept for the next launch to commit.
 */
export async function profileReleased(
	dir: string,
	timeoutMs = 30_000,
	pollMs = 100,
): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	while (profileHeld(dir) && Date.now() < deadline) {
		await new Promise((resolve) => setTimeout(resolve, pollMs));
	}
}

function profileHeld(dir: string): boolean {
	if (OS_NAME === "win") {
		// Windows Firefox holds parent.lock open without sharing for as long as it runs.
		try {
			fs.closeSync(fs.openSync(path.join(dir, "parent.lock"), "r+"));
			return false;
		} catch (error) {
			return (error as NodeJS.ErrnoException).code !== "ENOENT";
		}
	}
	// Elsewhere it is a `lock` symlink to "<address>:+<pid>", left behind only by a crash.
	let target: string;
	try {
		target = fs.readlinkSync(path.join(dir, "lock"));
	} catch {
		return false;
	}
	const pid = Number(/\+(\d+)$/.exec(target)?.[1]);
	if (!pid) return false;
	try {
		process.kill(pid, 0);
		return true;
	} catch (error) {
		return (error as NodeJS.ErrnoException).code === "EPERM";
	}
}

async function commitState(
	lease: Lease,
	profileId: string,
	keys: AccountKeys,
	known: Map<string, Stored>,
	baseVersion: number,
	dir: string,
	ffVersion: string,
	{
		release = true,
		crashed = false,
	}: { release?: boolean; crashed?: boolean } = {},
): Promise<number> {
	const snapshot = await capture(dir);
	const sealedDir = `${dir}.sealed`;
	lease.scratch.push(sealedDir);
	fs.mkdirSync(sealedDir, { mode: 0o700 });
	// Each chunk's plaintext location, so one the store lost can be sealed again.
	const where = new Map<string, [string, number, number]>();
	const layout: { file: CapturedFile; refs: { id: string; size: number }[] }[] =
		[];
	const sealedSize = new Map<string, number>(
		[...known].map(([id, stored]) => [id, stored.stored]),
	);
	const claims = new Map<string, Claim>();
	const sealChunk = (id: string, plaintext: Uint8Array): Claim => {
		const blob = keys.sealChunk(chunkIdBytes(id), plaintext);
		fs.writeFileSync(path.join(sealedDir, id), blob, { mode: 0o600 });
		sealedSize.set(id, blob.length);
		return {
			chunk_id: id,
			size: blob.length,
			sha256: sha256(blob).toString("hex"),
		};
	};
	for (const file of snapshot.files) {
		const data = fs.readFileSync(path.join(dir, file.path));
		if (data.length !== file.size) {
			throw new Error(`${file.path} changed size after capture`);
		}
		const refs: { id: string; size: number }[] = [];
		let offset = 0;
		for (const length of chunkLengths(data)) {
			const plaintext = data.subarray(offset, offset + length);
			const id = chunkIdText(keys.chunkId(plaintext));
			where.set(id, [file.path, offset, length]);
			if (!claims.has(id)) {
				const stored = known.get(id);
				claims.set(
					id,
					stored
						? {
								chunk_id: id,
								size: stored.stored,
								sha256: stored.sha256.toString("hex"),
							}
						: sealChunk(id, plaintext),
				);
			}
			refs.push({ id, size: length });
			offset += length;
		}
		layout.push({ file, refs });
	}
	const total = snapshot.files.reduce((sum, file) => sum + file.size, 0);
	const route = `/api/v1/profiles/${profileId}/state`;

	const upload = async (list: Claim[]): Promise<void> => {
		const resealed: Claim[] = [];
		for (let start = 0; start < list.length; start += UPLOAD_BATCH) {
			const answer = await lease.api("POST", `${route}/uploads`, {
				lease_id: lease.leaseId,
				base_version: baseVersion,
				total_bytes: total,
				chunks: list.slice(start, start + UPLOAD_BATCH),
			});
			await withLimit(
				answer.missing as Record<string, any>[],
				async (missing) => {
					const file = path.join(sealedDir, missing.chunk_id);
					if (!fs.existsSync(file)) {
						// Stored once, lost since: sealed again, so its sha256 changes.
						const [relpath, offset, length] = where.get(missing.chunk_id) as [
							string,
							number,
							number,
						];
						const data = fs.readFileSync(path.join(dir, relpath));
						resealed.push(
							sealChunk(
								missing.chunk_id,
								data.subarray(offset, offset + length),
							),
						);
						return;
					}
					await transfer(
						missing.url,
						{
							method: "PUT",
							headers: missing.headers,
							body: fs.readFileSync(file),
						},
						"uploading a state chunk",
					);
				},
			);
		}
		if (resealed.length) await upload(resealed);
	};
	await upload([...claims.values()]);

	const version = baseVersion + 1;
	const manifest = manifestOf({
		profile_id: profileId,
		version,
		base_version: baseVersion,
		captured_at: new Date().toISOString(),
		crashed,
		integrity: snapshot.suspectFiles.length ? "suspect" : "ok",
		suspect_files: snapshot.suspectFiles,
		ff_version: ffVersion,
		driver_version: clientName(),
		policy_version: POLICY_VERSION,
		chunking: { ...CHUNKING },
		files: layout.map(({ file, refs }) => ({
			...file,
			chunks: refs.map((ref) => ({
				...ref,
				stored: sealedSize.get(ref.id) as number,
			})),
		})),
		dirs: snapshot.dirs,
	});
	const body = keys.sealManifest(
		profileId,
		version,
		Buffer.from(canonical(manifest)),
	);
	const commit = {
		lease_id: lease.leaseId,
		base_version: baseVersion,
		version,
		release,
		manifest: {
			sha256: sha256(body).toString("hex"),
			size: body.length,
			chunk_count: manifest.chunk_count,
			total_bytes: manifest.total_bytes,
			chunks: [...claims.keys()].map((id) => ({
				chunk_id: id,
				size: sealedSize.get(id) as number,
			})),
		},
		manifest_body: body.toString("base64url"),
		file_count: manifest.file_count,
		captured_at: manifest.captured_at,
		ff_version: ffVersion,
		driver_version: manifest.driver_version,
		crashed: manifest.crashed,
		integrity: manifest.integrity,
	};
	try {
		await lease.api("PUT", route, commit);
	} catch (error) {
		if (!(error instanceof ProError) || error.code !== "state_chunks_missing")
			throw error;
		const lost: string[] = (error.details as any)?.chunk_ids ?? [];
		await upload(lost.map((id) => claims.get(id) as Claim));
		await lease.api("PUT", route, commit);
	}
	return version;
}

// ── state the API serves ────────────────────────────────────────────────────

/** Download a warmed profile's state as the API restored it, into `target`. Resolves to its version. */
async function restoreServed(
	lease: Lease,
	section: Record<string, any>,
	target: string,
	ffVersion: string,
): Promise<number> {
	const response = await lease.send("GET", section.state.archive, {
		lease_id: lease.leaseId,
		ff_version: ffVersion,
	});
	const download = `${target}.cfpdir.gz`;
	lease.scratch.push(download);
	fs.writeFileSync(download, Buffer.from(await response.arrayBuffer()), {
		mode: 0o600,
	});
	try {
		await readArchive(download, target, HARD_CAP);
	} catch (error) {
		if (error instanceof ArchiveError) {
			throw new StateIntegrityError(
				`the served state is not a cfp-dir/1 archive: ${error.message}`,
			);
		}
		throw error;
	} finally {
		fs.rmSync(download, { force: true });
	}
	return Number(
		response.headers.get("x-cfp-state-version") ?? section.state.version,
	);
}

/** Capture a warmed profile and send it to the API, which commits it as the next version. */
async function commitServed(
	lease: Lease,
	section: Record<string, any>,
	baseVersion: number,
	dir: string,
	ffVersion: string,
	{
		release = true,
		crashed = false,
	}: { release?: boolean; crashed?: boolean } = {},
): Promise<number> {
	const snapshot = await capture(dir);
	const packed = `${dir}.cfpdir.gz`;
	lease.scratch.push(packed);
	await writeArchive(packed, snapshot);
	const params = {
		lease_id: lease.leaseId,
		base_version: String(baseVersion),
		ff_version: ffVersion,
		release: release ? "1" : "0",
		crashed: crashed ? "1" : "0",
	};
	for (const delay of [...proTiming.mintRetryS, null]) {
		try {
			const response = await lease.send("PUT", section.state.archive, params, {
				body: fs.readFileSync(packed),
				contentType: ARCHIVE_TYPE,
			});
			const answer = (await response.json()) as Record<string, any>;
			fs.rmSync(packed, { force: true });
			return Number(answer.version);
		} catch (error) {
			if (delay === null || !transient(error)) throw error;
			await sleepS((error as ProError).retry_after || delay);
		}
	}
	throw new Error("unreachable");
}

// ── a session that never closed ─────────────────────────────────────────────

/**
 * Commit the newest capture an earlier session left behind (a crash, or a
 * commit that failed and was kept to retry) before anything is restored, so a
 * launch never starts from older state than this machine holds. It is used
 * only when it was restored from the version the API still has as its head;
 * anything else is kept as a conflict, never merged. On success the capture
 * becomes this session's directory, at `target`, and its version is returned.
 */
async function recover(
	lease: Lease,
	section: Record<string, any>,
	keys: AccountKeys | null,
	baseVersion: number,
	target: string,
	ffVersion: string,
): Promise<number | null> {
	const home = profileHome(section.id);
	const live = new Set(
		[...LIVE.values()].flatMap((held) => held.scratch.map(String)),
	);
	const candidates: { mtime: number; dir: string; base: number }[] = [];
	for (const kind of ["pending", "sessions"]) {
		const folder = path.join(home, kind);
		if (!fs.existsSync(folder)) continue;
		for (const name of fs.readdirSync(folder)) {
			const dir = path.join(folder, name);
			if (dir === target || name.startsWith(".")) continue;
			if (!fs.statSync(dir).isDirectory()) continue;
			if (kind === "sessions" && live.has(dir)) continue;
			const found = readMark(dir);
			if (found) {
				candidates.push({
					mtime: fs.statSync(dir).mtimeMs,
					dir,
					base: found.base_version,
				});
			}
		}
	}
	if (!candidates.length) return null;
	candidates.sort((a, b) => b.mtime - a.mtime);
	for (const stale of candidates.slice(1)) {
		keep(
			stale.dir,
			section.id,
			"conflicts",
			"a newer capture of this profile was left behind as well",
		);
	}
	const { dir, base } = candidates[0];
	if (base !== baseVersion) {
		keep(
			dir,
			section.id,
			"conflicts",
			`it was restored from v${base} and the profile is at v${baseVersion} now`,
		);
		return null;
	}
	console.warn(
		`camoufox-pro: profile ${section.key}: committing the state a session left behind at ${dir}`,
	);
	let version: number;
	try {
		version = keys
			? await commitState(
					lease,
					section.id,
					keys,
					new Map(),
					baseVersion,
					dir,
					ffVersion,
					{ release: false, crashed: true },
				)
			: await commitServed(lease, section, baseVersion, dir, ffVersion, {
					release: false,
					crashed: true,
				});
	} catch (error) {
		if (error instanceof ProError && CONFLICT.has(error.code ?? "")) {
			keep(dir, section.id, "conflicts", error.message);
			return null;
		}
		throw error;
	}
	move(dir, target);
	return version;
}

/** The refusals after which a capture is kept as a conflict, never merged, and those after which it is kept to retry. */
const CONFLICT = new Set([
	"lease_not_holder",
	"state_window_expired",
	"state_conflict",
]);
const PENDING = new Set(["lease_conflict", "state_too_large"]);

// ── a profile launch ────────────────────────────────────────────────────────

/**
 * Prepare a profile's launch under `lease`: its identity, and a user-data
 * directory holding its restored state. The lease then syncs the state back
 * when the browser closes, and releases itself with that commit.
 */
export async function openProfile(
	lease: Lease,
	ffVersion: string,
): Promise<{ identity: ProfileIdentity; userDataDir: string }> {
	const section = lease.grants.profile as Record<string, any>;
	const identity = identityOptions(
		await fetchBundle(section),
		ffVersion.split(".", 1)[0],
	);
	const home = profileHome(section.id);
	fs.mkdirSync(path.join(home, "sessions"), { recursive: true, mode: 0o700 });
	const userDataDir = path.join(home, "sessions", lease.leaseId);
	lease.scratch.push(userDataDir, marker(userDataDir));
	const served = section.state?.transport === "server";
	if (section.key_class !== "account" && !served) {
		console.warn(
			`camoufox-pro: profile ${section.key} keeps its identity, but its browser state is not synced: ` +
				"this deployment does not serve warmed profiles' state",
		);
		fs.mkdirSync(userDataDir, { mode: 0o700 });
		return { identity, userDataDir };
	}
	const keys = served
		? null
		: AccountKeys.derive(contentKey(lease.accountId), lease.accountId);
	let baseVersion: number = section.state.version;
	let known = new Map<string, Stored>();
	const recovered = await recover(
		lease,
		section,
		keys,
		baseVersion,
		userDataDir,
		ffVersion,
	);
	if (recovered !== null) {
		baseVersion = recovered;
	} else if (keys === null) {
		baseVersion = await restoreServed(lease, section, userDataDir, ffVersion);
	} else {
		known = await restoreState(lease, section, keys, userDataDir, ffVersion);
	}
	mark(userDataDir, baseVersion, served);
	lease.stateDirty = true;
	lease.onClose = async () => {
		// The commit releases the lease, so a renewal racing it must not mint a new one.
		lease.stopRenewing();
		await profileReleased(userDataDir);
		try {
			if (keys === null) {
				await commitServed(lease, section, baseVersion, userDataDir, ffVersion);
			} else {
				await commitState(
					lease,
					section.id,
					keys,
					known,
					baseVersion,
					userDataDir,
					ffVersion,
				);
			}
		} catch (error) {
			const code = error instanceof ProError ? (error.code ?? "") : "";
			const kept =
				error instanceof StateTooLarge ||
				PENDING.has(code) ||
				CONFLICT.has(code);
			if (fs.existsSync(userDataDir)) {
				const kind =
					CONFLICT.has(code) || error instanceof StatePoolSealed
						? "conflicts"
						: "pending";
				keep(userDataDir, section.id, kind, (error as Error).message);
			}
			await lease.release();
			if (kept) return;
			throw error;
		}
		await lease.forget();
	};
	// The process is exiting with the browser never closed: keep the directory
	// where the next launch of this profile finds and commits it.
	lease.onAbort = () => {
		lease.scratch = lease.scratch.filter(
			(entry) => entry !== userDataDir && entry !== marker(userDataDir),
		);
	};
	return { identity, userDataDir };
}
