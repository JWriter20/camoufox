/**
 * Port of pythonlib/tests/test_pro.py: Camoufox Pro's client half, sign-in and
 * the lease a Pro build starts with.
 *
 * Every test talks to a local fake of the Camoufox Pro API whose answers follow
 * the server's own shapes; no test reaches the network.
 */
import { randomBytes, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as http from "node:http";
import type { AddressInfo } from "node:net";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
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
const { NewBrowser } = await import("../src/sync_api.js");
const { ensureModel } = await import("../src/fpgen/index.js");

let modelReady = true;
try {
	await ensureModel();
} catch (e) {
	modelReady = prerequisite("fpgen-model", false, String(e));
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const GOLDEN = JSON.parse(
	fs.readFileSync(
		path.resolve(HERE, "../../pythonlib/tests/data/pro-lease-golden.json"),
		"utf-8",
	),
);
const BUILD_HASH = `sha256:${"ab".repeat(32)}`;
const KEY = `cfp_live_${"k".repeat(43)}`;
const BUILD: import("../src/pro.js").ProBuild = {
	build_hash: BUILD_HASH,
	version: "156.0.1-pro.1",
	target: "linux-x86_64",
};

function tokenFor(leaseId: string, buildHash: string, expiresIn = 1800) {
	const now = Math.floor(Date.now() / 1000);
	const u64 = (n: number) => {
		const b = Buffer.alloc(8);
		b.writeBigUInt64BE(BigInt(n));
		return b;
	};
	const raw = Buffer.concat([
		Buffer.from([1, 0]),
		Buffer.from(leaseId.replaceAll("-", ""), "hex"),
		u64(42),
		Buffer.alloc(16),
		Buffer.from(buildHash.replace("sha256:", ""), "hex"),
		u64(now),
		u64(now + expiresIn),
		Buffer.from([0, 1]),
		randomBytes(64), // a signature: the launcher never checks it, the browser does
	]);
	return `cfl1_${raw.toString("base64url")}`;
}

interface Recorded {
	path: string;
	body: Record<string, any>;
	auth: string | undefined;
	status: number;
}

/** The routes the launcher calls, answering as the control plane does unless a test scripts otherwise. */
class FakeApi {
	requests: Recorded[] = [];
	scripted: [RegExp, [number, Record<string, any>][]][] = [];
	clockOffsetMs = 0;
	heartbeatS = 60;
	grants: Record<string, any> = {};
	#waiters: (() => void)[] = [];
	server = http.createServer((req, res) => {
		let data = "";
		req.on("data", (chunk) => {
			data += chunk;
		});
		req.on("end", () => {
			const body = data ? JSON.parse(data) : {};
			const url = req.url as string;
			const [status, answer] = this.answer(url, body);
			this.requests.push({
				path: url,
				body,
				auth: req.headers.authorization,
				status,
			});
			for (const wake of this.#waiters) wake();
			res.writeHead(status, { "Content-Type": "application/json" });
			res.end(JSON.stringify(answer));
		});
	});

	script(route: string, ...answers: [number, Record<string, any>][]) {
		this.scripted.push([new RegExp(`^${route}$`), answers]);
	}

	calls(route: string) {
		const pattern = new RegExp(`^${route}$`);
		return this.requests.filter((r) => pattern.test(r.path));
	}

	waitFor(predicate: () => boolean, timeoutMs = 5000): Promise<void> {
		return new Promise((resolve, reject) => {
			const timer = setTimeout(
				() => reject(new Error(JSON.stringify(this.requests))),
				timeoutMs,
			);
			const check = () => {
				if (!predicate()) return;
				clearTimeout(timer);
				this.#waiters = this.#waiters.filter((w) => w !== check);
				resolve();
			};
			this.#waiters.push(check);
			check();
		});
	}

	answer(
		url: string,
		body: Record<string, any>,
	): [number, Record<string, any>] {
		for (const [pattern, queue] of this.scripted) {
			if (pattern.test(url) && queue.length) return queue.shift() as any;
		}
		const now = new Date(Date.now() + this.clockOffsetMs).toISOString();
		if (url === "/api/v1/leases") {
			const leaseId = randomUUID();
			return [
				201,
				{
					v: 1,
					lease_id: `lse_${leaseId}`,
					server_time: now,
					expires_at: now,
					token: tokenFor(leaseId, body.build_hash),
					limits: {
						ttl_s: 1800,
						heartbeat_s: this.heartbeatS,
						grace_s: 180,
						degrade_drain_s: 900,
					},
					host: {
						fingerprint: body.host.fingerprint,
						fidelity: "layout",
						capability_hash: "",
					},
					notices: [],
					...this.grants,
				},
			];
		}
		const match =
			/^\/api\/v1\/leases\/lse_([0-9a-f-]+)\/(heartbeat|release)$/.exec(url);
		if (match?.[2] === "heartbeat") {
			return [
				200,
				{
					lease_id: `lse_${match[1]}`,
					server_time: now,
					expires_at: now,
					token: tokenFor(match[1], BUILD_HASH),
					token_kid: 0,
					notices: [],
				},
			];
		}
		if (match) return [200, { released: true, already_released: false }];
		return [404, { error: "not_found", message: "No such route." }];
	}
}

let api: FakeApi;
const TIMING = { ...pro.proTiming };
const SAVED_ENV = { ...process.env };

beforeAll(() => {
	for (const name of ["XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"]) {
		process.env[name] = path.join(SCRATCH, name.toLowerCase());
	}
});

beforeEach(async () => {
	for (const name of ["XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"]) {
		const dir = process.env[name] as string;
		fs.rmSync(dir, { recursive: true, force: true });
		fs.mkdirSync(dir, { recursive: true });
	}
	api = new FakeApi();
	await new Promise<void>((resolve) =>
		api.server.listen(0, "127.0.0.1", resolve),
	);
	const { port } = api.server.address() as AddressInfo;
	process.env[pro.API_ENV] = `http://127.0.0.1:${port}`;
	delete process.env[pro.KEY_ENV];
	delete process.env[pro.LEASE_FILE_ENV];
	Object.assign(pro.proTiming, {
		mintRetryS: [0, 0, 0],
		heartbeatBackoffS: [0.01],
	});
});

afterEach(async () => {
	await pro.releaseAll();
	Object.assign(pro.proTiming, TIMING);
	restoreDeps();
	await new Promise((resolve) => api.server.close(resolve));
});

afterAll(() => {
	for (const name of ["XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", pro.API_ENV]) {
		if (SAVED_ENV[name] === undefined) delete process.env[name];
		else process.env[name] = SAVED_ENV[name];
	}
});

const acquire = () => pro.acquire(BUILD, "win", KEY);
const mode = (file: string) => fs.statSync(file).mode & 0o777;

// ── the file the browser reads ──────────────────────────────────────────────

describe("the lease file", () => {
	it.each(
		GOLDEN.lease_files,
	)("is byte-identical to the shared golden ($os)", (c: any) => {
		expect(pro.leaseFileBytes(c.token, c.os, c.fidelity, c.written_at)).toBe(
			c.file,
		);
	});

	it.each(
		GOLDEN.host_fingerprints,
	)("host fingerprint matches the shared golden ($os)", (h: any) => {
		expect(pro.hostFingerprintOf(h.host_id, h.os, h.username)).toBe(
			h.fingerprint,
		);
	});

	it("host id is generated once and private", () => {
		const first = pro.hostFingerprint();
		expect(pro.hostFingerprint()).toBe(first);
		const hostId = path.join(
			process.env.XDG_CACHE_HOME as string,
			"camoufox",
			"pro",
			"host.id",
		);
		expect(mode(hostId)).toBe(0o600);
		expect(fs.readFileSync(hostId, "utf-8")).toHaveLength(32);
	});
});

// ── which builds get a lease ────────────────────────────────────────────────

describe.runIf(modelReady)("launchOptions", () => {
	let exe: string;
	beforeEach(() => {
		stubHost();
		const dir = fs.mkdtempSync(path.join(SCRATCH, "build-"));
		fs.cpSync(BUNDLE, dir, { recursive: true });
		exe = path.join(dir, "camoufox-bin");
	});
	const launch = (env: Record<string, string> = {}) =>
		quietly(() =>
			utils.launchOptions({
				env: { HOME, ...env },
				executable_path: exe,
				os: "windows",
				headless: true,
				i_know_what_im_doing: true,
			}),
		);
	const makePro = () =>
		fs.writeFileSync(
			path.join(path.dirname(exe), "pro-build.json"),
			JSON.stringify({
				build_hash: BUILD_HASH,
				version: "156.0.1-pro.1",
				target: "linux-x86_64",
			}),
		);

	it("makes no request for a stock build", async () => {
		process.env[pro.KEY_ENV] = KEY;
		const opts = await launch();
		expect(api.requests).toEqual([]);
		expect(pro.LEASE_FILE_ENV in opts.env).toBe(false);
	});

	it("says how to sign in when a Pro build has no key", async () => {
		makePro();
		await expect(launch()).rejects.toThrow(/camoufox login/);
		await expect(launch()).rejects.toBeInstanceOf(errors.NotSignedIn);
		expect(api.requests).toEqual([]);
	});

	it("launches a Pro build with a minted lease", async () => {
		makePro();
		process.env[pro.KEY_ENV] = KEY;
		const opts = await launch();
		expect(api.requests).toHaveLength(1);
		expect(api.requests[0].path).toBe("/api/v1/leases");
		expect(api.requests[0].auth).toBe(`Bearer ${KEY}`);
		const file = opts.env[pro.LEASE_FILE_ENV];
		const body = JSON.parse(fs.readFileSync(file, "utf-8"));
		expect(path.dirname(file)).toBe(
			path.join(process.env.XDG_RUNTIME_DIR as string, "camoufox-pro"),
		);
		expect(path.basename(file)).toBe(`${body.lease_id}.json`);
		expect(mode(file)).toBe(0o600);
		expect(mode(path.dirname(file))).toBe(0o700);
		expect(body.os).toBe("windows");
		expect(body.fidelity).toBe("layout");
		expect(body.build_hash).toBe(BUILD_HASH.replace("sha256:", ""));
	});

	it("leaves the caller's own lease file alone", async () => {
		makePro();
		const opts = await launch({ [pro.LEASE_FILE_ENV]: "/run/mine.json" });
		expect(opts.env[pro.LEASE_FILE_ENV]).toBe("/run/mine.json");
		expect(api.requests).toEqual([]);
	});
});

describe("the mint request", () => {
	it("is what the server parses", async () => {
		await acquire();
		const body = api.requests[0].body;
		// cloud/src/cfp/api/leases.py parse_request, rule for rule.
		expect(body.build_hash).toMatch(/^sha256:[0-9a-f]{64}$/);
		expect(body.host.fingerprint).toMatch(/^[0-9a-f]{64}$/);
		expect(["windows", "macos", "linux"]).toContain(
			body.host.capability.host.os,
		);
		expect(body.host.label.length).toBeGreaterThan(0);
		expect(body.host.label.length).toBeLessThanOrEqual(64);
		expect(body.os).toBe("windows");
		expect(body.client).toMatch(/^camoufox-js\/\S+$/);
		expect(body.idempotency_key.length).toBeLessThanOrEqual(64);
		expect("profile" in body).toBe(false);
	});
});

// ── the key ─────────────────────────────────────────────────────────────────

describe("the key", () => {
	it("is the argument, then the environment, then the file", () => {
		const stored = `cfp_live_${"f".repeat(43)}`;
		pro.storeKey(stored);
		expect(pro.resolveKey()).toBe(stored);
		process.env[pro.KEY_ENV] = `cfp_live_${"e".repeat(43)}`;
		expect(pro.resolveKey()).toBe(`cfp_live_${"e".repeat(43)}`);
		expect(pro.resolveKey(`cfp_live_${"a".repeat(43)}`)).toBe(
			`cfp_live_${"a".repeat(43)}`,
		);
	});

	it("is stored privately", () => {
		const file = pro.storeKey(KEY);
		expect(file).toBe(
			path.join(
				process.env.XDG_CONFIG_HOME as string,
				"camoufox",
				"pro-credentials.json",
			),
		);
		expect(mode(file)).toBe(0o600);
		expect(mode(path.dirname(file))).toBe(0o700);
	});

	it("is refused, with the fix, when other users can read it", () => {
		const file = pro.storeKey(KEY);
		fs.chmodSync(file, 0o644);
		expect(() => pro.resolveKey()).toThrow(`chmod 600 ${file}`);
	});

	it("is never a management token", () => {
		expect(() => pro.resolveKey(`cfp_mgmt_${"m".repeat(43)}`)).toThrow(
			errors.NotSignedIn,
		);
	});

	it("is deleted by logout", () => {
		pro.storeKey(KEY);
		expect(pro.forgetKey()).toBe(`cfp_live_...${KEY.slice(-4)}`);
		expect(pro.forgetKey()).toBeNull();
		expect(() => pro.resolveKey()).toThrow(errors.NotSignedIn);
	});
});

// ── sign-in ─────────────────────────────────────────────────────────────────

const START: [number, Record<string, any>] = [
	200,
	{
		device_code: "dc",
		user_code: "ABCD-EFGH",
		verification_uri: "https://camoufox.com/device",
		verification_uri_complete: "https://camoufox.com/device?code=ABCD-EFGH",
		expires_in: 900,
		interval: 5,
	},
];

describe("sign-in", () => {
	it("waits, slows down, and stores a runtime key", async () => {
		api.script("/api/v1/device/start", START);
		api.script(
			"/api/v1/device/token",
			[400, { error: "authorization_pending", message: "pending" }],
			[429, { error: "slow_down", message: "Polling too fast." }],
			[
				200,
				{
					access_token: "cfp_mgmt_token",
					token_type: "bearer",
					account: { id: 7, name: "acme", kind: "org" },
					role: "owner",
					user: { id: 3, github_login: "octo" },
				},
			],
		);
		api.script("/api/v1/keys", [201, { id: 9, api_key: KEY }]);
		const slept: number[] = [];
		const printed: string[] = [];

		const signedIn = await pro.login(
			(line) => printed.push(line),
			async (s) => slept.push(s),
		);

		expect(slept).toEqual([5, 5, 10]);
		expect(printed[0]).toContain("https://camoufox.com/device?code=ABCD-EFGH");
		expect(printed[1]).toContain("ABCD-EFGH");
		expect(api.calls("/api/v1/device/token").map((r) => r.body)).toEqual(
			Array(3).fill({ device_code: "dc" }),
		);
		expect(api.calls("/api/v1/keys")[0].auth).toBe("Bearer cfp_mgmt_token");
		expect(signedIn.account.name).toBe("acme");
		expect(signedIn.user.github_login).toBe("octo");
		expect(pro.resolveKey()).toBe(KEY);
	});

	it.each([
		"expired_token",
		"access_denied",
	])("fails clearly when not approved (%s)", async (code) => {
		api.script("/api/v1/device/start", START);
		api.script("/api/v1/device/token", [400, { error: code, message: "No." }]);
		await expect(
			pro.login(
				() => undefined,
				async () => undefined,
			),
		).rejects.toThrow(/camoufox login/);
		expect(api.calls("/api/v1/keys")).toEqual([]);
	});
});

// ── refusals ────────────────────────────────────────────────────────────────

describe("a refused mint", () => {
	it.each([
		[401, "invalid_api_key", errors.NotSignedIn],
		[402, "subscription_required", errors.SubscriptionRequired],
		[403, "account_suspended", errors.AccountSuspended],
		[403, "build_not_allowlisted", errors.BuildNotAllowlisted],
		[409, "lease_limit_reached", errors.LeaseLimitReached],
		[412, "capability_mismatch", errors.CapabilityMismatch],
	] as const)("%i %s raises its own type without retrying", async (status, code, Kind) => {
		api.script("/api/v1/leases", [
			status,
			{
				error: code,
				message: "Refused.",
				resolution_url: "https://camoufox.com/billing",
			},
		]);
		const error = await acquire().catch((e) => e);
		expect(error).toBeInstanceOf(Kind);
		expect(error.code).toBe(code);
		expect(error.resolution_url).toBe("https://camoufox.com/billing");
		expect(api.requests).toHaveLength(1);
	});

	it("names the holders of every slot", async () => {
		api.script("/api/v1/leases", [
			409,
			{
				error: "lease_limit_reached",
				message: "All 1 concurrent browsers are in use.",
				details: {
					limit: 1,
					active: 1,
					holders: [
						{ host_label: "runner-3", expires_at: "2026-09-29T12:00:00.000Z" },
					],
				},
			},
		]);
		await expect(acquire()).rejects.toThrow(/runner-3/);
	});

	it("is retried when the API is unreachable, with one idempotency key", async () => {
		api.script(
			"/api/v1/leases",
			...Array(4).fill([
				503,
				{ error: "service_unavailable", message: "down" },
			]),
		);
		await expect(acquire()).rejects.toBeInstanceOf(errors.ProUnavailable);
		expect(api.requests).toHaveLength(4);
		expect(new Set(api.requests.map((r) => r.body.idempotency_key)).size).toBe(
			1,
		);
	});

	it("raises when rate limiting outlasts the retries", async () => {
		api.script(
			"/api/v1/leases",
			...Array(4).fill([
				429,
				{ error: "rate_limited", message: "slow", retry_after: 0 },
			]),
		);
		await expect(acquire()).rejects.toBeInstanceOf(errors.RateLimited);
		expect(api.requests).toHaveLength(4);
	});

	it("mints after a transient failure", async () => {
		api.script("/api/v1/leases", [502, {}]);
		const lease = await acquire();
		expect(fs.existsSync(lease.path)).toBe(true);
		expect(api.requests).toHaveLength(2);
	});

	it("refuses and releases when the clock is far off the API's", async () => {
		api.clockOffsetMs = 10 * 60 * 1000;
		const error = await acquire().catch((e) => e);
		expect(error).toBeInstanceOf(errors.ProClockSkew);
		expect(error.skew).toBeGreaterThan(500);
		expect(api.calls(".*/release").map((r) => r.body)).toEqual([
			{ reason: "error" },
		]);
		const dir = pro.leaseDir();
		expect(fs.existsSync(dir) ? fs.readdirSync(dir) : []).toEqual([]);
	});
});

// ── renewal ─────────────────────────────────────────────────────────────────

describe("the heartbeat", () => {
	it("rewrites the file with each token", async () => {
		api.heartbeatS = 0.05;
		const lease = await acquire();
		const before = JSON.parse(fs.readFileSync(lease.path, "utf-8"));
		await api.waitFor(() => api.calls(".*/heartbeat").length >= 2);
		const after = JSON.parse(fs.readFileSync(lease.path, "utf-8"));
		await lease.release();
		const beats = api.calls(".*/heartbeat");
		expect(beats.slice(0, 2).map((b) => b.body.seq)).toEqual([1, 2]);
		expect(beats[0].body.host.fingerprint).toBe(
			api.requests[0].body.host.fingerprint,
		);
		expect(after.lease_id).toBe(before.lease_id);
		expect(after.token).not.toBe(before.token);
	});

	it("backs off and recovers", async () => {
		api.heartbeatS = 0.05;
		api.script(
			".*/heartbeat",
			[503, { error: "service_unavailable", message: "down" }],
			[402, {}],
		);
		const lease = await acquire();
		await api.waitFor(
			() =>
				api
					.calls(".*/heartbeat")
					.slice(0, 3)
					.map((r) => r.status)
					.join() === "503,402,200",
		);
		await lease.release();
	});

	it("mints a swept lease again into the same file", async () => {
		api.heartbeatS = 0.05;
		api.script(".*/heartbeat", [
			404,
			{ error: "lease_not_found", message: "gone" },
		]);
		const lease = await acquire();
		const file = lease.path;
		const firstId = lease.leaseId;
		await api.waitFor(() => api.calls("/api/v1/leases").length === 2);
		await api.waitFor(
			() =>
				JSON.parse(fs.readFileSync(file, "utf-8")).lease_id !==
				firstId.replace("lse_", ""),
		);
		expect(lease.path).toBe(file);
		expect(lease.leaseId).not.toBe(firstId);
		await lease.release();
	});

	it("stops when the key is revoked", async () => {
		api.heartbeatS = 0.05;
		api.script(".*/heartbeat", [
			401,
			{ error: "invalid_api_key", message: "revoked" },
		]);
		const lease = await acquire();
		await api.waitFor(() => api.calls(".*/heartbeat").length === 1);
		await new Promise((resolve) => setTimeout(resolve, 300));
		expect(api.calls(".*/heartbeat")).toHaveLength(1);
		expect(fs.existsSync(lease.path)).toBe(true);
	});
});

// ── release ─────────────────────────────────────────────────────────────────

describe("release", () => {
	it("happens when the browser closes, and deletes the file", async () => {
		const lease = await acquire();
		const handlers: Record<string, () => void> = {};
		pro.attachLease(
			lease,
			{
				on: (event: string, handler: () => void) => (handlers[event] = handler),
				close: async () => undefined,
			},
			"disconnected",
		);
		handlers.disconnected();
		await lease.release(); // the one in flight; releasing twice is one request
		expect(api.calls(".*/release").map((r) => r.body)).toEqual([
			{ reason: "clean_exit" },
		]);
		expect(fs.existsSync(lease.path)).toBe(false);
	});

	it("gives each browser its own lease", async () => {
		const first = await acquire();
		const second = await acquire();
		expect(first.path).not.toBe(second.path);
		expect(first.leaseId).not.toBe(second.leaseId);
		const options = { env: { [pro.LEASE_FILE_ENV]: first.path } };
		expect(pro.claim(options)).toBe(first);
		expect(() => pro.claim(options)).toThrow(/once per browser/);
	});

	it("is retried once when the API cannot take it", async () => {
		api.script(".*/release", [503, {}], [503, {}]);
		const lease = await acquire();
		await lease.release();
		expect(api.calls(".*/release")).toHaveLength(2);
		expect(fs.existsSync(lease.path)).toBe(false);
	});

	it("cleans stale lease files at the next start", async () => {
		const dir = pro.leaseDir();
		fs.mkdirSync(dir, { recursive: true });
		const stale = path.join(dir, "old.json");
		const fresh = path.join(dir, "new.json");
		fs.writeFileSync(stale, "{}");
		fs.writeFileSync(fresh, "{}");
		const dayAgo = (Date.now() - 25 * 3600 * 1000) / 1000;
		fs.utimesSync(stale, dayAgo, dayAgo);
		await acquire();
		expect(fs.existsSync(stale)).toBe(false);
		expect(fs.existsSync(fresh)).toBe(true);
	});
});

// ── a browser that refuses its lease ────────────────────────────────────────

describe("a browser that refuses its lease", () => {
	const failingLaunch = (message: string) =>
		({
			launch: async () => {
				throw new Error(message);
			},
		}) as any;

	it("exits 78 and raises LeaseRefused, releasing the lease", async () => {
		const lease = await acquire();
		const error = await NewBrowser(
			failingLaunch(
				"browserType.launch: Failed to launch the browser process.\n" +
					"Browser logs:\n\n" +
					"<launched> pid=4242\n" +
					"[pid=4242][err] camoufox-pro: lease refused (build_mismatch)\n" +
					"[pid=4242] <process did exit: exitCode=78, signal=null>",
			),
			{ from_options: { env: { [pro.LEASE_FILE_ENV]: lease.path } } },
		).catch((e) => e);
		expect(error).toBeInstanceOf(errors.LeaseRefused);
		expect(error.reason).toBe("build_mismatch");
		expect(api.calls(".*/release").map((r) => r.body)).toEqual([
			{ reason: "error" },
		]);
		expect(fs.existsSync(lease.path)).toBe(false);
	});

	it("raises any other launch failure as it was", async () => {
		const lease = await acquire();
		await expect(
			NewBrowser(failingLaunch("no display"), {
				from_options: { env: { [pro.LEASE_FILE_ENV]: lease.path } },
			}),
		).rejects.toThrow("no display");
		expect(api.calls(".*/release").map((r) => r.body)).toEqual([
			{ reason: "error" },
		]);
	});
});

// ── camoufox pro --activate ─────────────────────────────────────────────────

describe("camoufox pro --activate", () => {
	let buildFile: string;
	beforeEach(() => {
		const dir = fs.mkdtempSync(path.join(SCRATCH, "pro-build-"));
		buildFile = path.join(dir, "pro-build.json");
		fs.writeFileSync(buildFile, JSON.stringify(BUILD));
	});
	const activate = async (targetOs = "win") => {
		const lines: string[] = [];
		const verified = await pro.activate(buildFile, targetOs, {
			echo: (line) => lines.push(line),
		});
		return { verified, lines };
	};

	it("verifies a lease and releases it", async () => {
		process.env[pro.KEY_ENV] = KEY;
		const { verified, lines } = await activate();
		expect(verified).toBe(true);
		const [mint] = api.calls("/api/v1/leases");
		expect(mint.body.os).toBe("windows");
		expect(mint.body.build_hash).toBe(BUILD_HASH);
		const [release] = api.calls(".*/release");
		expect(release.body).toEqual({ reason: "clean_exit" });
		expect(lines).toEqual([
			`[ ok ] lease verified: ${release.path.split("/")[4]} for 156.0.1-pro.1, windows identity, layout fidelity`,
			"[ -- ] profile: not granted",
			"[ -- ] egress: not granted",
			"[ -- ] gpu: not granted",
			"[ -- ] captcha: not granted",
			"[ ok ] lease released",
		]);
		expect(
			fs.readdirSync(
				path.join(process.env.XDG_RUNTIME_DIR as string, "camoufox-pro"),
			),
		).toEqual([]);
	});

	it("reports only the sections the lease carries, and no credentials", async () => {
		process.env[pro.KEY_ENV] = KEY;
		api.grants = {
			profile: null,
			egress: {
				server: "http://203.0.113.7:8080",
				username: "user-1",
				password: "secret-pass",
				class: "residential",
				country: "US",
				exit_ip: null,
			},
			gpu: null,
			captcha: {
				endpoint: "https://captcha.example/v1",
				remaining: 250,
				expires_at: "2026-10-02T12:00:00Z",
			},
		};
		const { verified, lines } = await activate();
		expect(verified).toBe(true);
		expect(lines.slice(1, 5)).toEqual([
			"[ -- ] profile: not granted",
			"[ ok ] egress: granted (residential, US)",
			"[ -- ] gpu: not granted",
			"[ ok ] captcha: granted (250)",
		]);
		expect(lines.join("\n")).not.toMatch(/secret-pass|user-1/);
	});

	it("says there is no Pro build and fails", async () => {
		process.env[pro.KEY_ENV] = KEY;
		fs.rmSync(buildFile);
		const { verified, lines } = await activate();
		expect(verified).toBe(false);
		expect(lines).toEqual([
			`[FAIL] no Camoufox Pro build: ${buildFile} does not exist`,
		]);
		expect(api.requests).toEqual([]);
	});

	it("fails without a key, saying how to sign in", async () => {
		const { verified, lines } = await activate();
		expect(verified).toBe(false);
		expect(lines[0]).toMatch(/^\[FAIL\] lease: .*camoufox login/);
		expect(api.requests).toEqual([]);
	});

	it("reads the key camoufox login stored", async () => {
		pro.storeKey(KEY);
		expect((await activate()).verified).toBe(true);
		expect(api.calls("/api/v1/leases")[0].auth).toBe(`Bearer ${KEY}`);
	});

	it("reports a refused lease and fails", async () => {
		process.env[pro.KEY_ENV] = KEY;
		api.script("/api/v1/leases", [
			402,
			{
				error: "subscription_required",
				message: "No active plan.",
				resolution_url: "https://camoufox.com/pro",
			},
		]);
		const { verified, lines } = await activate();
		expect(verified).toBe(false);
		expect(lines).toEqual([
			"[FAIL] lease: No active plan. https://camoufox.com/pro",
		]);
		expect(api.calls(".*/release")).toEqual([]);
	});

	it("says when the release did not reach the API", async () => {
		process.env[pro.KEY_ENV] = KEY;
		api.script("/api/v1/leases/.*/release", [500, {}], [500, {}]);
		const { verified, lines } = await activate();
		expect(verified).toBe(true);
		expect(lines.at(-1)).toBe(
			"[ -- ] lease not released: the API frees it when it expires",
		);
	});

	it("runs from the CLI against the build beside an executable", async () => {
		process.env[pro.KEY_ENV] = KEY;
		const savedArgv = process.argv;
		const output: string[] = [];
		const log = vi
			.spyOn(console, "log")
			.mockImplementation((line) => void output.push(String(line)));
		try {
			process.argv = [
				"node",
				"camoufox",
				"pro",
				"--activate",
				"--executable-path",
				path.join(path.dirname(buildFile), "camoufox-bin"),
				"--os",
				"linux",
			];
			await import("../src/__main__.js");
			await vi.waitFor(() =>
				expect(output.at(-1)).toBe("[ ok ] lease released"),
			);
		} finally {
			process.argv = savedArgv;
			log.mockRestore();
		}
		expect(api.calls("/api/v1/leases")[0].body.os).toBe("linux");
		expect(process.exitCode ?? 0).toBe(0);
	});
});
