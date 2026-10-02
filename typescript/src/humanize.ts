/**
 * Per-channel humanized input: the engine factories and the launch-time config.
 * TypeScript twin of pythonlib/camoufox/humanize.py.
 *
 *     import { Camoufox, cursory, raw } from "@camoufox/camoufox";
 *     await Camoufox({ humanize: { mouse: cursory({ maxTime: 1.0 }), scroll: raw(), seed: 1234 } });
 *
 * The browser intercepts the Juggler input commands Playwright already sends,
 * so the same Playwright script drives every build. Each channel (mouse,
 * keyboard, scroll) names the engine that plans its input. Which engines
 * exist, and which options each takes, is the build's humanize-engines.json
 * (docs/humanize.md); `engine(name, options)` names any of them. A factory
 * returns plain data, never behaviour.
 *
 * A channel may also run a client-side engine, `custom(fn)`: a function in this
 * process plans the channel's input and plays it through Playwright's own
 * input methods, with schedule pacing and a seeded stream (humanize-custom.ts).
 * The browser then runs that channel raw, so input is never humanized twice.
 *
 * `humanize` itself may also be:
 *
 *     undefined / false   every channel raw(): Playwright's own dispatch
 *     true                every channel auto()
 *     a number            { mouse: cursory({ maxTime: <number> }) }, the rest auto()
 *     an object           per channel; an omitted channel is auto()
 */
import { HumanizeEngineUnavailable } from "./exceptions.js";
import { PyFloat, pyRepr, ValueError } from "./pycompat.js";
import { LeakWarning } from "./warnings.js";

export const CHANNELS = ["mouse", "keyboard", "scroll"] as const;
export type Channel = (typeof CHANNELS)[number];

export interface Engine {
	engine: string;
	options: Record<string, number>;
	/** The function of a custom() engine. */
	fn?: CustomEngineFn;
}

/** A client-side engine's function. Per channel it receives:
 *
 *     mouse     (page, x, y, { original, rng, play })
 *     keyboard  (page, text, { original, rng, play, kind })  kind: "type" | "press" | "fill"
 *     scroll    (page, target, { original, rng, play })       target: a Locator or [dx, dy]
 */
export type CustomEngineFn = (...args: any[]) => unknown;

export type HumanizeSetting =
	| undefined
	| null
	| boolean
	| number
	| {
			mouse?: Engine | string;
			keyboard?: Engine | string;
			scroll?: Engine | string;
			seed?: number | bigint;
	  };

interface OptionSpec {
	key: string;
	min?: number;
	max?: number;
}

/** A build's humanize-engines.json. */
export type HumanizeManifest = Partial<Record<Channel, string[]>> & {
	version?: number;
	auto?: Partial<Record<Channel, string[]>>;
	engines?: Record<
		string,
		{
			module?: string;
			movesCursor?: boolean;
			options?: Record<string, OptionSpec>;
		}
	>;
};

// What a build without humanize-engines.json runs. It predates per-channel
// humanize: it reads only `humanize` (cursory mouse plus notched wheel) and
// humanize:maxTime / minTime, so it runs every base engine or none.
export const LEGACY_MANIFEST: HumanizeManifest = {
	version: 0,
	mouse: ["raw", "cursory"],
	keyboard: ["raw"],
	scroll: ["raw", "notches"],
	auto: { mouse: ["cursory"], keyboard: ["raw"], scroll: ["notches"] },
	engines: {
		cursory: {
			options: {
				maxTime: { key: "humanize:maxTime", min: 0 },
				minTime: { key: "humanize:minTime", min: 0 },
			},
		},
		notches: {},
	},
};

const isNumber = (value: unknown): value is number =>
	typeof value === "number" && !Number.isNaN(value);

/** Any engine the build's humanize-engines.json lists, with its options. The
 *  options are checked against the manifest at launch. */
export function engine(
	name: string,
	options: Record<string, number> = {},
): Engine {
	return { engine: name, options };
}

/** The first engine the build lists for the channel that is available. */
export function auto(): Engine {
	return engine("auto");
}

/** Playwright's own dispatch, byte for byte. */
export function raw(): Engine {
	return engine("raw");
}

/** Mouse: replayed human cursor paths (Cursory), scaled into [minTime, maxTime]
 *  seconds. Defaults: 0 and 1.5. */
export function cursory(
	options: { maxTime?: number; minTime?: number } = {},
): Engine {
	return engine(
		"cursory",
		Object.fromEntries(
			Object.entries(options).filter(([, v]) => v !== undefined && v !== null),
		) as Record<string, number>,
	);
}

/** Scroll: a wheel call arrives as native notches of 3 lines, tens of ms apart. */
export function notches(): Engine {
	return engine("notches");
}

/** A client-side engine: `fn` plans the channel's input in this process and
 *  plays it through Playwright. `original` runs Playwright's own behaviour for
 *  the call, `rng` is the channel's seeded stream (the browser's own
 *  derivation), and `play(steps)` dispatches steps on a schedule. */
export function custom(fn: CustomEngineFn): Engine {
	if (typeof fn !== "function") {
		throw new ValueError(
			`custom() takes the function that plans the input, got ${pyRepr(fn)}`,
		);
	}
	return { engine: "custom", options: {}, fn };
}

function asEngine(channel: Channel, value: unknown): Engine {
	const candidate = typeof value === "string" ? engine(value) : value;
	if (
		candidate === null ||
		typeof candidate !== "object" ||
		Array.isArray(candidate) ||
		typeof (candidate as Engine).engine !== "string"
	) {
		throw new ValueError(
			`humanize["${channel}"] must be an engine such as auto(), raw(), cursory() or custom(fn), got ${pyRepr(value)}`,
		);
	}
	const name = (candidate as Engine).engine;
	if (name === "custom") {
		const fn = (candidate as Engine).fn;
		if (typeof fn !== "function") {
			throw new ValueError(
				`humanize["${channel}"]: a custom engine needs its function, as custom(fn)`,
			);
		}
		if (Object.keys((candidate as Engine).options ?? {}).length) {
			throw new ValueError(
				`humanize["${channel}"]: custom() takes no options; the function is the engine`,
			);
		}
		return { engine: "custom", options: {}, fn };
	}
	const options = { ...((candidate as Engine).options ?? {}) };
	for (const [key, option] of Object.entries(options)) {
		if (!isNumber(option)) {
			throw new ValueError(
				`${name}() option ${key} must be a number, got ${pyRepr(option)}`,
			);
		}
	}
	return { engine: name, options };
}

/** The four channel values of a `humanize` setting: the engine per channel, and
 *  the seed (undefined when not given). */
export function normalize(humanize: HumanizeSetting): {
	engines: Record<Channel, Engine>;
	seed?: bigint;
} {
	const all = (make: () => Engine) =>
		Object.fromEntries(CHANNELS.map((c) => [c, make()])) as Record<
			Channel,
			Engine
		>;
	const isObject =
		typeof humanize === "object" &&
		humanize !== null &&
		!Array.isArray(humanize);
	if (!humanize && !isObject) return { engines: all(raw) };
	if (humanize === true) return { engines: all(auto) };
	if (isNumber(humanize)) {
		return {
			engines: {
				mouse: cursory({ maxTime: humanize }),
				keyboard: auto(),
				scroll: auto(),
			},
		};
	}
	if (!isObject) {
		throw new ValueError(
			`humanize must be None, a bool, a number or a dict, got ${pyRepr(humanize)}`,
		);
	}
	const setting = humanize as Record<string, unknown>;
	const unknown = Object.keys(setting)
		.filter((k) => !(CHANNELS as readonly string[]).includes(k) && k !== "seed")
		.sort();
	if (unknown.length) {
		throw new ValueError(
			`humanize has no channel ${pyRepr(unknown)}; the channels are ('mouse', 'keyboard', 'scroll') and "seed"`,
		);
	}
	let seed: bigint | undefined;
	const rawSeed = setting.seed;
	if (rawSeed !== undefined && rawSeed !== null) {
		const valid =
			(typeof rawSeed === "bigint" && rawSeed >= 0n && rawSeed < 2n ** 64n) ||
			(typeof rawSeed === "number" &&
				Number.isSafeInteger(rawSeed) &&
				rawSeed >= 0);
		if (!valid) {
			throw new ValueError(
				`humanize["seed"] must be an integer in [0, 2**64), got ${pyRepr(rawSeed)}`,
			);
		}
		seed = BigInt(rawSeed as number | bigint);
	}
	const engines = Object.fromEntries(
		CHANNELS.map((c) => [c, asEngine(c, setting[c] ?? "auto")]),
	) as Record<Channel, Engine>;
	return { engines, seed };
}

/** An engine's options as config keys, checked against what the manifest declares. */
function optionKeys(
	name: string,
	options: Record<string, number>,
	manifest: HumanizeManifest,
): Record<string, number> {
	const declared = manifest.engines?.[name]?.options ?? {};
	const keys: Record<string, number> = {};
	for (const [option, value] of Object.entries(options)) {
		const spec = declared[option];
		if (!spec) {
			throw new ValueError(
				`${name}() has no option ${pyRepr(option)}; it takes ${pyRepr(Object.keys(declared).sort())}`,
			);
		}
		const low = spec.min ?? 0;
		const high = spec.max;
		if (value < low || (high !== undefined && value > high)) {
			const bounds =
				high !== undefined ? `in [${low}, ${high}]` : `at least ${low}`;
			throw new ValueError(
				`${name}() ${option} must be ${bounds}, got ${pyRepr(value)}`,
			);
		}
		keys[spec.key] = value;
	}
	return keys;
}

/**
 * The camoucfg keys for a `humanize` setting on a build whose
 * humanize-engines.json is `manifest` (null for a build without one).
 *
 * Throws HumanizeEngineUnavailable for an engine the build does not ship.
 */
export function humanizeConfig(
	humanize: HumanizeSetting,
	manifest: HumanizeManifest | null,
	iKnowWhatImDoing?: boolean,
): Record<string, unknown> {
	const { engines, seed } = normalize(humanize);
	// A custom channel runs client-side, so the browser runs it raw.
	const names = Object.fromEntries(
		CHANNELS.map((c) => [
			c,
			engines[c].engine === "custom" ? "raw" : engines[c].engine,
		]),
	) as Record<Channel, string>;
	const legacy = manifest === null;
	const available = manifest ?? LEGACY_MANIFEST;

	for (const channel of CHANNELS) {
		const listed = available[channel] ?? [];
		const name = names[channel];
		if (name !== "auto" && name !== "raw" && !listed.includes(name)) {
			throw new HumanizeEngineUnavailable(channel, name, listed);
		}
	}

	// A custom mouse leaves the browser's own cursor moves to mouse:internal.
	if (
		available.engines?.[names.scroll]?.movesCursor &&
		engines.mouse.engine === "raw"
	) {
		LeakWarning.warn("humanize_scroll_teleports", iKnowWhatImDoing);
	}

	const options: Record<string, number> = {};
	for (const channel of CHANNELS) {
		const keys = optionKeys(
			names[channel],
			engines[channel].options,
			available,
		);
		for (const [key, value] of Object.entries(keys)) {
			const existing = options[key];
			if (existing !== undefined && existing !== value) {
				throw new ValueError(
					`humanize sets ${key} twice, to ${pyRepr(new PyFloat(existing))} and ${pyRepr(new PyFloat(value))}`,
				);
			}
			options[key] = value;
		}
	}
	// Python writes these as floats: 1 -> 1.0.
	const floats = Object.fromEntries(
		Object.entries(options).map(([k, v]) => [k, new PyFloat(v)]),
	);

	const enabled = CHANNELS.some((c) => names[c] !== "raw");
	if (legacy) {
		const on = Object.fromEntries(
			CHANNELS.map((c) => [c, available.auto?.[c]?.[0] ?? "raw"]),
		) as Record<Channel, string>;
		const unsupported = CHANNELS.find(
			(c) => names[c] !== "auto" && names[c] !== on[c],
		);
		if (enabled && unsupported) {
			throw new HumanizeEngineUnavailable(
				unsupported,
				names[unsupported],
				[on[unsupported]],
				"this build predates per-channel humanize: it runs every base engine or none",
			);
		}
		if (seed !== undefined) {
			throw new HumanizeEngineUnavailable(
				"seed",
				String(seed),
				[],
				"this build predates per-channel humanize and has no seeded input",
			);
		}
		return enabled ? { humanize: true, ...floats } : {};
	}

	// Every key on every launch, so nothing depends on what an earlier launch set.
	const config: Record<string, unknown> = { humanize: enabled };
	for (const channel of CHANNELS)
		config[`humanize:${channel}`] = names[channel];
	if (engines.mouse.engine === "custom") {
		// Moves the browser originates (the cursor move before a planned
		// scroll) still get a humanized path, not a jump.
		config["humanize:mouse:internal"] = "auto";
	}
	Object.assign(config, floats);
	if (seed !== undefined) {
		// A string: Juggler reads config numbers as doubles, which cannot hold
		// every 64-bit seed exactly.
		config["humanize:seed"] = String(seed);
	}
	return config;
}

/** The functions of a `humanize` setting's custom channels, by channel. */
export function customEngines(
	humanize: HumanizeSetting,
): Partial<Record<Channel, CustomEngineFn>> {
	const { engines } = normalize(humanize);
	return Object.fromEntries(
		CHANNELS.filter((c) => engines[c].engine === "custom").map((c) => [
			c,
			engines[c].fn as CustomEngineFn,
		]),
	);
}

// The seeded streams. The browser draws its engines' randomness from the same
// derivation (additions/juggler/input/HumanizeRng.js), so a custom engine gets
// the stream a built-in engine on that channel would:
//
//   stream(channel) = Mulberry32(splitmix64(seed ^ TAG[channel]) & 0xffffffff)

const MASK64 = (1n << 64n) - 1n;

export const CHANNEL_TAGS: Readonly<Record<Channel, bigint>> = Object.freeze({
	mouse: 0x6d6f7573n,
	keyboard: 0x6b657962n,
	scroll: 0x7363726fn,
});

/** One splitmix64 output for the 64-bit state `x`. */
export function splitmix64(x: bigint): bigint {
	let z = (BigInt.asUintN(64, x) + 0x9e3779b97f4a7c15n) & MASK64;
	z = ((z ^ (z >> 30n)) * 0xbf58476d1ce4e5b9n) & MASK64;
	z = ((z ^ (z >> 27n)) * 0x94d049bb133111ebn) & MASK64;
	return z ^ (z >> 31n);
}

/** Mulberry32 over a 32-bit seed. Calling it returns a float in [0, 1), bit
 *  for bit what the browser's stream returns; `position` counts the draws. */
export interface SeededRng {
	(): number;
	random(): number;
	uniform(low: number, high: number): number;
	position: number;
}

export function seededRng(seed32: number): SeededRng {
	let a = seed32 | 0;
	const rng = (() => {
		rng.position++;
		a = (a + 0x6d2b79f5) | 0;
		let t = Math.imul(a ^ (a >>> 15), 1 | a);
		t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	}) as SeededRng;
	rng.position = 0;
	rng.random = () => rng();
	rng.uniform = (low, high) => low + (high - low) * rng();
	return rng;
}

/** The stream a channel draws from for the 64-bit master `seed`. */
export function channelStream(seed: bigint, channel: Channel): SeededRng {
	const tag = CHANNEL_TAGS[channel];
	if (tag === undefined)
		throw new ValueError(`unknown humanize channel: ${channel}`);
	return seededRng(
		Number(splitmix64(BigInt.asUintN(64, seed) ^ tag) & 0xffffffffn),
	);
}
