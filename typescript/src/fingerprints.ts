/**
 * Fingerprint generation: BrowserForge synthesis, real presets, per-context
 * identities, and the geometry/arch corrections applied on top of both.
 *
 * TypeScript twin of python/src/fingerprints.py.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import {
	type Fingerprint,
	FingerprintGenerator,
	type FingerprintGeneratorOptions,
	type ScreenFingerprint,
} from "fingerprint-generator";
import { normalizeLocale } from "./locale.js";
import BROWSERFORGE_DATA from "./mappings/browserforge.config.js";
import { LOCAL_DATA } from "./pkgman.js";
import { sampleWebGL, type TargetOS } from "./webgl/sample.js";

export const SUPPORTED_OS = ["linux", "macos", "windows"] as const;
export type SupportedOS = (typeof SUPPORTED_OS)[number];

const FP_GENERATOR = new FingerprintGenerator({
	browsers: ["firefox"],
	operatingSystems: [...SUPPORTED_OS] as any,
});

// Bundled real fingerprint presets
const PRESETS_FILE = path.join(LOCAL_DATA, "fingerprint-presets.json");
const PRESETS_V150_FILE = path.join(
	LOCAL_DATA,
	"fingerprint-presets-v150.json",
);
// Firefox major version at which the v150 preset bundle becomes preferred.
const PRESETS_V150_MIN_FF = 149;
const PRESETS_CACHE = new Map<string, PresetBundle | null>();

export interface Preset {
	navigator?: Record<string, any>;
	screen?: Record<string, any>;
	webgl?: Record<string, any>;
	timezone?: string;
	fonts?: string[];
	speechVoices?: Array<string | VoiceObject>;
	[key: string]: any;
}

interface PresetBundle {
	presets?: Partial<Record<SupportedOS, Preset[]>>;
	[key: string]: any;
}

export interface VoiceObject {
	name: string;
	lang: string;
	voiceUri: string;
	isDefault: boolean;
	isLocalService: boolean;
}

// CreepJS OS marker fonts used for OS detection
const MACOS_MARKER_FONTS = [
	"Helvetica Neue",
	"PingFang HK",
	"PingFang SC",
	"PingFang TC",
];
const LINUX_MARKER_FONTS = ["Arimo", "Cousine", "Tinos", "Twemoji Mozilla"];
const WINDOWS_MARKER_FONTS = [
	"Segoe UI",
	"Tahoma",
	"Cambria Math",
	"Nirmala UI",
];

/** Add any missing marker fonts to the font list (in-place). */
function ensureMarkerFonts(fonts: string[], markers: string[]): void {
	const existing = new Set(fonts);
	for (const m of markers) {
		if (!existing.has(m)) fonts.push(m);
	}
}

// OS font lists loaded from fonts.json
let osFontsCache: Record<string, string[]> | null = null;

function loadOsFonts(): Record<string, string[]> {
	if (osFontsCache) return osFontsCache;
	osFontsCache = JSON.parse(
		fs.readFileSync(path.join(LOCAL_DATA, "fonts.json"), "utf-8"),
	);
	return osFontsCache as Record<string, string[]>;
}

// Essential fonts per OS that must always be included in subsets
const ESSENTIAL_FONTS_MACOS = [
	"Arial",
	"Helvetica",
	"Times New Roman",
	"Courier New",
	"Verdana",
	"Georgia",
	"Trebuchet MS",
	"Tahoma",
	"Helvetica Neue",
	"Lucida Grande",
	"Menlo",
	"Monaco",
	"Geneva",
	"PingFang HK",
	"PingFang SC",
	"PingFang TC",
];
const ESSENTIAL_FONTS_WINDOWS = [
	"Arial",
	"Times New Roman",
	"Courier New",
	"Verdana",
	"Georgia",
	"Trebuchet MS",
	"Tahoma",
	"Segoe UI",
	"Calibri",
	"Cambria Math",
	"Nirmala UI",
	"Consolas",
];
const ESSENTIAL_FONTS_LINUX = [
	"Arimo",
	"Cousine",
	"Tinos",
	"Twemoji Mozilla",
	"Noto Sans Devanagari",
	"Noto Sans JP",
	"Noto Sans KR",
	"Noto Sans SC",
	"Noto Sans TC",
];

const OS_TO_SHORT: Record<string, TargetOS> = {
	macos: "mac",
	windows: "win",
	linux: "lin",
	mac: "mac",
	win: "win",
	lin: "lin",
};

/** Fisher-Yates sample of `count` items, the analogue of random.sample. */
function sample<T>(items: T[], count: number): T[] {
	const pool = [...items];
	const picked: T[] = [];
	for (let i = 0; i < count && pool.length; i++) {
		const idx = Math.floor(Math.random() * pool.length);
		picked.push(pool.splice(idx, 1)[0]);
	}
	return picked;
}

function randint(min: number, max: number): number {
	return Math.floor(Math.random() * (max - min + 1)) + min;
}

function randrange(start: number, stop: number): number {
	// Python's randrange(start, stop) excludes stop.
	return Math.floor(Math.random() * (stop - start)) + start;
}

/**
 * Generate a random subset of fonts for the given OS. Picks a random
 * percentage between 30-78% of non-essential fonts, always including the
 * essential + marker fonts.
 */
export function generateRandomFontSubset(targetOs: string): string[] {
	const osFontsData = loadOsFonts();
	const osKey = OS_TO_SHORT[targetOs] ?? "mac";
	const fullList = osFontsData[osKey] ?? osFontsData.mac ?? [];

	let essential: Set<string>;
	let markers: string[];
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

	// Split into essential and non-essential
	const result = fullList.filter((f) => essential.has(f));
	const nonEssential = fullList.filter((f) => !essential.has(f));

	// Random percentage between 30-78%
	const pct = 30 + Math.floor(Math.random() * 49);
	const count = Math.round((pct / 100) * nonEssential.length);

	result.push(
		...(count < nonEssential.length
			? sample(nonEssential, count)
			: nonEssential),
	);

	ensureMarkerFonts(result, markers);

	return result;
}

// OS voice lists loaded from voices.json, parsed into [name, lang, type].
let osVoicesCache: Record<string, Array<[string, string, string]>> | null =
	null;

/**
 * Load OS voice lists from voices.json as [name, lang, type] tuples.
 *
 * Each entry is "Name:lang:type" (type is "local" or "remote"). Voice names
 * may contain parens/commas but not colons, so a last-two-colons split is safe.
 */
function loadOsVoices(): Record<string, Array<[string, string, string]>> {
	if (osVoicesCache) return osVoicesCache;
	const raw: Record<string, string[]> = JSON.parse(
		fs.readFileSync(path.join(LOCAL_DATA, "voices.json"), "utf-8"),
	);
	osVoicesCache = {};
	for (const [osKey, entries] of Object.entries(raw)) {
		const parsed: Array<[string, string, string]> = [];
		for (const entry of entries) {
			const parsedEntry = splitVoiceEntry(entry);
			if (parsedEntry) parsed.push(parsedEntry);
		}
		osVoicesCache[osKey] = parsed;
	}
	return osVoicesCache;
}

function splitVoiceEntry(entry: string): [string, string, string] | null {
	const last = entry.lastIndexOf(":");
	if (last < 0) return null;
	const vtype = entry.slice(last + 1);
	const before = entry.slice(0, last);
	const langsep = before.lastIndexOf(":");
	if (langsep < 0) return null;
	const lang = before.slice(langsep + 1);
	const name = before.slice(0, langsep);
	if (!name || !lang) return null;
	return [name, lang, vtype];
}

// Essential speech voices per OS that must always be included in subsets
const ESSENTIAL_VOICES_MACOS = [
	"Samantha",
	"Alex",
	"Fred",
	"Victoria",
	"Karen",
	"Daniel",
];

// Real Firefox speechSynthesis URI prefixes per backend.
//   macOS NSSpeechSynthesizer -> "urn:moz-tts:osx:<dotted-slug>"
//   Windows SAPI              -> "urn:moz-tts:sapi:<dotted-slug>"
//   Linux speech-dispatcher   -> "urn:moz-tts:speechd:<escaped-name>?<lang>"
const VOICE_URI_PREFIX: Record<string, string> = {
	mac: "urn:moz-tts:osx:",
	win: "urn:moz-tts:sapi:",
	lin: "urn:moz-tts:speechd:",
};

/** Stable dotted slug for mac/win URIs (shape-plausible, not catalog-exact). */
function voiceUriSlug(name: string): string {
	return name
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, ".")
		.replace(/^\.|\.$/g, "");
}

/** Build a voiceUri matching what real Firefox emits for the OS backend. */
function voiceUri(osKey: string, name: string, lang: string): string {
	if (osKey === "lin") {
		// Firefox's SpeechDispatcherService.cpp builds:
		//   "urn:moz-tts:speechd:" + NS_EscapeURL(name, OnlyNonASCII|Spaces) + "?" + lang
		// i.e. spaces -> %20 and non-ASCII bytes -> %XX, ASCII punctuation intact.
		let escaped = "";
		for (const ch of name) {
			if (ch === " ") {
				escaped += "%20";
			} else if (ch.charCodeAt(0) <= 0x7f) {
				escaped += ch;
			} else {
				for (const byte of new TextEncoder().encode(ch)) {
					escaped += `%${byte.toString(16).toUpperCase().padStart(2, "0")}`;
				}
			}
		}
		return `${VOICE_URI_PREFIX.lin}${escaped}?${lang}`;
	}
	return `${VOICE_URI_PREFIX[osKey] ?? ""}${voiceUriSlug(name)}`;
}

/**
 * Generate the speech voice list for the given OS as MaskConfig objects.
 *
 * Returns {lang, name, voiceUri, isDefault, isLocalService} entries, the shape
 * MaskConfig::MVoices() requires (it silently drops any entry missing a field,
 * so raw name strings would register nothing).
 *
 * Without this override, Firefox registers the HOST machine's
 * speech-dispatcher / SAPI / NSSpeech voices, leaking the OS the wrapper
 * actually runs on. We therefore emit a list for EVERY target OS:
 *   macOS:   essential voices + a random 40-80% of the rest.
 *   Windows: full SAPI set (subsetting a fixed list reads as suspicious).
 *   Linux:   full espeak-ng base-language set as enumerated by
 *            speech-dispatcher -- the fixed list a Linux Firefox exposes.
 */
export function generateRandomVoiceSubset(
	targetOs: string,
	locale?: string,
): VoiceObject[] {
	const osVoicesData = loadOsVoices();
	const osKey = OS_TO_SHORT[targetOs] ?? "mac";
	const fullList = osVoicesData[osKey] ?? [];

	if (!fullList.length) return [];

	let selected: Array<[string, string, string]>;
	if (osKey === "win" || osKey === "lin") {
		// Fixed lists across installs (SAPI / espeak-ng) -- ship the whole set.
		selected = [...fullList];
	} else {
		// macOS: essential voices + random 40-80% of the rest.
		const essential = new Set(ESSENTIAL_VOICES_MACOS);
		const result = fullList.filter((v) => essential.has(v[0]));
		const nonEssential = fullList.filter((v) => !essential.has(v[0]));
		const pct = 40 + Math.floor(Math.random() * 41); // 40-80%
		const count = Math.round((pct / 100) * nonEssential.length);
		result.push(
			...(count < nonEssential.length
				? sample(nonEssential, count)
				: nonEssential),
		);
		selected = result;
	}

	const voices: VoiceObject[] = selected.map(([name, lang, vtype]) => ({
		name,
		lang,
		voiceUri: voiceUri(osKey, name, lang),
		isDefault: false,
		isLocalService: vtype === "local",
	}));

	// Mark a default voice matching the spoofed locale prefix so it lines up
	// with Intl.DateTimeFormat().resolvedOptions().locale (CreepJS flags a
	// voiceLangMismatch otherwise).
	if (voices.length) {
		const prefix = locale ? locale.split("-")[0].toLowerCase() : "en";
		let idx = locale
			? voices.findIndex((v) => v.lang.toLowerCase() === locale.toLowerCase())
			: -1;
		if (idx < 0) {
			idx = voices.findIndex(
				(v) => v.lang.split("-")[0].toLowerCase() === prefix,
			);
		}
		if (idx < 0) idx = 0;
		voices[idx].isDefault = true;
	}

	return voices;
}

/**
 * Coerce a preset's `speechVoices` into MaskConfig voice objects.
 *
 * Presets historically store voices as "Name:lang:type" strings, which the C++
 * MaskConfig::MVoices() silently drops (it needs full objects).
 */
export function normalizePresetVoices(
	voices: Array<string | VoiceObject>,
	targetOs: string,
): VoiceObject[] {
	const osKey = OS_TO_SHORT[targetOs] ?? "mac";
	const result: VoiceObject[] = [];
	for (const entry of voices) {
		if (typeof entry === "object") {
			result.push(entry);
			continue;
		}
		const parsed = splitVoiceEntry(entry);
		if (!parsed) continue;
		const [name, lang, vtype] = parsed;
		result.push({
			name,
			lang,
			voiceUri: voiceUri(osKey, name, lang),
			isDefault: false,
			isLocalService: vtype === "local",
		});
	}
	if (result.length && !result.some((v) => v.isDefault)) {
		result[0].isDefault = true;
	}
	return result;
}

/**
 * Force navigator.platform AND navigator.oscpu to match the UA's arch.
 *
 * ~8% of Linux Firefox fingerprints in the BrowserForge pool report
 * "Linux armv81" for platform/oscpu while the UA says "Linux x86_64". That arch
 * mismatch is itself a CreepJS lie signal (CreepJS cross-checks oscpu,
 * platform, and the UA arch). Mac/Windows pools are consistent and need no
 * correction.
 */
export function fixNavigatorArch(
	config: Record<string, any>,
	targetOs: string,
): void {
	if (targetOs !== "lin") return;
	const ua = config["navigator.userAgent"];
	if (!ua) return;

	let target = "";
	if (ua.includes("Linux x86_64")) target = "Linux x86_64";
	else if (ua.includes("Linux i686")) target = "Linux i686";
	if (!target) return;

	if (config["navigator.platform"] !== target) {
		config["navigator.platform"] = target;
	}
	if (config["navigator.oscpu"] !== target) {
		config["navigator.oscpu"] = target;
	}
}

/**
 * Ensure screen.availHeight < screen.height so CreepJS's noTaskbar flag
 * (screen.height === availHeight and screen.width === availWidth) doesn't flip.
 *
 * Every desktop OS keeps some chrome visible (Mac menu bar ~25px, Win taskbar
 * ~40px, Linux panel ~27px); the BrowserForge pool occasionally ships
 * fingerprints with identical screen/avail values which leak as a headless
 * tell. Also clamps window.outerHeight (and innerHeight) to the new avail so
 * the window isn't taller than the available area.
 */
export function fixScreenNoTaskbar(
	config: Record<string, any>,
	targetOs: string,
): void {
	const sw = config["screen.width"];
	const sh = config["screen.height"];
	const aw = config["screen.availWidth"];
	const ah = config["screen.availHeight"];
	if (!(sw && sh && aw === sw && ah === sh)) return;

	const taskbar = targetOs === "win" ? 40 : targetOs === "mac" ? 25 : 27;
	const newAvail = sh - taskbar;
	config["screen.availHeight"] = newAvail;

	const oh = config["window.outerHeight"];
	if (oh && oh > newAvail) {
		const ih = config["window.innerHeight"];
		const chrome = ih ? oh - ih : 0;
		config["window.outerHeight"] = newAvail;
		if (ih) config["window.innerHeight"] = newAvail - chrome;
	}
}

/**
 * Enforce inner <= outer <= avail <= screen on BOTH axes.
 *
 * The browser faithfully reports whatever we inject, so a BrowserForge
 * fingerprint that ships e.g. outerWidth > screen.width leaks as an impossible
 * geometry. Shrink each level down to its container, preserving the chrome
 * delta between outer and inner where possible.
 */
export function clampWindowDimensions(config: Record<string, any>): void {
	for (const axis of ["Width", "Height"] as const) {
		const screen = config[`screen.${axis.toLowerCase()}`];
		const avail = config[`screen.avail${axis}`];
		const outer = config[`window.outer${axis}`];
		const inner = config[`window.inner${axis}`];

		// avail must not exceed screen
		if (screen && avail && avail > screen) {
			config[`screen.avail${axis}`] = screen;
		}
		const availClamped = config[`screen.avail${axis}`] ?? screen;

		// outer must not exceed avail (or screen if avail is unknown)
		const outerCap = availClamped ?? screen;
		if (outer && outerCap && outer > outerCap) {
			const chrome = inner ? Math.max(0, outer - inner) : 0;
			config[`window.outer${axis}`] = outerCap;
			if (inner) {
				config[`window.inner${axis}`] = Math.max(1, outerCap - chrome);
			}
		}

		// inner must not exceed outer
		const outerClamped = config[`window.outer${axis}`] ?? outer;
		const innerNow = config[`window.inner${axis}`];
		if (innerNow && outerClamped && innerNow > outerClamped) {
			config[`window.inner${axis}`] = outerClamped;
		}
	}
}

/**
 * Shrink screen.width/height down to the bounds of the real display.
 *
 * BrowserForge takes a Screen constraint but drops it silently whenever it
 * filters the fingerprint pool too far, so the bound from getScreenCons() is
 * best-effort only and a 1366x768 laptop routinely gets a 2560x1440
 * fingerprint. browser-init.patch resizes the real chrome window to
 * window.outerWidth/outerHeight, so an unbounded value renders past the edge of
 * the monitor (daijro/camoufox#499).
 *
 * Keeps the taskbar delta (screen - avail) intact so fixScreenNoTaskbar's
 * invariant survives. Callers must run clampWindowDimensions afterwards to
 * cascade the new bounds down to avail/outer/inner.
 */
export function clampScreenToDisplay(
	config: Record<string, any>,
	maxWidth?: number,
	maxHeight?: number,
): void {
	for (const [axis, cap] of [
		["width", maxWidth],
		["height", maxHeight],
	] as const) {
		const screen = config[`screen.${axis}`];
		if (!(screen && cap) || screen <= cap) continue;
		const availKey =
			axis === "width" ? "screen.availWidth" : "screen.availHeight";
		const avail = config[availKey];
		config[`screen.${axis}`] = cap;
		if (avail) {
			config[availKey] = Math.max(1, cap - Math.max(0, screen - avail));
		}
	}
}

/**
 * Keep the window box inside the screen: 0 <= screenX/Y <= screen - outer.
 *
 * BrowserForge's screenX/screenY are consistent with the screen it generated
 * them against, so clampScreenToDisplay invalidates them. A window positioned
 * partly off its own reported screen is an impossible geometry.
 */
export function clampWindowPosition(config: Record<string, any>): void {
	for (const [axis, posKey] of [
		["Width", "window.screenX"],
		["Height", "window.screenY"],
	] as const) {
		const screen = config[`screen.${axis.toLowerCase()}`];
		const outer = config[`window.outer${axis}`];
		const pos = config[posKey];
		if (pos === undefined || pos === null || !screen || !outer) continue;
		config[posKey] = Math.max(0, Math.min(pos, screen - outer));
	}
}

/**
 * Spoof navigator.mediaDevices.enumerateDevices() so headless contexts expose a
 * plausible device list.
 *
 * A real desktop browser without explicit mic permission reports one
 * audioinput + one videoinput; an empty list is a headless tell. The patched
 * MediaDevices::FilterExposedDevices reads
 * mediaDevices:{enabled,micros,webcams,speakers}.
 */
export function setMediaDevicesDefaults(config: Record<string, any>): void {
	if (Object.keys(config).some((k) => k.startsWith("mediaDevices:"))) return;
	config["mediaDevices:enabled"] = true;
	config["mediaDevices:micros"] = 1;
	config["mediaDevices:webcams"] = 1;
	config["mediaDevices:speakers"] = 0;
}

/**
 * Pick the bundled-presets file appropriate for a given Firefox version.
 *
 * For Firefox >= PRESETS_V150_MIN_FF, prefer the v150 bundle (real
 * fingerprints scraped from contemporary browsers); otherwise fall back to the
 * original bundle.
 */
function selectPresetsFile(ffVersion?: string | number): string {
	let major = 0;
	if (ffVersion !== undefined && ffVersion !== null) {
		const parsed = Number.parseInt(String(ffVersion).split(".", 1)[0], 10);
		major = Number.isNaN(parsed) ? 0 : parsed;
	}
	if (major >= PRESETS_V150_MIN_FF && fs.existsSync(PRESETS_V150_FILE)) {
		return PRESETS_V150_FILE;
	}
	return PRESETS_FILE;
}

/**
 * Load the bundled fingerprint presets from disk.
 */
export function loadPresets(ffVersion?: string | number): PresetBundle | null {
	const presetPath = selectPresetsFile(ffVersion);
	if (PRESETS_CACHE.has(presetPath)) {
		return PRESETS_CACHE.get(presetPath) ?? null;
	}
	if (!fs.existsSync(presetPath)) {
		PRESETS_CACHE.set(presetPath, null);
		return null;
	}
	const bundle = JSON.parse(fs.readFileSync(presetPath, "utf-8"));
	PRESETS_CACHE.set(presetPath, bundle);
	return bundle;
}

// Map OS names to preset keys
const OS_TO_PRESET_KEY: Record<string, SupportedOS> = {
	windows: "windows",
	macos: "macos",
	linux: "linux",
	win: "windows",
	mac: "macos",
	lin: "linux",
};

/**
 * Get a random preset for the given OS. Returns null when none are available.
 */
export function getRandomPreset(
	os?: string | string[],
	ffVersion?: string | number,
): Preset | null {
	const presets = loadPresets(ffVersion);
	if (!presets) return null;

	const allOsKeys: SupportedOS[] = ["macos", "windows", "linux"];
	let osKeys: string[];
	if (os) {
		const list = Array.isArray(os) ? os : [os];
		osKeys = list.map((o) => OS_TO_PRESET_KEY[o] ?? o);
	} else {
		osKeys = allOsKeys;
	}

	const candidates: Preset[] = [];
	for (const key of osKeys) {
		candidates.push(...(presets.presets?.[key as SupportedOS] ?? []));
	}

	if (!candidates.length) return null;
	return candidates[Math.floor(Math.random() * candidates.length)];
}

function targetOsFromPlatform(plat: string): SupportedOS {
	if (plat === "MacIntel") return "macos";
	if (plat === "Win32") return "windows";
	if (plat.toLowerCase().includes("linux")) return "linux";
	return "macos";
}

function oscpuFromPlatform(plat: string): string | undefined {
	if (plat === "MacIntel") return "Intel Mac OS X 10.15";
	if (plat === "Win32") return "Windows NT 10.0; Win64; x64";
	if (plat.toLowerCase().includes("linux")) return "Linux x86_64";
	return undefined;
}

/**
 * Convert a real fingerprint preset to CAMOU_CONFIG format.
 */
export function fromPreset(
	preset: Preset,
	ffVersion?: string,
): Record<string, any> {
	const config: Record<string, any> = {};

	const nav = preset.navigator ?? {};
	if (nav.userAgent) {
		let ua: string = nav.userAgent;
		// Replace the Firefox version in the UA when ff_version is provided
		if (ffVersion) {
			ua = ua.replace(/Firefox\/\d+\.0/g, `Firefox/${ffVersion}.0`);
			ua = ua.replace(/rv:\d+\.0/g, `rv:${ffVersion}.0`);
		}
		config["navigator.userAgent"] = ua;
	}
	if (nav.platform) config["navigator.platform"] = nav.platform;
	if (nav.hardwareConcurrency) {
		config["navigator.hardwareConcurrency"] = nav.hardwareConcurrency;
	}
	if (nav.oscpu) {
		config["navigator.oscpu"] = nav.oscpu;
	} else if (nav.platform) {
		// Derive oscpu from platform when not explicitly in the preset
		const oscpu = oscpuFromPlatform(nav.platform);
		if (oscpu) config["navigator.oscpu"] = oscpu;
	}
	if ("maxTouchPoints" in nav) {
		config["navigator.maxTouchPoints"] = nav.maxTouchPoints;
	}

	const screen = preset.screen ?? {};
	if (screen.width) config["screen.width"] = screen.width;
	if (screen.height) config["screen.height"] = screen.height;
	if (screen.colorDepth) {
		config["screen.colorDepth"] = screen.colorDepth;
		config["screen.pixelDepth"] = screen.colorDepth;
	}
	if (screen.availWidth) config["screen.availWidth"] = screen.availWidth;
	if (screen.availHeight) config["screen.availHeight"] = screen.availHeight;

	const webgl = preset.webgl ?? {};
	if (webgl.unmaskedVendor) config["webGl:vendor"] = webgl.unmaskedVendor;
	if (webgl.unmaskedRenderer) config["webGl:renderer"] = webgl.unmaskedRenderer;

	// Unique random seeds per launch (1 to 2^32-1; 0 is a no-op in C++)
	config["fonts:spacing_seed"] = randint(1, 4_294_967_295);
	config["audio:seed"] = randint(1, 4_294_967_295);
	config["canvas:seed"] = randint(1, 4_294_967_295);

	if (preset.timezone) config.timezone = preset.timezone;

	// Unique random font subset from the OS font list.
	const targetOs = targetOsFromPlatform(nav.platform ?? "");
	try {
		config.fonts = generateRandomFontSubset(targetOs);
	} catch {
		// Fallback to preset fonts if font generation fails
		if (preset.fonts?.length) {
			const fonts = [...preset.fonts];
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

	// Unique random voice subset from the OS voice list
	try {
		config.voices = generateRandomVoiceSubset(targetOs);
	} catch {
		if (preset.speechVoices?.length) {
			config.voices = normalizePresetVoices(preset.speechVoices, targetOs);
		}
	}

	return config;
}

interface InitValues {
	fontSpacingSeed?: number;
	audioFingerprintSeed?: number;
	canvasSeed?: number;
	navigatorPlatform?: string;
	navigatorOscpu?: string;
	navigatorUserAgent?: string;
	hardwareConcurrency?: number;
	webglVendor?: string;
	webglRenderer?: string;
	screenWidth?: number;
	screenHeight?: number;
	screenColorDepth?: number;
	timezone?: string;
	fontList?: string[];
	speechVoices?: Array<string | VoiceObject>;
	webrtcIP?: string;
}

/**
 * Builds the JavaScript init script that calls the per-context window.setXxx()
 * functions. Those self-destruct after their first call, so they must run via
 * addInitScript.
 */
export function buildInitScript(values: InitValues): string {
	const lines = ["(function(v) {", "  var w = window;"];

	const setters: Array<[keyof InitValues, string]> = [
		["fontSpacingSeed", "setFontSpacingSeed"],
		["audioFingerprintSeed", "setAudioFingerprintSeed"],
		["canvasSeed", "setCanvasSeed"],
		["navigatorPlatform", "setNavigatorPlatform"],
		["navigatorOscpu", "setNavigatorOscpu"],
		["navigatorUserAgent", "setNavigatorUserAgent"],
		["hardwareConcurrency", "setNavigatorHardwareConcurrency"],
		["webglVendor", "setWebGLVendor"],
		["webglRenderer", "setWebGLRenderer"],
	];

	for (const [key, fnName] of setters) {
		const val = values[key];
		if (val !== undefined && val !== null) {
			lines.push(
				`  if (typeof w.${fnName} === "function") w.${fnName}(${JSON.stringify(val)});`,
			);
		}
	}

	// Screen dimensions (requires width + height together)
	const sw = values.screenWidth;
	const sh = values.screenHeight;
	if (sw && sh) {
		lines.push(
			`  if (typeof w.setScreenDimensions === "function") w.setScreenDimensions(${sw}, ${sh});`,
		);
		const scd = values.screenColorDepth;
		if (scd) {
			lines.push(
				`  if (typeof w.setScreenColorDepth === "function") w.setScreenColorDepth(${scd});`,
			);
		}
	}

	// Timezone -- only call setTimezone() when we have an explicit value.
	// Without this, the C++ MaskConfig fallback (from CAMOU_CONFIG set by geoip
	// in launchOptions) handles timezone for both the main thread and workers.
	const tz = values.timezone;
	if (tz) {
		lines.push(
			`  if (typeof w.setTimezone === "function") w.setTimezone(${JSON.stringify(tz)});`,
		);
	}

	// WebRTC IP
	const ip = values.webrtcIP;
	lines.push(
		ip
			? `  if (typeof w.setWebRTCIPv4 === "function") w.setWebRTCIPv4(${JSON.stringify(ip)});`
			: '  if (typeof w.setWebRTCIPv4 === "function") w.setWebRTCIPv4("");',
	);

	// Font list (comma-separated)
	const fontList = values.fontList;
	if (fontList?.length) {
		lines.push(
			`  if (typeof w.setFontList === "function") w.setFontList(${JSON.stringify(fontList.join(","))});`,
		);
	}

	// Speech voices (comma-separated names). config.voices holds MaskConfig
	// voice objects; extract the display name from each (tolerating a legacy
	// list of plain name strings).
	const voices = values.speechVoices;
	if (voices?.length) {
		const names = voices.map((v) => (typeof v === "object" ? v.name : v));
		lines.push(
			`  if (typeof w.setSpeechVoices === "function") w.setSpeechVoices(${JSON.stringify(names.join(","))});`,
		);
	}

	lines.push("})();");
	return lines.join("\n");
}

export interface ContextFingerprint {
	init_script: string;
	context_options: Record<string, any>;
	config: Record<string, any>;
	preset: Preset;
}

/**
 * Generate fingerprint values for a single per-context identity. Returns the
 * init script (a JS string) plus the Playwright context options.
 *
 * By default, uses BrowserForge for infinite unique synthetic fingerprints.
 * Pass a preset to use a real fingerprint preset instead.
 *
 * @param timezone IANA timezone string (e.g. 'Europe/London'). Takes priority
 *   over any timezone from the preset.
 * @param locale BCP-47 locale string (e.g. 'en-GB'). Also sets
 *   context_options.locale for Playwright.
 * @param configOverrides CAMOU_CONFIG keys to override after the config is
 *   built but before the init script is rendered. Useful for disabling
 *   perturbation (e.g. {'fonts:spacing_seed': 0}).
 */
export async function generateContextFingerprint({
	preset,
	os,
	ff_version,
	webrtc_ip,
	timezone,
	locale,
	config_overrides,
}: {
	preset?: Preset | null;
	os?: string;
	ff_version?: string;
	webrtc_ip?: string;
	timezone?: string;
	locale?: string;
	config_overrides?: Record<string, any>;
} = {}): Promise<ContextFingerprint> {
	let config: Record<string, any>;
	let nav: Record<string, any>;
	let screen: Record<string, any>;
	let webgl: Record<string, any>;
	let resolvedPreset: Preset;

	if (preset) {
		// Use a real fingerprint preset
		config = fromPreset(preset, ff_version);
		nav = preset.navigator ?? {};
		screen = preset.screen ?? {};
		webgl = preset.webgl ?? {};
		resolvedPreset = preset;
	} else {
		// Fall back to BrowserForge synthetic generation
		const fp = generateFingerprint(undefined, {
			operatingSystems: os ? ([os] as any) : undefined,
		});
		config = fromBrowserforge(fp, ff_version);

		// Add seeds (BrowserForge doesn't generate these)
		setDefault(config, "fonts:spacing_seed", randint(1, 4_294_967_295));
		setDefault(config, "audio:seed", randint(1, 4_294_967_295));
		setDefault(config, "canvas:seed", randint(1, 4_294_967_295));

		// Determine target OS from platform for font/voice generation
		const plat: string = config["navigator.platform"] ?? "";
		const osName = targetOsFromPlatform(plat);

		// Add fonts (BrowserForge doesn't generate these)
		if (!("fonts" in config)) {
			try {
				config.fonts = generateRandomFontSubset(osName);
			} catch {
				// Leave fonts unset; the launcher fills them in.
			}
		}

		// Add voices (BrowserForge doesn't generate these)
		if (!("voices" in config)) {
			try {
				config.voices = generateRandomVoiceSubset(osName);
			} catch {
				// Leave voices unset.
			}
		}

		// Derive oscpu if BrowserForge didn't provide it
		if (!("navigator.oscpu" in config)) {
			const oscpu = oscpuFromPlatform(plat);
			if (oscpu) config["navigator.oscpu"] = oscpu;
		}

		// Sample WebGL vendor/renderer (BrowserForge doesn't generate these)
		if (!config["webGl:vendor"] || !config["webGl:renderer"]) {
			const targetOs: TargetOS =
				(os ? OS_TO_SHORT[os] : undefined) ??
				OS_TO_SHORT[targetOsFromPlatform(plat)];
			try {
				const webglFp = await sampleWebGL(targetOs);
				delete webglFp.webGl2Enabled;
				Object.assign(config, webglFp);
			} catch {
				// No WebGL data for this OS; leave the config alone.
			}
		}

		// Build source objects from the BrowserForge config for init_values
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

	// Inject explicit timezone/locale into config (takes priority over preset)
	if (timezone) config.timezone = timezone;
	if (locale) {
		const parsed = normalizeLocale(locale);
		config["locale:language"] = parsed.language;
		config["locale:region"] = parsed.region;
		config["navigator.language"] = parsed.asString;
		if (parsed.script) config["locale:script"] = parsed.script;
	}

	// Apply caller overrides before rendering the init script
	if (config_overrides) Object.assign(config, config_overrides);

	const initValues: InitValues = {
		fontSpacingSeed: config["fonts:spacing_seed"],
		audioFingerprintSeed: config["audio:seed"],
		canvasSeed: config["canvas:seed"],
		navigatorPlatform: nav.platform,
		navigatorOscpu: config["navigator.oscpu"],
		navigatorUserAgent: config["navigator.userAgent"],
		hardwareConcurrency:
			nav.hardwareConcurrency || config["navigator.hardwareConcurrency"],
		webglVendor: webgl.unmaskedVendor,
		webglRenderer: webgl.unmaskedRenderer,
		screenWidth: screen.width,
		screenHeight: screen.height,
		screenColorDepth: screen.colorDepth,
		timezone:
			typeof resolvedPreset.timezone === "string"
				? resolvedPreset.timezone
				: config.timezone,
		fontList: config.fonts,
		speechVoices: config.voices,
		webrtcIP: webrtc_ip ?? "",
	};

	const initScript = buildInitScript(initValues);

	// Playwright context options that must be set at context creation
	const contextOptions: Record<string, any> = {};
	const ua = config["navigator.userAgent"];
	if (ua) contextOptions.userAgent = ua;
	const sw = screen.width;
	const sh = screen.height;
	if (sw && sh) {
		contextOptions.viewport = { width: sw, height: Math.max(sh - 28, 600) };
	}
	const dpr = screen.devicePixelRatio;
	if (dpr) contextOptions.deviceScaleFactor = dpr;
	const tz = config.timezone ?? resolvedPreset.timezone;
	if (tz) contextOptions.timezoneId = tz;
	const navLang = config["navigator.language"];
	if (navLang) contextOptions.locale = navLang;

	return {
		init_script: initScript,
		context_options: contextOptions,
		config,
		preset: resolvedPreset,
	};
}

function setDefault(
	target: Record<string, any>,
	key: string,
	value: any,
): void {
	if (!(key in target)) target[key] = value;
}

interface ExtendedScreen extends ScreenFingerprint {
	screenY?: number;
}

/**
 * Casts Browserforge fingerprints to Camoufox config properties.
 */
function castToProperties(
	camoufoxData: Record<string, any>,
	castEnum: Record<string, any>,
	bfDict: Record<string, any>,
	ffVersion?: string,
): void {
	for (const [key, rawData] of Object.entries(bfDict)) {
		// Ignore non-truthy values
		if (!rawData) continue;
		// Get the associated Camoufox property
		const typeKey = castEnum[key];
		if (!typeKey) continue;
		// If the value is an object, recurse
		if (typeof rawData === "object" && !Array.isArray(rawData)) {
			castToProperties(camoufoxData, typeKey, rawData, ffVersion);
			continue;
		}
		let data = rawData;
		// Fix values that are out of bounds
		if (
			typeof typeKey === "string" &&
			typeKey.startsWith("screen.") &&
			typeof data === "number" &&
			data < 0
		) {
			data = 0;
		}
		// Replace the Firefox versions with ffVersion
		if (ffVersion && typeof data === "string") {
			data = data.replace(/(?<!\d)(1[0-9]{2})(\.0)(?!\d)/g, `${ffVersion}$2`);
		}
		camoufoxData[typeKey] = data;
	}
}

/**
 * Sets window.screenY based on Browserforge's screenX value.
 */
export function handleScreenXY(
	camoufoxData: Record<string, any>,
	fpScreen: ScreenFingerprint,
): void {
	// Skip if manually provided
	if ("window.screenY" in camoufoxData) return;

	// Default screenX to 0 if not provided
	const screenX = fpScreen.screenX;
	if (!screenX) {
		camoufoxData["window.screenX"] = 0;
		camoufoxData["window.screenY"] = 0;
		return;
	}

	// If screenX is within [-50, 50], use the same value for screenY
	if (screenX >= -50 && screenX <= 50) {
		camoufoxData["window.screenY"] = screenX;
		return;
	}

	// Browserforge thinks the browser is windowed. Randomly generate a screenY.
	const screenY = fpScreen.availHeight - fpScreen.outerHeight;
	if (screenY === 0) {
		camoufoxData["window.screenY"] = 0;
	} else if (screenY > 0) {
		camoufoxData["window.screenY"] = randrange(0, screenY);
	} else {
		camoufoxData["window.screenY"] = randrange(screenY, 0);
	}
}

/**
 * Converts a Browserforge fingerprint to a Camoufox config.
 */
export function fromBrowserforge(
	fingerprint: Fingerprint,
	ffVersion?: string,
): Record<string, any> {
	const camoufoxData: Record<string, any> = {};
	castToProperties(
		camoufoxData,
		BROWSERFORGE_DATA,
		{ ...fingerprint },
		ffVersion,
	);
	handleScreenXY(camoufoxData, fingerprint.screen);
	return camoufoxData;
}

/**
 * Sets a custom outer window size and centers it in the screen.
 */
export function handleWindowSize(
	fp: Fingerprint,
	outerWidth: number,
	outerHeight: number,
): void {
	const sc: ExtendedScreen = { ...fp.screen, screenY: undefined };

	// Center the window on the screen
	sc.screenX += Math.floor((sc.width - outerWidth) / 2);
	sc.screenY = Math.floor((sc.height - outerHeight) / 2);

	// Update inner dimensions if set
	if (sc.innerWidth) {
		sc.innerWidth = Math.max(outerWidth - sc.outerWidth + sc.innerWidth, 0);
	}
	if (sc.innerHeight) {
		sc.innerHeight = Math.max(outerHeight - sc.outerHeight + sc.innerHeight, 0);
	}

	// Set outer dimensions
	sc.outerWidth = outerWidth;
	sc.outerHeight = outerHeight;
	fp.screen = sc;
}

/**
 * Canonical HTTP header casing: "accept-encoding" -> "Accept-Encoding".
 */
function canonicalHeaderName(name: string): string {
	return name
		.split("-")
		.map((part) => (part ? part[0].toUpperCase() + part.slice(1) : part))
		.join("-");
}

/**
 * Generates a Firefox fingerprint with Browserforge.
 *
 * `fingerprint-generator` hands the headers back *beside* the fingerprint and
 * lowercased, whereas browserforge's Python `Fingerprint` dataclass carries
 * them on the object with canonical casing -- which is the casing
 * browserforge.yml keys off. Attaching them here is what makes
 * `headers.Accept-Encoding` reach the config, as it does in the Python twin;
 * without it Firefox sent its own Accept-Encoding while every other header
 * was spoofed.
 */
export function generateFingerprint(
	window?: [number, number],
	config?: Partial<FingerprintGeneratorOptions>,
): Fingerprint {
	const { fingerprint, headers } = FP_GENERATOR.getFingerprint(config);
	if (window) {
		handleWindowSize(fingerprint, window[0], window[1]);
	}
	const canonical: Record<string, string> = {};
	for (const [name, value] of Object.entries(headers ?? {})) {
		canonical[canonicalHeaderName(name)] = value as string;
	}
	return { ...fingerprint, headers: canonical } as Fingerprint;
}
