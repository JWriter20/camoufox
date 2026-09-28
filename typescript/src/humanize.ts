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
}

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

function asEngine(channel: Channel, value: unknown): Engine {
	const candidate = typeof value === "string" ? engine(value) : value;
	if (
		candidate === null ||
		typeof candidate !== "object" ||
		Array.isArray(candidate) ||
		typeof (candidate as Engine).engine !== "string"
	) {
		throw new ValueError(
			`humanize["${channel}"] must be an engine such as auto(), raw() or cursory(), got ${pyRepr(value)}`,
		);
	}
	const name = (candidate as Engine).engine;
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
	const names = Object.fromEntries(
		CHANNELS.map((c) => [c, engines[c].engine]),
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

	if (available.engines?.[names.scroll]?.movesCursor && names.mouse === "raw") {
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
	Object.assign(config, floats);
	if (seed !== undefined) {
		// A string: Juggler reads config numbers as doubles, which cannot hold
		// every 64-bit seed exactly.
		config["humanize:seed"] = String(seed);
	}
	return config;
}
