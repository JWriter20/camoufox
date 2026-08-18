/**
 * CAPTCHA solving via CaptchaKraken. Twin of `python/src/captcha.py`.
 *
 * Camoufox gets you a browser a site cannot fingerprint as automated. It does
 * not, on its own, get you past a challenge that is already on screen. This
 * module bridges the two: hand it a page and it drives the visible captcha to
 * completion using CaptchaKraken (https://github.com/JWriter20/CaptchaKraken)
 * — OpenCV grid detection plus a fine-tuned vision model.
 *
 * ```ts
 * import { Camoufox, solveCaptcha } from "camoufox";
 *
 * const browser = await Camoufox({ headless: false });
 * const page = await browser.newPage();
 * await page.goto("https://example.com/protected");
 * const result = await solveCaptcha(page);
 * if (result?.isSolved) {
 *   // ...
 * }
 * ```
 *
 * INSTALL
 * The solver is an OPTIONAL peer dependency. Camoufox does not depend on it,
 * and nothing here is loaded unless you call `solveCaptcha`:
 *
 *     npm install captchakraken
 *
 * SELF-HOSTED OR HOSTED
 * CaptchaKraken is source-available and runs entirely on your own GPU — point
 * `VLLM_BASE_URL` at your server and no request leaves your machine. The hosted
 * API at api.captchakraken.com is a convenience for people who don't want to
 * run a 9B vision model; set `CAPTCHA_KRAKEN_API_KEY` to use it. Both paths use
 * the same code and the same weights.
 *
 * ATTRIBUTION
 * Every request this module makes is tagged `camoufox/<version>`. See
 * `CLIENT_ENV` below for the mechanics and why it is set unconditionally.
 *
 * LICENSING
 * CaptchaKraken is source-available, and its license restricts SHIPPING the
 * solver as a built-in feature of a stealth browser distributed to third
 * parties (v1.1 §3(d)) — not USING it with one (§2(c)). Calling into it from
 * your own automation is permitted, commercially or otherwise. This module is
 * published by the CaptchaKraken copyright holder, which is what makes
 * Camoufox's own integration fine; a third-party fork that redistributes it is
 * not automatically covered.
 * https://github.com/JWriter20/CaptchaKraken/blob/main/LICENSE
 */

import { LIBRARY_VERSION } from "./__version__.js";

/**
 * Read by CaptchaKraken's planner, which sends it as the `X-CK-Client` header.
 *
 * The JS driver shells out to the bundled Python CLI, and its `cliEnv` spreads
 * `process.env` into the child — so setting this here is what reaches the
 * planner, exactly as the Python twin's `os.environ` write does.
 *
 * WHY IT IS SET UNCONDITIONALLY, overwriting whatever is already in the
 * environment: this tag is the attribution signal for the CaptchaKraken/camoufox
 * partnership — it is what distinguishes camoufox-originated solves from
 * everyone else's, and the revenue share is computed from it. An integration
 * that let the tag be replaced would let attribution be silently redirected,
 * which is precisely what both parties agreed the tracking must not permit. It
 * is restored on the way out so it never leaks into unrelated code in the same
 * process.
 *
 * It carries NO pricing power: the server derives the billable puzzle class
 * from the request body, never from this header, so a forged tag cannot buy
 * cheaper inference. Attribution only.
 */
const CLIENT_ENV = "CAPTCHA_KRAKEN_CLIENT";

/**
 * Where a hosted (token-bearing) user's inference goes.
 *
 * CaptchaKraken's own base-URL lookup falls back to LOCALHOST when it finds no
 * explicit endpoint and no credentials file — correct for a self-hoster, and
 * useless for someone handed a cloud token and nothing else: every solve dials
 * a dead local port and fails with connection-refused, which says nothing about
 * the request having been meant for the cloud. So a token supplied here with no
 * URL resolves the endpoint instead of letting that fallback decide.
 */
export const HOSTED_BASE_URL = "https://api.captchakraken.com/v1";

// The two variables CaptchaKraken reads for "where does this go, and as whom".
// The JS driver shells out to the bundled Python CLI and its `cliEnv` spreads
// `process.env` into the child, so setting them here is what reaches the planner.
const BASE_URL_ENV = "VLLM_BASE_URL";
const API_KEY_ENV = "CAPTCHA_KRAKEN_API_KEY";

/**
 * The served adapter name CaptchaKraken asks vLLM for, as the `model` field.
 *
 * WHY THIS IS EXPOSED. It defaults, via CaptchaKraken's pinned manifest, to
 * whatever that release pinned — which is NOT necessarily the adapter you just
 * deployed. A fleet can serve several at once (`captcha`, `captcha-v12`, …),
 * and picking the wrong one is silent: the prompts and the weights come from
 * different generations and every puzzle just answers worse. Naming the model
 * here targets a specific adapter without editing the installed package.
 */
const MODEL_ENV = "CAPTCHA_LORA_NAME";

/** Thrown when the optional `captchakraken` package is not installed. */
export class CaptchaSolverUnavailable extends Error {
	constructor(message: string, options?: { cause?: unknown }) {
		super(message, options);
		this.name = "CaptchaSolverUnavailable";
	}
}

/** Thrown for a `captcha` option naming neither an endpoint nor a key. */
export class CaptchaCredentialsError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "CaptchaCredentialsError";
	}
}

/** Resolved answer to "where do solves go, and as whom". */
export interface CaptchaConfig {
	baseUrl?: string;
	apiKey?: string;
	model?: string;
}

/** What the `captcha` launch option accepts. */
export type CaptchaOption =
	| boolean
	| string
	| {
			token?: string;
			url?: string;
			apiKey?: string;
			baseUrl?: string;
			model?: string;
	  };

/**
 * Normalise the `captcha` launch option into a {@link CaptchaConfig}.
 *
 * ```
 * "ck_live_…"                    a cloud token   -> hosted endpoint
 * "http://localhost:8000/v1"     a self-hosted endpoint
 * { token }                      same as the first
 * { url, token }                 self-hosted behind an auth proxy
 * true                           defer to the environment / credentials file
 * ```
 *
 * A token with no URL resolves to {@link HOSTED_BASE_URL} — the "camoufox
 * queries CaptchaKraken by default when you give it a token" rule, and the
 * reason this exists rather than passing the values straight through.
 */
export function resolveConfig(option: CaptchaOption): CaptchaConfig {
	if (option === true) return {};
	if (option === false) return {};

	let spec: {
		token?: string;
		url?: string;
		apiKey?: string;
		baseUrl?: string;
		model?: string;
	};
	if (typeof option === "string") {
		const value = option.trim();
		if (!value) {
			throw new CaptchaCredentialsError("captcha was given an empty string.");
		}
		// A key is `ck_live_…`; anything with a scheme is an endpoint. Telling
		// them apart lets the shorthand take either.
		spec = value.includes("://") ? { url: value } : { token: value };
	} else {
		spec = option;
	}

	const allowed = new Set(["token", "url", "apiKey", "baseUrl", "model"]);
	const unknown = Object.keys(spec).filter((k) => !allowed.has(k));
	if (unknown.length > 0) {
		throw new CaptchaCredentialsError(
			`Unknown captcha option(s): ${unknown.sort().join(", ")}. ` +
				"Use 'token' for a cloud key and 'url' for a self-hosted endpoint.",
		);
	}

	const token = spec.token ?? spec.apiKey;
	const url = spec.url ?? spec.baseUrl;
	const model = spec.model;

	// A model alone is a legitimate config: it says "the endpoint and key are
	// already in my environment, just point me at a different adapter".
	if (!token && !url && model) return { model };

	if (!token && !url) {
		throw new CaptchaCredentialsError(
			"captcha needs either a cloud token or a self-hosted URL.\n" +
				"  Camoufox({ captcha: { token: 'ck_live_…' } })         // hosted\n" +
				"  Camoufox({ captcha: { url: 'http://host:8000/v1' } }) // self-hosted\n" +
				"Get a key at https://captchakraken.com, or pass captcha: true to use " +
				"the CAPTCHA_KRAKEN_API_KEY / VLLM_BASE_URL already in your environment.",
		);
	}

	// The rule the whole option exists for: a token alone means the cloud.
	return { baseUrl: url ?? HOSTED_BASE_URL, apiKey: token, model };
}

let registered: CaptchaConfig | undefined;

/** Register the config every later `solveCaptcha` should use (null clears). */
export function configure(
	option: CaptchaOption | null,
): CaptchaConfig | undefined {
	registered =
		option === null || option === false ? undefined : resolveConfig(option);
	return registered;
}

/** The config registered by the last `Camoufox({ captcha })`, if any. */
export function configured(): CaptchaConfig | undefined {
	return registered;
}

/** CaptchaKraken's `SolveResult`, redeclared so this module typechecks without the optional dep. */
export interface SolveResult {
	isSolved: boolean;
	finalMousePosition: { x: number; y: number };
	tokenUsage: {
		modelName: string;
		inputTokens: number;
		outputTokens: number;
		cachedInputTokens: number;
		estimatedCost: number;
	};
}

interface CaptchaWatcherHandle {
	stop(): Promise<void>;
	readonly running: boolean;
	readonly solves: number;
}

interface CaptchaKrakenModule {
	CaptchaKrakenSolver: new (
		config?: Record<string, unknown>,
	) => {
		solve(page: unknown): Promise<SolveResult | undefined>;
		detectCaptcha(page: unknown): Promise<unknown | null>;
	};
	watchPage: (
		solver: {
			detectCaptcha(page: unknown): Promise<unknown | null>;
			solve(page: unknown): Promise<SolveResult | undefined>;
		},
		page: unknown,
		options?: Record<string, unknown>,
	) => CaptchaWatcherHandle;
}

/** The attribution tag this camoufox build reports, e.g. `camoufox/0.5.4`. */
export function clientTag(): string {
	return `camoufox/${LIBRARY_VERSION}`;
}

// Unlike the Python twin — which is synchronous, so a solve can never overlap
// another — `solve()` here is async and callers legitimately drive several
// pages at once. A naive try/finally would let the FIRST solve to finish
// restore the environment out from under the others, silently dropping the
// attribution tag on every in-flight request. Depth-count instead and restore
// only when the last solve unwinds. Every solve writes the same values, so the
// shared snapshot is safe to share; only the teardown had to be made reentrant.
const MANAGED_ENV = [CLIENT_ENV, BASE_URL_ENV, API_KEY_ENV, MODEL_ENV] as const;
let applyDepth = 0;
let savedEnv: Record<string, string | undefined> = {};

/**
 * Apply the attribution tag, plus the launch-time endpoint/key if registered.
 *
 * Scoped to the solve rather than written once at launch: a launcher that
 * permanently mutated `process.env` would silently redirect any OTHER
 * CaptchaKraken usage in the same program.
 *
 * An explicit env var still wins — we only set what is not already there, which
 * is the precedence CaptchaKraken's own base-URL lookup documents.
 */
async function applied<T>(fn: () => Promise<T>): Promise<T> {
	if (applyDepth === 0) {
		savedEnv = {};
		for (const key of MANAGED_ENV) savedEnv[key] = process.env[key];
	}
	applyDepth += 1;

	process.env[CLIENT_ENV] = clientTag();
	const config = registered;
	if (config?.baseUrl && !savedEnv[BASE_URL_ENV]) {
		process.env[BASE_URL_ENV] = config.baseUrl;
	}
	if (config?.apiKey && !savedEnv[API_KEY_ENV]) {
		process.env[API_KEY_ENV] = config.apiKey;
	}
	if (config?.model && !savedEnv[MODEL_ENV]) {
		process.env[MODEL_ENV] = config.model;
	}

	try {
		return await fn();
	} finally {
		applyDepth -= 1;
		if (applyDepth === 0) {
			for (const key of MANAGED_ENV) {
				const was = savedEnv[key];
				if (was === undefined) delete process.env[key];
				else process.env[key] = was;
			}
			savedEnv = {};
		}
	}
}

/**
 * Check the configured endpoint answers, the key is accepted, and the account
 * has credit — before a solve rather than in the middle of one.
 *
 * Asks for the model list, the cheapest authenticated call the endpoint serves.
 * Resolves to a report rather than rejecting on a bad key: "your key is
 * invalid" is an answer, not a failure to get one. A 402 is the credits signal.
 */
export async function verifyCredentials(option?: CaptchaOption): Promise<{
	ok: boolean;
	status: number | null;
	baseUrl: string;
	models?: string[];
	error?: string;
}> {
	const config =
		option !== undefined ? resolveConfig(option) : (registered ?? {});
	const baseUrl = (
		config.baseUrl ??
		process.env[BASE_URL_ENV] ??
		HOSTED_BASE_URL
	).replace(/\/+$/, "");
	const key = config.apiKey ?? process.env[API_KEY_ENV];

	const headers: Record<string, string> = { "User-Agent": clientTag() };
	if (key) headers.Authorization = `Bearer ${key}`;

	try {
		const response = await fetch(`${baseUrl}/models`, {
			method: "GET",
			headers,
			signal: AbortSignal.timeout(15_000),
		});
		if (!response.ok) {
			const hint: Record<number, string> = {
				401: "The key was rejected. Check it, or issue a new one from your dashboard.",
				402: "Out of credit — top up at https://captchakraken.com before solving.",
				404: "Endpoint not found. A self-hosted URL must end in /v1.",
			};
			return {
				ok: false,
				status: response.status,
				baseUrl,
				error: hint[response.status] ?? (await response.text()).slice(0, 400),
			};
		}
		const body = (await response.json()) as { data?: { id?: string }[] };
		return {
			ok: true,
			status: response.status,
			baseUrl,
			models: (body.data ?? []).map((m) => m.id ?? ""),
		};
	} catch (error) {
		return {
			ok: false,
			status: null,
			baseUrl,
			error: error instanceof Error ? error.message : String(error),
		};
	}
}

async function loadSolver(): Promise<CaptchaKrakenModule> {
	// Indirect specifier on purpose: `captchakraken` is optional and usually
	// absent, and a literal would make `tsc` demand its types at build time.
	const specifier = "captchakraken";
	try {
		return (await import(specifier)) as CaptchaKrakenModule;
	} catch (cause) {
		throw new CaptchaSolverUnavailable(
			"CAPTCHA solving needs the optional CaptchaKraken dependency.\n" +
				"  npm install captchakraken\n" +
				"Docs: https://github.com/JWriter20/CaptchaKraken",
			{ cause },
		);
	}
}

/**
 * Solve whatever captcha is currently on `page`.
 *
 * `page` is a Playwright page — what `Camoufox()` hands you via `newPage()`.
 * `config` is forwarded to `CaptchaKrakenSolver`; the useful keys are the
 * timeout and retry budgets, and `apiKey` for the hosted API.
 *
 * Resolves to CaptchaKraken's `SolveResult`, or `undefined` when the driver
 * completed without producing one. Rejects with `CaptchaSolverUnavailable` if
 * the optional package is not installed, or with one of CaptchaKraken's own
 * errors, which are worth catching separately because they mean genuinely
 * different things about the page.
 *
 * Credentials come from the `captcha` option the browser was launched with,
 * unless the environment already sets them.
 */
export async function solveCaptcha(
	page: unknown,
	config?: Record<string, unknown>,
): Promise<SolveResult | undefined> {
	const { CaptchaKrakenSolver } = await loadSolver();
	return applied(() => new CaptchaKrakenSolver(config).solve(page));
}

/**
 * Install an auto-solver on `page`: captchas are solved as they appear, until
 * `stop()`.
 *
 * Returns immediately — watching happens in the background, so your automation
 * carries on and challenges are handled underneath it.
 *
 * ```ts
 * const browser = await Camoufox({ captcha: "ck_live_…" });
 * const page = await browser.newPage();
 * const watcher = await watchCaptcha(page);
 * await page.goto("https://example.com/protected");   // solved as it appears
 * await watcher.stop();
 * ```
 *
 * ISOLATED WORLD, FOR FREE
 * The watcher injects nothing into the page — it drives CaptchaKraken's own
 * `detectCaptcha()` from the driver side on a timer. Under Camoufox the DOM
 * reads that probe performs run in the sandboxed Juggler world, because that is
 * Camoufox's default for ALL Playwright evaluation (`main_world_eval` and an
 * "mw:" prefix are the opt-OUT). Nothing here opts out, so the page cannot see
 * the watcher any more than it can see Playwright itself.
 *
 * WHY EACH SOLVE IS WRAPPED, NOT THE INSTALL
 * `applied()` restores the environment when its callback unwinds. Wrapping the
 * INSTALLATION would apply the attribution tag and the launch-time credentials
 * for the microsecond it takes to return the handle and restore them long
 * before the first captcha ever appears — every later solve would then run
 * untagged, and a hosted user's key would be missing entirely. Wrapping each
 * solve puts them in scope exactly when a request is made. `applied()` is
 * depth-counted, so concurrent watchers on several pages nest safely.
 */
export async function watchCaptcha(
	page: unknown,
	options?: Record<string, unknown>,
	config?: Record<string, unknown>,
): Promise<CaptchaWatcherHandle> {
	const { CaptchaKrakenSolver, watchPage } = await loadSolver();
	return watchPage(tagged(new CaptchaKrakenSolver(config)), page, options);
}

/**
 * Wrap a solver so every SOLVE runs inside `applied()`.
 *
 * Exported for tests: this is the whole of what `watchCaptcha` adds over
 * `watchPage`, and testing it directly is far steadier than mocking the
 * optional package and asserting on a loop that belongs to CaptchaKraken.
 * Twin of the Python bridge's `_TaggedSolver`.
 */
export function tagged(solver: {
	detectCaptcha(page: unknown): Promise<unknown | null>;
	solve(page: unknown): Promise<SolveResult | undefined>;
}): {
	detectCaptcha(page: unknown): Promise<unknown | null>;
	solve(page: unknown): Promise<SolveResult | undefined>;
} {
	return {
		// Pure DOM reads — no request leaves the process, so no tag is needed.
		detectCaptcha: (page: unknown) => solver.detectCaptcha(page),
		solve: (page: unknown) => applied(() => solver.solve(page)),
	};
}
