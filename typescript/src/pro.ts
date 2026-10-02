/**
 * Camoufox Pro: signing in, and the session lease a Pro build starts with.
 *
 * TypeScript twin of pythonlib/camoufox/pro.py. A Pro build ships
 * pro-build.json beside its executable. The browser verifies a signed lease
 * from the file named by CAMOU_LEASE_FILE and will not start without one. This
 * module is the client half: it mints the lease from the Camoufox Pro API,
 * writes the file, renews it while the browser runs, and releases it when the
 * browser closes. A build without pro-build.json is launched as before, with no
 * request made. See docs/pro.md.
 */
import { execFileSync } from "node:child_process";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { LIBRARY_VERSION } from "./__version__.js";
import {
	AccountSuspended,
	AllowanceExhausted,
	BuildNotAllowlisted,
	CapabilityMismatch,
	GpuUnavailable,
	InvalidRequest,
	LeaseLimitReached,
	LeaseRefused,
	NotSignedIn,
	ProClockSkew,
	ProError,
	type ProErrorFields,
	ProfileMismatch,
	ProUnavailable,
	RateLimited,
	StatePoolSealed,
	SubscriptionRequired,
} from "./exceptions.js";
import { OS_NAME, userCacheDir, userConfigDir } from "./paths.js";

export const API_ENV = "CAMOUFOX_PRO_API";
export const KEY_ENV = "CAMOUFOX_PRO_KEY";
export const LEASE_FILE_ENV = "CAMOU_LEASE_FILE";
/** Where a Pro build reads its remote-rendering lease from, when the lease grants one. */
export const RENDER_FILE_ENV = "RENDERFARM_FIREFOX_CONFIG";
export const DEFAULT_API = "https://api.camoufox.com";
export const KEY_PREFIX = "cfp_live_";

/**
 * SPEC-LEASE § 10: mint retries, the heartbeat backoff table, and the skew
 * thresholds (the last is the lease grace, past which a fresh token already
 * looks expired to the browser). Seconds, and mutable for tests.
 */
export const proTiming = {
	mintRetryS: [1, 2, 4],
	heartbeatBackoffS: [5, 10, 20, 40, 60],
	heartbeatTimeoutS: 10,
	releaseTimeoutS: 5,
	unreachableWarnS: 180,
	skewWarnS: 60,
	skewRefuseS: 180,
	staleLeaseS: 24 * 3600,
	/**
	 * A profile another session holds: how long a mint waits for it (its commit
	 * releases the lease), polling at most this often. CAMOUFOX_PRO_PROFILE_WAIT
	 * overrides the wait, in seconds; 0 refuses at once.
	 */
	profileWaitS: 120,
	profilePollS: 5,
};
export const PROFILE_WAIT_ENV = "CAMOUFOX_PRO_PROFILE_WAIT";

export function profileWaitS(): number {
	const raw = (process.env[PROFILE_WAIT_ENV] ?? "").trim();
	if (!raw) return proTiming.profileWaitS;
	const value = Number(raw);
	if (!Number.isFinite(value)) {
		throw new Error(
			`${PROFILE_WAIT_ENV} must be a number of seconds, not ${JSON.stringify(raw)}`,
		);
	}
	return Math.max(0, value);
}

const REFUSAL = /camoufox-pro: lease refused \(([^)\n]*)\)/;
const NOT_SIGNED_IN =
	"Camoufox Pro needs a key: run `camoufox login`, or set CAMOUFOX_PRO_KEY.";

export const HOST_OS = (
	{ lin: "linux", mac: "macos", win: "windows" } as const
)[OS_NAME];
export const TARGET_OS: Record<string, string> = {
	win: "windows",
	mac: "macos",
	lin: "linux",
};

const BY_CODE: Record<
	string,
	new (
		message: string,
		fields: ProErrorFields,
	) => ProError
> = {
	invalid_request: InvalidRequest,
	invalid_api_key: NotSignedIn,
	subscription_required: SubscriptionRequired,
	allowance_exhausted: AllowanceExhausted,
	account_suspended: AccountSuspended,
	build_not_allowlisted: BuildNotAllowlisted,
	lease_limit_reached: LeaseLimitReached,
	capability_mismatch: CapabilityMismatch,
	rate_limited: RateLimited,
	profile_mismatch: ProfileMismatch,
	state_pool_sealed: StatePoolSealed,
	gpu_unavailable: GpuUnavailable,
};

export function apiBase(): string {
	return ((process.env[API_ENV] ?? "").trim() || DEFAULT_API).replace(
		/\/+$/,
		"",
	);
}

export function clientName(): string {
	return `camoufox-js/${LIBRARY_VERSION}`;
}

/** Python's getpass.getuser(): the login variables first, then the account. */
function getUser(): string {
	for (const name of ["LOGNAME", "USER", "LNAME", "USERNAME"]) {
		if (process.env[name]) return process.env[name] as string;
	}
	return os.userInfo().username;
}

// ── files only this user may read ───────────────────────────────────────────

/** Windows has no mode bits; strip inherited ACEs and grant the owner alone. */
function ownerOnly(file: string): void {
	execFileSync("icacls", [
		file,
		"/inheritance:r",
		"/grant:r",
		`${getUser()}:F`,
	]);
}

function privateDir(dir: string): void {
	fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
	if (OS_NAME === "win") ownerOnly(dir);
	else fs.chmodSync(dir, 0o700);
}

/**
 * Replace `file` atomically with a file only this user can read: a temporary
 * file beside it, fsynced, then renamed over it, so a reader never sees half a
 * file (SPEC-LEASE § 8.3).
 */
export function writePrivate(
	file: string,
	data: string,
	{ replace = true }: { replace?: boolean } = {},
): boolean {
	privateDir(path.dirname(file));
	const tmp = path.join(
		path.dirname(file),
		`${path.basename(file)}.tmp-${randomBytes(6).toString("hex")}`,
	);
	try {
		const fd = fs.openSync(tmp, "wx", 0o600);
		try {
			fs.writeSync(fd, data);
			fs.fsyncSync(fd);
		} finally {
			fs.closeSync(fd);
		}
		if (OS_NAME === "win") ownerOnly(tmp);
		if (replace) {
			fs.renameSync(tmp, file);
			return true;
		}
		// A link fails rather than replace a file another process wrote first.
		try {
			fs.linkSync(tmp, file);
			return true;
		} catch (error: any) {
			if (error?.code === "EEXIST") return false;
			throw error;
		} finally {
			fs.rmSync(tmp, { force: true });
		}
	} catch (error) {
		fs.rmSync(tmp, { force: true });
		throw error;
	}
}

// ── the key ─────────────────────────────────────────────────────────────────

export function credentialPath(): string {
	return path.join(userConfigDir("camoufox"), "pro-credentials.json");
}

function storedKey(): string | null {
	const file = credentialPath();
	let mode: number;
	try {
		mode = fs.statSync(file).mode;
	} catch (error: any) {
		if (error?.code === "ENOENT") return null;
		throw error;
	}
	if (OS_NAME !== "win" && mode & 0o077) {
		throw new Error(
			`${file} can be read by other users, so it is not used. Run: chmod 600 ${file}`,
		);
	}
	return JSON.parse(fs.readFileSync(file, "utf-8")).api_key;
}

/** The key to use: the `pro_key` option, else CAMOUFOX_PRO_KEY, else `camoufox login`'s. */
export function resolveKey(explicit?: string | null): string {
	const key = explicit || (process.env[KEY_ENV] ?? "").trim() || storedKey();
	if (!key) throw new NotSignedIn(NOT_SIGNED_IN);
	if (!key.startsWith(KEY_PREFIX)) {
		throw new NotSignedIn(
			`A Camoufox Pro key starts with ${KEY_PREFIX}. ${NOT_SIGNED_IN}`,
		);
	}
	return key;
}

export function masked(key: string): string {
	return `${KEY_PREFIX}...${key.slice(-4)}`;
}

export function storeKey(key: string): string {
	const file = credentialPath();
	writePrivate(file, JSON.stringify({ api_key: key }));
	return file;
}

/** Delete the stored key. Returns its masked form, or null when there was none. */
export function forgetKey(): string | null {
	const file = credentialPath();
	if (!fs.existsSync(file)) return null;
	const key = JSON.parse(fs.readFileSync(file, "utf-8")).api_key;
	fs.rmSync(file);
	return masked(key);
}

// ── the API ─────────────────────────────────────────────────────────────────

function apiError(
	status: number,
	body: Record<string, any>,
	retryAfter: string | null,
): ProError {
	const code: string | undefined = body.error;
	let message = String(body.message || `HTTP ${status}`);
	if (code === "invalid_api_key") message = `${message} ${NOT_SIGNED_IN}`;
	if (code === "lease_limit_reached") {
		const holders: Record<string, any>[] = body.details?.holders ?? [];
		const held = holders
			.map(
				(holder) =>
					`${holder.host_label || (holder.host_fingerprint ?? "").slice(0, 12)} until ${holder.expires_at}`,
			)
			.join(", ");
		if (held) message = `${message} Held by: ${held}.`;
	}
	const Kind =
		(code && BY_CODE[code]) ||
		(status === 429 ? RateLimited : status >= 500 ? ProUnavailable : ProError);
	const wait = body.retry_after ?? retryAfter;
	return new Kind(message, {
		code,
		status,
		resolution_url: body.resolution_url,
		retry_after: wait != null ? Number.parseInt(String(wait), 10) : null,
		details: body.details,
	});
}

/** Call the Camoufox Pro API. Every failure is a ProError; ProUnavailable is the retryable kind. */
export async function call(
	method: "GET" | "POST" | "PUT",
	route: string,
	payload: Record<string, any> | null,
	{ key, timeoutS = 30 }: { key?: string; timeoutS?: number } = {},
): Promise<Record<string, any>> {
	const headers: Record<string, string> = { "User-Agent": clientName() };
	if (payload) headers["Content-Type"] = "application/json";
	if (key) headers.Authorization = `Bearer ${key}`;
	let response: Response;
	let text: string;
	try {
		response = await fetch(`${apiBase()}${route}`, {
			method,
			headers,
			body: payload ? JSON.stringify(payload) : undefined,
			signal: AbortSignal.timeout(timeoutS * 1000),
		});
		text = await response.text();
	} catch (error) {
		throw new ProUnavailable(
			`The Camoufox Pro API at ${apiBase()} could not be reached: ${(error as Error).message}`,
		);
	}
	let body: any = {};
	try {
		body = JSON.parse(text);
	} catch {}
	if (response.status >= 400) {
		throw apiError(
			response.status,
			body && typeof body === "object" && !Array.isArray(body) ? body : {},
			response.headers.get("Retry-After"),
		);
	}
	return body;
}

export function post(
	route: string,
	payload: Record<string, any>,
	options: { key?: string; timeoutS?: number } = {},
): Promise<Record<string, any>> {
	return call("POST", route, payload, options);
}

export function transient(error: unknown): boolean {
	return error instanceof ProUnavailable || error instanceof RateLimited;
}

export const sleepS = (seconds: number) =>
	new Promise((resolve) => setTimeout(resolve, seconds * 1000));

// ── sign-in: RFC 8628's device flow ─────────────────────────────────────────

export interface SignedIn {
	account: Record<string, any>;
	user: Record<string, any>;
	path: string;
}

/**
 * Sign in on this machine: show a code to approve in a browser, then create a
 * Camoufox Pro key for this machine and store it.
 */
export async function login(
	echo: (line: string) => void = console.log,
	sleep: (seconds: number) => Promise<unknown> = sleepS,
): Promise<SignedIn> {
	const start = await post("/api/v1/device/start", { client: clientName() });
	echo(`Open ${start.verification_uri_complete}`);
	echo(`and confirm the code ${start.user_code}.`);
	let interval = Number(start.interval);
	const deadline = performance.now() + Number(start.expires_in) * 1000;
	let granted: Record<string, any>;
	for (;;) {
		await sleep(interval);
		if (performance.now() > deadline) {
			throw new NotSignedIn(
				"The sign-in code expired before it was approved. Run `camoufox login` again.",
			);
		}
		try {
			granted = await post("/api/v1/device/token", {
				device_code: start.device_code,
			});
			break;
		} catch (error) {
			if (!(error instanceof ProError)) throw error;
			if (
				error.code === "authorization_pending" ||
				error instanceof ProUnavailable
			)
				continue;
			if (error.code === "slow_down") {
				interval += 5;
				continue;
			}
			if (
				["expired_token", "access_denied", "invalid_grant"].includes(
					error.code ?? "",
				)
			) {
				throw new NotSignedIn(
					`Sign-in did not complete: ${error.detail} Run \`camoufox login\` again.`,
				);
			}
			throw error;
		}
	}
	// The device flow grants a management token, which cannot run a browser; a
	// runtime key is what a lease is minted with, so one is created for this machine.
	const created = await post(
		"/api/v1/keys",
		{ name: `camoufox on ${os.hostname()}`.slice(0, 64) },
		{ key: granted.access_token },
	);
	return {
		account: granted.account,
		user: granted.user,
		path: storeKey(created.api_key),
	};
}

// ── the lease ───────────────────────────────────────────────────────────────

/** The sections a lease answer carries, each null when the lease does not grant it. */
export const SECTIONS = ["profile", "egress", "gpu", "captcha"] as const;

/** What a Pro build's pro-build.json declares. */
export interface ProBuild {
	build_hash: string;
	version: string;
	target: string;
}

/** The build's pro-build.json, or null when the build is not a Pro build. */
export function readBuild(file: string): ProBuild | null {
	if (!fs.existsSync(file)) return null;
	const declared = JSON.parse(fs.readFileSync(file, "utf-8"));
	if (!/^sha256:[0-9a-f]{64}$/.test(String(declared.build_hash))) {
		throw new Error(
			`${file} does not declare a build_hash of the form sha256:<64 hex>.`,
		);
	}
	return {
		build_hash: declared.build_hash,
		version: String(declared.version),
		target: String(declared.target),
	};
}

/** SPEC-LEASE § 8.1. */
export function leaseDir(): string {
	if (OS_NAME === "win") {
		return path.join(
			process.env.LOCALAPPDATA as string,
			"camoufox-pro",
			"leases",
		);
	}
	const runtime = process.env.XDG_RUNTIME_DIR;
	return runtime
		? path.join(runtime, "camoufox-pro")
		: path.join(os.homedir(), ".cache", "camoufox-pro", "leases");
}

/** SPEC-LEASE § 10.6. Telemetry naming this install; the API never refuses on it. */
export function hostFingerprintOf(
	hostId: string,
	osName: string,
	username: string,
): string {
	return createHash("sha256")
		.update(`cfp-host-v1\0${hostId}\0${osName}\0${username}`, "utf-8")
		.digest("hex");
}

export function hostFingerprint(): string {
	const file = path.join(userCacheDir("camoufox"), "pro", "host.id");
	let hostId: string;
	if (fs.existsSync(file)) {
		hostId = fs.readFileSync(file, "utf-8").trim();
	} else {
		hostId = randomBytes(16).toString("hex");
		writePrivate(file, hostId);
	}
	return hostFingerprintOf(hostId, HOST_OS, getUser());
}

function uuidOf(bytes: Buffer): string {
	const hex = bytes.toString("hex");
	return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** The binding fields, read back from the token (SPEC-LEASE § 5.2) so the file always matches it. */
export function tokenFields(token: string): Record<string, any> {
	const raw = Buffer.from(token.slice("cfl1_".length), "base64url");
	const profile = raw.subarray(26, 42);
	return {
		lease_id: uuidOf(raw.subarray(2, 18)),
		account_id: Number(raw.readBigUInt64BE(18)),
		profile_id: profile.equals(Buffer.alloc(16)) ? null : uuidOf(profile),
		build_hash: raw.subarray(42, 74).toString("hex"),
		issued_at: Number(raw.readBigUInt64BE(74)),
		expires_at: Number(raw.readBigUInt64BE(82)),
	};
}

/** SPEC-LEASE § 8.2. Byte for byte what the Python launcher writes for the same inputs. */
export function leaseFileBytes(
	token: string,
	targetOs: string,
	fidelity: string,
	writtenAt: number,
): string {
	return JSON.stringify({
		v: 1,
		...tokenFields(token),
		token,
		os: targetOs,
		fidelity,
		paths: {
			identity_bundle: null,
			font_metrics: null,
			scene_cache_dir: null,
			gpu_bundle: null,
		},
		written_at: writtenAt,
	});
}

let skewWarned = false;

function checkSkew(serverTime: string): number {
	const skew = (Date.parse(serverTime) - Date.now()) / 1000;
	if (Math.abs(skew) > proTiming.skewWarnS && !skewWarned) {
		skewWarned = true;
		console.warn(
			`camoufox-pro: this machine's clock is ${skew >= 0 ? "+" : ""}${Math.round(skew)} s off the API's; ` +
				"lease expiry is evaluated on this machine's clock; fix NTP",
		);
	}
	return skew;
}

/** Remove lease files a process that died without releasing left behind (SPEC-LEASE § 8.1). */
export function cleanStaleLeases(): void {
	const dir = leaseDir();
	if (!fs.existsSync(dir)) return;
	const cutoff = Date.now() - proTiming.staleLeaseS * 1000;
	for (const name of fs.readdirSync(dir)) {
		const file = path.join(dir, name);
		try {
			if (fs.statSync(file).mtimeMs < cutoff) fs.rmSync(file);
		} catch (error: any) {
			if (error?.code !== "ENOENT") throw error;
		}
	}
}

export const LIVE = new Map<string, Lease>();

/**
 * What a mint asks for beyond the build and OS, each field left out unless
 * set: `profile` names the profile to launch (and `warm_plan` the plan a new
 * one is created with), `egress: false` keeps managed
 * egress off (the caller's own proxy is never replaced) and an object states
 * the egress wanted, and `gpu: false` renders on this machine.
 */
export interface LeaseRequest {
	profile?: string;
	warm_plan?: "none" | "standard" | "continuous";
	egress?: false | { class?: string; country?: string; sticky?: boolean };
	gpu?: false;
}

/** Ends a lease's browser session: set for a profile whose state syncs when it closes. */
export type SessionEnd = (lease: Lease) => Promise<void>;

/**
 * One browser's lease: minted by `Lease.acquire()`, renewed on an unref'd
 * timer, and released by `release()`, which is idempotent.
 */
export class Lease {
	path = "";
	leaseId = "";
	/** The lease's sections (`SECTIONS`), each null when it does not grant it. */
	grants: Record<string, any> = {};
	/** The account the lease belongs to. */
	accountId = 0;
	/** The remote-rendering file, while the lease grants remote rendering. */
	renderPath: string | null = null;
	/** Run in place of a plain release when the browser closes. */
	onClose: SessionEnd | null = null;
	/** Run when the process exits with the browser never closed, before the release. */
	onAbort: ((lease: Lease) => void) | null = null;
	/** Sent with each heartbeat once set: whether the profile has state not yet committed. */
	stateDirty: boolean | null = null;
	/** Directories and files that go with the lease: deleted when it ends. */
	scratch: string[] = [];
	claimed = false;
	readonly #key: string;
	readonly #host: string;
	#seq = 0;
	#heartbeatS = 60;
	#fidelity = "native";
	#timer: NodeJS.Timeout | null = null;
	#released: Promise<boolean> | null = null;
	#closed: Promise<void> | null = null;
	#renewing = true;
	#failingSince: number | null = null;
	#failures = 0;

	private constructor(
		readonly build: ProBuild,
		readonly targetOs: string,
		key: string,
		readonly request: LeaseRequest,
	) {
		this.#key = key;
		this.#host = hostFingerprint();
	}

	static async acquire(
		build: ProBuild,
		targetOs: string,
		key: string,
		request: LeaseRequest = {},
	): Promise<Lease> {
		const lease = new Lease(build, targetOs, key, request);
		await lease.#mint();
		LIVE.set(lease.path, lease);
		watchExit();
		lease.#schedule(lease.#nextBeat());
		return lease;
	}

	/** Call the API with this lease's key. */
	api(
		method: "GET" | "POST" | "PUT",
		route: string,
		payload: Record<string, any> | null = null,
	): Promise<Record<string, any>> {
		return call(method, route, payload, { key: this.#key });
	}

	/**
	 * An API call whose body or answer is not JSON (a profile's served state),
	 * with this lease's key. Refusals throw as `call` throws them.
	 */
	async send(
		method: "GET" | "PUT",
		route: string,
		params: Record<string, string>,
		{
			body,
			contentType,
			timeoutS = 600,
		}: {
			body?: Uint8Array<ArrayBuffer>;
			contentType?: string;
			timeoutS?: number;
		} = {},
	): Promise<Response> {
		const headers: Record<string, string> = {
			"User-Agent": clientName(),
			Authorization: `Bearer ${this.#key}`,
		};
		if (contentType) headers["Content-Type"] = contentType;
		let response: Response;
		try {
			response = await fetch(
				`${apiBase()}${route}?${new URLSearchParams(params)}`,
				{ method, headers, body, signal: AbortSignal.timeout(timeoutS * 1000) },
			);
		} catch (error) {
			throw new ProUnavailable(
				`The Camoufox Pro API at ${apiBase()} could not be reached: ${(error as Error).message}`,
			);
		}
		if (response.status >= 400) {
			let answer: any = {};
			try {
				answer = JSON.parse(await response.text());
			} catch {}
			throw apiError(
				response.status,
				answer && typeof answer === "object" && !Array.isArray(answer)
					? answer
					: {},
				response.headers.get("Retry-After"),
			);
		}
		return response;
	}

	async #mint(): Promise<void> {
		const body = {
			...this.request,
			build_hash: this.build.build_hash,
			os: this.targetOs,
			client: clientName(),
			host: {
				fingerprint: this.#host,
				label: os.hostname().slice(0, 64),
				capability: { host: { os: HOST_OS } },
			},
			// One key across the retries of this mint, so a retry after a lost
			// response gets the same lease back rather than a second one.
			idempotency_key: randomUUID().replaceAll("-", ""),
		};
		let lease: Record<string, any> | undefined;
		const retries = [...proTiming.mintRetryS];
		const patience = profileWaitS();
		let waited = 0;
		for (;;) {
			try {
				lease = await post("/api/v1/leases", body, { key: this.#key });
				break;
			} catch (error) {
				if (error instanceof ProError && error.code === "lease_conflict") {
					// Another session holds the profile, most often the last one
					// still committing its state: its commit releases the lease.
					const pause = Math.min(
						error.retry_after || proTiming.profilePollS,
						proTiming.profilePollS,
					);
					if (waited + pause > patience) throw error;
					await sleepS(pause);
					waited += pause;
					continue;
				}
				const delay = retries.shift();
				if (delay === undefined || !transient(error)) throw error;
				await sleepS((error as ProError).retry_after || delay);
			}
		}
		const minted = lease as Record<string, any>;
		if (this.leaseId && this.grants.egress) {
			console.warn(
				"camoufox-pro: the new lease carries a new egress credential; " +
					"the running browser keeps the old one, so restart it to use managed egress again",
			);
		}
		this.leaseId = minted.lease_id;
		this.grants = Object.fromEntries(
			SECTIONS.map((name) => [name, minted[name] ?? null]),
		);
		this.accountId = Number(
			minted.account_id ?? tokenFields(minted.token).account_id,
		);
		this.#seq = 0;
		this.#heartbeatS = Number(minted.limits.heartbeat_s);
		this.#fidelity = minted.host.fidelity;
		this.path ||= path.join(
			leaseDir(),
			`${tokenFields(minted.token).lease_id}.json`,
		);
		const skew = checkSkew(minted.server_time);
		if (Math.abs(skew) > proTiming.skewRefuseS) {
			await this.#postRelease("error");
			throw new ProClockSkew(skew);
		}
		this.#write(minted.token);
		this.#writeRender(this.grants.gpu);
	}

	/** Write the remote-rendering file verbatim, as the browser reads it, when `gpu` carries one. */
	#writeRender(gpu: Record<string, any> | null | undefined): void {
		if (this.#released || !gpu?.render) return;
		this.renderPath ??= this.path.replace(/\.json$/, ".render.json");
		writePrivate(this.renderPath, JSON.stringify(gpu.render));
	}

	/** Delete every file and directory that goes with this lease. */
	#clean(): void {
		for (const file of [this.path, this.renderPath, ...this.scratch]) {
			if (file) fs.rmSync(file, { recursive: true, force: true });
		}
	}

	/** At process exit, when nothing else can run. */
	cleanAtExit(): void {
		this.#clean();
	}

	get fidelity(): string {
		return this.#fidelity;
	}

	#write(token: string): void {
		if (this.#released) return;
		writePrivate(
			this.path,
			leaseFileBytes(
				token,
				this.targetOs,
				this.#fidelity,
				Math.floor(Date.now() / 1000),
			),
		);
	}

	#nextBeat(): number {
		// Jittered by a twelfth, so a fleet started together does not beat together.
		return this.#heartbeatS * (11 / 12 + Math.random() / 6);
	}

	#schedule(seconds: number): void {
		if (this.#released || !this.#renewing) return;
		this.#timer = setTimeout(() => void this.#beat(), seconds * 1000);
		this.#timer.unref();
	}

	/** SPEC-LEASE § 10.2 and § 10.3. */
	async #beat(): Promise<void> {
		let answer: Record<string, any>;
		try {
			answer = await post(
				`/api/v1/leases/${this.leaseId}/heartbeat`,
				{
					seq: this.#seq + 1,
					host: { fingerprint: this.#host },
					...(this.stateDirty === null ? {} : { state_dirty: this.stateDirty }),
				},
				{ key: this.#key, timeoutS: proTiming.heartbeatTimeoutS },
			);
		} catch (error) {
			if (this.#released || !this.#renewing) return;
			if (!(error instanceof ProError)) throw error;
			// A 402 cannot happen on a heartbeat (allowances are checked at
			// mint), so it is treated as the server failing.
			if (transient(error) || error.status === 402) {
				const now = performance.now();
				if (this.#failingSince === null) {
					this.#failingSince = now;
				} else if (
					now - this.#failingSince >=
					proTiming.unreachableWarnS * 1000
				) {
					console.warn(
						`camoufox-pro: the API has been unreachable for ${Math.round((now - this.#failingSince) / 1000)} s; ` +
							"the browser keeps running until its lease expires",
					);
					this.#failingSince = Number.POSITIVE_INFINITY; // warned; not again this outage
				}
				const table = proTiming.heartbeatBackoffS;
				this.#schedule(
					error.retry_after ||
						table[Math.min(this.#failures, table.length - 1)],
				);
				this.#failures += 1;
				return;
			}
			if (error.code === "lease_not_found") {
				// Swept after an outage, or released elsewhere: one new lease into the same file.
				try {
					await this.#mint();
				} catch (again) {
					console.error(
						`camoufox-pro: lease ${this.leaseId} is gone and a new one was refused: ${(again as Error).message}`,
					);
					return;
				}
				this.#failingSince = null;
				this.#failures = 0;
				this.#schedule(this.#nextBeat());
				return;
			}
			console.error(
				`camoufox-pro: heartbeat refused, the lease will not be renewed: ${error.message}`,
			);
			return;
		}
		if (this.#released) return;
		this.#seq += 1;
		checkSkew(answer.server_time);
		this.#write(answer.token);
		this.#writeRender(answer.gpu);
		if (answer.gpu === null && this.grants.gpu) {
			for (const notice of answer.notices ?? []) {
				if (notice.code === "gpu_lost") {
					console.warn(
						"camoufox-pro: remote rendering for this browser was lost; " +
							"restart it with a new lease to render remotely again",
					);
				}
			}
		}
		this.#failingSince = null;
		this.#failures = 0;
		this.#schedule(this.#nextBeat());
	}

	async #postRelease(reason: string): Promise<boolean> {
		for (const attempt of [0, 1]) {
			try {
				await post(
					`/api/v1/leases/${this.leaseId}/release`,
					{ reason },
					{ key: this.#key, timeoutS: proTiming.releaseTimeoutS },
				);
				return true;
			} catch (error) {
				if (attempt || !transient(error)) {
					console.warn(
						`camoufox-pro: releasing lease ${this.leaseId} failed: ${(error as Error).message}`,
					);
					return false;
				}
			}
		}
		return false;
	}

	/**
	 * Release the lease and delete its file. Only the first call does anything;
	 * every call resolves to whether that release reached the API.
	 */
	release(reason = "clean_exit"): Promise<boolean> {
		return this.#end(() => this.#postRelease(reason));
	}

	/** Stop renewing, for a lease about to end with a commit that releases it. */
	stopRenewing(): void {
		this.#renewing = false;
		if (this.#timer) clearTimeout(this.#timer);
	}

	/** Stop renewing and delete the files of a lease the API already released (a commit with release). */
	forget(): Promise<boolean> {
		return this.#end(async () => true);
	}

	#end(released: () => Promise<boolean>): Promise<boolean> {
		this.#released ??= (async () => {
			if (this.#timer) clearTimeout(this.#timer);
			LIVE.delete(this.path);
			const done = await released();
			this.#clean();
			return done;
		})();
		return this.#released;
	}

	/**
	 * End the browser session: `onClose` when one is set (a profile's state
	 * sync, which releases the lease itself), else a release. Only the first
	 * call does anything.
	 */
	close(): Promise<void> {
		this.#closed ??= (async () => {
			if (this.onClose) await this.onClose(this);
			else await this.release();
		})();
		return this.#closed;
	}
}

let watchingExit = false;

/**
 * A process that ends without closing its browsers still gives their leases
 * back: `beforeExit` has a loop left to release on, and `exit` can only delete
 * the files.
 */
function watchExit(): void {
	if (watchingExit) return;
	watchingExit = true;
	process.on("beforeExit", () => {
		for (const lease of LIVE.values()) {
			abort(lease);
			void lease.release("driver_shutdown");
		}
	});
	process.on("exit", () => {
		for (const lease of LIVE.values()) {
			abort(lease);
			lease.cleanAtExit();
		}
	});
}

/** A lease whose browser never closed: keep what the next launch of its profile needs. */
function abort(lease: Lease): void {
	const hook = lease.onAbort;
	lease.onAbort = null;
	try {
		hook?.(lease);
	} catch (error) {
		console.warn(
			`camoufox-pro: keeping lease ${lease.leaseId}'s state for the next launch failed: ${(error as Error).message}`,
		);
	}
}

/** Release every lease this process holds, as process exit does. */
export async function releaseAll(): Promise<void> {
	await Promise.all(
		[...LIVE.values()].map((lease) => {
			abort(lease);
			return lease.release("driver_shutdown");
		}),
	);
}

/** Mint a lease for a Pro build about to launch an identity of `targetOs` ('win', 'mac', 'lin'). */
export async function acquire(
	build: ProBuild,
	targetOs: string,
	key?: string | null,
	request: LeaseRequest = {},
): Promise<Lease> {
	const resolved = resolveKey(key);
	cleanStaleLeases();
	return Lease.acquire(build, TARGET_OS[targetOs], resolved, request);
}

/**
 * The lease launchOptions() minted for these options, taken by the browser
 * about to launch with them. null when they carry no lease this process holds:
 * a stock build, or a lease file the caller manages.
 */
export function claim(options: Record<string, any>): Lease | null {
	const lease = LIVE.get(String(options.env?.[LEASE_FILE_ENV]));
	if (!lease) return null;
	if (lease.claimed) {
		throw new Error(
			"These launch options' Camoufox Pro lease is already held by another browser. " +
				"Each browser needs its own: call launchOptions() once per browser.",
		);
	}
	lease.claimed = true;
	return lease;
}

/** The reason in a Pro browser's `lease refused` line, when a launch failed with one. */
export function refusal(error: unknown): string | null {
	return REFUSAL.exec(String((error as Error)?.message ?? error))?.[1] ?? null;
}

/**
 * For a launch that threw `error`: release its lease, and throw LeaseRefused
 * when the browser exited because it refused the lease.
 */
export async function launchFailed(
	lease: Lease | null,
	error: unknown,
): Promise<void> {
	await lease?.release("error");
	const reason = refusal(error);
	if (reason !== null) throw new LeaseRefused(reason);
}

/** What a Pro browser or persistent context exposes of its lease, as `.pro`. */
export interface ProSession {
	/** The lease's id. */
	leaseId: string;
	/**
	 * The captcha solver the lease grants: `endpoint` is an OpenAI-compatible
	 * API base URL, called with your own captcha key (docs/pro.md). null when
	 * the lease does not grant it.
	 */
	captcha: { endpoint: string; remaining: number; expires_at?: string } | null;
}

/**
 * End `lease`'s session when the browser or persistent context emits `event`,
 * make `close()` resolve only once that is done (a profile's state is synced
 * by then), and expose the session as `target.pro`.
 */
export function attachLease<
	T extends {
		on(event: any, listener: () => void): unknown;
		close(...args: any[]): Promise<void>;
	},
>(
	lease: Lease,
	target: T,
	event: "disconnected" | "close",
): T & { pro: ProSession } {
	// A failed sync has already said why; close() still rejects with it.
	target.on(event, () => void lease.close().catch(() => undefined));
	const close = target.close.bind(target);
	target.close = async (...args: any[]) => {
		await close(...args);
		await lease.close();
	};
	return Object.assign(target, {
		pro: { leaseId: lease.leaseId, captcha: lease.grants.captcha ?? null },
	});
}

// ── camoufox pro --activate ─────────────────────────────────────────────────

/** What --activate shows of a granted section: never its credentials. */
const SHOWN: Record<string, string[]> = {
	egress: ["class", "country"],
	gpu: ["mode", "renderer"],
	captcha: ["remaining"],
};

function granted(name: string, section: Record<string, any>): string {
	const detail = (SHOWN[name] ?? [])
		.filter((field) => section[field] != null)
		.map((field) => String(section[field]))
		.join(", ");
	return detail ? `${name}: granted (${detail})` : `${name}: granted`;
}

/**
 * `camoufox pro --activate`: mint a lease for the Pro build whose
 * pro-build.json is `buildFile`, for an identity of `targetOs` ('win', 'mac',
 * 'lin'), report what the lease grants, and release it. Resolves to whether
 * the API granted it.
 */
export async function activate(
	buildFile: string,
	targetOs: string,
	{
		key = null,
		echo = console.log,
	}: { key?: string | null; echo?: (line: string) => void } = {},
): Promise<boolean> {
	let build: ProBuild | null;
	let lease: Lease;
	try {
		build = readBuild(buildFile);
		if (!build) {
			echo(`[FAIL] no Camoufox Pro build: ${buildFile} does not exist`);
			return false;
		}
		lease = await acquire(build, targetOs, key);
	} catch (error) {
		echo(`[FAIL] lease: ${(error as Error).message}`);
		return false;
	}
	let released: boolean;
	try {
		echo(
			`[ ok ] lease verified: ${lease.leaseId} for ${build.version}, ` +
				`${lease.targetOs} identity, ${lease.fidelity} fidelity`,
		);
		for (const name of SECTIONS) {
			const section = lease.grants[name];
			echo(
				section
					? `[ ok ] ${granted(name, section)}`
					: `[ -- ] ${name}: not granted`,
			);
		}
	} finally {
		released = await lease.release();
	}
	echo(
		released
			? "[ ok ] lease released"
			: "[ -- ] lease not released: the API frees it when it expires",
	);
	return true;
}
