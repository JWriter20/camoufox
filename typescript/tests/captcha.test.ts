/**
 * Tests for src/captcha.ts, the optional CaptchaKraken bridge.
 *
 * Mirrors python/tests/test_captcha.py, plus one case the Python twin cannot
 * have: the Python driver is synchronous, so two solves can never overlap.
 * Here `solve()` is async and callers legitimately drive several pages at once,
 * which is what `attributionDepth` exists to survive.
 *
 * The regression these guard: `CAPTCHA_KRAKEN_CLIENT` must be set for the
 * duration of every solve and restored afterwards. That tag is the attribution
 * signal for the CaptchaKraken/camoufox partnership -- if it stops being sent,
 * solves stop being credited and nothing else in the system would notice.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
	CaptchaCredentialsError,
	CaptchaSolverUnavailable,
	clientTag,
	configure,
	configured,
	HOSTED_BASE_URL,
	resolveConfig,
	solveCaptcha,
	tagged,
	watchCaptcha,
} from "../src/captcha.js";

const CLIENT_ENV = "CAPTCHA_KRAKEN_CLIENT";
const BASE_URL_ENV = "VLLM_BASE_URL";
const API_KEY_ENV = "CAPTCHA_KRAKEN_API_KEY";
const MODEL_ENV = "CAPTCHA_LORA_NAME";

interface Stub {
	/** Attribution tag the stub saw at the moment it "solved", one per call. */
	seen: (string | undefined)[];
	/** Endpoint/key the stub saw, one entry per call. */
	env: { baseUrl?: string; apiKey?: string; model?: string }[];
	/** When true, each solve parks until released, so solves can overlap. */
	hold: boolean;
	/** One resolver per parked solve, in the order they entered. */
	releases: (() => void)[];
	/** Every solver `watchPage` was installed with, in install order. */
	watched: WatchedSolver[];
}

/** What the real `watchPage` receives — CaptchaKraken's `WatchableSolver`. */
interface WatchedSolver {
	detectCaptcha(page: unknown): Promise<unknown | null>;
	solve(page: unknown): Promise<unknown>;
}

/** Yield a macrotask; `await import()` needs more than a microtask to settle. */
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

async function until(predicate: () => boolean): Promise<void> {
	for (let i = 0; i < 200 && !predicate(); i++) {
		await tick();
	}
	if (!predicate()) {
		throw new Error("timed out waiting for the stub solver");
	}
}

// Kept on globalThis, not in a module-level `let`: vi.mock is hoisted above
// every declaration in this file, so a factory closing over one reads it in
// the temporal dead zone.
const stub = (): Stub => {
	const g = globalThis as typeof globalThis & { __ckStub?: Stub };
	g.__ckStub ??= { seen: [], env: [], hold: false, releases: [], watched: [] };
	return g.__ckStub;
};

vi.mock("captchakraken", () => ({
	CaptchaKrakenSolver: class {
		config: unknown;
		constructor(config?: unknown) {
			this.config = config;
		}
		async solve(_page: unknown) {
			const g = (globalThis as typeof globalThis & { __ckStub: Stub }).__ckStub;
			g.seen.push(process.env[CLIENT_ENV]);
			g.env.push({
				baseUrl: process.env[BASE_URL_ENV],
				apiKey: process.env[API_KEY_ENV],
				model: process.env[MODEL_ENV],
			});
			if (g.hold) {
				await new Promise<void>((resolve) => {
					g.releases.push(resolve);
				});
			}
			return { isSolved: true };
		}
		async detectCaptcha(_page: unknown) {
			return {};
		}
	},
	// Mirrors captchakraken 2.6.0: SYNCHRONOUS, returns the handle directly.
	// It records the solver it was handed rather than polling, because the
	// poll loop is CaptchaKraken's and is tested there -- what this bridge
	// owns is which solver goes in, and that is what the record pins.
	watchPage: (solver: WatchedSolver, _page: unknown, _options?: unknown) => {
		const g = (globalThis as typeof globalThis & { __ckStub: Stub }).__ckStub;
		g.watched.push(solver);
		let running = true;
		return {
			get running() {
				return running;
			},
			solves: 0,
			async stop() {
				running = false;
			},
		};
	},
}));

function clearEnv() {
	delete process.env[CLIENT_ENV];
	delete process.env[BASE_URL_ENV];
	delete process.env[API_KEY_ENV];
	delete process.env[MODEL_ENV];
}

beforeEach(() => {
	const g = stub();
	g.seen.length = 0;
	g.releases.length = 0;
	g.hold = false;
	g.env.length = 0;
	g.watched.length = 0;
	configure(null);
	clearEnv();
});

afterEach(() => {
	configure(null);
	clearEnv();
});

describe("clientTag", () => {
	it("reports the launcher package version", () => {
		// The LAUNCHER version, never the supported-browser range from
		// CONSTRAINTS -- a Firefox build range makes the audit trail unreadable.
		expect(clientTag()).toMatch(/^camoufox\/\d+\.\d+\.\d+/);
	});
});

describe("solveCaptcha", () => {
	it("sets the attribution tag for the duration of the solve", async () => {
		await solveCaptcha({});
		expect(stub().seen).toEqual([clientTag()]);
	});

	it("restores an unset environment afterwards", async () => {
		await solveCaptcha({});
		expect(CLIENT_ENV in process.env).toBe(false);
	});

	it("overwrites, then restores, a tag someone else set", async () => {
		process.env[CLIENT_ENV] = "someone-else/1.0";
		await solveCaptcha({});
		// Overwritten for the solve (attribution may not be redirected) and put
		// back on the way out so it never leaks into unrelated code.
		expect(stub().seen).toEqual([clientTag()]);
		expect(process.env[CLIENT_ENV]).toBe("someone-else/1.0");
	});

	it("keeps the tag set when an overlapping solve finishes first", async () => {
		// Two solves in flight, and the one that STARTED FIRST finishes first.
		// That ordering is what breaks a naive save/restore: A saved "unset" on
		// the way in, so A's teardown deletes the tag while B is still solving,
		// silently dropping attribution on every request B has left to make.
		// (The reverse order happens to survive a naive implementation, because
		// B would have saved A's tag as its own "previous" -- so only this
		// interleaving actually pins the behaviour.)
		const g = stub();
		g.hold = true;

		const a = solveCaptcha({});
		await until(() => g.releases.length === 1);
		const b = solveCaptcha({});
		await until(() => g.releases.length === 2);

		g.releases[0]();
		await a;
		expect(process.env[CLIENT_ENV]).toBe(clientTag());

		g.releases[1]();
		await b;
		expect(CLIENT_ENV in process.env).toBe(false);
	});

	it("forwards its config to the solver", async () => {
		const result = await solveCaptcha({}, { apiKey: "k" });
		expect(result).toEqual({ isSolved: true });
	});
});

// ── the `captcha` launch option ─────────────────────────────────────────────

describe("resolveConfig", () => {
	it("sends a bare token to the hosted service", () => {
		// The rule the option exists for. CaptchaKraken's own fallback would
		// send this user to localhost, where nothing is listening.
		expect(resolveConfig({ token: "ck_live_abc" })).toEqual({
			baseUrl: HOSTED_BASE_URL,
			apiKey: "ck_live_abc",
			model: undefined,
		});
	});

	it("leaves a bare URL self-hosted", () => {
		expect(resolveConfig({ url: "http://localhost:8000/v1" })).toEqual({
			baseUrl: "http://localhost:8000/v1",
			apiKey: undefined,
			model: undefined,
		});
	});

	it("does not let a token override an explicit URL", () => {
		expect(
			resolveConfig({
				token: "ck_live_abc",
				url: "http://gpu.internal:8000/v1",
			}),
		).toEqual({
			baseUrl: "http://gpu.internal:8000/v1",
			apiKey: "ck_live_abc",
			model: undefined,
		});
	});

	it.each([
		["ck_live_abc", HOSTED_BASE_URL, "ck_live_abc"],
		["https://gpu.example/v1", "https://gpu.example/v1", undefined],
	])("tells a key from an endpoint in the shorthand (%s)", (value, baseUrl, apiKey) => {
		expect(resolveConfig(value as string)).toEqual({
			baseUrl,
			apiKey,
			model: undefined,
		});
	});

	it("defers to the environment for `true`", () => {
		expect(resolveConfig(true)).toEqual({});
	});

	it("rejects an option with neither a token nor a URL", () => {
		expect(() => resolveConfig({})).toThrow(CaptchaCredentialsError);
		expect(() => resolveConfig({})).toThrow(/token|url/);
	});

	it("reports a misspelled key rather than ignoring it", () => {
		// Silently dropping `tokne` would send unauthenticated requests to
		// localhost and report it as connection-refused.
		expect(() =>
			resolveConfig({ tokne: "ck_live_abc" } as unknown as { token?: string }),
		).toThrow(/tokne/);
	});
});

describe("picking a served adapter", () => {
	it("accepts a model alongside credentials", () => {
		expect(
			resolveConfig({ token: "ck_live_abc", model: "captcha-v12" }),
		).toEqual({
			baseUrl: HOSTED_BASE_URL,
			apiKey: "ck_live_abc",
			model: "captcha-v12",
		});
	});

	it("accepts a model on its own", () => {
		// "endpoint and key are already in my env, just use a different adapter".
		expect(resolveConfig({ model: "captcha-v12" })).toEqual({
			model: "captcha-v12",
		});
	});
});

describe("the launch option", () => {
	it("registers and clears", () => {
		expect(configured()).toBeUndefined();
		configure({ token: "ck_live_abc" });
		expect(configured()?.apiKey).toBe("ck_live_abc");
		configure(null);
		expect(configured()).toBeUndefined();
	});

	it("reaches the solver, then leaves no trace", async () => {
		configure({ token: "ck_live_abc", model: "captcha-v12" });
		await solveCaptcha({});
		expect(stub().env[0]).toEqual({
			baseUrl: HOSTED_BASE_URL,
			apiKey: "ck_live_abc",
			model: "captcha-v12",
		});
		expect(BASE_URL_ENV in process.env).toBe(false);
		expect(API_KEY_ENV in process.env).toBe(false);
		expect(MODEL_ENV in process.env).toBe(false);
	});

	it("loses to an explicit environment variable", async () => {
		// Env is the highest-precedence source; a launcher that overrode it
		// would silently redirect a self-hoster.
		process.env[BASE_URL_ENV] = "http://my-own-box:8000/v1";
		configure({ token: "ck_live_abc" });
		await solveCaptcha({});
		expect(stub().env[0]?.baseUrl).toBe("http://my-own-box:8000/v1");
	});
});

describe("watchCaptcha", () => {
	// A watcher solves LATER, long after watchCaptcha() returned. The naive
	// implementation — building it inside applied() — would restore the tag and
	// the key before any captcha ever appeared, so every solve would go out
	// unattributed and, for a hosted user, unauthenticated.
	//
	// `tagged()` IS that difference, so it is what these drive. The loop belongs
	// to CaptchaKraken and is tested there (js/src/watcher.test.ts); mocking the
	// optional package just to re-test someone else's loop through this bridge
	// would assert less and break more.

	const recorder = () => {
		const seen: { tag?: string; key?: string }[] = [];
		const detected: (string | undefined)[] = [];
		return {
			seen,
			detected,
			solver: {
				async detectCaptcha(_p: unknown) {
					detected.push(process.env[CLIENT_ENV]);
					return {};
				},
				async solve(_p: unknown) {
					seen.push({
						tag: process.env[CLIENT_ENV],
						key: process.env[API_KEY_ENV],
					});
					return { isSolved: true } as never;
				},
			},
		};
	};

	it("returns a handle without solving anything yet", async () => {
		const watcher = await watchCaptcha({}, { intervalMs: 10_000 });
		expect(typeof watcher.stop).toBe("function");
		expect(watcher.running).toBe(true);
		// Installing is not solving: nothing has been billed yet.
		expect(stub().seen).toEqual([]);
		await watcher.stop();
		expect(watcher.running).toBe(false);
	});

	it("does not leave the tag applied after installation", async () => {
		configure("ck_live_abc");
		const watcher = await watchCaptcha({}, { intervalMs: 10_000 });

		expect(process.env[CLIENT_ENV]).toBeUndefined();
		expect(process.env[API_KEY_ENV]).toBeUndefined();
		await watcher.stop();
	});

	it("installs a TAGGED solver, so the loop's own solves are credited", async () => {
		// The end-to-end version of the case above: it is not enough that the
		// environment is clean after installation -- what the poll loop holds
		// has to be the wrapper, or every solve it makes later goes out
		// untagged and, for a hosted user, unauthenticated.
		configure("ck_live_abc");
		const watcher = await watchCaptcha({}, { intervalMs: 10_000 });

		const installed = stub().watched.at(-1);
		if (!installed) throw new Error("watchPage was never handed a solver");
		await installed.solve({});

		expect(stub().seen).toEqual([clientTag()]);
		expect(stub().env).toEqual([
			{ baseUrl: HOSTED_BASE_URL, apiKey: "ck_live_abc", model: undefined },
		]);
		// And back out again once that solve unwound.
		expect(process.env[CLIENT_ENV]).toBeUndefined();
		await watcher.stop();
	});

	it("forwards its options and config through to watchPage", async () => {
		const watcher = await watchCaptcha(
			{},
			{ intervalMs: 10_000 },
			{ apiKey: "k" },
		);
		expect(stub().watched).toHaveLength(1);
		await watcher.stop();
	});

	it("applies the tag and the launch key at SOLVE time", async () => {
		configure("ck_live_abc");
		const r = recorder();

		await tagged(r.solver).solve({});

		expect(r.seen).toEqual([{ tag: clientTag(), key: "ck_live_abc" }]);
	});

	it("applies it on EVERY solve, not just the first", async () => {
		configure("ck_live_abc");
		const r = recorder();
		const t = tagged(r.solver);

		await t.solve({});
		await t.solve({});
		await t.solve({});

		expect(r.seen.map((s) => s.tag)).toEqual([
			clientTag(),
			clientTag(),
			clientTag(),
		]);
	});

	it("restores the environment after each solve", async () => {
		configure("ck_live_abc");
		await tagged(recorder().solver).solve({});

		expect(process.env[CLIENT_ENV]).toBeUndefined();
		expect(process.env[API_KEY_ENV]).toBeUndefined();
	});

	it("does not tag detection — no request leaves the process", async () => {
		configure("ck_live_abc");
		const r = recorder();

		await tagged(r.solver).detectCaptcha({});

		expect(r.detected).toEqual([undefined]);
	});
});

// ── blocks that swap the module registry ────────────────────────────────────
//
// These two go LAST, and nothing that drives the mocked package may follow
// them. Each installs its own factory and calls vi.resetModules(); the first
// also calls vi.doUnmock(), which retires the file-level vi.mock() above for
// every later import, so a `describe` appended after this point silently
// resolves the REAL captchakraken out of node_modules instead of the stub.
// (That is not hypothetical: the watchCaptcha block was once appended here,
// and its tests were quietly running against whatever version happened to be
// installed.)

describe("when the installed captchakraken has no watcher", () => {
	// `captchakraken` is a PEER dependency, so bumping the floor to 2.6.0 asks
	// for an upgrade but cannot perform one -- a tree that already carries
	// 2.4.0 resolves the import and leaves `watchPage` undefined. Without the
	// guard that is a bare "watchPage is not a function", naming neither the
	// package nor the version that fixes it.
	it("names the version that added it, and keeps solveCaptcha working", async () => {
		vi.doMock("captchakraken", () => ({
			CaptchaKrakenSolver: class {
				async solve(_page: unknown) {
					return { isSolved: true };
				}
				async detectCaptcha(_page: unknown) {
					return null;
				}
			},
			// Absent, as in 2.4.0 and every release before it. Spelled as an
			// explicit `undefined` rather than omitted: a real old build is a
			// plain module object where the property simply is not there, but
			// vitest's mock namespace throws on any export the factory did not
			// declare, which would fail this test on the wrong error.
			watchPage: undefined,
		}));
		vi.resetModules();
		const fresh = await import("../src/captcha.js");

		await expect(fresh.watchCaptcha({})).rejects.toThrow(
			fresh.CaptchaSolverUnavailable,
		);
		await expect(fresh.watchCaptcha({})).rejects.toThrow("2.6.0");
		// The half that does not need the watcher is unaffected.
		await expect(fresh.solveCaptcha({})).resolves.toEqual({ isSolved: true });

		vi.doUnmock("captchakraken");
		vi.resetModules();
	});
});

// MUST BE LAST IN THIS FILE. It swaps in a throwing module factory and calls
// vi.resetModules(), and the dynamic-import path does not fully recover from
// that -- anything declared after it resolves `captchakraken` as missing and
// fails for a reason that has nothing to do with what it was testing.
describe("when captchakraken is not installed", () => {
	it("rejects with a CaptchaSolverUnavailable naming the install command", async () => {
		vi.doMock("captchakraken", () => {
			throw new Error("Cannot find module 'captchakraken'");
		});
		vi.resetModules();
		// Re-imported after resetModules, so take the error class from the SAME
		// module instance -- the top-level import is now a different identity
		// and `instanceof` against it would fail for the wrong reason.
		const fresh = await import("../src/captcha.js");

		await expect(fresh.solveCaptcha({})).rejects.toThrow(
			fresh.CaptchaSolverUnavailable,
		);
		await expect(fresh.solveCaptcha({})).rejects.toThrow(
			"npm install captchakraken",
		);
		// The bridge's own type, not a leaked module-resolution error.
		expect(CaptchaSolverUnavailable.name).toBe("CaptchaSolverUnavailable");

		vi.doUnmock("captchakraken");
		vi.resetModules();
	});
});
