/**
 * Camoufox Pro: the scene cache a leased browser keeps on this machine.
 *
 * TypeScript twin of python/src/camoufox/scene_cache.py. One SQLite file per
 * browser build and OS, fetched from the render gateway with the lease's render
 * token, updated about once a day, and deleted when the browser build or OS
 * changes. A miss is still drawn on the farm, so nothing here can fail a launch:
 * every problem is logged, and the browser runs without the file.
 *
 * The browser reads the pref once per process, so a new file takes effect at the
 * next launch, and the first launch has none.
 */
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { userCacheDir } from "./paths.js";

/** The only way the browser learns the file. It takes one regular file, not a directory. */
export const PREF = "gfx.canvas.renderfarm.cache.path";
export const UPDATE_ENV = "CAMOUFOX_PRO_SCENE_CACHE_UPDATE_S";
export const DEFAULT_UPDATE_S = 24 * 3600;
/** What the browser will open; a larger file could only ever miss. */
export const MAX_BYTES = 20 * 1024 ** 3;
export const STATE = "state.json";
export const LOCK = "download.lock";
export const SQLITE_MAGIC = Buffer.from("SQLite format 3\0", "latin1");
/** Room to spare once the file is down, so a download does not fill the disk. */
const FREE_SPACE_FACTOR = 1.1;
const ATTEMPTS = 5;
const CHUNK = 1024 * 1024;
const HEX64 = /^[0-9a-f]{64}$/;

/** Mutable for tests. A live download touches its lock as it goes, so an older lock is a process that died. */
export const sceneCacheTiming = {
	retryS: 5,
	lockStaleS: 600,
	connectTimeoutS: 15,
	readTimeoutS: 60,
	freeBytes(folder: string): number {
		const { bavail, bsize } = fs.statfsSync(folder);
		return Number(bavail) * Number(bsize);
	},
};

export type Render = {
	endpoint: string;
	token: string;
	version: string;
	[key: string]: unknown;
};

type Manifest = { sha256: string; bytes: number; path: string };

type State = {
	sha256?: string | null;
	bytes?: number | null;
	checked_at?: number;
};

export function root(): string {
	return path.join(userCacheDir("camoufox"), "pro", "scene-cache");
}

/** Where this browser build keeps the file for this OS. */
export function directory(
	version: string,
	buildHash: string,
	targetOs: string,
): string {
	const name = `${version}-${buildHash.slice(0, 16)}`.replace(
		/[^0-9A-Za-z._-]/g,
		"_",
	);
	return path.join(root(), name, targetOs);
}

/** How often to ask for a new file. Works unset; a bad override falls back rather than fail a launch. */
export function updateSeconds(): number {
	const raw = process.env[UPDATE_ENV];
	if (!raw) return DEFAULT_UPDATE_S;
	const seconds = Number(raw);
	if (!Number.isFinite(seconds) || seconds < 0) {
		console.warn(
			`camoufox-pro: ignoring ${UPDATE_ENV}=${raw}; it must be seconds`,
		);
		return DEFAULT_UPDATE_S;
	}
	return seconds;
}

function removeQuietly(target: string): void {
	try {
		fs.rmSync(target, { recursive: true, force: true });
	} catch {
		// One still open stays until next time.
	}
}

/** A file for another browser build or OS can only miss: delete it. */
export function clearOthers(keep: string): void {
	const base = root();
	if (!fs.existsSync(base)) return;
	const keepParent = path.dirname(keep);
	for (const name of fs.readdirSync(base)) {
		const buildDir = path.join(base, name);
		if (buildDir !== keepParent) {
			removeQuietly(buildDir);
			continue;
		}
		for (const osName of fs.readdirSync(buildDir)) {
			const osDir = path.join(buildDir, osName);
			if (osDir !== keep) removeQuietly(osDir);
		}
	}
}

function readState(folder: string): State {
	try {
		const state = JSON.parse(fs.readFileSync(path.join(folder, STATE), "utf8"));
		return state && typeof state === "object" ? state : {};
	} catch {
		return {};
	}
}

function writeState(folder: string, state: State): void {
	const staged = path.join(folder, `${STATE}.new`);
	const fd = fs.openSync(staged, "w");
	try {
		fs.writeSync(fd, JSON.stringify(state));
		fs.fsyncSync(fd);
	} finally {
		fs.closeSync(fd);
	}
	fs.renameSync(staged, path.join(folder, STATE));
}

/**
 * The installed file, if it is the one the state names. Hashed when it was
 * installed, not at every launch: 20 GiB of hashing would hold the launch up.
 */
export function verifiedFile(folder: string): string | null {
	const { sha256, bytes } = readState(folder);
	if (typeof sha256 !== "string" || !HEX64.test(sha256)) return null;
	const file = path.join(folder, `${sha256}.sqlite`);
	try {
		const stat = fs.lstatSync(file);
		// The browser refuses a symlink, and would miss on everything.
		return stat.isFile() && stat.size === bytes ? file : null;
	} catch {
		return null;
	}
}

function sizeOf(file: string): number {
	try {
		return fs.statSync(file).size;
	} catch {
		return 0;
	}
}

class Stopped extends Error {}

/** One download per partition across this machine's launches. */
class Lock {
	#held = false;
	constructor(readonly file: string) {}

	acquire(): boolean {
		for (let attempt = 0; attempt < 2; attempt++) {
			try {
				fs.closeSync(fs.openSync(this.file, "wx", 0o600));
				this.#held = true;
				return true;
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
			}
			try {
				const age = Date.now() - fs.statSync(this.file).mtimeMs;
				if (age < sceneCacheTiming.lockStaleS * 1000) return false;
				fs.unlinkSync(this.file);
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
			}
		}
		return false;
	}

	touch(): void {
		const now = new Date();
		fs.utimesSync(this.file, now, now);
	}

	release(): void {
		if (this.#held) fs.rmSync(this.file, { force: true });
		this.#held = false;
	}
}

function checked(manifest: any, version: string, targetOs: string): Manifest {
	if (!manifest || typeof manifest !== "object") {
		throw new Error("the manifest is not an object");
	}
	const { sha256, bytes, path: route } = manifest;
	if (
		manifest.schema !== 1 ||
		manifest.build !== version ||
		manifest.os !== targetOs
	) {
		throw new Error("the manifest is for something else");
	}
	if (typeof sha256 !== "string" || !HEX64.test(sha256)) {
		throw new Error("the manifest has no SHA-256");
	}
	if (!Number.isSafeInteger(bytes) || bytes <= 0 || bytes > MAX_BYTES) {
		throw new Error(`the manifest advertises ${bytes} bytes`);
	}
	// Always a path on the gateway that holds the token, so it can never send the token elsewhere.
	if (route !== `/v1/scene-cache/snapshot/${version}/${targetOs}/${sha256}`) {
		throw new Error(`the manifest points at ${route}`);
	}
	return { sha256, bytes, path: route };
}

function abortableSleep(seconds: number, signal: AbortSignal): Promise<void> {
	return new Promise((resolve, reject) => {
		if (signal.aborted) return reject(new Stopped());
		const done = () => {
			clearTimeout(timer);
			signal.removeEventListener("abort", aborted);
		};
		const aborted = () => {
			done();
			reject(new Stopped());
		};
		const timer = setTimeout(() => {
			done();
			resolve();
		}, seconds * 1000);
		timer.unref();
		signal.addEventListener("abort", aborted, { once: true });
	});
}

class Updater {
	readonly lock: Lock;
	constructor(
		readonly folder: string,
		readonly targetOs: string,
		readonly render: () => Render,
		readonly signal: AbortSignal,
	) {
		this.lock = new Lock(path.join(folder, LOCK));
	}

	#base(): string {
		const endpoint = new URL(this.render().endpoint);
		const scheme = { "wss:": "https:", "ws:": "http:" }[endpoint.protocol];
		if (!scheme) throw new Error(`cannot derive the gateway from ${endpoint}`);
		return `${scheme}//${endpoint.host}`;
	}

	#headers(): Record<string, string> {
		// The current token: renewal replaces it while a long download runs.
		return { Authorization: `Bearer ${this.render().token}` };
	}

	/** Bounds the wait for an answer; the body has its own per-read bound. */
	async #fetch(
		url: string,
		headers: Record<string, string>,
		timeoutS: number,
	): Promise<Response> {
		const timeout = AbortSignal.timeout(timeoutS * 1000);
		return fetch(url, {
			headers,
			redirect: "manual",
			signal: AbortSignal.any([this.signal, timeout]),
		});
	}

	async #manifest(): Promise<Manifest | null> {
		const version = this.render().version;
		const query = new URLSearchParams({ build: version, os: this.targetOs });
		const response = await this.#fetch(
			`${this.#base()}/v1/scene-cache/manifest?${query}`,
			this.#headers(),
			sceneCacheTiming.connectTimeoutS + sceneCacheTiming.readTimeoutS,
		);
		if (response.status === 404) {
			await response.body?.cancel();
			return null;
		}
		if (response.status !== 200) {
			await response.body?.cancel();
			throw new Error(`the manifest answered ${response.status}`);
		}
		return checked(await response.json(), version, this.targetOs);
	}

	/** One connection's worth of the file. False when the server no longer has it. */
	async #fetchPart(part: string, manifest: Manifest): Promise<boolean> {
		let have = sizeOf(part);
		if (have > manifest.bytes) {
			fs.rmSync(part, { force: true });
			have = 0;
		}
		if (have === manifest.bytes) return true;
		const headers = this.#headers();
		if (have) headers.Range = `bytes=${have}-`;
		// Only the start of the answer is timed here: a whole file takes as long as it takes.
		const connect = new AbortController();
		const connecting = setTimeout(
			() => connect.abort(),
			sceneCacheTiming.connectTimeoutS * 1000,
		);
		connecting.unref();
		const reading = new AbortController();
		let response: Response;
		try {
			response = await fetch(`${this.#base()}${manifest.path}`, {
				headers,
				redirect: "manual",
				signal: AbortSignal.any([this.signal, connect.signal, reading.signal]),
			});
		} finally {
			clearTimeout(connecting);
		}
		if (response.status === 404) {
			await response.body?.cancel();
			return false;
		}
		if (response.status === 416) {
			await response.body?.cancel();
			fs.rmSync(part, { force: true });
			throw new Error("the server refused the resume offset; starting over");
		}
		if (response.status !== 200 && response.status !== 206) {
			await response.body?.cancel();
			throw new Error(`the snapshot answered ${response.status}`);
		}
		const resumed = response.status === 206;
		if (
			resumed &&
			!(response.headers.get("content-range") ?? "").startsWith(
				`bytes ${have}-`,
			)
		) {
			await response.body?.cancel();
			fs.rmSync(part, { force: true });
			throw new Error("the server resumed from another offset; starting over");
		}
		let written = resumed ? have : 0;
		let tooLong = false;
		const fd = fs.openSync(part, resumed ? "a" : "w");
		let stalled: NodeJS.Timeout | null = null;
		const stall = () => {
			if (stalled) clearTimeout(stalled);
			stalled = setTimeout(
				() => reading.abort(),
				sceneCacheTiming.readTimeoutS * 1000,
			);
			stalled.unref();
		};
		try {
			if (response.body) {
				stall();
				for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
					stall();
					written += chunk.length;
					if (written > manifest.bytes) {
						tooLong = true;
						break;
					}
					fs.writeSync(fd, chunk);
					this.lock.touch();
				}
			}
			fs.fsyncSync(fd);
		} catch (error) {
			if (this.signal.aborted) throw new Stopped();
			throw error;
		} finally {
			if (stalled) clearTimeout(stalled);
			fs.closeSync(fd);
		}
		if (tooLong) {
			fs.rmSync(part, { force: true });
			throw new Error(
				`the server sent more than the ${manifest.bytes} bytes it advertised`,
			);
		}
		return true;
	}

	/** The whole file in `part`, resuming across dropped connections. False when the server no longer has it. */
	async #download(part: string, manifest: Manifest): Promise<boolean> {
		for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
			try {
				if (!(await this.#fetchPart(part, manifest))) return false;
				if (sizeOf(part) === manifest.bytes) return true;
			} catch (error) {
				if (error instanceof Stopped || this.signal.aborted)
					throw new Stopped();
				if (attempt === ATTEMPTS - 1) throw error;
				console.info(
					`camoufox-pro: scene cache download interrupted (${error}); resuming`,
				);
			}
			await abortableSleep(sceneCacheTiming.retryS, this.signal);
		}
		return false;
	}

	async #install(part: string, manifest: Manifest): Promise<string> {
		const digest = createHash("sha256");
		const fd = fs.openSync(part, "r");
		const magic = Buffer.alloc(SQLITE_MAGIC.length);
		let isSqlite = false;
		try {
			const buffer = Buffer.alloc(CHUNK);
			let first = true;
			while (true) {
				const count = fs.readSync(fd, buffer, 0, CHUNK, null);
				if (count === 0) break;
				if (first) {
					buffer.copy(magic, 0, 0, Math.min(count, magic.length));
					isSqlite = magic.equals(SQLITE_MAGIC);
					first = false;
				}
				digest.update(buffer.subarray(0, count));
				if (this.signal.aborted) throw new Stopped();
				this.lock.touch();
				// Let the browser launch that started this keep moving through a large file.
				await new Promise((resolve) => setImmediate(resolve));
			}
		} finally {
			fs.closeSync(fd);
		}
		if (digest.digest("hex") !== manifest.sha256 || !isSqlite) {
			// Not resumable: whichever bytes are bad are already in the part file.
			fs.rmSync(part, { force: true });
			throw new Error(
				`the downloaded scene cache does not match ${manifest.sha256}`,
			);
		}
		const target = path.join(this.folder, `${manifest.sha256}.sqlite`);
		fs.renameSync(part, target);
		this.#record(manifest.sha256, manifest.bytes);
		return target;
	}

	#record(sha256: string | null, bytes: number | null): void {
		writeState(this.folder, {
			sha256,
			bytes,
			checked_at: Math.floor(Date.now() / 1000),
		});
	}

	#collect(keep: string | null, part: string | null): void {
		for (const name of fs.readdirSync(this.folder)) {
			const file = path.join(this.folder, name);
			if (
				(name.endsWith(".sqlite") && file !== keep) ||
				(name.endsWith(".part") && file !== part)
			) {
				// A browser still running from the old file holds it on Windows.
				removeQuietly(file);
			}
		}
	}

	async run(): Promise<void> {
		const current = verifiedFile(this.folder);
		const state = readState(this.folder);
		const recent =
			Date.now() / 1000 - Number(state.checked_at ?? 0) < updateSeconds();
		// Recently asked and nothing published is an answer too; a state naming a lost file is not.
		const answered =
			current !== null ||
			((state.sha256 === null || state.sha256 === undefined) &&
				state.checked_at !== undefined);
		if (recent && answered) return;
		if (!this.lock.acquire()) return;
		try {
			const manifest = await this.#manifest();
			if (manifest === null) {
				this.#record(
					current ? (state.sha256 ?? null) : null,
					current ? (state.bytes ?? null) : null,
				);
				this.#collect(current, null);
				return;
			}
			if (
				current !== null &&
				path.basename(current) === `${manifest.sha256}.sqlite`
			) {
				this.#record(manifest.sha256, manifest.bytes);
				return;
			}
			const part = path.join(this.folder, `${manifest.sha256}.sqlite.part`);
			const free = sceneCacheTiming.freeBytes(this.folder);
			if (free < (manifest.bytes - sizeOf(part)) * FREE_SPACE_FACTOR) {
				console.warn(
					`camoufox-pro: ${free} bytes free is too little for the ${manifest.bytes}-byte scene cache; drawing on the farm`,
				);
				return;
			}
			this.#collect(current, part);
			if (await this.#download(part, manifest)) {
				const installed = await this.#install(part, manifest);
				this.#collect(installed, null);
				console.info(
					`camoufox-pro: scene cache ${manifest.sha256} is in place for the next launch`,
				);
			}
		} finally {
			this.lock.release();
		}
	}
}

/** Bring `folder` up to date if it is due. Never rejects: a failure is logged and tried at the next launch. */
export async function update(
	folder: string,
	targetOs: string,
	render: () => Render,
	signal: AbortSignal = new AbortController().signal,
): Promise<void> {
	try {
		await new Updater(folder, targetOs, render, signal).run();
	} catch (error) {
		if (error instanceof Stopped || signal.aborted) return;
		console.warn(`camoufox-pro: scene cache not updated: ${error}`);
	}
}

export type AttachableLease = {
	sceneCacheDir: string | null;
	targetOs: string;
	onEnd: (() => Promise<void> | void)[];
	grants: Record<string, any>;
};

/**
 * Get the cache ready for a launch with this lease: the file to hand the browser,
 * if there is one, and a background update ended with the lease.
 */
export function attach(lease: AttachableLease): string | null {
	const folder = lease.sceneCacheDir as string;
	let current: string | null;
	try {
		fs.mkdirSync(folder, { recursive: true, mode: 0o700 });
		clearOthers(folder);
		current = verifiedFile(folder);
	} catch (error) {
		console.warn(`camoufox-pro: no scene cache for this launch: ${error}`);
		return null;
	}
	const stop = new AbortController();
	lease.onEnd.push(() => stop.abort());
	void update(
		folder,
		lease.targetOs,
		() => lease.grants.gpu.render,
		stop.signal,
	);
	return current;
}
