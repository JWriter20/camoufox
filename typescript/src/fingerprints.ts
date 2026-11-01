/**
 * The identity layer: fpgen fingerprints and real presets turned into a
 * Camoufox config, the per-identity draws (fonts, voices, media devices, GPU)
 * and the geometry / arch corrections applied on top of both.
 *
 * TypeScript twin of python/src/camoufox/fingerprints.py. Every seeded draw is
 * bit-for-bit the Python one -- `random.Random(seed)` is the python-random package, and
 * identitySalt() hashes the same orjson bytes -- so one config and salt
 * present one identity whichever launcher built it. Unseeded Python draws
 * (`random.randint`, `random.choice`) go through python-random's
 * module-level functions, the twin of Python's module-level generator.
 */
import { createHash, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { generate, InvalidConstraints } from "fpgen-js";
import * as random from "python-random";
import {
	supported as cpuAffinitySupported,
	hostCoreCount,
} from "./cpu_affinity.js";
import { validateIP, validIPv4 } from "./ip.js";
import { normalizeLocale } from "./locales.js";
import { LAUNCHER_CONSTANTS, LOCAL_DATA, loadDataFile } from "./paths.js";
import {
	comparePyStr,
	crc32,
	isPlainObject,
	isPyError,
	KeyError,
	num,
	orjsonDumps,
	pyStr,
	pySum,
	pySumFloats,
	pyTruthy,
	ValueError,
} from "./pycompat.js";
import { FallbackWarning, loadWarnings, warn } from "./warnings.js";
import { sampleWebglForScreen, type TargetOS, webglForGpu } from "./webgl.js";

export type { TargetOS } from "./webgl.js";

type Config = Record<string, any>;

export const SUPPORTED_OS = ["linux", "macos", "windows"] as const;
export type SupportedOS = (typeof SUPPORTED_OS)[number];

/** The fpgen -> Camoufox config mapping. */
export const FPGEN_DATA: Readonly<Record<string, Record<string, string>>> =
	loadDataFile("fpgen.yml");
// fpgen's OS names, from Camoufox's.
export const FPGEN_OS: Readonly<Record<string, string>> = {
	lin: "Linux",
	linux: "Linux",
	mac: "macOS",
	macos: "macOS",
	win: "Windows",
	windows: "Windows",
};

/** Python dict.get(key, default): the default only when the key is absent. */
function get(config: Config, key: string, dflt: any = undefined): any {
	return Object.hasOwn(config, key) ? config[key] : dflt;
}

function isNone(value: unknown): boolean {
	return value === null || value === undefined;
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export interface ScreenBounds {
	minWidth?: number | null;
	maxWidth?: number | null;
	minHeight?: number | null;
	maxHeight?: number | null;
}

/**
 * A bound on the screen a generated fingerprint may claim (the twin of the
 * Python dataclass that replaced browserforge's Screen).
 */
export class Screen {
	minWidth: number | null;
	maxWidth: number | null;
	minHeight: number | null;
	maxHeight: number | null;

	constructor({
		minWidth = null,
		maxWidth = null,
		minHeight = null,
		maxHeight = null,
	}: ScreenBounds = {}) {
		this.minWidth = minWidth;
		this.maxWidth = maxWidth;
		this.minHeight = minHeight;
		this.maxHeight = maxHeight;
	}

	/**
	 * The bound as fpgen conditions: one predicate over the whole screen node.
	 * fpgen resolves screen.width and screen.height to that same node and each
	 * condition resets its evidence (build_evidence), so with one per field
	 * only the height bound held and 1280x1024 met a 1300 px minimum width.
	 */
	asConditions(): Record<string, (value: unknown) => boolean> {
		const {
			minWidth: loW,
			maxWidth: hiW,
			minHeight: loH,
			maxHeight: hiH,
		} = this;
		if (loW === null && hiW === null && loH === null && hiH === null) return {};
		const within = (v: unknown, lo: number | null, hi: number | null) =>
			typeof v === "number" &&
			Number.isInteger(v) &&
			(lo === null || v >= lo) &&
			(hi === null || v <= hi);
		return {
			screen: (s) => {
				if (typeof s !== "object" || s === null) return false;
				const { width, height } = s as Record<string, unknown>;
				return within(width, loW, hiW) && within(height, loH, hiH);
			},
		};
	}

	/** dataclasses.asdict(), with the Python field names (for identitySalt). */
	toPyDict(): Record<string, number | null> {
		return {
			min_width: this.minWidth,
			max_width: this.maxWidth,
			min_height: this.minHeight,
			max_height: this.maxHeight,
		};
	}
}

// ---------------------------------------------------------------------------
// Presets
// ---------------------------------------------------------------------------

export interface Preset {
	navigator?: Record<string, any>;
	screen?: Record<string, any>;
	webgl?: Record<string, any>;
	timezone?: string;
	fonts?: string[];
	speechVoices?: Array<string | VoiceObject>;
	[key: string]: any;
}

export interface PresetBundle {
	presets?: Partial<Record<SupportedOS, Preset[]>>;
	[key: string]: any;
}

export const PRESETS_FILE = path.join(LOCAL_DATA, "fingerprint-presets.json");
export const PRESETS_V150_FILE = path.join(
	LOCAL_DATA,
	"fingerprint-presets-v150.json",
);
/** Firefox major version at which the v150 preset bundle becomes preferred. */
export const PRESETS_V150_MIN_FF = 149;
const PRESETS_CACHE = new Map<string, PresetBundle>();

// ---------------------------------------------------------------------------
// Fonts
// ---------------------------------------------------------------------------

// CreepJS OS marker fonts used for OS detection (see fingerprints.py).
export const MACOS_MARKER_FONTS: readonly string[] =
	LAUNCHER_CONSTANTS.markerFonts.mac;
export const LINUX_MARKER_FONTS: readonly string[] =
	LAUNCHER_CONSTANTS.markerFonts.lin;
export const WINDOWS_MARKER_FONTS: readonly string[] =
	LAUNCHER_CONSTANTS.markerFonts.win;

/** Add any missing marker fonts to the font list (in place). */
function ensureMarkerFonts(fonts: string[], markers: readonly string[]): void {
	const existing = new Set(fonts);
	for (const m of markers) {
		if (!existing.has(m)) fonts.push(m);
	}
}

let osFontsCache: Record<string, string[]> | null = null;

/** The full OS font lists (fonts.json). */
export function loadOsFonts(): Record<string, string[]> {
	osFontsCache ??= loadDataFile<Record<string, string[]>>("fonts.json");
	return osFontsCache;
}

// The OS BASE font sets that must always be reported: every family a real
// machine of that OS ships by default, intersected with fonts.json (see
// fingerprints.py for the sources). essential-fonts.json is the file
// pythonlib reads, so the two launchers cannot disagree on them.
const ESSENTIAL_FONTS = loadDataFile<Record<"win" | "mac" | "lin", string[]>>(
	"essential-fonts.json",
);
export const ESSENTIAL_FONTS_MACOS: readonly string[] = ESSENTIAL_FONTS.mac;
export const ESSENTIAL_FONTS_WINDOWS: readonly string[] = ESSENTIAL_FONTS.win;
export const ESSENTIAL_FONTS_LINUX: readonly string[] = ESSENTIAL_FONTS.lin;

/**
 * Fonts only a Windows 11 base has: a Windows identity whose font list
 * contains them presents Windows 11, and the rest of the identity must agree.
 */
export const WINDOWS_11_MARKER_FONTS: ReadonlySet<string> = new Set(
	LAUNCHER_CONSTANTS.windows11MarkerFonts,
);

/**
 * The entropy that makes identitySeed() belong to ONE identity.
 *
 * Pass whatever the caller pinned the identity with -- an fpgen fingerprint,
 * a preset, the caller's own config -- for a salt that is stable across
 * launches of that identity; pass nothing for a fresh, random one. The salt
 * is the first 8 bytes (big-endian) of the SHA-256 of the orjson
 * serialization with sorted keys, exactly as Python computes it, so a pinned
 * identity draws the same fonts, voices, GPU and noise seeds in both
 * launchers. (Integral floats and integers above 2**53 only hash like
 * Python's when they arrive as PyFloat / bigint -- see pycompat.parsePyJson.)
 */
export function identitySalt(pinned?: unknown): bigint {
	if (pinned === null || pinned === undefined) {
		return randomBytes(8).readBigUInt64BE(0);
	}
	const blob = Buffer.from(orjsonDumps(pinned), "utf-8");
	return createHash("sha256").update(blob).digest().readBigUInt64BE(0);
}

/** The browser's version in a UA: "rv:158.0" and "Firefox/158.0". */
const UA_VERSION = /(rv:|Firefox\/)[0-9][0-9.]*/g;

/**
 * The UA with the browser's version taken out, for identitySeed(). A saved
 * identity on a newer browser claims the new engine version and nothing else
 * changes, so the seed must not move with it (Python: versionless_ua).
 */
export function versionlessUa(userAgent: string): string {
	return userAgent.replace(UA_VERSION, "$1");
}

/**
 * A seed for the per-identity draws: a pure function of the presented
 * identity (UA without its version, platform, screen, cores) and its salt --
 * zlib.crc32 of the values' Python str() joined by "|".
 */
export function identitySeed(
	config: Config,
	salt: number | bigint = 0,
): number {
	const parts = [
		versionlessUa(pyStr(get(config, "navigator.userAgent", ""))),
		pyStr(get(config, "navigator.platform", "")),
		pyStr(get(config, "screen.width", "")),
		pyStr(get(config, "screen.height", "")),
		pyStr(get(config, "navigator.hardwareConcurrency", "")),
		// not the GPU: it is sampled after the font draw in launchOptions
		pyStr(salt),
	];
	return crc32(parts.join("|"));
}

/**
 * audio:seed is gone: the browser renders audio as stock Firefox does. A
 * caller's seed is dropped with a warning rather than sent to a browser that
 * would noise with it (a released one) or ignore it (this one).
 */
export function dropAudioSeed(config: Record<string, unknown>): void {
	if ("audio:seed" in config) {
		delete config["audio:seed"];
		warn(loadWarnings().audio_seed_removed, "FutureWarning");
	}
}

/** A seeded generator for a draw, or a fresh OS-seeded one when unseeded. */
function rng(seed: number | bigint | null | undefined): random.Random {
	return new random.Random(seed ?? null);
}

interface FontUnit {
	id: string;
	kind: "bundle" | "alacarte" | string;
	prob?: number;
	fonts: string[];
	requiresLocale?: string;
	sizes?: Array<{ n: number; w: number }>;
}

interface FontBase {
	id: string;
	weight?: number;
	fonts: string[];
}

let fontGroupsCache: Record<string, FontUnit[]> | null = null;

/** The addition units per OS (font-groups.json), each with its own probability. */
function loadFontGroups(): Record<string, FontUnit[]> {
	if (!fontGroupsCache) {
		try {
			fontGroupsCache =
				loadDataFile<Record<string, FontUnit[]>>("font-groups.json");
		} catch (e) {
			if (!isPyError(e, "OSError", "ValueError")) throw e;
			FallbackWarning.warn(
				"Reading font-groups.json",
				"an OS-version base with no font additions",
				e,
			);
			fontGroupsCache = {};
		}
	}
	return fontGroupsCache;
}

let fontBasesCache: Record<string, FontBase[]> | null = null;

/** The OS-version bases per OS (font-bases.json), drawn entire by weight. */
function loadFontBases(): Record<string, FontBase[]> {
	if (!fontBasesCache) {
		try {
			fontBasesCache =
				loadDataFile<Record<string, FontBase[]>>("font-bases.json");
		} catch (e) {
			if (!isPyError(e, "OSError", "ValueError")) throw e;
			FallbackWarning.warn(
				"Reading font-bases.json",
				"only the always-present core fonts as its OS base",
				e,
			);
			fontBasesCache = {};
		}
	}
	return fontBasesCache;
}

/**
 * Draw the OS version a machine runs (a font-bases.json entry) by its
 * real-world weight (`_pick_release`). It is the identity's first draw, so the
 * font list and the voice list, drawn from the same seed, land on the same
 * version.
 */
function pickRelease(osKey: string, r: random.Random): any | null {
	const bases = loadFontBases()[osKey] ?? [];
	if (!bases.length) return null;
	const roll = r.random();
	let cumulative = 0.0;
	for (const base of bases) {
		cumulative += base.weight ?? 0.0;
		if (roll < cumulative) return base;
	}
	return bases[bases.length - 1];
}

function localeMatches(
	locale: string | null | undefined,
	required: string,
): boolean {
	return (locale ?? "").toLowerCase().startsWith(required.toLowerCase());
}

/** The additions this machine has, each unit judged on its own probability. */
function drawUnits(
	osKey: string,
	r: random.Random,
	exclude: Set<string>,
	locale?: string | null,
): string[] {
	const out: string[] = [];
	for (const unit of loadFontGroups()[osKey] ?? []) {
		const required = unit.requiresLocale;
		if (required && !localeMatches(locale, required)) continue;
		if (r.random() >= (unit.prob ?? 0.0)) continue;
		let members = unit.fonts.filter((f) => !exclude.has(f));
		if (!members.length) continue;
		if (unit.kind === "alacarte") {
			const sizes = unit.sizes?.length
				? unit.sizes
				: [{ n: members.length, w: 1.0 }];
			const roll = r.random() * pySumFloats(sizes.map((s) => s.w));
			let cumulative = 0.0;
			let count = sizes[sizes.length - 1].n;
			for (const size of sizes) {
				cumulative += size.w;
				if (roll < cumulative) {
					count = size.n;
					break;
				}
			}
			count = Math.max(1, Math.min(count, members.length));
			members = r.sample(members, count);
		}
		out.push(...members);
	}
	return out;
}

/** Whether the host itself ships the OS-version font variant (native identities only). */
function hostHasVariantFonts(targetOs: string): boolean {
	if (targetOs !== "windows") return false;
	const fontsDir = path.join(process.env.WINDIR || "C:\\Windows", "Fonts");
	// SegUIVar.ttf is Segoe UI Variable: on every Windows 11 and on no Windows 10.
	return fs.existsSync(path.join(fontsDir, "SegUIVar.ttf"));
}

const OS_TO_KEY: Readonly<Record<string, TargetOS>> = {
	macos: "mac",
	windows: "win",
	linux: "lin",
};

function osKeyOf(targetOs: string): TargetOS {
	return OS_TO_KEY[targetOs] ?? "mac";
}

/**
 * The font list of one plausible machine of the given OS
 * (`_generate_random_font_subset`): one OS-version base by weight, never
 * subsetted, then each addition unit at its own measured probability, then
 * the marker fonts.
 *
 * @param locale gates the units the manifest marks `requiresLocale`.
 * @param native the identity is the host's own OS (macOS / Windows), where the
 *   real system fonts are used: only the OS base is claimed.
 */
export function generateRandomFontSubset(
	targetOs: string,
	seed?: number | bigint | null,
	native = false,
	locale?: string | null,
): string[] {
	const r = rng(seed);
	const osFontsData = loadOsFonts();
	const osKey = osKeyOf(targetOs);
	const fullList = osFontsData[osKey] ?? osFontsData.mac ?? [];

	let essential: Set<string>;
	let markers: readonly string[];
	if (targetOs === "windows") {
		essential = new Set(ESSENTIAL_FONTS_WINDOWS);
		markers = WINDOWS_MARKER_FONTS;
	} else if (targetOs === "linux") {
		essential = new Set(ESSENTIAL_FONTS_LINUX);
		markers = LINUX_MARKER_FONTS;
	} else {
		essential = new Set(ESSENTIAL_FONTS_MACOS);
		markers = MACOS_MARKER_FONTS;
	}

	if (native) {
		let result = fullList.filter((f) => essential.has(f));
		const full = new Set(fullList);
		result.push(
			...[...essential].filter((f) => !full.has(f)).sort(comparePyStr),
		);
		// A native identity may run on a Windows 10 host, which cannot render
		// the Windows 11 families the base carries.
		if (targetOs === "windows" && !hostHasVariantFonts(targetOs)) {
			result = result.filter((f) => !WINDOWS_11_MARKER_FONTS.has(f));
		}
		return result;
	}

	const release = pickRelease(osKey, r);
	let base: string[] = release ? [...release.fonts] : [];
	if (!base.length) base = fullList.filter((f) => essential.has(f));
	const result = [...base];
	const chosen = new Set(result);

	// The guaranteed floor underneath whichever base was drawn.
	for (const font of fullList) {
		if (essential.has(font) && !chosen.has(font)) {
			result.push(font);
			chosen.add(font);
		}
	}

	// Every addition unit on its own real-world probability, atomically.
	for (const font of drawUnits(osKey, r, chosen, locale)) {
		if (!chosen.has(font)) {
			result.push(font);
			chosen.add(font);
		}
	}

	ensureMarkerFonts(result, markers);
	return result;
}

// ---------------------------------------------------------------------------
// Voices
// ---------------------------------------------------------------------------

export interface VoiceObject {
	name: string;
	lang: string;
	voiceUri: string;
	isDefault: boolean;
	isLocalService: boolean;
}

// Real Firefox speechSynthesis URI prefixes per backend.
const VOICE_URI_PREFIX: Readonly<Record<string, string>> = {
	mac: "urn:moz-tts:osx:",
	win: "urn:moz-tts:sapi:",
	lin: "urn:moz-tts:speechd:",
};

export const MAC_NOVELTY_VOICES: ReadonlySet<string> = new Set(
	LAUNCHER_CONSTANTS.macNoveltyVoices,
);
export const MAC_ELOQUENCE_VOICES: ReadonlySet<string> = new Set(
	LAUNCHER_CONSTANTS.macEloquenceVoices,
);

let voiceUrisCache: Record<string, Record<string, string>> | null = null;

/** Real voiceURI per "Name|lang" as a stock browser reports it (voice-uris.json). */
function loadVoiceUris(): Record<string, Record<string, string>> {
	if (!voiceUrisCache) {
		try {
			voiceUrisCache =
				loadDataFile<Record<string, Record<string, string>>>("voice-uris.json");
		} catch {
			voiceUrisCache = {};
		}
	}
	return voiceUrisCache;
}

let voiceManifestsCache: Record<string, any> | null = null;

/** The per-OS installed-voice model (voice-manifests.json). */
function loadVoiceManifests(): Record<string, any> {
	voiceManifestsCache ??= loadDataFile<Record<string, any>>(
		"voice-manifests.json",
	);
	return voiceManifestsCache;
}

/** Stable dotted slug for mac/win URIs (shape-plausible, not catalog-exact). */
function voiceUriSlug(name: string): string {
	return name
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, ".")
		.replace(/^\.|\.$/g, "");
}

/** A voiceUri matching what real Firefox emits for the OS backend. */
export function voiceUri(osKey: string, name: string, lang: string): string {
	if (osKey === "lin") {
		// SpeechDispatcherService.cpp: NS_EscapeURL(name, OnlyNonASCII|Spaces)
		let escaped = "";
		for (const ch of name) {
			if (ch === " ") escaped += "%20";
			else if ((ch.codePointAt(0) as number) <= 0x7f) escaped += ch;
			else {
				for (const b of Buffer.from(ch, "utf-8")) {
					escaped += `%${b.toString(16).toUpperCase().padStart(2, "0")}`;
				}
			}
		}
		return `${VOICE_URI_PREFIX.lin}${escaped}?${lang}`;
	}
	if (osKey === "win") {
		// SapiService.cpp: the name and lang, verbatim.
		return `${VOICE_URI_PREFIX.win}${name}?${lang}`;
	}
	if (osKey === "mac") {
		const uri = loadVoiceUris().mac?.[`${name}|${lang}`];
		if (uri) return uri;
		const asciiName = name.normalize("NFKD").replace(/[^A-Za-z0-9]/g, "");
		if (MAC_NOVELTY_VOICES.has(name)) {
			return `${VOICE_URI_PREFIX.mac}com.apple.speech.synthesis.voice.${asciiName}`;
		}
		if (MAC_ELOQUENCE_VOICES.has(name)) {
			return `${VOICE_URI_PREFIX.mac}com.apple.eloquence.${lang}.${asciiName}`;
		}
		return `${VOICE_URI_PREFIX.mac}com.apple.voice.compact.${lang}.${asciiName}`;
	}
	return `${VOICE_URI_PREFIX[osKey] ?? ""}${voiceUriSlug(name)}`;
}

/** "Name:lang:type" -> [name, lang, type] (str.rsplit(':', 2)). */
function splitVoiceEntry(entry: string): [string, string, string] {
	const last = entry.lastIndexOf(":");
	const langsep = last < 0 ? -1 : entry.lastIndexOf(":", last - 1);
	if (last < 0 || langsep < 0) {
		throw new ValueError(
			`not enough values to unpack (voice entry ${JSON.stringify(entry)})`,
		);
	}
	return [
		entry.slice(0, langsep),
		entry.slice(langsep + 1, last),
		entry.slice(last + 1),
	];
}

function weightedPick<T extends Record<string, any>>(
	r: random.Random,
	items: T[],
	wkey = "w",
): T {
	const total = pySumFloats(items.map((i) => Number(i[wkey] ?? 0)));
	let x = r.random() * total;
	for (const i of items) {
		x -= Number(i[wkey] ?? 0);
		if (x <= 0) return i;
	}
	return items[items.length - 1];
}

function weightedSample<T>(
	r: random.Random,
	items: T[],
	k: number,
	weight: (x: T) => number,
): T[] {
	const pool = [...items];
	const out: T[] = [];
	while (pool.length && out.length < k) {
		const total = pySumFloats(pool.map(weight));
		let x = r.random() * total;
		let picked = false;
		for (const item of pool) {
			x -= weight(item);
			if (x <= 0) {
				out.push(item);
				pool.splice(pool.indexOf(item), 1);
				picked = true;
				break;
			}
		}
		if (!picked) out.push(pool.pop() as T);
	}
	return out;
}

function resolveDisplayPack(
	packs: Record<string, any>,
	fallback: string,
	locale?: string | null,
): string {
	const keys = Object.keys(packs);
	if (locale) {
		if (Object.hasOwn(packs, locale)) return locale;
		const lang = locale.split("-")[0].toLowerCase();
		for (const key of keys) {
			if (key.split("-")[0].toLowerCase() === lang) return key;
		}
	}
	return Object.hasOwn(packs, fallback) ? fallback : keys[0];
}

function codePointGreater(a: string, b: string): boolean {
	return comparePyStr(a, b) > 0;
}

/** A Linux identity's speech-dispatcher voices (`voices:speechd`). */
export interface SpeechdGrid {
	languages: Array<[string, string]>;
	variants: string[];
}

/**
 * The speech-dispatcher voice grid of the Ubuntu release the identity runs
 * (`_speechd_grid`).
 */
function speechdGrid(seed?: number | bigint | null): SpeechdGrid {
	const release = pickRelease("lin", rng(seed));
	const releases = loadVoiceManifests().lin.releases;
	return release
		? releases[release.id].speechd
		: (Object.values(releases)[0] as any).speechd;
}

/**
 * (name, lang) per voice in speech-dispatcher's order (`_speechd_voices`):
 * each language alone, then with every variant.
 */
export function speechdVoices(grid: SpeechdGrid): Array<[string, string]> {
	return grid.languages.flatMap(([name, lang]) => [
		[name, lang] as [string, string],
		...grid.variants.map((v) => [`${name}+${v}`, lang] as [string, string]),
	]);
}

/**
 * The identity's voices as the config keys the browser reads
 * (`draw_voice_config`): `voices` on macOS and Windows, `voices:speechd` on
 * Linux, whose ~15000 voices only fit in the environment as their grid.
 */
export function drawVoiceConfig(
	targetOs: string,
	locale?: string | null,
	seed?: number | bigint | null,
): Record<string, any> {
	if (osKeyOf(targetOs) === "lin")
		return { "voices:speechd": speechdGrid(seed) };
	return { voices: generateRandomVoiceSubset(targetOs, locale, seed) };
}

/**
 * The speech voice list for the given OS as MaskConfig voice objects
 * (`_generate_random_voice_subset`), following the measured per-OS model in
 * voice-manifests.json. Seeded by the identity so it always reports the same
 * list. macOS marks its system voice default; Windows and Linux mark none.
 */
export function generateRandomVoiceSubset(
	targetOs: string,
	locale?: string | null,
	seed?: number | bigint | null,
): VoiceObject[] {
	const r = rng(seed);
	const osKey = osKeyOf(targetOs);
	if (osKey === "lin") {
		return speechdVoices(speechdGrid(seed)).map(([name, lang]) => ({
			name,
			lang,
			voiceUri: voiceUri("lin", name, lang),
			isDefault: false,
			// SpeechDispatcherService registers voices as local only when one
			// is named "default"; espeak-ng has none.
			isLocalService: false,
		}));
	}
	const manifests = loadVoiceManifests();
	const manifest = pyTruthy(manifests[osKey])
		? manifests[osKey]
		: manifests.mac;
	if (manifest === undefined) throw new KeyError("'mac'");

	const out: string[] = [];
	const seen = new Set<string>();
	const add = (entries: string[] | null | undefined) => {
		for (const e of entries ?? []) {
			if (!seen.has(e)) {
				seen.add(e);
				out.push(e);
			}
		}
	};

	const legacy: string[] = [];
	const packs: Record<string, any> = pyTruthy(manifest.langPacks)
		? manifest.langPacks
		: {};

	const takePack = (pack: Record<string, any>) => {
		add(pack.oneCore);
		if (
			pyTruthy(pack.desktop) &&
			r.random() < Number(pyTruthy(pack.desktopProb) ? pack.desktopProb : 0)
		) {
			for (const e of pack.desktop as string[]) {
				if (!legacy.includes(e)) legacy.push(e);
			}
		}
	};

	add(manifest.base);
	const chosen = new Set<string>();
	if (Object.keys(packs).length) {
		const key = resolveDisplayPack(
			packs,
			pyTruthy(manifest.fallbackLocale) ? manifest.fallbackLocale : "en-US",
			locale,
		);
		chosen.add(key);
		takePack(packs[key]);
	}

	for (const addition of manifest.additions ?? []) {
		if (pyTruthy(addition.deferred)) continue;
		const req = addition.requiresLocale;
		if (pyTruthy(req) && !localeMatches(locale, req)) continue;
		if (r.random() >= Number(pyTruthy(addition.prob) ? addition.prob : 0))
			continue;
		const kind = addition.kind;
		if (kind === "bundle") {
			add(addition.voices);
		} else if (kind === "alacarte") {
			const sizes = pyTruthy(addition.sizes)
				? addition.sizes
				: [{ n: 1, w: 1 }];
			const k = Math.trunc(Number(weightedPick(r, sizes).n));
			for (const e of weightedSample<string>(
				r,
				addition.voices ?? [],
				k,
				() => 1.0,
			)) {
				if (!seen.has(e)) {
					seen.add(e);
					// a downloaded voice sits in its alphabetical place
					const el = e.toLowerCase();
					let idx = out.findIndex((v) => codePointGreater(v.toLowerCase(), el));
					if (idx < 0) idx = out.length;
					out.splice(idx, 0, e);
				}
			}
		} else if (kind === "groups" && Object.keys(packs).length) {
			const eligible = ((addition.groups ?? []) as string[]).filter(
				(g) => Object.hasOwn(packs, g) && !chosen.has(g),
			);
			if (!eligible.length) continue;
			const k = pyTruthy(addition.sizes)
				? Math.trunc(Number(weightedPick(r, addition.sizes).n))
				: eligible.length;
			const weight = (g: string) =>
				Number(pyTruthy(packs[g].weight) ? packs[g].weight : 0.01);
			for (const g of weightedSample(r, eligible, k, weight)) {
				chosen.add(g);
				takePack(packs[g]);
			}
		}
	}

	const selected = [...out, ...legacy].map(splitVoiceEntry);
	const voices = selected.map(([name, lang, vtype]) => ({
		name,
		lang,
		voiceUri: voiceUri(osKey, name, lang),
		isDefault: false,
		isLocalService: vtype === "local",
	}));
	const systemVoice = manifest.systemVoice?.default;
	const def = voices.find((v) => v.name === systemVoice);
	if (def) def.isDefault = true;
	return voices;
}

/**
 * Coerce a preset's `speechVoices` into MaskConfig voice objects: presets
 * store "Name:lang:type" strings, which MaskConfig drops. Objects pass through.
 */
export function normalizePresetVoices(
	voices: Array<string | VoiceObject>,
	targetOs: string,
): VoiceObject[] {
	const osKey = osKeyOf(targetOs);
	const result: VoiceObject[] = [];
	for (const entry of voices) {
		if (entry !== null && typeof entry === "object") {
			result.push(entry);
			continue;
		}
		const last = entry.lastIndexOf(":");
		if (last < 0) continue;
		const vtype = entry.slice(last + 1);
		const before = entry.slice(0, last);
		const langsep = before.lastIndexOf(":");
		if (langsep < 0) continue;
		const lang = before.slice(langsep + 1);
		const name = before.slice(0, langsep);
		if (!name || !lang) continue;
		result.push({
			name,
			lang,
			voiceUri: voiceUri(osKey, name, lang),
			isDefault: false,
			isLocalService: vtype === "local",
		});
	}
	if (result.length && !result.some((v) => v.isDefault))
		result[0].isDefault = true;
	return result;
}

// ---------------------------------------------------------------------------
// Hardware concurrency and navigator arch
// ---------------------------------------------------------------------------

/**
 * Logical CPUs this process may run on (affinity aware), as they were before
 * any launch pinned it: see cpu_affinity.hostCoreCount().
 */
export function hostCpuCount(): number | null {
	return hostCoreCount() || null;
}

/**
 * Core counts real desktop machines ship with, from the recorded fingerprint
 * corpus. 2 is excluded: no Apple Silicon part has 2 cores, and 85% of macOS
 * identities draw an Apple GPU (see fingerprints.py).
 */
export const PLAUSIBLE_CORE_COUNTS: readonly number[] =
	LAUNCHER_CONSTANTS.plausibleCoreCounts;

export interface HostCpu {
	/** Logical CPUs of the host (default: hostCpuCount()). */
	cpuCount?: number | null;
	/** Whether the host can pin processes to cores (default: cpu_affinity.supported()). */
	canPinHost?: boolean;
}

/**
 * navigator.hardwareConcurrency = the host's parallelism, snapped DOWN into
 * PLAUSIBLE_CORE_COUNTS -- or the fingerprint's own value (snapped, capped by
 * the host) when the browser can be pinned to that many cores.
 *
 * @param canPin false when nothing will pin the browser (launchServer, or
 *   launchOptions used directly); undefined/null: whatever the host supports.
 * @param host overrides for the host probes (tests).
 */
export function fixHardwareConcurrency(
	config: Config,
	canPin?: boolean | null,
	host: HostCpu = {},
): void {
	const n = "cpuCount" in host ? host.cpuCount : hostCpuCount();
	if (!n) return;
	const pinnable =
		(host.canPinHost ?? cpuAffinitySupported()) && canPin !== false;

	const cap = Math.trunc(n);
	const hostAllowed = PLAUSIBLE_CORE_COUNTS.filter((c) => c <= cap);
	const hostValue = hostAllowed.length
		? hostAllowed[hostAllowed.length - 1]
		: PLAUSIBLE_CORE_COUNTS[0];

	let drawn = config["navigator.hardwareConcurrency"];
	if (typeof drawn === "boolean") drawn = Number(drawn); // a Python bool is an int
	if (
		pinnable &&
		typeof drawn === "number" &&
		Number.isInteger(drawn) &&
		drawn >= 1
	) {
		const target = Math.min(drawn, cap);
		const allowed = PLAUSIBLE_CORE_COUNTS.filter((c) => c <= target);
		config["navigator.hardwareConcurrency"] = allowed.length
			? allowed[allowed.length - 1]
			: PLAUSIBLE_CORE_COUNTS[0];
		return;
	}
	config["navigator.hardwareConcurrency"] = hostValue;
}

/** Force navigator.platform AND navigator.oscpu to match the UA's arch (Linux). */
export function fixNavigatorArch(config: Config, targetOs: string): void {
	if (targetOs !== "lin") return;
	const ua = config["navigator.userAgent"];
	if (!pyTruthy(ua)) return;
	let target = "";
	if (ua.includes("Linux x86_64")) target = "Linux x86_64";
	else if (ua.includes("Linux i686")) target = "Linux i686";
	if (!target) return;
	if (config["navigator.platform"] !== target)
		config["navigator.platform"] = target;
	if (config["navigator.oscpu"] !== target) config["navigator.oscpu"] = target;
}

// ---------------------------------------------------------------------------
// Screen / window geometry
// ---------------------------------------------------------------------------

// What Firefox's baseline fingerprinting protection, on by default, reports in
// place of the device's values (RFPTargetsDefaultBaseline.inc), and the browser
// applies it to the spoofed ones for the claimed OS: the available screen is
// the whole screen less a fixed taskbar (nsRFPService::GetSpoofedScreenAvailSize),
// and more than one touch point reads as 5 (CollapseMaxTouchPoints).
export const BASELINE_TASKBAR_HEIGHT: Record<string, number> =
	LAUNCHER_CONSTANTS.baselineTaskbarHeight;
export const BASELINE_MULTI_TOUCH_POINTS: number =
	LAUNCHER_CONSTANTS.baselineMultiTouchPoints;

// The browser chrome around a page, outer minus inner, as [width, height] CSS
// px per claimed OS. From fpgen's recorded Firefox windows (every record whose
// window has a toolbar, outerHeight > innerHeight): the most common value per
// axis, raised to the largest recorded within 2 px of it, the spread DPR
// rounding gives one layout. Linux 85 -> 86 tall, macOS 84 -> 86, Windows 129
// -> 130 and 16 -> 18 wide. No recorded window has a page taller than its
// screen leaves room for: the 1366x768 records peak at innerHeight 683.
// python/tests/test_viewport_fit.py derives these from the model again.
export const RECORDED_WINDOW_CHROME: Record<string, [number, number]> =
	LAUNCHER_CONSTANTS.recordedWindowChrome;

/** A page's size, or a window's position and outer size. */
export interface Box {
	x?: number | null;
	y?: number | null;
	width: number;
	height: number;
}

/**
 * The smallest screen, [width, height], that holds a page's window.
 *
 * A page with `viewport` sits in a window the recorded chrome larger; the page
 * also reports `window` (its position and outer size), which has to be on the
 * screen too. The height adds the taskbar the browser takes out of the
 * available screen for the claimed OS (BASELINE_TASKBAR_HEIGHT), so the window
 * fits screen.availHeight.
 */
export function screenFloor(
	osKey: string,
	viewport?: Box | null,
	window?: Box | null,
): [number, number] {
	let width = 0;
	let height = 0;
	if (viewport) {
		const [chromeW, chromeH] = RECORDED_WINDOW_CHROME[osKey];
		width = viewport.width + chromeW;
		height = viewport.height + chromeH;
	}
	if (window) {
		width = Math.max(width, Math.max(0, window.x || 0) + window.width);
		height = Math.max(height, Math.max(0, window.y || 0) + window.height);
	}
	return [width, height + BASELINE_TASKBAR_HEIGHT[osKey]];
}

/**
 * A recorded screen of the OS at least `floor` in size, or null if the model
 * has none that large.
 */
export function drawScreen(
	osKey: string,
	floor: [number, number],
): [number, number] | null {
	const conditions = new Screen({
		minWidth: floor[0],
		minHeight: floor[1],
	}).asConditions();
	let screen: Record<string, any>;
	try {
		screen = generate(
			{ browser: "Firefox", os: FPGEN_OS[osKey], ...conditions },
			{ target: "screen" },
		);
	} catch (err) {
		if (!(err instanceof InvalidConstraints)) throw err;
		return null;
	}
	return [screen.width, screen.height];
}

/**
 * Claim the available screen and touch points the browser will report.
 * clampWindowDimensions then fits the window inside that screen.
 */
export function followBaselineProtection(
	config: Config,
	targetOs: string,
): void {
	const width = config["screen.width"];
	const height = config["screen.height"];
	if (pyTruthy(width) && pyTruthy(height)) {
		config["screen.availWidth"] = width;
		config["screen.availHeight"] =
			num(height) - BASELINE_TASKBAR_HEIGHT[targetOs];
	}
	if (num(config["navigator.maxTouchPoints"] ?? 0) > 1) {
		config["navigator.maxTouchPoints"] = BASELINE_MULTI_TOUCH_POINTS;
	}
}

/** Enforce inner <= outer <= avail <= screen on both axes. */
export function clampWindowDimensions(config: Config): void {
	for (const axis of ["Width", "Height"]) {
		const screen = config[`screen.${axis.toLowerCase()}`];
		const avail = config[`screen.avail${axis}`];
		const outer = config[`window.outer${axis}`];
		const inner = config[`window.inner${axis}`];

		// avail must not exceed screen
		if (pyTruthy(screen) && pyTruthy(avail) && num(avail) > num(screen)) {
			config[`screen.avail${axis}`] = screen;
		}
		const availClamped = get(config, `screen.avail${axis}`, screen);

		// outer must not exceed avail (or screen if avail is unknown)
		const outerCap = !isNone(availClamped) ? availClamped : screen;
		if (pyTruthy(outer) && pyTruthy(outerCap) && num(outer) > num(outerCap)) {
			const chrome = pyTruthy(inner) ? Math.max(0, num(outer) - num(inner)) : 0;
			config[`window.outer${axis}`] = outerCap;
			if (pyTruthy(inner))
				config[`window.inner${axis}`] = Math.max(1, num(outerCap) - chrome);
		}

		// inner must not exceed outer
		const outerClamped = get(config, `window.outer${axis}`, outer);
		const innerNow = config[`window.inner${axis}`];
		if (
			pyTruthy(innerNow) &&
			pyTruthy(outerClamped) &&
			num(innerNow) > num(outerClamped)
		) {
			config[`window.inner${axis}`] = outerClamped;
		}
	}
}

/**
 * Shrink screen.width/height down to the bounds of the real display, keeping
 * the taskbar delta. Run clampWindowDimensions afterwards.
 */
export function clampScreenToDisplay(
	config: Config,
	maxWidth: number | null | undefined,
	maxHeight: number | null | undefined,
): void {
	for (const [axis, cap] of [
		["width", maxWidth],
		["height", maxHeight],
	] as const) {
		const screen = config[`screen.${axis}`];
		if (!(pyTruthy(screen) && pyTruthy(cap)) || num(screen) <= (cap as number))
			continue;
		const availKey =
			axis === "width" ? "screen.availWidth" : "screen.availHeight";
		const avail = config[availKey];
		config[`screen.${axis}`] = cap;
		if (pyTruthy(avail)) {
			config[availKey] = Math.max(
				1,
				(cap as number) - Math.max(0, num(screen) - num(avail)),
			);
		}
	}
}

/**
 * Keep the window box inside the available screen: 0 <= screenX/Y <= avail -
 * outer. The available screen, not the whole one: pages are fitted to the
 * window on screen.availHeight (screenFloor), and a window at y=2 whose outer
 * height is the available height ran 2 px into the taskbar, so every new
 * context drew a larger screen for it. Only `keys` are moved.
 */
export function clampWindowPosition(
	config: Config,
	keys: ReadonlySet<string> = new Set(["window.screenX", "window.screenY"]),
): void {
	for (const [axis, posKey] of [
		["Width", "window.screenX"],
		["Height", "window.screenY"],
	] as const) {
		const room =
			config[`screen.avail${axis}`] || config[`screen.${axis.toLowerCase()}`];
		const outer = config[`window.outer${axis}`];
		const pos = config[posKey];
		if (
			!keys.has(posKey) ||
			isNone(pos) ||
			!(pyTruthy(room) && pyTruthy(outer))
		)
			continue;
		config[posKey] = Math.max(0, Math.min(num(pos), num(room) - num(outer)));
	}
}

/** The smallest screen mainstream hardware still ships. */
export const MODERN_SCREEN_FLOOR: readonly [number, number] = [1366, 768];

/**
 * Lift netbook-era screen geometry to the modern floor, keeping the
 * screen-to-avail gaps. Call BEFORE clampScreenToDisplay.
 */
export function raiseScreenToModernFloor(config: Config): void {
	const [minW, minH] = MODERN_SCREEN_FLOOR;
	const sw = config["screen.width"];
	const sh = config["screen.height"];
	if (!(pyTruthy(sw) && pyTruthy(sh)) || (num(sw) >= minW && num(sh) >= minH))
		return;

	const aw = config["screen.availWidth"];
	const ah = config["screen.availHeight"];
	const gapW = pyTruthy(aw) ? num(sw) - num(aw) : null;
	const gapH = pyTruthy(ah) ? num(sh) - num(ah) : null;

	const newW = Math.max(num(sw), minW);
	const newH = Math.max(num(sh), minH);
	config["screen.width"] = newW;
	config["screen.height"] = newH;
	if (gapW !== null)
		config["screen.availWidth"] = Math.max(1, newW - Math.max(0, gapW));
	if (gapH !== null)
		config["screen.availHeight"] = Math.max(1, newH - Math.max(0, gapH));
}

// ---------------------------------------------------------------------------
// Media devices
// ---------------------------------------------------------------------------

let mediaDevicesCache: Record<string, any> | null = null;

/** Per-OS catalogue of common sound cards / headsets / displays / cameras. */
function loadMediaDevices(): Record<string, any> {
	mediaDevicesCache ??= loadDataFile<Record<string, any>>("media-devices.json");
	return mediaDevicesCache;
}

function weightedChoice<T extends Record<string, any>>(
	r: random.Random,
	items: T[],
): T {
	const w = (item: T) => (Object.hasOwn(item, "w") ? item.w : 1);
	const total = Number(pySum(items.map(w)));
	let x = r.random() * total;
	for (const item of items) {
		x -= w(item);
		if (x < 0) return item;
	}
	return items[items.length - 1];
}

// Share of machines with no microphone at all, and with a built-in camera.
const MEDIA_P_NO_MIC: Readonly<Record<string, number>> = {
	win: 0.08,
	mac: 0.0,
	lin: 0.2,
};
const MEDIA_P_BUILTIN_CAM: Readonly<Record<string, number>> = {
	win: 0.78,
	mac: 0.0,
	lin: 0.45,
};
// A camera media-devices.json has no camera_modes entry for: a 720p laptop
// camera, as most are (width, height, frame rate of its largest mode).
const DEFAULT_CAMERA_MODE: readonly number[] = [1280, 720, 30];

/**
 * Draw one machine's media devices for `osKey` ('win'|'mac'|'lin'): the
 * mediaDevices:* config keys, counts plus aligned label and group lists.
 */
export function drawMediaDevices(
	osKey: string,
	seed: number | bigint | null | undefined,
): Config {
	const r = rng(seed);
	const catalogue = loadMediaDevices();
	const cat = pyTruthy(catalogue[osKey]) ? catalogue[osKey] : catalogue.win;
	const mics: Array<[string, string]> = [];
	const outs: Array<[string, string]> = [];
	const cams: Array<[string, string]> = [];
	let counter = 0;
	const group = () => {
		counter += 1;
		return `hw-${counter}`;
	};
	const add = (item: Record<string, any>, grp: string) => {
		for (const m of item.mics ?? []) mics.push([m, grp]);
		for (const o of item.outs ?? []) outs.push([o, grp]);
		if (pyTruthy(item.cam)) cams.push([item.cam, group()]);
	};

	// 1. the machine's own sound card (+ built-in camera on macOS models)
	const card = weightedChoice(r, cat.cards);
	const noMic = r.random() < (MEDIA_P_NO_MIC[osKey] ?? 0.0);
	const cardGrp = group();
	add(noMic ? { ...card, mics: [] } : card, cardGrp);
	// 2. a built-in laptop camera (Windows/Linux); rare on a mic-less tower
	const pCam = MEDIA_P_BUILTIN_CAM[osKey] ?? 0.0;
	if (r.random() < (noMic ? pCam * 0.3 : pCam)) {
		const builtin = (cat.cameras as any[]).filter((c) => !pyTruthy(c.mic));
		if (builtin.length) cams.push([weightedChoice(r, builtin).cam, group()]);
	}
	// 3. a headset / USB microphone
	if (r.random() < (cat.p_headset ?? 0.0))
		add(weightedChoice(r, cat.headsets), group());
	// 4. display audio (HDMI/DP)
	if (r.random() < (cat.p_display ?? 0.0))
		add(weightedChoice(r, cat.displays), group());
	// 5. an external webcam, usually with its own microphone
	if (r.random() < (cat.p_extra_camera ?? 0.0)) {
		const withMic = (cat.cameras as any[]).filter((c) => pyTruthy(c.mic));
		const external = withMic.length ? withMic : cat.cameras;
		const cam = weightedChoice(r, external);
		const grp = group();
		cams.push([cam.cam, grp]);
		if (pyTruthy(cam.mic)) mics.push([cam.mic, grp]);
	}
	// 6. PulseAudio exposes a monitor source per output as a capture device
	if (pyTruthy(cat.monitor_sources)) {
		for (const [label, grp] of [...outs])
			mics.push([`Monitor of ${label}`, grp]);
	}

	// What each device reports once captured (media-device-spoofing.patch):
	// a camera's largest mode, a microphone's channel count.
	const modes: Record<string, number[]> = cat.camera_modes ?? {};
	const mono = new Set<string>(cat.mono_mics ?? []);
	const camModes = cams.map(([c]) =>
		Object.hasOwn(modes, c) ? modes[c] : DEFAULT_CAMERA_MODE,
	);

	return {
		"mediaDevices:enabled": true,
		"mediaDevices:micros": mics.length,
		"mediaDevices:webcams": cams.length,
		"mediaDevices:speakers": outs.length,
		"mediaDevices:microphoneLabels": mics.map(([m]) => m),
		"mediaDevices:microphoneGroups": mics.map(([, g]) => g),
		"mediaDevices:webcamLabels": cams.map(([c]) => c),
		"mediaDevices:webcamGroups": cams.map(([, g]) => g),
		"mediaDevices:speakerLabels": outs.map(([o]) => o),
		"mediaDevices:speakerGroups": outs.map(([, g]) => g),
		"mediaDevices:webcamMaxWidths": camModes.map((m) => m[0]),
		"mediaDevices:webcamMaxHeights": camModes.map((m) => m[1]),
		"mediaDevices:webcamMaxFrameRates": camModes.map((m) => m[2]),
		"mediaDevices:microphoneChannels": mics.map(([m]) => (mono.has(m) ? 1 : 2)),
	};
}

/**
 * Give the identity a plausible set of media devices, drawn from the common
 * desktop population for its OS and seeded by the identity. Nothing is drawn
 * when the caller already set any mediaDevices: key.
 */
export function setMediaDevicesDefaults(
	config: Config,
	salt: number | bigint = 0,
): void {
	if (Object.keys(config).some((k) => k.startsWith("mediaDevices:"))) return;
	const plat = pyStr(get(config, "navigator.platform", ""));
	const osKey = plat.startsWith("Win")
		? "win"
		: plat.startsWith("Mac")
			? "mac"
			: "lin";
	Object.assign(config, drawMediaDevices(osKey, identitySeed(config, salt)));
}

/**
 * Share of machines whose one adapter carries IPv6 next to IPv4. No recorded
 * corpus has the ICE shape; this is Google's IPv6 adoption figure (~45% of its
 * users in 2025, google.com/ipv6/statistics), which counts global IPv6 only,
 * so it is a lower bound. A real-device measurement replaces it.
 */
export const HOST_IPV6_SHARE = 0.45;

/**
 * Draw the identity's network shape: one adapter, IPv4 only or IPv4 + IPv6
 * (webrtc:hostipv6, webrtc-topology-host-ipv6.patch). Mirrors Python's
 * set_host_ipv6_default; nothing is drawn when the caller set it.
 */
export function setHostIpv6Default(
	config: Config,
	salt: number | bigint = 0,
): void {
	if ("webrtc:hostipv6" in config) return;
	if (config["network:ipv4Only"]) {
		// The host's own IPv6 is turned off: a machine on an IPv4-only network.
		config["webrtc:hostipv6"] = false;
	} else if ("webrtc:ipv6" in config) {
		// The exit address is IPv6, so the machine has IPv6.
		config["webrtc:hostipv6"] = true;
	} else {
		const draw = rng((identitySeed(config, salt) ^ 0x1ce6) >>> 0).random();
		config["webrtc:hostipv6"] = draw < HOST_IPV6_SHARE;
	}
}

/**
 * Windows' location setting for desktop apps, as Firefox reads it
 * (GeolocationSystemWin.cpp). No recorded corpus has it; an estimate a
 * real-device measurement replaces. Mirrors Python's
 * WINDOWS_LOCATION_SETTING_SHARES.
 */
export const WINDOWS_LOCATION_SETTING_SHARES: ReadonlyArray<[string, number]> =
	[
		["allowed", 0.55],
		["prompt", 0.3],
		["denied", 0.15],
	];

/**
 * Draw the identity's OS location setting (geolocation:systemPermission,
 * geolocation-spoofing.patch) for a Windows identity on another host. Mirrors
 * Python's set_location_setting_default.
 */
export function setLocationSettingDefault(
	config: Config,
	salt: number | bigint,
	targetOs: string,
	hostOs: string | null,
): void {
	if (
		"geolocation:systemPermission" in config ||
		targetOs !== "win" ||
		hostOs === "win"
	)
		return;
	if ("geolocation:latitude" in config && "geolocation:longitude" in config) {
		config["geolocation:systemPermission"] = "allowed";
		return;
	}
	// No coordinates: nothing at the exit to report, so location is never on.
	const shares = WINDOWS_LOCATION_SETTING_SHARES.filter(
		([name]) => name !== "allowed",
	);
	let draw =
		rng((identitySeed(config, salt) ^ 0x10ca) >>> 0).random() *
		shares.reduce((sum, [, share]) => sum + share, 0);
	let state = shares[0][0];
	for (const [name, share] of shares) {
		state = name;
		draw -= share;
		if (draw < 0) break;
	}
	config["geolocation:systemPermission"] = state;
}

let audioOutputLatencyCache: Record<string, any> | undefined;

/** Measured AudioContext.outputLatency per OS (audio-output-latency.json). */
function loadAudioOutputLatency(): Record<string, any> {
	audioOutputLatencyCache ??= loadDataFile<Record<string, any>>(
		"audio-output-latency.json",
	);
	return audioOutputLatencyCache;
}

/**
 * When stock Windows Firefox's deferred font-info loader lands the legacy
 * styled-family aliases after the first legacy lookup (stock 158.0.1 on win-i9:
 * 1727, 1826, 1832 ms on a 100 ms poll). Mirrors Python's
 * WINDOWS_ALIAS_LOAD_MS / set_windows_alias_load_default: drawn per launch for a
 * Windows identity on another OS; a caller's value is kept.
 */
export const WINDOWS_ALIAS_LOAD_MS: readonly [number, number] = [1630, 1832];

/**
 * How long stock Windows Firefox's character-map pass takes to land after the
 * first fallback within a family whose cmaps are not loaded yet
 * (windows-lazy-cmaps.patch; win-i9: 80, 154, 182 ms). Mirrors Python's
 * WINDOWS_CMAP_LOAD_MS; drawn with the alias load time.
 */
export const WINDOWS_CMAP_LOAD_MS: readonly [number, number] = [80, 182];

/**
 * Injection point for the draws that belong to the launch, not the identity
 * (timing: a real machine's figure moves from launch to launch). Mirrors
 * Python's launch_uniform(); the launch goldens pin both to one value.
 */
export const fingerprintDeps = {
	random: (): number => Math.random(),
};

export function setWindowsAliasLoadDefault(
	config: Config,
	targetOs: string,
	hostOs: string | null,
): void {
	if (targetOs !== "win" || targetOs === hostOs) return;
	for (const [key, [low, high]] of [
		["fonts:windowsAliasLoadMs", WINDOWS_ALIAS_LOAD_MS],
		["fonts:windowsCmapLoadMs", WINDOWS_CMAP_LOAD_MS],
	] as const) {
		if (key in config) continue;
		config[key] = low + Math.floor(fingerprintDeps.random() * (high - low + 1));
	}
}

/**
 * Draw the identity's audio output device latency (AudioContext:outputLatency,
 * audio-context-spoofing.patch). The device latency follows the identity seed;
 * the stream's opening delay is drawn per launch. Mirrors Python's
 * set_output_latency_default; nothing is set when the caller set the key, the
 * identity claims the host's OS, or the OS has no measured devices.
 */
export function setOutputLatencyDefault(
	config: Config,
	salt: number | bigint,
	targetOs: string,
	hostOs: string | null,
): void {
	if ("AudioContext:outputLatency" in config || targetOs === hostOs) return;
	const table = loadAudioOutputLatency()[targetOs] ?? {};
	const devices: Array<{ w?: number; v: number }> =
		table.device_latency_s ?? [];
	if (!devices.length) return;
	const device = weightedChoice(
		rng((identitySeed(config, salt) ^ 0xa0d1) >>> 0),
		devices,
	);
	config["AudioContext:outputLatency"] = Number(device.v);
	const [low, high] = table.open_delay_ms ?? [20, 60];
	if (!("AudioContext:outputLatencyDelayMs" in config)) {
		config["AudioContext:outputLatencyDelayMs"] =
			low + (high - low) * fingerprintDeps.random();
	}
}

// ---------------------------------------------------------------------------
// WebGL <-> screen coherence (#729)
// ---------------------------------------------------------------------------

/** Software rasterizers: never preferred, never screen-constrained. */
const SOFTWARE_RENDERERS: readonly string[] = [
	"llvmpipe",
	"Microsoft Basic Render Driver",
	"SwiftShader",
	"Generic Renderer",
];

/** Gecko renderer buckets that are discrete GPUs (no netbook shipped one). */
const DISCRETE_GPU_BUCKETS: ReadonlySet<string> = new Set([
	"GeForce 8800 GTX",
	"GeForce GTX 480",
	"GeForce GTX 980",
	"Radeon R9 200 Series",
]);

/** Netbook panels topped out at 1024x600; an area, not a per-axis floor. */
const NETBOOK_MAX_PIXELS = 1024 * 600;

const ANGLE_D3D_RE = /^ANGLE \([^,]*, (.*?) Direct3D.*\)$/s;
const ANGLE_VULKAN_RE = /^ANGLE \((.*)\) on Vulkan$/s;
const PCIE_SSE2_RE = /^(.*)\/PCIe?\/SSE2$/s;

/** Reduce a reported renderer to Gecko's sanitized device bucket. */
export function rendererBucket(renderer: string): string {
	let core = renderer.endsWith(", or similar")
		? renderer.slice(0, -", or similar".length)
		: renderer;
	let match = ANGLE_D3D_RE.exec(core) ?? ANGLE_VULKAN_RE.exec(core);
	if (match) core = match[1];
	match = PCIE_SSE2_RE.exec(core);
	if (match) core = match[1];
	return core.startsWith("NVIDIA ") ? core.slice("NVIDIA ".length) : core;
}

/** Whether `renderer` is a software rasterizer rather than real hardware. */
export function isSoftwareRenderer(
	renderer: string | null | undefined,
): boolean {
	return (
		!!renderer && SOFTWARE_RENDERERS.some((name) => renderer.includes(name))
	);
}

/** Whether `renderer` is a GPU that plausibly drives a `width` x `height` screen. */
export function gpuScreenIsPlausible(
	renderer: string | null | undefined,
	width: number | null | undefined,
	height: number | null | undefined,
): boolean {
	if (!renderer || !width || !height) return true;
	if (isSoftwareRenderer(renderer)) return true;
	if (!DISCRETE_GPU_BUCKETS.has(rendererBucket(renderer))) return true;
	return width * height > NETBOOK_MAX_PIXELS;
}

// ---------------------------------------------------------------------------
// Presets
// ---------------------------------------------------------------------------

/** The bundled-presets file for a Firefox version (v150 bundle from 149 up). */
export function selectPresetsFile(ffVersion?: string | number | null): string {
	let major = 0;
	if (ffVersion) {
		const parsed = Number.parseInt(
			String(ffVersion).split(".", 1)[0].trim(),
			10,
		);
		major = /^\s*[+-]?\d+\s*$/.test(String(ffVersion).split(".", 1)[0])
			? parsed
			: 0;
	}
	if (major >= PRESETS_V150_MIN_FF && fs.existsSync(PRESETS_V150_FILE))
		return PRESETS_V150_FILE;
	return PRESETS_FILE;
}

/** Load the bundled fingerprint presets. */
export function loadPresets(
	ffVersion?: string | number | null,
): PresetBundle | null {
	const file = selectPresetsFile(ffVersion);
	const cached = PRESETS_CACHE.get(file);
	if (cached) return cached;
	if (!fs.existsSync(file)) return null;
	const bundle = JSON.parse(fs.readFileSync(file, "utf-8")) as PresetBundle;
	PRESETS_CACHE.set(file, bundle);
	return bundle;
}

const OS_TO_PRESET_KEY: Readonly<Record<string, string>> = {
	windows: "windows",
	macos: "macos",
	linux: "linux",
	win: "windows",
	mac: "macos",
	lin: "linux",
};

/**
 * A random preset for the given OS (or OSes), or null when none is bundled.
 * Draws from python-random's module-level `choice` (Python's random.choice).
 */
export function getRandomPreset(
	os?: string | readonly string[] | null,
	ffVersion?: string | number | null,
): Preset | null {
	const presets = loadPresets(ffVersion);
	if (!pyTruthy(presets)) return null;
	let osKeys: string[];
	if (os?.length) {
		const names = typeof os === "string" ? [os] : [...os];
		osKeys = names.map((o) => OS_TO_PRESET_KEY[o] ?? o);
	} else {
		osKeys = ["macos", "windows", "linux"];
	}
	const candidates: Preset[] = [];
	for (const key of osKeys) {
		candidates.push(
			...((presets?.presets as Record<string, Preset[]> | undefined)?.[key] ??
				[]),
		);
	}
	if (!candidates.length) return null;
	return random.choice(candidates);
}

// Tokens that name the machine rather than the platform.
const APP_VERSION_DROPPED: ReadonlySet<string> = new Set([
	"Win64",
	"x64",
	"Mobile",
	"Tablet",
]);

/**
 * The appVersion Firefox reports for a browser sending this user agent:
 * "5.0 (<OS tokens>)" without the architecture, the Gecko revision, or the
 * Windows build number.
 */
export function appVersionFromUserAgent(
	userAgent: string | null | undefined,
): string | null {
	const block = /^Mozilla\/5\.0 \(([^)]*)\)/.exec(userAgent ?? "");
	if (!block) return null;
	const kept: string[] = [];
	for (const token of block[1].split(";").map((part) => part.trim())) {
		if (
			token.startsWith("rv:") ||
			APP_VERSION_DROPPED.has(token) ||
			token.startsWith("Linux ") ||
			token.startsWith("Intel Mac OS X")
		) {
			continue;
		}
		kept.push(token.startsWith("Windows") ? "Windows" : token);
	}
	return kept.length ? `5.0 (${kept.join("; ")})` : null;
}

/**
 * Sets navigator.appVersion the way Firefox computes it: from the userAgent.
 * Left unset it falls through to the HOST's value; taken from fpgen it is a
 * draw of its own, and fpgen pairs some Linux user agents with "5.0 (Windows)".
 */
function setAppVersion(config: Config): void {
	const derived = appVersionFromUserAgent(config["navigator.userAgent"] ?? "");
	if (derived) config["navigator.appVersion"] = derived;
}

function oscpuFromPlatform(plat: string): string | null {
	if (plat === "MacIntel") return "Intel Mac OS X 10.15";
	if (plat === "Win32") return "Windows NT 10.0; Win64; x64";
	if (plat.includes("Linux") || plat.includes("linux")) return "Linux x86_64";
	return null;
}

/**
 * Convert a real fingerprint preset to CAMOU_CONFIG format.
 *
 * `salt` (identitySalt) keys the font/voice draws; undefined draws a fresh
 * one, so two users of the same recorded device do not share its font list.
 */
export function fromPreset(
	preset: Preset,
	ffVersion?: string | number | null,
	salt?: number | bigint | null,
): Config {
	const s = isNone(salt) ? identitySalt() : (salt as number | bigint);
	const config: Config = {};

	const nav: Record<string, any> = preset.navigator ?? {};
	if (pyTruthy(nav.userAgent)) {
		let ua: string = nav.userAgent;
		if (ffVersion) {
			ua = ua.replace(/Firefox\/\d+\.0/g, `Firefox/${ffVersion}.0`);
			ua = ua.replace(/rv:\d+\.0/g, `rv:${ffVersion}.0`);
		}
		config["navigator.userAgent"] = ua;
	}
	if (pyTruthy(nav.platform)) config["navigator.platform"] = nav.platform;
	if (pyTruthy(nav.hardwareConcurrency))
		config["navigator.hardwareConcurrency"] = nav.hardwareConcurrency;
	if (pyTruthy(nav.oscpu)) {
		config["navigator.oscpu"] = nav.oscpu;
	} else if (pyTruthy(nav.platform)) {
		const oscpu = oscpuFromPlatform(nav.platform);
		if (oscpu) config["navigator.oscpu"] = oscpu;
	}
	setAppVersion(config);
	if (Object.hasOwn(nav, "maxTouchPoints"))
		config["navigator.maxTouchPoints"] = nav.maxTouchPoints;

	const screen: Record<string, any> = preset.screen ?? {};
	if (pyTruthy(screen.width)) config["screen.width"] = screen.width;
	if (pyTruthy(screen.height)) config["screen.height"] = screen.height;
	if (pyTruthy(screen.colorDepth)) {
		config["screen.colorDepth"] = screen.colorDepth;
		config["screen.pixelDepth"] = screen.colorDepth;
	}
	if (pyTruthy(screen.availWidth))
		config["screen.availWidth"] = screen.availWidth;
	if (pyTruthy(screen.availHeight))
		config["screen.availHeight"] = screen.availHeight;

	const webgl: Record<string, any> = preset.webgl ?? {};
	if (pyTruthy(webgl.unmaskedVendor))
		config["webGl:vendor"] = webgl.unmaskedVendor;
	if (pyTruthy(webgl.unmaskedRenderer))
		config["webGl:renderer"] = webgl.unmaskedRenderer;

	if (pyTruthy(preset.timezone)) config.timezone = preset.timezone;

	const plat: string = nav.platform ?? "";
	let targetOs: string;
	if (plat === "MacIntel") targetOs = "macos";
	else if (plat === "Win32") targetOs = "windows";
	else if (plat.includes("Linux") || plat.includes("linux")) targetOs = "linux";
	else targetOs = "macos";

	const presetKey = `${pyStr(config["navigator.userAgent"])} / ${pyStr(config["webGl:renderer"])}`;
	try {
		config.fonts = generateRandomFontSubset(targetOs, identitySeed(config, s));
	} catch (e) {
		if (!isPyError(e, "OSError", "ValueError")) throw e;
		FallbackWarning.warn(
			"Drawing the font list",
			pyTruthy(preset.fonts)
				? "the preset's recorded fonts"
				: "the browser's own fonts",
			e,
			presetKey,
		);
		if (pyTruthy(preset.fonts)) {
			const fonts = [...(preset.fonts as string[])];
			ensureMarkerFonts(
				fonts,
				{
					macos: MACOS_MARKER_FONTS,
					windows: WINDOWS_MARKER_FONTS,
					linux: LINUX_MARKER_FONTS,
				}[targetOs] ?? MACOS_MARKER_FONTS,
			);
			config.fonts = fonts;
		}
	}
	try {
		Object.assign(
			config,
			drawVoiceConfig(targetOs, null, identitySeed(config, s)),
		);
	} catch (e) {
		if (!isPyError(e, "OSError", "ValueError", "KeyError")) throw e;
		FallbackWarning.warn(
			"Drawing the speech voices",
			pyTruthy(preset.speechVoices)
				? "the preset's recorded voices"
				: "the browser's own voices",
			e,
			presetKey,
		);
		if (pyTruthy(preset.speechVoices)) {
			config.voices = normalizePresetVoices(
				preset.speechVoices as Array<string | VoiceObject>,
				targetOs,
			);
		}
	}
	return config;
}

// ---------------------------------------------------------------------------
// Per-context fingerprints
// ---------------------------------------------------------------------------

export interface InitValues {
	navigatorPlatform?: string;
	navigatorOscpu?: string;
	navigatorUserAgent?: string;
	navigatorAppVersion?: string;
	hardwareConcurrency?: number;
	webglVendor?: string;
	webglRenderer?: string;
	screenWidth?: number;
	screenHeight?: number;
	screenAvailWidth?: number;
	screenAvailHeight?: number;
	screenColorDepth?: number;
	timezone?: string;
	fontList?: string[];
	speechVoices?: Array<string | VoiceObject> | { speechd: SpeechdGrid };
	webrtcIP?: string;
}

/**
 * The JavaScript init script that calls the per-context window.setXxx()
 * functions. Those self-destruct after their first call, so they must run
 * via addInitScript.
 */
export function buildInitScript(values: InitValues): string {
	const lines = ["(function(v) {", "  var w = window;"];

	const setters: Array<[keyof InitValues, string]> = [
		["navigatorPlatform", "setNavigatorPlatform"],
		["navigatorOscpu", "setNavigatorOscpu"],
		["navigatorUserAgent", "setNavigatorUserAgent"],
		["navigatorAppVersion", "setNavigatorAppVersion"],
		["hardwareConcurrency", "setNavigatorHardwareConcurrency"],
		["webglVendor", "setWebGLVendor"],
		["webglRenderer", "setWebGLRenderer"],
	];
	for (const [key, fnName] of setters) {
		const val = values[key];
		if (!isNone(val)) {
			lines.push(
				`  if (typeof w.${fnName} === "function") w.${fnName}(${pyJsonDumps(val)});`,
			);
		}
	}

	// Screen dimensions (requires width + height together). The available rect
	// rides along when the identity has one; a build that keeps only the full
	// size ignores the extra arguments.
	const sw = values.screenWidth;
	const sh = values.screenHeight;
	if (pyTruthy(sw) && pyTruthy(sh)) {
		const aw = values.screenAvailWidth;
		const ah = values.screenAvailHeight;
		const dims =
			pyTruthy(aw) && pyTruthy(ah)
				? `${sw}, ${sh}, ${aw}, ${ah}`
				: `${sw}, ${sh}`;
		lines.push(
			`  if (typeof w.setScreenDimensions === "function") w.setScreenDimensions(${dims});`,
		);
		const scd = values.screenColorDepth;
		if (pyTruthy(scd)) {
			lines.push(
				`  if (typeof w.setScreenColorDepth === "function") w.setScreenColorDepth(${scd});`,
			);
		}
	}

	// Timezone -- only with an explicit value; MaskConfig handles the rest.
	const tz = values.timezone;
	if (pyTruthy(tz)) {
		lines.push(
			`  if (typeof w.setTimezone === "function") w.setTimezone(${pyJsonDumps(tz)});`,
		);
	}

	// WebRTC IP
	const ip = values.webrtcIP;
	if (pyTruthy(ip)) {
		validateIP(ip as string);
		const fnName = validIPv4(ip as string) ? "setWebRTCIPv4" : "setWebRTCIPv6";
		lines.push(
			`  if (typeof w.${fnName} === "function") w.${fnName}(${pyJsonDumps(ip)});`,
		);
	} else {
		lines.push(
			'  if (typeof w.setWebRTCIPv4 === "function") w.setWebRTCIPv4("");',
		);
	}

	// Font list (comma-separated)
	const fontList = values.fontList;
	if (fontList?.length) {
		lines.push(
			`  if (typeof w.setFontList === "function") w.setFontList(${pyJsonDumps(fontList.join(","))});`,
		);
	}

	// Speech voices: comma-separated names, or a Linux identity's
	// speech-dispatcher grid as JSON
	const voices = values.speechVoices;
	if (voices && !Array.isArray(voices)) {
		lines.push(
			`  if (typeof w.setSpeechVoices === "function") w.setSpeechVoices(${pyJsonDumps(asciiJson(voices))});`,
		);
	} else if (voices?.length) {
		const names = voices.map((v) =>
			v !== null && typeof v === "object" ? v.name : v,
		);
		lines.push(
			`  if (typeof w.setSpeechVoices === "function") w.setSpeechVoices(${pyJsonDumps(names.join(","))});`,
		);
	}

	lines.push("})();");
	return lines.join("\n");
}

/** json.dumps() with its defaults (ASCII-escaped, ", " / ": " separators) for scalars. */
/** Python's json.dumps(value, separators=(",", ":")) for plain JSON data. */
function asciiJson(value: unknown): string {
	return JSON.stringify(value).replace(
		/[\u0080-\uffff]/g,
		(c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
	);
}

function pyJsonDumps(value: unknown): string {
	if (typeof value === "string") {
		return JSON.stringify(value).replace(
			/[\u007f-\uffff]/g,
			(c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
		);
	}
	if (typeof value === "boolean") return value ? "true" : "false";
	if (isNone(value)) return "null";
	if (typeof value === "number" && !Number.isSafeInteger(value)) {
		if (Number.isNaN(value)) return "NaN";
		if (!Number.isFinite(value)) return value > 0 ? "Infinity" : "-Infinity";
		return pyStr(value);
	}
	return String(value);
}

export interface ContextFingerprint {
	init_script: string;
	context_options: Record<string, any>;
	config: Config;
	preset: Preset;
}

/** The OS a navigator.platform value presents, as a Camoufox OS name. */
export function targetOsFromPlatform(plat: string): SupportedOS {
	if (plat === "Win32") return "windows";
	if (plat.includes("Linux") || plat.includes("linux")) return "linux";
	return "macos";
}

/**
 * Fingerprint values for a single per-context identity: the init script plus
 * the Playwright context options (camelCase, for playwright-core).
 *
 * By default an fpgen fingerprint is generated; pass a preset to use a real
 * recorded one instead.
 *
 * @param timezone IANA timezone; takes priority over the preset's.
 * @param locale BCP-47 locale; also sets context_options.locale.
 * @param config_overrides CAMOU_CONFIG keys applied after the config is built
 *   and before the init script is rendered.
 * @param webgl_config The [vendor, renderer] a drawn identity claims, instead
 *   of drawing one.
 * @param webgl false claims no WebGL values, for a browser whose render node
 *   reports its own (render_node).
 * @param screen_bound A bound on the drawn screen (screenFloor: one that holds
 *   the window the context's pages report). A preset keeps its own.
 */
export function generateContextFingerprint({
	preset,
	os,
	ff_version,
	webrtc_ip,
	timezone,
	locale,
	config_overrides,
	webgl_config,
	webgl: claimWebgl = true,
	screen_bound,
}: {
	preset?: Preset | null;
	os?: string | null;
	ff_version?: string | null;
	webrtc_ip?: string | null;
	timezone?: string | null;
	locale?: string | null;
	config_overrides?: Config | null;
	webgl_config?: [string, string] | null;
	webgl?: boolean;
	screen_bound?: Screen | null;
} = {}): ContextFingerprint {
	let config: Config;
	let nav: Record<string, any>;
	let screen: Record<string, any>;
	let webgl: Record<string, any>;
	let resolvedPreset: Preset;

	if (!isNone(preset)) {
		const p = preset as Preset;
		config = fromPreset(p, ff_version);
		nav = p.navigator ?? {};
		screen = p.screen ?? {};
		webgl = p.webgl ?? {};
		resolvedPreset = p;
	} else {
		const fp = generateFingerprint({
			os: os ?? undefined,
			screen: screen_bound ?? undefined,
		});
		config = fromFpgen(fp, ff_version);
		// fpgen's Linux pool now and then pairs the Linux UA with a Windows
		// platform and oscpu. launchOptions() corrects that; this path did not,
		// so ~1.6% of Linux contexts said Win32 -- and, since the OS below is
		// read from the platform, drew Windows fonts and voices as well.
		if (pyStr(get(config, "navigator.userAgent", "")).includes("Linux"))
			fixNavigatorArch(config, "lin");

		// A fresh identity: every seeded draw below gets its own salt.
		const salt = identitySalt();

		const osName = targetOsFromPlatform(
			pyStr(get(config, "navigator.platform", "")),
		);

		if (!("fonts" in config)) {
			try {
				config.fonts = generateRandomFontSubset(
					osName,
					identitySeed(config, salt),
				);
			} catch (e) {
				if (!isPyError(e, "OSError", "ValueError")) throw e;
				FallbackWarning.warn(
					"Drawing the font list",
					"the browser's launch-time fonts",
					e,
					config["navigator.userAgent"],
				);
			}
		}
		if (!("voices" in config) && !("voices:speechd" in config)) {
			try {
				Object.assign(
					config,
					drawVoiceConfig(osName, null, identitySeed(config, salt)),
				);
			} catch (e) {
				if (!isPyError(e, "OSError", "ValueError", "KeyError")) throw e;
				FallbackWarning.warn(
					"Drawing the speech voices",
					"the browser's launch-time voices",
					e,
					config["navigator.userAgent"],
				);
			}
		}
		if (!("navigator.oscpu" in config)) {
			const oscpu = oscpuFromPlatform(
				pyStr(get(config, "navigator.platform", "")),
			);
			if (oscpu) config["navigator.oscpu"] = oscpu;
		}

		if (!claimWebgl) {
			for (const key of Object.keys(config)) {
				if (key.startsWith("webGl:") || key.startsWith("webGl2:"))
					delete config[key];
			}
		} else if (
			!pyTruthy(config["webGl:vendor"]) ||
			!pyTruthy(config["webGl:renderer"])
		) {
			const osMap: Record<string, TargetOS> = {
				macos: "mac",
				linux: "lin",
				windows: "win",
			};
			let targetOs: TargetOS | undefined = osMap[os ?? ""];
			if (!targetOs) {
				const plat = pyStr(get(config, "navigator.platform", ""));
				targetOs =
					plat === "Win32"
						? "win"
						: plat.includes("Linux") || plat.includes("linux")
							? "lin"
							: "mac";
			}
			// Same coherence treatment launchOptions applies (#729): lift netbook
			// geometry, then keep the GPU consistent with the resulting screen.
			raiseScreenToModernFloor(config);
			const webglFp = webgl_config
				? webglForGpu(targetOs, ...webgl_config)
				: sampleWebglForScreen(
						targetOs,
						config["screen.width"],
						config["screen.height"],
						undefined,
						config["navigator.hardwareConcurrency"],
					);
			delete webglFp.webGl2Enabled;
			Object.assign(config, webglFp);
		}

		nav = {
			platform: config["navigator.platform"],
			hardwareConcurrency: config["navigator.hardwareConcurrency"],
		};
		screen = {
			width: config["screen.width"],
			height: config["screen.height"],
			colorDepth: config["screen.colorDepth"],
			devicePixelRatio: null,
		};
		webgl = {
			unmaskedVendor: config["webGl:vendor"],
			unmaskedRenderer: config["webGl:renderer"],
		};
		resolvedPreset = { navigator: nav, screen, webgl };
	}

	if (timezone) config.timezone = timezone;
	if (locale) {
		const parsed = normalizeLocale(locale);
		config["locale:language"] = parsed.language;
		config["locale:region"] = parsed.region;
		config["navigator.language"] = parsed.asString;
		if (parsed.script) config["locale:script"] = parsed.script;
	}

	if (config_overrides) {
		const overrides = { ...config_overrides };
		dropAudioSeed(overrides);
		Object.assign(config, overrides);
	}

	const initValues: InitValues = {
		navigatorPlatform: nav.platform,
		navigatorOscpu: config["navigator.oscpu"],
		navigatorUserAgent: config["navigator.userAgent"],
		navigatorAppVersion: config["navigator.appVersion"],
		hardwareConcurrency: pyTruthy(nav.hardwareConcurrency)
			? nav.hardwareConcurrency
			: config["navigator.hardwareConcurrency"],
		webglVendor: webgl.unmaskedVendor,
		webglRenderer: webgl.unmaskedRenderer,
		screenWidth: screen.width,
		screenHeight: screen.height,
		screenAvailWidth: config["screen.availWidth"],
		screenAvailHeight: config["screen.availHeight"],
		screenColorDepth: screen.colorDepth,
		timezone:
			typeof resolvedPreset.timezone === "string"
				? resolvedPreset.timezone
				: config.timezone,
		fontList: config.fonts,
		speechVoices:
			"voices:speechd" in config
				? { speechd: config["voices:speechd"] }
				: config.voices,
		webrtcIP: webrtc_ip || "",
	};
	const initScript = buildInitScript(initValues);

	const contextOptions: Record<string, any> = {};
	const ua = config["navigator.userAgent"];
	if (pyTruthy(ua)) contextOptions.userAgent = ua;
	// No viewport: the pages are the browser's window, whose size the page
	// reads in outerWidth/outerHeight, and the screen is drawn to hold it
	// (screen_bound). A viewport the size of the screen sat in that window as a
	// page larger than its own window.
	// Playwright takes a device scale factor only with a viewport: a context
	// with its own devicePixelRatio gets the drawn page size as its viewport,
	// and Juggler sizes the window to it plus the claimed OS's chrome.
	const dpr = screen.devicePixelRatio;
	let innerW = config["window.innerWidth"];
	let innerH = config["window.innerHeight"];
	const availW = config["screen.availWidth"];
	const availH = config["screen.availHeight"];
	if (
		pyTruthy(dpr) &&
		!(pyTruthy(innerW) && pyTruthy(innerH)) &&
		pyTruthy(availW) &&
		pyTruthy(availH)
	) {
		// A preset records its screen but no window: a maximised one, the
		// available screen less the recorded chrome of the OS it claims.
		const [chromeW, chromeH] =
			RECORDED_WINDOW_CHROME[
				OS_TO_KEY[
					targetOsFromPlatform(pyStr(get(config, "navigator.platform", "")))
				]
			];
		innerW = num(availW) - chromeW;
		innerH = num(availH) - chromeH;
	}
	if (pyTruthy(dpr) && pyTruthy(innerW) && pyTruthy(innerH)) {
		contextOptions.viewport = {
			width: Math.trunc(Number(innerW)),
			height: Math.trunc(Number(innerH)),
		};
		contextOptions.deviceScaleFactor = dpr;
	} else {
		contextOptions.viewport = null;
	}
	let tz = config.timezone;
	if (!pyTruthy(tz)) tz = resolvedPreset.timezone;
	if (pyTruthy(tz)) contextOptions.timezoneId = tz;
	const navLang = config["navigator.language"];
	if (pyTruthy(navLang)) contextOptions.locale = navLang;

	return {
		init_script: initScript,
		context_options: contextOptions,
		config,
		preset: resolvedPreset,
	};
}

// ---------------------------------------------------------------------------
// fpgen fingerprints
// ---------------------------------------------------------------------------

/** Casts an fpgen fingerprint node onto Camoufox config properties. */
function castToProperties(
	camoufoxData: Config,
	castEnum: Record<string, any>,
	fpDict: Record<string, any>,
	ffVersion?: string | number | null,
): void {
	for (const [key, raw] of Object.entries(fpDict)) {
		// Ignore non-truthy values
		if (!pyTruthy(raw)) continue;
		const typeKey = castEnum[key];
		if (!pyTruthy(typeKey)) continue;
		if (isPlainObject(raw)) {
			castToProperties(camoufoxData, typeKey, raw, ffVersion);
			continue;
		}
		let data: any = raw;
		// fpgen carries header values as a list; a single string is the value.
		if (Array.isArray(data)) {
			if (data.length === 1 && typeof data[0] === "string") data = data[0];
			else continue;
		}
		if (
			typeof typeKey === "string" &&
			typeKey.startsWith("screen.") &&
			(typeof data === "boolean" ||
				(typeof data === "number" && Number.isInteger(data))) &&
			Number(data) < 0
		) {
			data = 0;
		}
		if (ffVersion && typeof data === "string") {
			data = data.replace(
				/(?<!\d)(1[0-9]{2})(\.0)(?!\d)/g,
				(_m, _v, dot) => `${ffVersion}${dot}`,
			);
		}
		camoufoxData[typeKey] = data;
	}
}

/** Sets window.screenY from the generated screenX value. */
function handleScreenXY(
	camoufoxData: Config,
	fingerprint: Record<string, any>,
): void {
	if ("window.screenY" in camoufoxData) return;
	const screen = pyTruthy(fingerprint.screen) ? fingerprint.screen : {};
	const window = pyTruthy(fingerprint.window) ? fingerprint.window : {};
	const screenX = window.screenX;
	if (!pyTruthy(screenX)) {
		camoufoxData["window.screenX"] = 0;
		camoufoxData["window.screenY"] = 0;
		return;
	}
	if (Number.isInteger(screenX) && screenX >= -50 && screenX <= 50) {
		camoufoxData["window.screenY"] = screenX;
		return;
	}
	// The generator thinks the browser is windowed. Randomly generate a screenY.
	const screenY = num(screen.availHeight || 0) - num(window.outerHeight || 0);
	if (screenY === 0) camoufoxData["window.screenY"] = 0;
	else if (screenY > 0)
		camoufoxData["window.screenY"] = random.randrange(0, screenY);
	else camoufoxData["window.screenY"] = random.randrange(screenY, 0);
}

/** Converts an fpgen fingerprint to a Camoufox config. */
export function fromFpgen(
	fingerprint: Record<string, any>,
	ffVersion?: string | number | null,
): Config {
	const camoufoxData: Config = {};
	castToProperties(camoufoxData, FPGEN_DATA, fingerprint, ffVersion);
	handleScreenXY(camoufoxData, fingerprint);
	setAppVersion(camoufoxData);
	return camoufoxData;
}

/** Sets a custom outer window size and centers it in the screen (in place). */
export function handleWindowSize(
	fp: Record<string, any>,
	outerWidth: number,
	outerHeight: number,
): void {
	fp.screen ??= {};
	fp.window ??= {};
	const screen = fp.screen;
	const window = fp.window;

	window.screenX =
		(window.screenX || 0) +
		Math.floor(((screen.width || outerWidth) - outerWidth) / 2);
	window.screenY = Math.floor(
		((screen.height || outerHeight) - outerHeight) / 2,
	);

	if (pyTruthy(window.innerWidth)) {
		window.innerWidth = Math.max(
			outerWidth - (window.outerWidth || 0) + window.innerWidth,
			0,
		);
	}
	if (pyTruthy(window.innerHeight)) {
		window.innerHeight = Math.max(
			outerHeight - (window.outerHeight || 0) + window.innerHeight,
			0,
		);
	}
	window.outerWidth = outerWidth;
	window.outerHeight = outerHeight;
}

export interface GenerateFingerprintOptions {
	/** Outer window size [width, height], applied after generation. */
	window?: readonly [number, number] | null;
	/** A bound on the generated screen (best-effort, see below). */
	screen?: Screen | null;
	/** Camoufox OS name(s): 'linux', 'macos', 'windows' (or lin/mac/win). */
	os?: string | readonly string[] | null;
	/** Any other fpgen conditions. */
	[condition: string]: any;
}

/**
 * Generates a Firefox fingerprint with fpgen (the model must be installed:
 * `await ensureModel()` from fpgen).
 *
 * `screen` bounds the generated screen; `window` overrides the outer window
 * size afterwards; `os` is Camoufox's name for the platform; anything else is
 * passed to fpgen as a condition.
 */
export function generateFingerprint({
	window,
	screen,
	os,
	...conditions
}: GenerateFingerprintOptions = {}): Record<string, any> {
	if (os?.length) {
		const names = typeof os === "string" ? [os] : [...os];
		const resolved = names.map((n) => {
			const v = FPGEN_OS[String(n).toLowerCase()];
			if (!v) throw new Error(`Unknown OS for fingerprint generation: '${n}'`);
			return v;
		});
		// fpgen takes one value or a predicate, not a list of alternatives.
		// DIVERGENCE from Python: fpgen hands a predicate the CASEFOLDED value
		// ("linux"), so Python's `lambda v: v in set(resolved)` compares it with
		// "Linux" and never matches -- os=['linux', 'windows'] always raises
		// InvalidConstraints there. The comparison here is casefolded, so a
		// list of OSes works.
		const allowed = new Set(resolved.map((r) => r.toLowerCase()));
		conditions.os =
			resolved.length === 1
				? resolved[0]
				: (v: unknown) => allowed.has(String(v).toLowerCase());
	}
	const screenConditions = screen ? screen.asConditions() : {};
	let fingerprint: Record<string, any>;
	try {
		fingerprint = generate({
			browser: "Firefox",
			...conditions,
			...screenConditions,
		});
	} catch (err) {
		if (
			!Object.keys(screenConditions).length ||
			!(err instanceof InvalidConstraints)
		)
			throw err;
		// The screen bound is best-effort: a display the pool has nothing to fit
		// must not stop a fingerprint being generated. clampScreenToDisplay()
		// still bounds the result afterwards.
		fingerprint = generate({ browser: "Firefox", ...conditions });
	}
	if (window) handleWindowSize(fingerprint, window[0], window[1]);
	return fingerprint;
}
