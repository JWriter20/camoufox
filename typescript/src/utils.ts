/**
 * Launch-option assembly, the twin of utils.py: launchOptions() must return what
 * launch_options() does (tests/launch-golden.test.ts). `utilsDeps` holds what the
 * Python tests monkeypatch.
 */
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { inspect } from "node:util";
import { ensureModel } from "fpgen-js";
import { UAParser } from "ua-parser-js";
import { addDefaultAddons, confirmPaths, type DefaultAddon } from "./addons.js";
import * as coherence from "./coherence.js";
import * as cpuAffinity from "./cpu_affinity.js";
import { hasDisplay, largestDisplay } from "./display.js";
import {
	InvalidOS,
	InvalidPropertyType,
	NonFirefoxFingerprint,
	UnknownIPLocation,
} from "./exceptions.js";
import {
	clampScreenToDisplay,
	clampWindowDimensions,
	clampWindowPosition,
	drawVoiceConfig,
	dropAudioSeed,
	fixHardwareConcurrency,
	fixNavigatorArch,
	followBaselineProtection,
	fromFpgen,
	fromPreset,
	generateFingerprint,
	generateRandomFontSubset,
	generateRandomVoiceSubset,
	getRandomPreset,
	identitySalt,
	identitySeed,
	loadOsFonts,
	raiseScreenToModernFloor,
	Screen,
	setMediaDevicesDefaults,
	targetOsFromPlatform,
	WINDOWS_11_MARKER_FONTS,
} from "./fingerprints.js";
import { geoipAllowed, getGeolocation } from "./geolocation.js";
import { GPU_PREFS, GpuDisplay, proveGpu, selectGpu } from "./gpudisplay.js";
import {
	addCanvasPlaceholder,
	type Gpu,
	hasCanvasPlaceholder,
	hostGpu,
	rendersOnHardware,
	setWebgpu,
} from "./host_rendering.js";
import {
	type HumanizeManifest,
	type HumanizeSetting,
	humanizeConfig,
} from "./humanize.js";
import {
	type ProxyConfig,
	ProxyHelper,
	proxyAuthEnv,
	proxyAuthInitScript,
	publicIP,
	publicIPv6,
	socks5Auth,
	validIPv4,
	validIPv6,
} from "./ip.js";
import { handleLocales } from "./locales.js";
import {
	INSTALL_DIR,
	LAUNCHER_CONSTANTS,
	loadDataFile,
	OS_NAME,
	userCacheDir,
} from "./paths.js";
import {
	buildFile,
	effectiveVersionMin,
	ensureBrowserProfileDir,
	ensureCamoufoxInstalled,
	getPath,
	installedVerStr,
	launchPath,
	resolvedPlaywrightVersionStr,
	selectedLaunchPath,
	Version,
	warnIfPackageOutdated,
} from "./pkgman.js";
import * as pro from "./pro.js";
import { openProfile } from "./pro-profile.js";
import {
	comparePyStr,
	formatPyFloatRepr,
	isPlainObject,
	isPyError,
	orjsonDumps,
	PyFloat,
	pyRepr,
	pyStr,
	pyTruthy,
	pyTypeName,
	ValueError,
} from "./pycompat.js";
import * as renderNodes from "./render_node.js";
import { VirtualDisplay } from "./virtdisplay.js";
import { FallbackWarning, LeakWarning, warn } from "./warnings.js";
import { firefoxGpus, sampleWebglForScreen, webglForGpu } from "./webgl.js";

type ListOrString = string | string[];
export type TargetOS = "mac" | "win" | "lin";
type EnvVars = Record<string, string | number | boolean>;

/** The OS names Camoufox takes, by target_os key. */
const OS_NAMES = { win: "windows", mac: "macos", lin: "linux" } as const;

// Camoufox preferences to cache previous pages and requests
export const CACHE_PREFS: Record<string, any> = LAUNCHER_CONSTANTS.cachePrefs;

// System colours stock Firefox reports on Windows 10/11 in the light theme,
// where they differ from a Linux host's GTK theme.
export const WINDOWS_UI_COLORS: Readonly<Record<string, string>> =
	LAUNCHER_CONSTANTS.windowsUiColors;
// Prefs whose Firefox default differs between the Windows and Linux builds and
// that a page or the user can observe, at the Windows default (see
// WINDOWS_BEHAVIOUR_PREFS in the Python launcher for each one's reason).
export const WINDOWS_BEHAVIOUR_PREFS: Record<string, any> =
	LAUNCHER_CONSTANTS.windowsBehaviourPrefs;

/** Monitor pixels per CSS pixel of the window at the scale it renders at.
 * Linux reports the monitor in pixels at scale 1. On Windows and macOS the
 * host's own scaling is already in the bounds and is not known here, so 1. */
function physicalScale(
	config: Record<string, any>,
	prefs: Record<string, any> = {},
): number {
	if (utilsDeps.osName() !== "lin") return 1;
	// -1 is Firefox's "the system's".
	const pref = Number(prefs["layout.css.devPixelsPerPx"] ?? -1);
	return pref > 0 ? pref : Number(config["window.devicePixelRatio"] || 1);
}

/** The host OS in fonts.json / target_os terms ('mac', 'win', 'lin'). */
function hostOsKey(): TargetOS | null {
	return (
		({ darwin: "mac", win32: "win", linux: "lin" } as Record<string, TargetOS>)[
			process.platform
		] ?? null
	);
}

// navigator.storage.estimate().quota is not a constant: Gecko derives it from
// the disk. GetTemporaryStorageLimit() (dom/quota/ActorsParent.cpp) takes
// nsIFile::GetDiskCapacity() of the storage directory and halves it, then
// QuotaManager::GetGroupLimitForLimit() reports min(that / 5, 10 GiB) to the
// page -- so any disk of 100 GB or more reads back as exactly 10 GiB, and a
// smaller one as its own capacity / 10.
const QUOTA_FIXED_LIMIT_PREF = "dom.quotaManager.temporaryStorage.fixedLimit";
// The pref is a signed 32-bit int in KB. Any value above 50 GiB already reports
// the 10 GiB group cap, so clamping a multi-terabyte disk changes nothing a page
// can see.
const INT32_MAX = 2 ** 31 - 1;

/** shutil.disk_usage(path).total */
function diskTotal(p: string): number {
	const st = fs.statfsSync(p);
	return st.blocks * st.bsize;
}

/**
 * Half the capacity of the disk a stock Firefox profile would live on, in KB.
 *
 * That is the number Gecko's own GetTemporaryStorageLimit() would compute on
 * this machine, and it is what `dom.quotaManager.temporaryStorage.fixedLimit`
 * takes. The disk a stock profile lives on, not the one Playwright's throwaway
 * profile lands on: on a host whose temp directory is a tmpfs, that profile
 * sits on a RAM-sized volume no real Firefox profile would.
 */
function stockProfileDiskCapacityKb(): number | null {
	const home = os.homedir();
	let candidates: string[];
	const osName = utilsDeps.osName();
	if (osName === "win") {
		const appdata = process.env.APPDATA;
		candidates = [appdata ? path.join(appdata, "Mozilla") : home, home];
	} else if (osName === "mac") {
		candidates = [
			path.join(home, "Library", "Application Support", "Firefox"),
			home,
		];
	} else {
		candidates = [path.join(home, ".mozilla"), home];
	}

	for (const candidate of candidates) {
		// The directory only exists if Firefox has ever run here; walk up to the
		// first path that does, which is on the same filesystem anyway.
		let probe = candidate;
		while (!fs.existsSync(probe) && probe !== path.dirname(probe)) {
			probe = path.dirname(probe);
		}
		let total: number;
		try {
			total = utilsDeps.diskTotal(probe);
		} catch {
			continue;
		}
		if (total > 0) {
			return Math.min(Math.floor(Math.floor(total / 2) / 1024), INT32_MAX);
		}
	}
	return null;
}

/**
 * Loads humanize-engines.json, found the same way as properties.json. null for
 * a build that predates it.
 */
function loadHumanizeEngines(
	executablePath?: string | null,
): HumanizeManifest | null {
	const manifest = executablePath
		? buildFile(executablePath, "humanize-engines.json")
		: utilsDeps.getPath("humanize-engines.json");
	if (!fs.existsSync(manifest)) return null;
	return JSON.parse(fs.readFileSync(manifest, "utf-8"));
}

/**
 * The font.* prefs that make a build for `buildOs` resolve fonts the way stock
 * Firefox does on `targetOs` (font-prefs.json, from all.js by
 * browser/scripts/gen-font-prefs.py): the target's values where they differ,
 * and an empty list for name-lists only the build's platform sets.
 */
function stockFontPrefs(
	targetOs: TargetOS,
	buildOs: TargetOS = "lin",
): Record<string, any> {
	const table: Record<string, Record<string, any>> = loadDataFile(
		"font-prefs.json",
	);
	const target = table[OS_NAMES[targetOs]];
	const build = table[OS_NAMES[buildOs]];
	const prefs: Record<string, any> = {};
	for (const [k, v] of Object.entries(target)) {
		if (build[k] !== v) prefs[k] = v;
	}
	for (const [k, v] of Object.entries(build)) {
		if (!(k in target) && typeof v === "string") prefs[k] = "";
	}
	return prefs;
}

/**
 * Injection points: everything launchOptions() reaches outside this module.
 * Mirrors the names the Python tests monkeypatch on camoufox.utils.
 */
export const utilsDeps = {
	osName: (): TargetOS => OS_NAME,
	installDir: (): string => INSTALL_DIR,
	hostOsKey,
	diskTotal,
	stockProfileDiskCapacityKb,
	hasDisplay,
	largestDisplay,
	getScreenCons: (headless?: boolean) => getScreenCons(headless),
	print: (line: string): void => {
		process.stdout.write(`${line}\n`);
	},
	ensureBrowserProfileDir,
	ensureCamoufoxInstalled,
	addDefaultAddons,
	confirmPaths,
	installedVerStr: () => installedVerStr(),
	effectiveVersionMin,
	resolvedPlaywrightVersionStr,
	getPath,
	launchPath,
	selectedLaunchPath,
	loadHumanizeEngines,
	identitySalt,
	identitySeed,
	generateFingerprint: async (
		options: Parameters<typeof generateFingerprint>[0],
	): Promise<Record<string, any>> => {
		// fpgen's model is fetched on first use, like Python's.
		await ensureModel();
		return generateFingerprint(options);
	},
	fromFpgen,
	fromPreset,
	getRandomPreset,
	fixNavigatorArch,
	fixHardwareConcurrency,
	followBaselineProtection,
	clampScreenToDisplay,
	clampWindowDimensions,
	clampWindowPosition,
	raiseScreenToModernFloor,
	generateRandomFontSubset,
	generateRandomVoiceSubset,
	drawVoiceConfig,
	setMediaDevicesDefaults,
	hostGpu,
	selectGpu,
	// The WebGL draws read fpgen's model, fetched on first use like Python's.
	firefoxGpus: async (targetOs: string) => {
		await ensureModel();
		return firefoxGpus(targetOs);
	},
	webglForGpu: async (...args: Parameters<typeof webglForGpu>) => {
		await ensureModel();
		return webglForGpu(...args);
	},
	sampleWebglForScreen: async (
		...args: Parameters<typeof sampleWebglForScreen>
	) => {
		await ensureModel();
		return sampleWebglForScreen(...args);
	},
	publicIp: publicIP,
	publicIpv6: publicIPv6,
	getGeolocation,
	validateConfig: (config: Record<string, any>, p?: string | null) =>
		validateConfig(config, p),
	getEnvVars: (config: Record<string, any>, uaOs: string, p?: string | null) =>
		getEnvVars(config, uaOs, p),
};

/*
 * Python-compatible serialisation helpers
 */

/**
 * json.dumps(value, ensure_ascii=True, separators=(',', ':')): compact, every
 * non-ASCII (and DEL) character \u-escaped, floats in Python's repr.
 */
export function pyJsonDumpsAscii(value: unknown): string {
	const encode = (v: unknown): string => {
		if (v === null || v === undefined) return "null";
		if (v === true) return "true";
		if (v === false) return "false";
		if (typeof v === "number") {
			if (Number.isInteger(v)) return String(v);
			if (Number.isNaN(v)) return "NaN";
			if (!Number.isFinite(v)) return v > 0 ? "Infinity" : "-Infinity";
			return formatPyFloatRepr(v);
		}
		if (typeof v === "bigint") return v.toString();
		if (v instanceof PyFloat) return formatPyFloatRepr(v.value);
		if (typeof v === "string") {
			return JSON.stringify(v).replace(
				/[\u007f-￿]/g,
				(c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
			);
		}
		if (Array.isArray(v)) return `[${v.map(encode).join(",")}]`;
		return `{${Object.entries(v as Record<string, unknown>)
			.map(([k, x]) => `${encode(k)}:${encode(x)}`)
			.join(",")}}`;
	};
	return encode(value);
}

/**
 * orjson.dumps(config): compact, UTF-8, insertion order. JavaScript has one
 * number type, so the Python int/float split is carried the way pycompat
 * carries it: a safe-integer number is an int, a bigint is an int beyond
 * 2**53, a PyFloat is an integral float, and any other number is a float.
 */
export function configJson(value: unknown): string {
	return orjsonDumps(value, false);
}

/** Slice `s` into chunks of `size` code points (Python str slicing). */
function chunkCodePoints(s: string, size: number): string[] {
	// Fast path: no astral characters, so UTF-16 units == code points.
	if (!/[\ud800-\udfff]/.test(s)) {
		const out: string[] = [];
		for (let i = 0; i < s.length; i += size) out.push(s.slice(i, i + size));
		return out;
	}
	const cps = Array.from(s);
	const out: string[] = [];
	for (let i = 0; i < cps.length; i += size) {
		out.push(cps.slice(i, i + size).join(""));
	}
	return out;
}

/*
 * Environment
 */

/**
 * Generates a runtime fontconfig that resolves bundled font paths absolutely.
 * The bundled fonts.conf uses prefix="cwd" relative paths which break when
 * Playwright's working directory differs from the browser install directory.
 * Writes a patched copy to the platform cache dir (deterministic, only
 * regenerated when content changes). This must not live inside the versioned
 * browser bundle: the bundle is commonly baked into an image as root and run
 * as a non-root user, so it is read-only at launch time.
 */
function generateFontconfig(
	fontconfigPath: string,
	executablePath?: string | null,
	osDir?: string | null,
): string {
	// Beside the caller's own binary when they supplied one; see getEnvVars.
	const fontsDir = executablePath
		? path.join(path.dirname(executablePath), "fonts")
		: utilsDeps.getPath("fonts");

	// Which directories this identity's OS may see.
	//
	// fontconfig scans <dir> RECURSIVELY, so the parent must never be named: it
	// would make every other OS's files reachable by the renderer -- hidden by
	// the allowlist for direct lookups, but still candidates for glyph fallback.
	//
	// The bundle stores each face ONCE, in a directory named for the set of
	// OSes that use it (L, M, W, LM, LW, MW, LMW) -- see browser/bundle/fonts/groups.json
	// and browser/scripts/gen-font-groups.py. An OS reads the groups its letter appears
	// in, so nothing has to be hidden after the fact.
	let scanDirs: string[] = [];
	const groupsPath = path.join(fontsDir, "groups.json");
	const osKey = (
		{ linux: "lin", macos: "mac", windows: "win" } as Record<string, string>
	)[osDir ?? ""];
	if (osKey && fs.existsSync(groupsPath)) {
		try {
			const parsed = JSON.parse(fs.readFileSync(groupsPath, "utf-8"));
			const readBy: string[] = parsed?.readBy?.[osKey] ?? [];
			scanDirs = readBy
				.map((g) => path.join(fontsDir, g))
				.filter((d) => isDir(d));
		} catch {
			scanDirs = [];
		}
	}
	if (!scanDirs.length) {
		// Older bundles ship fonts/<os>/ with each OS's set duplicated in full.
		if (osDir && isDir(path.join(fontsDir, osDir))) {
			scanDirs = [path.join(fontsDir, osDir)];
		} else {
			scanDirs = [fontsDir];
		}
	}

	const fontsConfSrc = path.join(fontconfigPath, "fonts.conf");
	let confContent = fs.readFileSync(fontsConfSrc, "utf-8");
	confContent = confContent.replace(
		'<dir prefix="cwd">fonts</dir>',
		scanDirs.map((d) => `<dir>${d}</dir>`).join("\n\t"),
	);

	// INSTALL_DIR is platformdirs' user_cache_dir("camoufox"); see paths.ts.
	const cacheDir = path.join(utilsDeps.installDir(), "fontconfig");
	fs.mkdirSync(cacheDir, { recursive: true });

	const contentHash = createHash("sha256")
		.update(confContent)
		.digest("hex")
		.slice(0, 12);
	const runtimeConf = path.join(cacheDir, `fonts-${contentHash}.conf`);
	if (!fs.existsSync(runtimeConf)) {
		fs.writeFileSync(runtimeConf, confContent);
	}
	return runtimeConf;
}

function isDir(p: string): boolean {
	try {
		return fs.statSync(p).isDirectory();
	} catch {
		return false;
	}
}

/**
 * Warn when a caller's own binary is older than their Playwright needs.
 *
 * A managed install below the floor is simply upgraded (pkgman resolves it),
 * but `executable_path` deliberately bypasses that. That leaves the one
 * pairing nothing checks: an old build driven by Playwright >= 1.61, which
 * sends viewport fields the older Juggler schema rejects. Warns rather than
 * raises: the default no-viewport path still works on an old build. A build
 * with no version.json beside it tells us nothing, so it is left alone.
 */
export function warnIfExecutablePredatesPlaywright(
	executablePath?: string | null,
): void {
	if (!executablePath) return;
	let installed: Version;
	try {
		installed = Version.fromPath(path.dirname(executablePath));
	} catch {
		return;
	}

	const required = utilsDeps.effectiveVersionMin();
	if (!installed.lessThan(required)) return;

	warn(
		`The Camoufox build at ${executablePath} is ${installed.build}, but Playwright ` +
			`${utilsDeps.resolvedPlaywrightVersionStr()} needs at least ${required.build}. ` +
			"Contexts created with an explicit viewport will fail with " +
			'"Protocol error (Browser.setDefaultViewport)". Update the build, or pin ' +
			"playwright<1.61.",
		"RuntimeWarning",
	);
}

/**
 * Pass the launcher's Firefox prefs to browser/settings/camoufox.cfg, which applies them
 * at STARTUP (CAMOU_PREFS_1..N, chunked like CAMOU_CONFIG).
 *
 * Playwright's non-persistent launch writes no user.js: firefoxUserPrefs only
 * reach the browser at runtime, through juggler's Browser.enable, after
 * startup. Anything Gecko reads during startup therefore raced or never
 * applied.
 */
export function getPrefEnvVars(
	prefs: Record<string, any>,
): Record<string, string> {
	if (!prefs || !Object.keys(prefs).length) return {};
	// ASCII only: on Windows autoconfig's getenv() reads the environment through
	// the ANSI code page, which would mangle a raw UTF-8 pref value (\u escapes
	// survive it and JSON.parse restores them).
	const data = pyJsonDumpsAscii(prefs);
	const chunkSize = utilsDeps.osName() === "win" ? 2047 : 32767;
	const out: Record<string, string> = {};
	chunkCodePoints(data, chunkSize).forEach((chunk, i) => {
		out[`CAMOU_PREFS_${i + 1}`] = chunk;
	});
	return out;
}

/**
 * Gets the environment variables for Camoufox: the chunked CAMOU_CONFIG, plus
 * the Linux fontconfig pointer.
 *
 * `executablePath` is the caller's own executable, when they supplied one. The
 * bundled fontconfig is read from beside that binary rather than from the
 * managed install, the same way loadProperties() treats properties.json.
 */
export function getEnvVars(
	configMap: Record<string, any>,
	userAgentOs: string,
	executablePath?: string | null,
): EnvVars {
	const envVars: EnvVars = {};
	const configStr = configJson(configMap);

	// Split the config into chunks
	const osName = utilsDeps.osName();
	const chunkSize = osName === "win" ? 2047 : 32767;
	chunkCodePoints(configStr, chunkSize).forEach((chunk, i) => {
		envVars[`CAMOU_CONFIG_${i + 1}`] = chunk;
	});

	if (osName === "lin") {
		// https://github.com/coryking/camoufox/commit/f21eeb2850a74cc104fb57e17e0a2fa27b7a2a28
		// Thanks @coryking
		const directoryMap: Record<string, string> = {
			lin: "linux",
			mac: "macos",
			win: "windows",
		};
		const osDir = directoryMap[userAgentOs] ?? userAgentOs;

		// v150+ uses "fontconfig/" (matching the Go launcher); older bundles
		// shipped "fontconfigs/".
		const bundlePath = (...parts: string[]): string =>
			executablePath
				? path.join(path.dirname(executablePath), ...parts)
				: utilsDeps.getPath(path.join(...parts));

		let fontconfigPath = bundlePath("fontconfig", osDir);
		if (!fs.existsSync(path.join(fontconfigPath, "fonts.conf"))) {
			fontconfigPath = bundlePath("fontconfigs", osDir);
		}

		if (!fs.existsSync(path.join(fontconfigPath, "fonts.conf"))) {
			const err = new Error(
				`fonts.conf not found in ${fontconfigPath}!  Something ain't right with your camoufox bundle.`,
			);
			err.name = "FileNotFoundError";
			throw err;
		}

		envVars.FONTCONFIG_FILE = generateFontconfig(
			fontconfigPath,
			executablePath,
			osDir,
		);
	}

	return envVars;
}

interface PropertyEntry {
	property: string;
	type: string;
	removed?: string;
	min?: number;
}

/**
 * Loads the properties.json file: property -> its entry. An entry with a
 * "removed" field is a key the browser no longer reads; it stays declared so
 * that older libraries, which reject undeclared keys, still launch.
 */
function loadProperties(
	executablePath?: string | null,
): Record<string, PropertyEntry> {
	const propFile = executablePath
		? buildFile(executablePath, "properties.json")
		: utilsDeps.getPath("properties.json");
	const propDict: PropertyEntry[] = JSON.parse(
		fs.readFileSync(propFile, "utf-8"),
	);
	const out: Record<string, PropertyEntry> = {};
	for (const prop of propDict) out[prop.property] = prop;
	return out;
}

/**
 * Validates the config map against the build's properties.json, and lifts a
 * numeric value below its declared `min` up to it.
 */
export function validateConfig(
	configMap: Record<string, any>,
	executablePath?: string | null,
): void {
	const properties = loadProperties(executablePath);

	for (const [key, value] of Object.entries(configMap)) {
		const entry = properties[key];
		if (!entry) {
			utilsDeps.print(`Skipping unknown patch ${key} : ${pyStr(value)}`);
			continue; // Property not supported by this browser version; skip silently
		}
		if (entry.removed !== undefined) {
			utilsDeps.print(
				`Skipping ${key}: removed in ${entry.removed}, it has no effect`,
			);
			continue;
		}
		const expectedType = entry.type;
		if (!validateType(value, expectedType)) {
			throw new InvalidPropertyType(
				`Invalid type for property ${key}. Expected ${expectedType}, got ${pyTypeName(value)}`,
			);
		}

		if (entry.min !== undefined) {
			const n = value instanceof PyFloat ? value.value : Number(value);
			if (n < entry.min) configMap[key] = entry.min;
		}

		if (key === "voices:speechd") {
			validateSpeechdVoices(value);
		}
		if (key === "voices") {
			validateVoices(value);
		}
	}
}

interface LaunchRule {
	target?: string[];
	host?: string[];
	prefs?: Record<string, any>;
	env?: Record<string, string>;
	envPaths?: Record<string, string>;
	envPathsOptional?: Record<string, string>;
	envFromConfig?: Record<string, string>;
	config?: Record<string, any>;
	ldPreload?: string[];
	envCacheDirs?: Record<string, string>;
	exclusive?: boolean;
	warn?: string;
}

/**
 * Applies the prefs and environment variables the build declares in its
 * launch.json. A build that needs a pref or variable set at launch for a
 * feature it compiles in says so there, so the launcher needs no knowledge of
 * any particular build. Most builds ship no launch.json.
 *
 * Each rule may restrict itself to identity OSes (`target`) and host OSes
 * (`host`), both in 'win'/'mac'/'lin' terms. `env` sets variables verbatim,
 * `envPaths` to a file relative to the build's directory, which must exist,
 * `envPathsOptional` likewise but only when the file exists, and
 * `envFromConfig` to the value of a config key when the identity has one.
 * `config` sets config keys. `ldPreload` appends each build-relative
 * library that exists to LD_PRELOAD (appended, never replacing one the
 * caller has), and `envCacheDirs` points each variable at a per-user cache
 * directory of that name, created on demand. A pref or config key the caller
 * set, or a variable already in the environment, is never replaced.
 *
 * An `exclusive` rule owns its variables: where it does not apply they are
 * removed from the environment, the caller's included, so a feature meant for
 * one target/host pairing cannot leak into another through an inherited
 * variable. A rule's `warn` is emitted as a RuntimeWarning when it applies.
 */
export function applyLaunchRules(
	targetOs: string,
	config: Record<string, any>,
	firefoxUserPrefs: Record<string, any>,
	userPrefKeys: Set<string>,
	env: Record<string, any>,
	executablePath?: string | null,
): void {
	const launchFile = executablePath
		? buildFile(executablePath, "launch.json")
		: utilsDeps.getPath("launch.json");
	if (!fs.existsSync(launchFile)) return;
	const rules: LaunchRule[] = JSON.parse(
		fs.readFileSync(launchFile, "utf-8"),
	).rules;

	const hostOs = utilsDeps.hostOsKey();
	const applies = (rule: LaunchRule): boolean =>
		!(rule.target && !rule.target.includes(targetOs)) &&
		!(rule.host && !(hostOs && rule.host.includes(hostOs)));
	const owned = (rule: LaunchRule): string[] => [
		...Object.keys(rule.env ?? {}),
		...Object.keys(rule.envPaths ?? {}),
		...Object.keys(rule.envPathsOptional ?? {}),
		...Object.keys(rule.envFromConfig ?? {}),
		...Object.keys(rule.envCacheDirs ?? {}),
	];

	const matched = rules.filter(applies);
	const claimed = new Set(matched.flatMap(owned));
	for (const rule of rules) {
		if (!rule.exclusive || applies(rule)) continue;
		for (const key of owned(rule)) {
			if (!claimed.has(key)) delete env[key];
		}
	}

	for (const rule of matched) {
		if (rule.warn) warn(String(rule.warn), "RuntimeWarning");
		for (const [key, value] of Object.entries(rule.prefs ?? {})) {
			if (!userPrefKeys.has(key)) firefoxUserPrefs[key] = value;
		}
		for (const [key, value] of Object.entries(rule.env ?? {})) {
			if (!(key in env)) env[key] = value;
		}
		for (const [key, relative] of Object.entries(rule.envPaths ?? {})) {
			if (key in env) continue;
			const resolved = path.join(path.dirname(launchFile), relative);
			if (!fs.existsSync(resolved)) {
				throw new Error(
					`${launchFile} needs ${resolved} for ${key}, and it does not exist.`,
				);
			}
			env[key] = resolved;
		}
		for (const [key, relative] of Object.entries(rule.envPathsOptional ?? {})) {
			const resolved = path.join(path.dirname(launchFile), relative);
			if (!(key in env) && fs.existsSync(resolved)) env[key] = resolved;
		}
		for (const [key, value] of Object.entries(rule.config ?? {})) {
			if (!(key in config)) config[key] = value;
		}
		// `env` is the caller's environment (a copy of process.env by default)
		// and is spread last into the launch environment, so appending here is
		// what keeps an LD_PRELOAD someone already has.
		for (const relative of rule.ldPreload ?? []) {
			const resolved = path.join(path.dirname(launchFile), relative);
			const preload = env.LD_PRELOAD ? String(env.LD_PRELOAD) : "";
			if (fs.existsSync(resolved) && !preload.split(":").includes(resolved)) {
				env.LD_PRELOAD = preload ? `${preload}:${resolved}` : resolved;
			}
		}
		for (const [key, name] of Object.entries(rule.envCacheDirs ?? {})) {
			if (key in env) continue;
			const cacheDir = userCacheDir(`camoufox-${name}`);
			fs.mkdirSync(cacheDir, { recursive: true });
			env[key] = cacheDir;
		}
		for (const [key, configKey] of Object.entries(rule.envFromConfig ?? {})) {
			if (configKey in config && !(key in env)) {
				env[key] = pyStr(config[configKey]);
			}
		}
	}
}

/**
 * Validates the type of the value. (Python's bool is an int, so a boolean
 * passes the numeric checks there, and here.)
 */
export function validateType(value: any, expectedType: string): boolean {
	if (value instanceof PyFloat) {
		// A Python float: int/uint only when it is integral (float.is_integer()).
		const v = value.value;
		if (expectedType === "double") return true;
		if (expectedType === "int") return Number.isInteger(v);
		if (expectedType === "uint") return Number.isInteger(v) && v >= 0;
		return false;
	}
	const isInt =
		typeof value === "boolean" ||
		typeof value === "bigint" ||
		(typeof value === "number" && Number.isInteger(value));
	switch (expectedType) {
		case "str":
			return typeof value === "string";
		case "int":
			return isInt;
		case "uint":
			return isInt && Number(value) >= 0;
		case "double":
			return typeof value === "number" || isInt;
		case "bool":
			return typeof value === "boolean";
		case "array":
			return Array.isArray(value);
		case "dict":
			return (
				typeof value === "object" && value !== null && !Array.isArray(value)
			);
		default:
			return false;
	}
}

// The five fields MaskConfig::MVoices() requires of every `voices` entry. It
// skips anything missing one of them, so a bare "Name:lang:type" string or a
// half-filled object registers nothing -- and a voice list that registers
// nothing leaves the host's native voices exposed (#731).
const VOICE_FIELDS = [
	"lang",
	"name",
	"voiceUri",
	"isDefault",
	"isLocalService",
] as const;

/**
 * Validates that every `voices` entry is a complete voice object.
 */
export function validateVoices(voices: any): void {
	if (!Array.isArray(voices)) {
		throw new InvalidPropertyType(
			`Invalid type for property voices. Expected array, got ${pyTypeName(voices)}`,
		);
	}
	voices.forEach((voice, index) => {
		if (typeof voice !== "object" || voice === null || Array.isArray(voice)) {
			throw new InvalidPropertyType(
				`Invalid voices[${index}]: expected an object with ` +
					`{${VOICE_FIELDS.join(", ")}}, got ${pyTypeName(voice)} ` +
					`(${pyRepr(voice)}). Camoufox needs full voice objects, not names.`,
			);
		}
		const missing = VOICE_FIELDS.filter((field) => !(field in voice));
		if (missing.length) {
			throw new InvalidPropertyType(
				`Invalid voices[${index}]: missing ${missing.join(", ")}. ` +
					`Every voice needs {${VOICE_FIELDS.join(", ")}}.`,
			);
		}
	});
}

/**
 * Validates a `voices:speechd` grid: {"languages": [[name, lang], ...],
 * "variants": [name, ...]}, the shape MaskConfig::MSpeechdVoices() expands.
 */
export function validateSpeechdVoices(grid: any): void {
	const { languages, variants } = grid ?? {};
	const ok =
		Array.isArray(languages) &&
		languages.every(
			(e: unknown) =>
				Array.isArray(e) &&
				e.length === 2 &&
				e.every((x) => typeof x === "string"),
		) &&
		Array.isArray(variants) &&
		variants.every((v: unknown) => typeof v === "string");
	if (!ok) {
		throw new InvalidPropertyType(
			'Invalid voices:speechd: expected {"languages": [[name, lang], ...], "variants": [name, ...]}',
		);
	}
}

/**
 * Gets the OS from the config if the user agent is set, otherwise returns the
 * OS of the current system.
 */
export function getTargetOs(config: Record<string, any>): TargetOS {
	if (config["navigator.userAgent"]) {
		return determineUaOs(config["navigator.userAgent"]);
	}
	return utilsDeps.osName();
}

/**
 * Determines the OS from the user agent string.
 */
export function determineUaOs(userAgent: string): TargetOS {
	// Python's ua_parser answers "Other" rather than nothing for an
	// unrecognised UA, so its `raise` never fires and "lin" is what runs.
	const parsed = new UAParser(userAgent).getOS().name || "Other";
	// ua-parser-js reports "macOS"; the Python ua_parser reports "Mac OS X".
	if (parsed.startsWith("Mac") || parsed.startsWith("macOS")) return "mac";
	if (parsed.startsWith("Windows")) return "win";
	return "lin";
}

/**
 * Determines a sane viewport size for Camoufox if being ran in headful mode.
 *
 * Bounds are CSS pixels, the unit Firefox lays its windows out in -- see
 * display.ts for why that differs from the monitor's physical size.
 */
export function getScreenCons(headless?: boolean): Screen | null {
	if (headless === true) {
		return null; // Skip if headless
	}
	const display = utilsDeps.largestDisplay();
	if (display === null) {
		return null; // Skip if the display can't be probed
	}
	return new Screen({ maxWidth: display.width, maxHeight: display.height });
}

/**
 * Updates the fonts for the target OS.
 */
function updateFonts(config: Record<string, any>, targetOs: string): void {
	const fonts = [...loadOsFonts()[targetOs]];

	// Merge with existing fonts (np.unique sorts)
	if ("fonts" in config) {
		config.fonts = [...new Set([...fonts, ...config.fonts])].sort(comparePyStr);
	} else {
		config.fonts = fonts;
	}
}

/**
 * Asserts that the passed fingerprint is a valid Firefox fingerprint, and
 * warns that passing one is not recommended.
 */
function checkCustomFingerprint(fingerprint: Record<string, any>): void {
	const userAgent = fingerprint?.navigator?.userAgent || "";
	const browserName = parseUaFamily(userAgent);
	if (browserName !== "Firefox") {
		throw new NonFirefoxFingerprint(
			`"${browserName}" fingerprints are not supported in Camoufox. ` +
				"Using fingerprints from a browser other than Firefox WILL lead to detection. " +
				"If this is intentional, pass `i_know_what_im_doing=True`.",
		);
	}

	LeakWarning.warn("custom_fingerprint", false);
}

/** ua_parser.user_agent_parser.ParseUserAgent(ua)['family']. */
function parseUaFamily(userAgent: string): string {
	const name = new UAParser(userAgent).getBrowser().name;
	if (!name) return "Other";
	// ua-parser-js and uap-core name a few browsers differently.
	const map: Record<string, string> = {
		"Mobile Firefox": "Firefox Mobile",
		"Mobile Chrome": "Chrome Mobile",
		"Mobile Safari": "Mobile Safari",
	};
	return map[name] ?? name;
}

/**
 * Checks if the target OS is valid.
 */
export function checkValidOs(osValue: ListOrString): void {
	if (typeof osValue !== "string") {
		for (const osName of osValue) checkValidOs(osName);
		return;
	}
	// Assert that the OS is lowercase (str.islower(): has a cased char, none upper)
	if (
		!(osValue !== osValue.toUpperCase() && osValue === osValue.toLowerCase())
	) {
		throw new InvalidOS(`OS values must be lowercase: '${osValue}'`);
	}
	// Assert that the OS is supported by Camoufox
	if (!["windows", "macos", "linux"].includes(osValue)) {
		throw new InvalidOS(`Camoufox does not support the OS: '${osValue}'`);
	}
}

/**
 * Merges new keys/values from the source into the target, given that the key
 * does not exist in the target.
 */
export function mergeInto(
	target: Record<string, any>,
	source: Record<string, any>,
): void {
	for (const [key, value] of Object.entries(source)) {
		if (!(key in target)) target[key] = value;
	}
}

/**
 * Sets a new key/value into the target, given that the key does not exist.
 */
export function setInto(
	target: Record<string, any>,
	key: string,
	value: any,
): void {
	if (!(key in target)) target[key] = value;
}

/**
 * Checks if a domain is set in the config.
 */
export function isDomainSet(
	config: Record<string, any>,
	...properties: string[]
): boolean {
	for (const prop of properties) {
		// If the . prefix exists, check if the domain is a prefix of any key
		if (prop.endsWith(".") || prop.endsWith(":")) {
			if (Object.keys(config).some((key) => key.startsWith(prop))) return true;
		} else if (prop in config) {
			// Otherwise, check if the domain is a direct key in the config
			return true;
		}
	}
	return false;
}

/**
 * Whether this host has a public IPv6 address that geolocates where the IPv4
 * identity does. Warns when it has one that does not.
 */
async function keepsIPv6(
	geoConfig: Record<string, any>,
	geoipDb: string | undefined,
	iKnowWhatImDoing: boolean,
): Promise<boolean> {
	const ipv6 = await utilsDeps.publicIpv6();
	if (ipv6 === null) {
		return false;
	}
	let ipv6Config: Record<string, any> = {};
	try {
		ipv6Config = (await utilsDeps.getGeolocation(ipv6, geoipDb)).asConfig();
	} catch (error) {
		if (!(error instanceof UnknownIPLocation)) {
			throw error;
		}
	}
	if (
		["timezone", "locale:region"].every(
			(key) => ipv6Config[key] === geoConfig[key],
		)
	) {
		return true;
	}
	LeakWarning.warn("ipv6_elsewhere", iKnowWhatImDoing);
	return false;
}

/**
 * Warns the user if they are manually setting properties that Camoufox already
 * sets internally.
 */
function warnManualConfig(config: Record<string, any>): void {
	// Manual locale setting
	if (
		isDomainSet(
			config,
			"navigator.language",
			"headers.Accept-Language",
			"locale:",
		)
	) {
		LeakWarning.warn("locale", false);
	}
	// Manual geolocation and timezone setting
	if (isDomainSet(config, "geolocation:", "timezone")) {
		LeakWarning.warn("geolocation", false);
	}
	// Manual User-Agent setting
	if (isDomainSet(config, "headers.User-Agent")) {
		LeakWarning.warn("header-ua", false);
	}
	// Manual navigator setting
	if (isDomainSet(config, "navigator.")) {
		LeakWarning.warn("navigator", false);
	}
	// Touchscreen digitizer spoofing. Called out separately from the blanket
	// navigator warning because the knock-on effects reach past navigator into
	// CSS pointer media queries and the TouchEvent interfaces.
	if (isDomainSet(config, "navigator.maxTouchPoints")) {
		LeakWarning.warn("max_touch_points", false);
	}
	if (pyTruthy(config.instantAnimations)) {
		LeakWarning.warn("instant_animations", false);
	}
	// Manual screen/window setting
	if (isDomainSet(config, "screen.", "window.", "document.body.")) {
		LeakWarning.warn("viewport", false);
	}
}

const WINDOW_DIM_KEYS = [
	"window.outerWidth",
	"window.outerHeight",
	"window.innerWidth",
	"window.innerHeight",
];

/** The CAMOU_CONFIG chunks of a set of launch options, reassembled in order. */
export function camouConfigBlob(fromOptions: Record<string, any>): string {
	const env: Record<string, any> = fromOptions.env ?? {};
	return Object.entries(env)
		.filter(([k]) => k.startsWith("CAMOU_CONFIG_"))
		.map(
			([k, v]) =>
				[
					Number.parseInt(k.slice(k.lastIndexOf("_") + 1), 10),
					String(v),
				] as const,
		)
		.sort((a, b) => a[0] - b[0])
		.map(([, v]) => v)
		.join("");
}

/**
 * The core count the browser must be pinned to for these launch options, or
 * null: the identity's navigator.hardwareConcurrency when this host can honour
 * it (see cpu_affinity), so a page measuring parallelism sees the reported
 * number.
 */
export function pinnedCoreCount(
	fromOptions: Record<string, any>,
): number | null {
	const blob = camouConfigBlob(fromOptions);
	if (!blob || !cpuAffinity.supported()) return null;
	let value: unknown;
	try {
		const parsed = JSON.parse(blob);
		value =
			parsed && typeof parsed === "object" && !Array.isArray(parsed)
				? parsed["navigator.hardwareConcurrency"]
				: undefined;
	} catch {
		return null;
	}
	// The host's count, not this process's live mask: a concurrent pinned
	// launch narrows that while its browser spawns.
	const cores = cpuAffinity.hostCoreCount();
	if (
		typeof value === "number" &&
		Number.isInteger(value) &&
		value >= 1 &&
		value < cores
	) {
		return value;
	}
	return null;
}

/**
 * PID of the process that will spawn the browser (its children inherit the
 * CPU affinity set on it). Python's driver is a separate Node process;
 * playwright-core runs in this one.
 */
export function driverPid(): number {
	return process.pid;
}

/**
 * Whether the CAMOU_CONFIG in a set of launch options spoofs any window
 * dimension. The config is chunked across CAMOU_CONFIG_<n> env vars, so
 * reassemble it in index order before looking.
 */
export function spoofsWindowDimensions(
	fromOptions: Record<string, any>,
): boolean {
	const blob = camouConfigBlob(fromOptions);
	if (!blob) return false;
	return WINDOW_DIM_KEYS.some((key) => blob.includes(key));
}

/**
 * Playwright emulates four media features on every context it creates, whether
 * or not the caller asked: `colorScheme` defaults to "light" and reducedMotion /
 * forcedColors / contrast to their no-preference values. That is an override,
 * not a passthrough -- the page then reports it whatever the host is set to. The
 * JS API's `null` (Python's "no-override") is Playwright's own opt-out: it sends
 * no emulation at all and the browser answers from the host.
 */
export const STOCK_MEDIA_DEFAULTS = {
	colorScheme: null,
	reducedMotion: null,
	forcedColors: null,
	contrast: null,
} as const;

/** Fill STOCK_MEDIA_DEFAULTS into any media option the caller left unset. */
function withStockMediaDefaults(
	opts: Record<string, any>,
): Record<string, any> {
	const out = { ...opts };
	for (const [option, value] of Object.entries(STOCK_MEDIA_DEFAULTS)) {
		if (out[option] === undefined) out[option] = value;
	}
	return out;
}

/**
 * Default newPage()/newContext() to the host's own media features.
 *
 * Explicit colorScheme / reducedMotion / forcedColors / contrast from the caller
 * always wins; this only replaces Playwright's silent defaults.
 */
export function attachStockMediaDefaults<T>(target: T): T {
	for (const name of ["newPage", "newContext"] as const) {
		const original = (target as any)[name];
		if (typeof original !== "function") continue;
		(target as any)[name] = (options?: Record<string, any>, ...rest: any[]) =>
			original.call(target, withStockMediaDefaults(options ?? {}), ...rest);
	}
	return target;
}

const CAMOUFOX_SOCKS5_AUTH = Symbol.for("camoufox.socks5Auth");

/**
 * Let newContext()/newPage() of `target` take a SOCKS5 proxy with a username
 * and password (python/src/camoufox/utils.py attach_socks5_auth).
 *
 * Playwright refuses one ("Browser does not support socks5 proxy
 * authentication"). The context is made with the proxy alone, and an init
 * script hands Juggler the credentials before newContext() resolves
 * (socks5Auth, proxyAuthInitScript). Playwright's newPage() makes its context
 * through newContext(), so it is covered too.
 */
export function attachSocks5Auth<T>(target: T): T {
	const owner = target as any;
	const original = owner?.newContext;
	if (typeof original !== "function" || original[CAMOUFOX_SOCKS5_AUTH])
		return target;
	const newContext = async (options?: Record<string, any>, ...rest: any[]) => {
		const [proxy, auth] = socks5Auth(options?.proxy);
		const context = await original.call(
			target,
			options && auth ? { ...options, proxy } : options,
			...rest,
		);
		if (auth) await context.addInitScript(proxyAuthInitScript(auth));
		return context;
	};
	(newContext as any)[CAMOUFOX_SOCKS5_AUTH] = true;
	owner.newContext = newContext;
	return target;
}

const CAMOUFOX_DRIVER_DEFAULTS = Symbol.for("camoufox.driverDefaults");

/**
 * Whether a fill() target is a text field a person types into, and whether it
 * has focus. Runs in the page, so it is a function (a string would be an
 * expression to Playwright's JS client).
 */
const FILL_TARGET = (e: any) => {
	const active = e.ownerDocument.activeElement;
	const type =
		e.localName === "input"
			? (e.getAttribute("type") || "text").toLowerCase()
			: null;
	const typed =
		e.localName === "textarea" ||
		e.isContentEditable ||
		["text", "search", "email", "url", "tel", "password", "number"].includes(
			type,
		);
	return {
		typed,
		focused: active === e || (e.isContentEditable && e.contains(active)),
	};
};

/**
 * Driver calls that reach the page as a person's actions would
 * (python/src/camoufox/driver_defaults.py). Applies to every page under
 * `target` (a Browser or BrowserContext from a Camoufox launch); the defaults
 * are filled in where the client sends the request, so page, frame, element
 * and locator calls all get them, and other browsers in the process are left
 * alone.
 *
 * Screenshots leave the page alone. Unless told caret: "initial", Playwright
 * hides the caret by setting `caret-color: transparent !important` in the
 * style attribute of every input, textarea and contenteditable element, from
 * its utility world: the page sees each change, reads the transparent caret
 * while the capture runs, and is left with `style` attributes it did not have.
 * Firefox's capture never paints the caret, so screenshots default to
 * caret: "initial"; an explicit caret is kept.
 *
 * fill() clicks the field first. Playwright focuses it by script, so the page
 * gets focus with no pointer or mouse event before it, where a person clicks
 * the field. A text field without focus is clicked (Playwright's click,
 * through the OS-path dispatch) before the fill; a focused one is typed into
 * as it is, and fields fill() sets without typing are left alone.
 */
export function attachDriverDefaults<T>(target: T): T {
	const owner = target as any;
	owner[CAMOUFOX_DRIVER_DEFAULTS] = true;
	const connection = owner._connection;
	if (!connection || connection[CAMOUFOX_DRIVER_DEFAULTS]) return target;
	connection[CAMOUFOX_DRIVER_DEFAULTS] = true;
	const send = connection.sendMessageToServer;
	connection.sendMessageToServer = async function (
		object: any,
		method: string,
		params: Record<string, any>,
		...rest: any[]
	) {
		if (launchedByCamoufox(object)) {
			if (method === "screenshot" && params?.caret === undefined)
				params = { ...params, caret: "initial" };
			if (method === "fill") await clickToFocus(object, params);
		}
		return send.call(this, object, method, params, ...rest);
	};
	return target;
}

async function clickToFocus(object: any, params: Record<string, any>) {
	// A Frame fills by selector (page, frame and locator fill); an ElementHandle fills itself.
	let field = object;
	if (
		typeof params?.selector === "string" &&
		typeof object.locator === "function"
	) {
		field = object.locator(params.selector);
		if (!params.strict) field = field.first();
	}
	if (typeof field.evaluate !== "function" || typeof field.click !== "function")
		return;
	const info = await field.evaluate(FILL_TARGET);
	if (info?.typed && !info.focused)
		await field.click({ timeout: params?.timeout, force: params?.force });
}

/** ElementHandle -> Frame -> Page -> BrowserContext -> Browser. */
function launchedByCamoufox(object: any): boolean {
	for (let owner = object; owner; owner = owner._parent)
		if (owner[CAMOUFOX_DRIVER_DEFAULTS]) return true;
	return false;
}

/**
 * Warn when newPage()/newContext() asks for isMobile: Camoufox only has desktop
 * identities, and Juggler ignores the option (TargetRegistry.js).
 */
export function attachDesktopOnlyWarning<T>(target: T): T {
	for (const name of ["newPage", "newContext"] as const) {
		const original = (target as any)[name];
		if (typeof original !== "function") continue;
		(target as any)[name] = (options?: Record<string, any>, ...rest: any[]) => {
			if (options?.isMobile || options?.is_mobile)
				LeakWarning.warn("is_mobile");
			return original.call(target, options, ...rest);
		};
	}
	return target;
}

/** The machine a browser's contexts must claim: its OS and, if hardware, its GPU. */
export type HostIdentity = [TargetOS | null, Gpu | null];

/**
 * Whether the canvas is covered, and the host GPU the identity claims.
 *
 * The canvas renders on the host, so the identity claims the host's GPU when it
 * can: on the host's OS, with no GPU named, on a hardware GPU fpgen has
 * recorded there. A software renderer matches any GPU, and with WebGL off no GPU
 * is claimed. Any other identity contradicts the pixels, so the canvas is
 * covered unless the caller chose canvas_noise: false.
 */
export async function planRendering(
	targetOs: string | null,
	canvasNoise: boolean | null | undefined,
	namedGpu: boolean,
	webglDisabled: boolean,
	probe: () => Promise<Gpu | null>,
): Promise<[boolean, Gpu | null]> {
	if (canvasNoise) return [true, null];
	if (targetOs && targetOs === utilsDeps.hostOsKey() && !namedGpu) {
		const host = webglDisabled ? null : await probe();
		if (!rendersOnHardware(host)) return [false, null];
		const recorded = await utilsDeps.firefoxGpus(targetOs);
		if (
			recorded.some(
				([vendor, renderer]) => vendor === host[0] && renderer === host[1],
			)
		) {
			return [false, host];
		}
	}
	return [canvasNoise == null, null];
}

/**
 * For a launch without the canvas placeholder, the machine its contexts must
 * claim: the host's OS, and the host GPU it claims (planRendering). Only for
 * the default `canvasNoise` (undefined); a caller who chose it, or built the
 * options, keeps their contexts as they ask. Read before the launch, so a
 * failed probe leaves no browser behind.
 */
export async function hostIdentity(
	fromOptions: Record<string, any>,
	canvasNoise: boolean | undefined,
): Promise<HostIdentity | null> {
	const prefs = fromOptions.firefoxUserPrefs ?? {};
	// A farm- or node-rendered browser draws there, not on this host.
	const env = fromOptions.env ?? {};
	if (
		canvasNoise != null ||
		hasCanvasPlaceholder(prefs) ||
		pro.RENDER_FILE_ENV in env ||
		renderNodes.RELAY_ENV in env
	)
		return null;
	// Without the placeholder, the launch claimed the host's OS and no named GPU.
	const hostOs = utilsDeps.hostOsKey();
	const [covered, gpu] = await planRendering(
		hostOs,
		undefined,
		false,
		Boolean(prefs["webgl.disabled"]),
		() =>
			utilsDeps.hostGpu(
				fromOptions.executablePath,
				fromOptions.headless,
				fromOptions.env,
				prefs,
			),
	);
	return covered ? null : [hostOs, gpu];
}

/**
 * The OS and GPU a new context on `browser` claims (null: drawn).
 *
 * On a browser without the canvas placeholder they are the host's, since the
 * canvas renders there; asking for anything else throws.
 */
export function contextIdentity(
	browser: any,
	os: string | undefined,
	preset: Record<string, any> | undefined,
): [string | undefined, Gpu | null] {
	if (browser._camoufoxRenderNode) {
		const presetOs = pyTruthy(preset)
			? targetOsFromPlatform(preset?.navigator?.platform ?? "")
			: null;
		if (
			(os || "windows") !== "windows" ||
			(presetOs || "windows") !== "windows"
		) {
			throw new ValueError(
				"This browser draws WebGL on a Windows render node, so its contexts are Windows.",
			);
		}
		return ["windows", null];
	}
	const hostIdentity: [TargetOS, Gpu | null] | undefined =
		browser._camoufoxHostIdentity;
	if (!hostIdentity) return [os, null];
	const [hostOs, gpu] = hostIdentity;
	const hostName = OS_NAMES[hostOs];
	const hasPreset = pyTruthy(preset);
	const presetGpu = preset?.webgl ?? {};
	if (
		(os || hostName) !== hostName ||
		(hasPreset &&
			targetOsFromPlatform(preset?.navigator?.platform ?? "") !== hostName) ||
		(gpu &&
			hasPreset &&
			(presetGpu.unmaskedVendor !== gpu[0] ||
				presetGpu.unmaskedRenderer !== gpu[1]))
	) {
		throw new ValueError(
			`This browser renders its canvas as this ${hostName} machine, so its contexts claim it` +
				`${gpu ? ` and its GPU ${JSON.stringify(gpu)}` : ""}. Launch it with canvas_noise: true ` +
				"to give a context another OS or GPU.",
		);
	}
	return [hostName, gpu];
}

/**
 * Normalise a context-options object onto the JS API's `viewport: null`,
 * defaulting to it when the caller expressed no preference. Playwright-Python
 * takes `no_viewport=True`; a caller-supplied `noViewport` is accepted and
 * translated.
 */
export function applyNoViewport(
	opts: Record<string, any>,
): Record<string, any> {
	const out = { ...opts };
	if ("noViewport" in out) {
		const noViewport = out.noViewport;
		delete out.noViewport;
		if (noViewport && !("viewport" in out)) out.viewport = null;
		return out;
	}
	if (!("viewport" in out)) out.viewport = null;
	return out;
}

/**
 * Default newPage()/newContext() to no viewport.
 *
 * Playwright applies a 1280x720 viewport by default, which makes Juggler ask
 * the content window to become 1280x720 (TargetRegistry.updateViewportSize).
 * When Camoufox is pinning the window to a spoofed size, that request can
 * never be satisfied, and awaitViewportDimensions has no timeout -- so the
 * second newPage() hangs forever (daijro/camoufox#666). An explicit viewport
 * from the caller always wins.
 */
export function attachNoViewportDefault<T>(target: T): T {
	for (const name of ["newPage", "newContext"] as const) {
		const original = (target as any)[name];
		if (typeof original !== "function") continue;
		(target as any)[name] = (options?: Record<string, any>, ...rest: any[]) =>
			original.call(target, applyNoViewport(options ?? {}), ...rest);
	}
	return target;
}

/** The headless modes NewBrowser and launchServer accept. */
export type Headless = boolean | "virtual" | "virtual-gpu";

/** The options of the display headless="virtual-gpu" starts. */
export interface GpuDisplayOptions {
	/** The GPU for headless="virtual-gpu": an index, a render or card node, a
	 * PCI address, vendor:device ids or a vendor name. Defaults to
	 * $CAMOUFOX_GPU, or the host's only GPU. */
	gpu?: string | number;
	/** Fail the launch (the default) when the browser on the GPU display does
	 * not render on the GPU; false warns instead. */
	require_gpu?: boolean;
}

/**
 * Start the display headless="virtual" or "virtual-gpu" asks for.
 *
 * Takes the display's own options out of `options` and points the launch at
 * the display. Returns the display, or null, and the headless value to launch
 * with.
 */
export async function startVirtualDisplay(
	headless: Headless | undefined,
	debug: boolean | undefined,
	options: LaunchOptions & GpuDisplayOptions,
): Promise<[VirtualDisplay | null, boolean | undefined]> {
	const { gpu, require_gpu } = options;
	delete options.gpu;
	delete options.require_gpu;
	let display: VirtualDisplay;
	if (headless === "virtual-gpu") {
		display = new GpuDisplay(
			utilsDeps.selectGpu(gpu),
			debug ?? false,
			require_gpu !== false,
		);
		options.firefox_user_prefs = {
			...GPU_PREFS,
			...options.firefox_user_prefs,
		};
	} else if (gpu !== undefined || require_gpu !== undefined) {
		throw new ValueError(
			"gpu and require_gpu apply to headless='virtual-gpu' only",
		);
	} else if (headless === "virtual") {
		display = new VirtualDisplay(debug ?? false);
	} else {
		return [null, headless];
	}
	options.virtual_display = await display.get();
	return [display, false];
}

/**
 * Attaches the virtual display to the browser's cleanup, and on a GPU display
 * checks the browser renders on the GPU. (Python has an async and a sync
 * variant; playwright-core has one API.)
 */
export async function attachVirtualDisplay<T>(
	browser: T,
	virtualDisplay?: VirtualDisplay | null,
): Promise<T> {
	if (!virtualDisplay) return browser; // Skip if no virtual display is provided

	const target = browser as any;
	const originalClose = target.close.bind(target);

	target.close = async (...args: any[]) => {
		try {
			return await originalClose(...args);
		} finally {
			virtualDisplay.kill();
		}
	};
	// close() never runs on an unexpected disconnect/close, so wire teardown
	// directly too; kill() is idempotent.
	target.on?.(target.contexts ? "disconnected" : "close", () =>
		virtualDisplay.kill(),
	);
	target._virtualDisplay = virtualDisplay;

	if (virtualDisplay instanceof GpuDisplay) {
		try {
			await proveGpu(target, virtualDisplay);
		} catch (error) {
			await target.close();
			throw error;
		}
	}
	return browser;
}

/**
 * The Firefox version of the build about to launch: the named executable's own
 * application.ini, or the installed release. A named executable without one
 * throws, since claiming another build's version would leak in the UA.
 */
export function resolveVerstr(executablePath?: string | null): string {
	if (!executablePath) return utilsDeps.installedVerStr();
	const ini = buildFile(executablePath, "application.ini");
	for (const line of fs.readFileSync(ini, "utf-8").split(/\r\n|\r|\n/)) {
		if (line.startsWith("Version=")) {
			return line.slice("Version=".length).trim();
		}
	}
	throw new ValueError(`${ini} names no Version`);
}

/** A bound on the screen a generated fingerprint may claim. */
type ScreenConstraint =
	| Screen
	| {
			min_width?: number;
			max_width?: number;
			min_height?: number;
			max_height?: number;
			minWidth?: number;
			maxWidth?: number;
			minHeight?: number;
			maxHeight?: number;
	  };

function toScreen(screen: ScreenConstraint): Screen {
	if (screen instanceof Screen) return screen;
	const s = screen as Record<string, number | undefined>;
	return new Screen({
		minWidth: s.minWidth ?? s.min_width,
		maxWidth: s.maxWidth ?? s.max_width,
		minHeight: s.minHeight ?? s.min_height,
		maxHeight: s.maxHeight ?? s.max_height,
	});
}

export interface LaunchOptions {
	/** Camoufox properties to use.
	 * (read https://github.com/daijro/camoufox/blob/main/README.md) */
	config?: Record<string, any>;
	/** Operating system to use for the fingerprint generation. Can be
	 * "windows", "macos", "linux", or a list to randomly choose from. */
	os?: ListOrString;
	/** Whether to block all images. */
	block_images?: boolean;
	/** Whether to block WebRTC entirely. */
	block_webrtc?: boolean;
	/** Whether to block WebGL. To prevent leaks, only use this for special cases. */
	block_webgl?: boolean;
	/** Disables the Cross-Origin-Opener-Policy, allowing elements in cross-origin
	 * iframes, such as the Turnstile checkbox, to be clicked. */
	disable_coop?: boolean;
	/** Use a specific WebGL vendor/renderer pair, as [vendor, renderer]. */
	webgl_config?: [string, string];
	/** Calculate longitude, latitude, timezone, country, & locale based on the IP
	 * address. Pass the target IP address to use, or `true` to find it. */
	geoip?: string | boolean;
	/** Name of the GeoIP database to use (e.g. "GeoIP AIO by daijro"). */
	geoip_db?: string;
	/** Humanize mouse, keyboard and scroll input, per channel. `true` uses the
	 * best engine the build offers on every channel; a number is the MAX
	 * duration in seconds of a cursor movement; an object picks an engine per
	 * channel with the factories in humanize.ts, e.g.
	 * `{ mouse: cursory({ maxTime: 1.0 }), scroll: raw(), seed: 1234 }`. */
	humanize?: HumanizeSetting;
	/** Locale(s) to use. The first listed locale is used for the Intl API. */
	locale?: string | string[];
	/** List of Firefox addons to use (paths to extracted addons). */
	addons?: string[];
	/** Fonts to load into Camoufox (in addition to the target `os`'s). */
	fonts?: string[];
	/** If enabled, OS-specific system fonts will not be passed to Camoufox. */
	custom_fonts_only?: boolean;
	/** Default addons to exclude. */
	exclude_addons?: DefaultAddon[];
	/** Constrains the screen dimensions of the generated fingerprint. */
	screen?: ScreenConstraint;
	/** Set a fixed window size instead of generating a random one. */
	window?: [number, number];
	/** Use a custom fpgen fingerprint. */
	fingerprint?: Record<string, any>;
	/** Opt into real fingerprint presets: `true` for a random bundled preset,
	 * or a preset object. */
	fingerprint_preset?: boolean | Record<string, any>;
	/** Firefox version to use. Defaults to the current Camoufox version. */
	ff_version?: number;
	/** Whether to run the browser in headless mode. Defaults to false. */
	headless?: boolean;
	/** Whether to enable running scripts in the main world ("mw:" prefix). */
	main_world_eval?: boolean;
	/** Whether to allow addons to open new tabs. Defaults to false. */
	allow_addon_new_tab?: boolean;
	/** Custom Camoufox browser executable path. */
	executable_path?: string;
	/** Select a specific installed browser version ("official/beta.20",
	 * "beta.20", "134.0.2-beta.20"). Never downloaded. Defaults to the paired build. */
	browser?: string;
	/** Firefox user preferences to set. */
	firefox_user_prefs?: Record<string, any>;
	/** Proxy to use for the browser. */
	proxy?: ProxyConfig;
	/** Cache previous pages, requests, etc (uses more memory). */
	enable_cache?: boolean;
	/** Arguments to pass to the browser. */
	args?: string[];
	/** Environment variables to set. Defaults to a copy of process.env. */
	env?: EnvVars;
	/** Suppress leak warnings for options you are deliberately overriding. */
	i_know_what_im_doing?: boolean;
	/** Prints the config being sent to Camoufox. */
	debug?: boolean;
	/** Virtual display number, e.g. ":99". Handled by Camoufox & NewBrowser. */
	virtual_display?: string;
	/** Pin the browser to navigator.hardwareConcurrency cores (Linux/Windows).
	 * OFF by default -- it costs real CPU and serializes concurrent launches. */
	pin_cpu_cores?: boolean;
	/** Replace canvas and WebGL readback with random data, as
	 * privacy.resistFingerprinting does in LibreWolf, Tor Browser and Mullvad
	 * Browser. Stock Firefox does not, so it warns. By default (undefined) the
	 * identity on the host's OS claims the GPU the host renders with, uncovered;
	 * it is on whenever the identity claims another OS or another GPU
	 * (webgl_config, a preset's, or a host GPU fpgen never recorded), whose
	 * canvas would otherwise show the host. */
	canvas_noise?: boolean | null;
	/** Camoufox Pro key (cfp_live_...) for a Pro build. Defaults to
	 * CAMOUFOX_PRO_KEY, then the key `camoufox login` stored. A Pro build is
	 * launched with a lease minted with it (see docs/pro.md); the lease is
	 * released when the browser closes, or at exit when these options are
	 * launched without NewBrowser/Camoufox. */
	pro_key?: string;
	/** A Camoufox Pro profile's key (any name up to 64 characters). The browser
	 * presents the profile's identity, the same on every launch, and its state
	 * (cookies, storage, logins) is restored before the launch and synced back
	 * when it closes. Needs `os`; the launch is a persistent context. See
	 * docs/pro.md, "Profiles". */
	profile?: string;
	/** The warm plan a new `profile` is created with: `"none"` (the default
	 * with your own `proxy`) is the only plan whose state syncs to this
	 * machine; `"standard"` and `"continuous"` profiles are kept warm in the
	 * cloud and launch with their identity only. Left out, the API decides. */
	warm_plan?: "none" | "standard" | "continuous";
	/** Camoufox Pro managed egress: `false` for none, `true` for CamouProxy
	 * with defaults, or the egress wanted, e.g. `{ class: "residential",
	 * country: "US" }`, with `provider` naming a partner provider instead of
	 * CamouProxy. Left out, a `profile` gets CamouProxy and a launch without
	 * one goes direct.
	 * With your own `proxy` there is none, and passing both throws. */
	egress?: boolean | pro.EgressRequest;
	/** `false` renders WebGL, WebGPU and canvas on this machine's GPU for a
	 * Camoufox Pro Windows identity, instead of on a remote GPU. */
	farm_gpu?: false;
	/** Camoufox Pro: draw WebGL on a render node, a Windows machine that runs
	 * every WebGL call on Windows Firefox's own ANGLE and GPU (see
	 * docs/pro.md). A path or object names the node's coordinator; undefined
	 * (the default) uses CAMOUFOX_RENDER_NODE or
	 * ~/.config/camoufox/render-node.json when either exists; `false` draws
	 * locally. The identity is Windows: `os` defaults to it, and another OS
	 * throws when the node was named here and draws locally otherwise. */
	render_node?: renderNodes.RenderNodeOption;
	/** Additional Firefox launch options, passed straight through to Playwright. */
	[key: string]: any;
}

/**
 * Builds the Playwright Firefox launch options for Camoufox.
 *
 * Accepts all Playwright Firefox launch options, along with the Camoufox ones
 * documented on {@link LaunchOptions}.
 */
export async function launchOptions(
	options: LaunchOptions = {},
): Promise<Record<string, any>> {
	// A Camoufox Pro lease or node session opened along the way goes back if the build fails.
	const held: { lease: pro.Lease | null; node: renderNodes.Session | null } = {
		lease: null,
		node: null,
	};
	try {
		return await buildLaunchOptions(options, held);
	} catch (error) {
		await held.lease?.release("error");
		await held.node?.release();
		throw error;
	}
}

const OS_CODE: Record<string, string> = {
	windows: "win",
	macos: "mac",
	linux: "lin",
};

/** The Firefox version of the build at `executablePath`, without Camoufox's release suffix. */
function firefoxVersion(executablePath: string): string {
	return resolveVerstr(executablePath).split("-", 1)[0];
}

async function buildLaunchOptions(
	{
		config,
		os: targetOsOption,
		block_images,
		block_webrtc,
		block_webgl,
		disable_coop,
		webgl_config,
		geoip,
		geoip_db,
		humanize,
		locale,
		addons,
		fonts,
		custom_fonts_only,
		exclude_addons,
		screen,
		window,
		fingerprint,
		fingerprint_preset,
		ff_version,
		headless,
		main_world_eval,
		allow_addon_new_tab,
		executable_path,
		browser,
		firefox_user_prefs,
		proxy,
		enable_cache,
		args,
		env,
		i_know_what_im_doing,
		debug,
		virtual_display,
		pin_cpu_cores,
		canvas_noise,
		pro_key,
		profile,
		warm_plan,
		egress,
		farm_gpu,
		render_node,
		...passthrough
	}: LaunchOptions,
	held: { lease: pro.Lease | null; node: renderNodes.Session | null },
): Promise<Record<string, any>> {
	utilsDeps.ensureBrowserProfileDir(env);

	// The launch fills these in, so it works on copies: a caller's object
	// reused for a second launch would otherwise carry the first identity's
	// values in, as if the caller had set them.
	config = { ...config };
	addons = [...(addons ?? [])];
	args = [...(args ?? [])];
	firefox_user_prefs = { ...firefox_user_prefs };

	// Set default values for optional arguments
	headless ??= false;
	// The caller's own prefs, before the launcher adds any: a pref the build
	// declares in launch.json never replaces one of these.
	const userPrefKeys = new Set(Object.keys(firefox_user_prefs));
	custom_fonts_only ??= false;
	i_know_what_im_doing ??= false;
	// Keep per-launch overrides isolated from the process environment and from
	// mappings supplied by callers. In particular, DISPLAY must not outlive the
	// virtual display that owns it.
	env = env == null ? ({ ...process.env } as EnvVars) : { ...env };
	// CAMOUFOX_EXECUTABLE_PATH points every launch at a specific build without
	// threading the path through each call site; the CI runners set it.
	const envExecutable = (process.env.CAMOUFOX_EXECUTABLE_PATH ?? "").trim();
	if (browser) {
		if (executable_path || envExecutable) {
			throw new ValueError(
				`browser '${browser}' selects an installed build, but ` +
					`${executable_path ? "executable_path" : "CAMOUFOX_EXECUTABLE_PATH"} ` +
					"names a binary too. Pass one of them.",
			);
		}
		// A selected build is launched like a caller's own binary: its files
		// are read from beside it, and the paired build is neither used nor fetched.
		executable_path = utilsDeps.selectedLaunchPath(browser);
	} else if (executable_path == null && envExecutable) {
		executable_path = envExecutable;
	}
	if (typeof executable_path === "string") {
		executable_path = path.resolve(executable_path);
	}

	// The managed install is resolved lazily in Python (camoufox_path() may
	// download); here that download is async, so do it before any sync lookup.
	if (!executable_path) {
		await utilsDeps.ensureCamoufoxInstalled();
	}

	// A Pro build is recognised by its files, so the binary is resolved before
	// the lease.
	const resolvedExecutable = executable_path
		? String(executable_path)
		: utilsDeps.launchPath();

	// A Pro build does not start without a lease. The caller's own lease file,
	// if they hold one, wins, as every other variable in the environment does.
	const proBuild = pro.readBuild(
		buildFile(resolvedExecutable, "pro-build.json"),
	);
	const leased = Boolean(proBuild) && !(pro.LEASE_FILE_ENV in env);
	const proRequest: pro.LeaseRequest = {};
	if (profile !== undefined) proRequest.profile = profile;
	if (warm_plan !== undefined) {
		if (profile === undefined) {
			throw new ValueError("warm_plan applies to a profile: pass profile too.");
		}
		proRequest.warm_plan = warm_plan;
	}
	// The caller's own proxy is never replaced by managed egress, and asking for both is a mistake
	// worth naming rather than a request to drop one of them quietly.
	if (proxy != null && egress !== undefined && egress !== false) {
		throw new ValueError(
			"proxy and egress conflict: with your own proxy there is no managed egress. " +
				"Pass proxy to use your own, or egress (and no proxy) for managed egress.",
		);
	}
	if (egress !== undefined) proRequest.egress = egress;
	if (farm_gpu === false) proRequest.gpu = false;
	if (Object.keys(proRequest).length && !leased) {
		throw new ValueError(
			proBuild
				? `profile, warm_plan, egress and farm_gpu are lease options, and this launch carries its own ${pro.LEASE_FILE_ENV}.`
				: "profile, warm_plan, egress and farm_gpu need a Camoufox Pro build (see docs/pro.md).",
		);
	}
	// A render node runs Windows Firefox's ANGLE, so it serves Windows identities of a Pro build.
	const namedNode = render_node != null && typeof render_node !== "boolean";
	let relay = proBuild || namedNode ? renderNodes.resolve(render_node) : null;
	if (relay) {
		const oses =
			typeof targetOsOption === "string"
				? [targetOsOption]
				: [...(targetOsOption ?? ["windows"])];
		const refusal = !proBuild
			? "render_node needs a Camoufox Pro build (see docs/pro.md)."
			: !leased && !namedNode
				? `render_node is a lease option, and this launch carries its own ${pro.LEASE_FILE_ENV}.`
				: !oses.includes("windows")
					? `render_node draws a Windows identity's WebGL, and os=${JSON.stringify(targetOsOption)} has no Windows.`
					: null;
		if (refusal === null) {
			targetOsOption = "windows";
			if (leased) proRequest.gpu = false;
		} else if (namedNode) {
			throw new ValueError(refusal);
		} else {
			relay = null;
		}
	}
	let node: renderNodes.Session | null = null;
	// Your own proxy, asked for no other way, means no managed egress.
	if (proxy != null) proRequest.egress = false;
	// What the lease adds to the environment, after every launch rule.
	const proEnv: EnvVars = {};

	if (profile !== undefined && proBuild) {
		if (typeof targetOsOption !== "string") {
			throw new ValueError(
				"A profile launch needs `os`: the one OS its identity presents.",
			);
		}
		checkValidOs(targetOsOption);
		if (
			fingerprint != null ||
			pyTruthy(fingerprint_preset) ||
			pyTruthy(config) ||
			passthrough.user_data_dir !== undefined ||
			passthrough.userDataDir !== undefined
		) {
			throw new ValueError(
				"A profile carries its own identity and browser state: pass no config, fingerprint, " +
					"fingerprint_preset or user_data_dir with it.",
			);
		}
		held.lease = await pro.acquire(
			proBuild,
			OS_CODE[targetOsOption],
			pro_key,
			proRequest,
		);
		const { identity, userDataDir } = await openProfile(
			held.lease,
			firefoxVersion(resolvedExecutable),
		);
		// The identity is the bundle's, whole: nothing is drawn for it here.
		config = identity.config;
		fingerprint = identity.fingerprint;
		targetOsOption = identity.os;
		ff_version = identity.ff_version;
		exclude_addons = identity.exclude_addons as DefaultAddon[];
		i_know_what_im_doing = true;
		Object.assign(firefox_user_prefs, identity.firefox_user_prefs);
		for (const key of Object.keys(identity.firefox_user_prefs)) {
			userPrefKeys.add(key);
		}
		passthrough.user_data_dir = userDataDir;
	}

	// Handle virtual display
	if (virtual_display) {
		env.DISPLAY = virtual_display;
		// Virtual display uses Xvfb (X11). If the host session forces Wayland via
		// env vars, GTK/Firefox may try Wayland and ignore DISPLAY.
		env.GDK_BACKEND = "x11";
		delete env.WAYLAND_DISPLAY;
		env.MOZ_ENABLE_WAYLAND = "0";
	}

	// Warn the user for manual config settings
	if (!i_know_what_im_doing) {
		warnManualConfig(config);
	}

	// Snapshot which domains the USER set before fingerprint generation fills in
	// the rest. The post-generation corrections below must only touch generated
	// values, never override what the user passed.
	const userSetNavigator = isDomainSet(config, "navigator.");
	const userSetScreenWindow = isDomainSet(config, "screen.", "window.");
	const userSetMediaDevices = isDomainSet(config, "mediaDevices:");
	const userSetFonts = Boolean(fonts?.length) || isDomainSet(config, "fonts");
	const userSetVoices = isDomainSet(config, "voices", "voices:speechd");
	const userSetDnt = "navigator.doNotTrack" in config;
	const userSetGpc = "navigator.globalPrivacyControl" in config;
	const userSetAcceptEncoding = "headers.Accept-Encoding" in config;
	dropAudioSeed(config);

	// The salt that makes every seeded draw belong to this identity (see
	// fingerprints.identitySalt): stable when the caller pinned the identity --
	// a fingerprint, a preset object, or their own config naming the UA -- and
	// fresh otherwise.
	let salt: bigint | number;
	if (fingerprint != null) {
		salt = utilsDeps.identitySalt(fingerprint);
	} else if (isPlainObject(fingerprint_preset)) {
		salt = utilsDeps.identitySalt(fingerprint_preset);
	} else if ("navigator.userAgent" in config) {
		salt = utilsDeps.identitySalt({ ...config });
	} else {
		salt = utilsDeps.identitySalt();
	}

	// Assert the target OS is valid
	if (pyTruthy(targetOsOption)) {
		checkValidOs(targetOsOption as ListOrString);
	} else if (pyTruthy(webgl_config)) {
		// webgl_config requires OS to be set
		throw new ValueError("OS must be set when using webgl_config");
	}

	// The managed install is resolved lazily in Python (camoufox_path() may
	// download); here that download is async, so do it before any sync lookup.
	if (!executable_path) {
		await utilsDeps.ensureCamoufoxInstalled();
	}
	warnIfPackageOutdated();

	// The default addons are the ones that build bundles, beside its binary
	await utilsDeps.addDefaultAddons(
		addons,
		exclude_addons,
		executable_path ? String(executable_path) : utilsDeps.launchPath(),
	);

	// Confirm all addon paths are valid
	if (addons.length) {
		utilsDeps.confirmPaths(addons);
		config.addons = addons;
	}

	// Get the Firefox version
	let ffVersionStr: string;
	if (ff_version) {
		ffVersionStr = String(ff_version);
		LeakWarning.warn("ff_version", i_know_what_im_doing);
	} else {
		ffVersionStr = resolveVerstr(executable_path).split(".")[0];
	}

	// Generate a fingerprint
	let usedPreset = false;
	if (fingerprint != null) {
		// User passed a custom fingerprint
		if (!i_know_what_im_doing) {
			checkCustomFingerprint(fingerprint);
		}
	} else if (pyTruthy(fingerprint_preset)) {
		// User opted into real fingerprint presets
		const preset = isPlainObject(fingerprint_preset)
			? fingerprint_preset
			: await utilsDeps.getRandomPreset(targetOsOption, ffVersionStr);
		if (pyTruthy(preset)) {
			mergeInto(
				config,
				await utilsDeps.fromPreset(preset as any, ffVersionStr, salt),
			);
			usedPreset = true;
		}
	}

	// Bound the geometry to the real display. The generator only honours this
	// when its pool has a match, so it is re-applied after generation as well.
	// `headless` and "is there a display to probe" are separate questions.
	const screenCons: Screen | null = screen
		? toScreen(screen)
		: utilsDeps.hasDisplay(env)
			? utilsDeps.getScreenCons(headless)
			: null;

	if (!usedPreset && fingerprint == null) {
		// Default: synthetic generation via fpgen (infinite unique fingerprints)
		fingerprint = await utilsDeps.generateFingerprint({
			screen: screenCons ?? undefined,
			window,
			os: targetOsOption,
		});
	}

	if (!usedPreset && fingerprint != null) {
		// Inject the generated fingerprint into the config
		mergeInto(config, utilsDeps.fromFpgen(fingerprint, ffVersionStr));
	}

	const targetOs = getTargetOs(config);

	if (leased) {
		held.lease ??= await pro.acquire(
			proBuild as pro.ProBuild,
			targetOs,
			pro_key,
			proRequest,
		);
		proEnv[pro.LEASE_FILE_ENV] = held.lease.path;
		const grants = held.lease.grants;
		if (grants.egress) {
			proxy = {
				server: grants.egress.server,
				username: grants.egress.username,
				password: grants.egress.password,
				// The egress refuses local destinations, so they go direct.
				bypass: "localhost,127.0.0.1,::1,*.local",
			};
			geoip ??= grants.egress.exit_ip ?? true;
		}
		// A profile's bundle leaves timezone and locale to the exit it runs behind.
		if (grants.profile) geoip ??= true;
		if (grants.gpu) {
			proEnv[pro.RENDER_FILE_ENV] = held.lease.renderPath as string;
			Object.assign(firefox_user_prefs, grants.gpu.prefs);
			for (const key of Object.keys(grants.gpu.prefs)) userPrefKeys.add(key);
			// The farm draws the canvas on the identity's own OS, so there is nothing to cover.
			canvas_noise ??= false;
			// The farm is reached directly: the page's proxy is for the page, and one that cannot reach
			// the render gateway leaves every farm call waiting on a route that never opens.
			if (proxy) {
				const farmHost = new URL(grants.gpu.render.endpoint).hostname;
				const bypass = (proxy.bypass ?? "").split(",").filter(Boolean);
				proxy = { ...proxy, bypass: [...bypass, farmHost].join(",") };
			}
		}
	}

	if (relay) {
		const directory = path.dirname(
			held.lease?.path ?? String(env[pro.LEASE_FILE_ENV]),
		);
		node = await renderNodes.Session.open(
			relay,
			firefoxVersion(resolvedExecutable),
			directory,
		);
		held.node = node;
		if (held.lease) {
			const opened = node;
			held.lease.onEnd.push(() => opened.release());
		} else {
			renderNodes.track(node);
		}
		proEnv[renderNodes.RELAY_ENV] = node.path;
		if (node.textSocket) proEnv[renderNodes.TEXT_ENV] = node.textSocket;
		// The node draws on the identity's own OS and GPU, so there is nothing to cover.
		canvas_noise ??= false;
	}

	// A preset whose screen is a phone viewport is not a real desktop device.
	if (!userSetScreenWindow && coherence.screenIsImplausible(config)) {
		coherence.repairScreenOrientation(config);
		utilsDeps.raiseScreenToModernFloor(config);
	}

	// Correct fingerprint inconsistencies that leak as headless /
	// impossible-geometry tells, unless the user is driving these themselves.
	if (!userSetNavigator) {
		utilsDeps.fixNavigatorArch(config, targetOs);
		utilsDeps.fixHardwareConcurrency(config, Boolean(pin_cpu_cores));
	}
	if (!userSetScreenWindow) {
		// Lift netbook-era geometry to something current hardware reports,
		// before the display clamp below so a genuinely small real monitor still
		// wins (#729). Synthetic draws only: a preset is a real device.
		if (!usedPreset) {
			utilsDeps.raiseScreenToModernFloor(config);
		}
		// Headful on a real monitor only: this bound exists so the window fits
		// the screen it is drawn on. headless has no window to overflow, and
		// headless="virtual" reaches here as headless=false with a 1x1 Xvfb.
		if (headless === false && !virtual_display && screenCons) {
			const fit = physicalScale(config);
			utilsDeps.clampScreenToDisplay(
				config,
				(screenCons.maxWidth &&
					Math.trunc(screenCons.maxWidth / fit)) as number,
				(screenCons.maxHeight &&
					Math.trunc(screenCons.maxHeight / fit)) as number,
			);
		}
		utilsDeps.followBaselineProtection(config, targetOs);
		utilsDeps.clampWindowDimensions(config);
		utilsDeps.clampWindowPosition(config);
	}

	// Deliberately NOT setting window.history.length: browser/settings/camoufox.cfg runs
	// Firefox's stock max_entries, so the real value starts at 1 and grows with
	// each navigation; pinning it would contradict history.back().

	// Update fonts list
	if (fonts?.length) {
		config.fonts = fonts;
	}

	if (targetOs === "win") {
		const expanded = [...(config.fonts || [])];
		const presented = new Set(expanded);
		for (const [canonical, aliases] of Object.entries(
			LAUNCHER_CONSTANTS.windowsLegacyFontFamilies,
		)) {
			if (!presented.has(canonical)) continue;
			for (const alias of aliases) {
				if (!presented.has(alias)) {
					expanded.push(alias);
					presented.add(alias);
				}
			}
		}
		config.fonts = expanded;
	}

	if (custom_fonts_only) {
		firefox_user_prefs["gfx.bundled-fonts.activate"] = 0;
		if (fonts?.length) {
			LeakWarning.warn("custom_fonts_only");
		} else {
			throw new ValueError(
				"No custom fonts were passed, but `custom_fonts_only` is enabled.",
			);
		}
	} else if (!userSetFonts || !pyTruthy(config.fonts)) {
		// Draw the font subset HERE, after every identity fix-up above, so the
		// seed sees the final UA/screen/cores/GPU: the same presented identity
		// always gets the same font list (#442/#765).
		const osName = OS_NAMES[targetOs] ?? "macos";
		try {
			config.fonts = utilsDeps.generateRandomFontSubset(
				osName,
				utilsDeps.identitySeed(config, salt),
				// host's own OS on macOS/Windows: the real system fonts are used
				// (font-hijacker.patch keeps the bundle inactive), so only the OS
				// base is claimed
				(targetOs === "mac" || targetOs === "win") &&
					utilsDeps.hostOsKey() === targetOs,
			);
		} catch (e) {
			if (!isPyError(e, "OSError", "ValueError")) throw e;
			FallbackWarning.warn(
				"Drawing the font list",
				`every font fonts.json lists for ${targetOs}`,
				e,
				config["navigator.userAgent"],
			);
			updateFonts(config, targetOs);
		}
	}

	// Draw the identity's media devices unless the caller set any mediaDevices:
	// key. An empty enumerateDevices() list is a headless tell; a wrong label
	// after a grant is a spoof tell.
	if (!userSetMediaDevices) {
		await utilsDeps.setMediaDevicesDefaults(config, salt);
	}

	// Scrollbars: pin the look-and-feel to the claimed OS so headless == headed
	// == stock, and for Windows to the version the identity's font draw
	// presents (Win11 fonts with classic scrollbars is a pair no real machine
	// produces).
	if (targetOs === "win") {
		const presentedFonts: string[] = config.fonts || [];
		const windows11 =
			!presentedFonts.length ||
			presentedFonts.some((font) => WINDOWS_11_MARKER_FONTS.has(font));
		setInto(firefox_user_prefs, "ui.useOverlayScrollbars", windows11 ? 1 : 0);
	} else {
		setInto(firefox_user_prefs, "ui.useOverlayScrollbars", 1);
	}

	// A Windows identity on another host: answers that come from the host
	// build's platform code are set to what stock Windows Firefox gives.
	if (targetOs === "win" && utilsDeps.hostOsKey() !== "win") {
		// Math.sin/cos/tan/pow as the Windows CRT computes them, to the last bit
		// (windows-crt-math.patch).
		setInto(firefox_user_prefs, "javascript.options.use_ucrt_math", true);

		// Keyboard: Windows' shortcut tables instead of GTK's text bindings, and
		// AltGr as keyCode 18 rather than 225 (windows-keyboard.patch).
		setInto(config, "input:windowsKeyboard", true);

		for (const [pref, value] of Object.entries(WINDOWS_BEHAVIOUR_PREFS)) {
			setInto(firefox_user_prefs, pref, value);
		}

		// The scrollbars pinned above are drawn by the host build's theme (GTK's
		// 12 px where Windows 10 draws 17): draw Windows' own, 4 Windows 10 or
		// 5 Windows 11 (17 px: only Windows' build reads a system metric).
		const overlay = firefox_user_prefs["ui.useOverlayScrollbars"];
		setInto(
			firefox_user_prefs,
			"widget.non-native-theme.scrollbar.style",
			overlay ? 5 : 4,
		);
	}

	// Per-character font fallback, LINUX ONLY: with async fallback on, the
	// first measurement of a character only one bundled family provides returns
	// the primary family's .notdef. macOS must NOT get this. The skip belongs to
	// the Linux platform code, so another OS's identity on a Linux host needs it
	// too.
	const hostOs = utilsDeps.hostOsKey();
	if (targetOs === "lin" || hostOs === "lin") {
		setInto(firefox_user_prefs, "gfx.font_rendering.fallback.async", false);
	}

	// Per-language and generic fonts: the build only has its own platform's
	// font.name-list defaults, so another OS's identity takes that OS's stock
	// values (with fallback-fonts-claimed-os.patch for the per-script table).
	// These and the settings below replace answers that come from the Linux
	// build's platform code (gfxPlatformGtk, FreeType, the GTK theme). Only a
	// Linux build has that code, so on a macOS or Windows host nothing is set.
	// A macOS identity gets the font defaults only: the values below were
	// measured against stock Windows alone (see utils.py).
	const rendersAnotherOs =
		hostOs === "lin" && (targetOs === "win" || targetOs === "mac");
	if (rendersAnotherOs) {
		for (const [pref, value] of Object.entries(stockFontPrefs(targetOs))) {
			setInto(firefox_user_prefs, pref, value);
		}
	}

	if (rendersAnotherOs && targetOs === "win") {
		// Line boxes: DirectWrite's ascent, descent and em height rather than
		// FreeType's pixel-rounded ones (font-metrics-windows.patch).
		setInto(config, "fonts:metrics", "windows");

		// canPlayType()/isTypeSupported() and decodingInfo() answer for Windows
		// instead of the host's decoders (#558), as stock Firefox 158 on
		// Windows 11 answers.
		setInto(config, "media:spoof_codecs", true);

		// Windows' system colours instead of the host theme's.
		for (const [pref, value] of Object.entries(WINDOWS_UI_COLORS)) {
			setInto(firefox_user_prefs, pref, value);
		}
	}

	// Storage quota, from the host's own disk (see stockProfileDiskCapacityKb).
	const quotaLimitKb = utilsDeps.stockProfileDiskCapacityKb();
	if (quotaLimitKb) {
		setInto(firefox_user_prefs, QUOTA_FIXED_LIMIT_PREF, quotaLimitKb);
	}

	// navigator.doNotTrack and navigator.globalPrivacyControl are pref-backed in
	// Firefox; spoofing them anywhere else leaves the wire (or the worker)
	// contradicting the API (daijro/camoufox#760). A stock Firefox 152 reports
	// "unspecified" and false, so generated values are dropped unless the
	// caller set them explicitly. screen.colorDepth is left as drawn.
	if (!userSetDnt) {
		delete config["navigator.doNotTrack"];
	}
	if (!userSetGpc) {
		delete config["navigator.globalPrivacyControl"];
	}
	const dnt = config["navigator.doNotTrack"];
	firefox_user_prefs["privacy.donottrackheader.enabled"] =
		dnt != null && pyStr(dnt) === "1";
	const gpc = config["navigator.globalPrivacyControl"];
	firefox_user_prefs["privacy.globalprivacycontrol.enabled"] =
		gpc != null ? pyTruthy(gpc) : false;

	// Accept-Encoding: Firefox's own value is already what the identity claims,
	// so the generated header is dropped unless the caller set it.
	if (!userSetAcceptEncoding) {
		delete config["headers.Accept-Encoding"];
	}

	// Set geolocation
	if (pyTruthy(geoip)) {
		geoipAllowed(); // Assert that geoip is allowed

		let geoipIp: string;
		if (geoip === true) {
			// Find the user's IP address
			geoipIp = proxy
				? await utilsDeps.publicIp(ProxyHelper.asString(proxy))
				: await utilsDeps.publicIp();
		} else {
			geoipIp = geoip as string;
		}

		// Spoof WebRTC if not blocked
		if (!block_webrtc) {
			if (validIPv4(geoipIp)) {
				setInto(config, "webrtc:ipv4", geoipIp);
			} else if (validIPv6(geoipIp)) {
				setInto(config, "webrtc:ipv6", geoipIp);
			}
		}

		const geolocation = await utilsDeps.getGeolocation(geoipIp, geoip_db);
		const geoConfig = geolocation.asConfig();
		for (const [key, value] of Object.entries(geoConfig)) {
			if (
				[
					"timezone",
					"locale:language",
					"locale:region",
					"locale:script",
				].includes(key)
			) {
				setInto(config, key, value);
			} else {
				config[key] = value;
			}
		}

		// Without a proxy, a site that has an IPv6 address sees this host's,
		// not the IPv4 address the identity was drawn from. Keep IPv6 only when
		// it places the host where the identity does; otherwise the browser
		// behaves as on an IPv4-only network.
		if (
			!proxy &&
			validIPv4(geoipIp) &&
			!(await keepsIPv6(geoConfig, geoip_db, i_know_what_im_doing))
		) {
			config["network:ipv4Only"] = true;
		}
	}

	// A page that receives a position without a prompt must also see
	// permissions.query({name: 'geolocation'}) report "granted" (#769).
	if ("geolocation:latitude" in config && "geolocation:longitude" in config) {
		setInto(firefox_user_prefs, "permissions.default.geo", 1);
	} else if (
		// Raise a warning when a proxy is being used without spoofing
		// geolocation. This warning cannot be ignored with i_know_what_im_doing.
		pyTruthy(proxy) &&
		!(proxy?.server ?? "").includes("localhost") &&
		!isDomainSet(config, "geolocation")
	) {
		LeakWarning.warn("proxy_without_geoip");
	}

	// Set locale
	if (pyTruthy(locale)) {
		await handleLocales(locale as string | string[], config);
	}

	// Select the browser's UI locale to match the Intl locale. Always set: an
	// EMPTY value would follow the host OS locale.
	let requested: string;
	if (pyTruthy(config["locale:language"])) {
		requested = [
			config["locale:language"],
			config["locale:script"],
			config["locale:region"],
		]
			.filter((part) => pyTruthy(part))
			.join("-");
	} else {
		requested = "en-US";
	}
	setInto(firefox_user_prefs, "intl.locale.requested", requested);

	// Spoof the speech-synthesis voice list. This has to fail CLOSED: leaving
	// `voices` unset exposes every native voice on the box (#731). Drawn after
	// the locale is resolved: the Windows voice list is the display language's
	// pack.
	if (
		!userSetVoices ||
		(!("voices" in config) && !("voices:speechd" in config))
	) {
		const osNameV = OS_NAMES[targetOs] ?? "macos";
		let voiceLocale = config["navigator.language"];
		if (pyTruthy(config["locale:language"])) {
			voiceLocale = [config["locale:language"], config["locale:region"]]
				.filter((part) => pyTruthy(part))
				.join("-");
		}
		try {
			Object.assign(
				config,
				utilsDeps.drawVoiceConfig(
					osNameV,
					voiceLocale ?? null,
					utilsDeps.identitySeed(config, salt),
				),
			);
		} catch (e) {
			if (!isPyError(e, "OSError", "ValueError", "KeyError")) throw e;
			// An empty list still blocks the host's voices (see below), so a
			// generation failure degrades to "no voices" rather than "all of
			// the host's".
			FallbackWarning.warn(
				"Drawing the speech voices",
				"no speech voices",
				e,
				config["navigator.userAgent"],
			);
			config.voices = [];
		}
	}

	// Pin the block explicitly instead of relying on a non-empty list to imply
	// it. setInto leaves an explicit caller value alone.
	setInto(config, "voices:blockIfNotDefined", true);

	// Pass the humanize option, checked against the engines the build ships
	for (const [key, value] of Object.entries(
		humanizeConfig(
			humanize,
			utilsDeps.loadHumanizeEngines(executable_path),
			i_know_what_im_doing,
		),
	)) {
		setInto(config, key, value);
	}

	// Enable the main world context creation
	if (main_world_eval) {
		setInto(config, "allowMainWorld", true);
	}

	// Allow addon open new tabs
	if (allow_addon_new_tab) {
		setInto(config, "allowAddonNewtab", true);
	}

	// Set Firefox user preferences. Each toggle writes its pref on or off: a
	// persistent profile keeps a user.js pref in prefs.js after the launch that
	// set it, so a flag that only wrote when on stayed on for good.
	if (block_images) {
		LeakWarning.warn("block_images", i_know_what_im_doing);
	}
	if (disable_coop) {
		LeakWarning.warn("disable_coop", i_know_what_im_doing);
	}
	firefox_user_prefs["permissions.default.image"] ??= block_images ? 2 : 1;
	firefox_user_prefs["media.peerconnection.enabled"] ??= !block_webrtc;
	firefox_user_prefs["browser.tabs.remote.useCrossOriginOpenerPolicy"] ??=
		!disable_coop;
	// A persistent context takes its context options here.
	if (passthrough.is_mobile || passthrough.isMobile) {
		LeakWarning.warn("is_mobile", i_know_what_im_doing);
	}

	// Drop values the source supplied that this identity cannot keep, before the
	// WebGL pool below defers to them. Here, not earlier, because the check
	// reads the core count and the screen, which the launch replaces above.
	coherence.dropIncoherentSourceValues(config, targetOs);

	const [coverCanvas, host] = await planRendering(
		targetOs,
		canvas_noise,
		pyTruthy(webgl_config) ||
			(pyTruthy(config["webGl:vendor"]) && pyTruthy(config["webGl:renderer"])),
		Boolean(block_webgl),
		() =>
			utilsDeps.hostGpu(resolvedExecutable, headless, env, firefox_user_prefs),
	);
	if (coverCanvas) {
		addCanvasPlaceholder(firefox_user_prefs);
		LeakWarning.warn("canvas_noise", i_know_what_im_doing);
	}

	if (block_webgl) {
		firefox_user_prefs["webgl.disabled"] = true;
		LeakWarning.warn("block_webgl", i_know_what_im_doing);
	} else if (node) {
		if (pyTruthy(webgl_config)) {
			throw new ValueError(
				"webgl_config names a GPU, but a render node reports its own: pass render_node: false.",
			);
		}
		// Every WebGL value is the node's own, as Windows Firefox reports it there.
		for (const key of Object.keys(config)) {
			if (key.startsWith("webGl:") || key.startsWith("webGl2:"))
				delete config[key];
		}
		mergeInto(firefox_user_prefs, {
			"webgl.enable-webgl2": true,
			"webgl.force-enabled": true,
			// Windows Firefox draws large canvases with WebGL on its ANGLE; here
			// that ANGLE is the node's.
			"gfx.canvas.accelerated": true,
			"gfx.canvas.accelerated.force-enabled": true,
		});
		if (
			node.routes.has("webgpu") &&
			!("dom.webgpu.renderfarm.node" in firefox_user_prefs)
		) {
			firefox_user_prefs["dom.webgpu.renderfarm.node"] = true;
		}
	} else {
		let webglFp: Record<string, any>;
		const seed = () =>
			utilsDeps.identitySeed(config as Record<string, any>, salt);
		// A pair the caller named, or the preset's own GPU, keeps its name and
		// gets that device's recorded parameters. webglForGpu raises for a GPU
		// fpgen has never seen: the caller asked for something that does not exist.
		if (pyTruthy(webgl_config)) {
			const [vendor, renderer] = webgl_config as [string, string];
			webglFp = await utilsDeps.webglForGpu(targetOs, vendor, renderer, seed());
		} else if (
			pyTruthy(config["webGl:vendor"]) &&
			pyTruthy(config["webGl:renderer"])
		) {
			webglFp = await utilsDeps.webglForGpu(
				targetOs,
				config["webGl:vendor"],
				config["webGl:renderer"],
				seed(),
			);
		} else if (host) {
			// Claim the GPU the canvas really renders on.
			webglFp = await utilsDeps.webglForGpu(targetOs, ...host, seed());
		} else {
			// Synthetic path: keep the GPU coherent with the screen fpgen already
			// picked. Sampling the two independently yields pairs no real machine
			// ships -- a discrete desktop GPU behind a 1024x600 panel (#729).
			webglFp = await utilsDeps.sampleWebglForScreen(
				targetOs,
				config["screen.width"],
				config["screen.height"],
				seed(),
				config["navigator.hardwareConcurrency"],
			);
		}
		const enableWebgl2 = webglFp.webGl2Enabled;
		delete webglFp.webGl2Enabled;

		// Merge the WebGL fingerprint into the config
		mergeInto(config, webglFp);
		// Set the WebGL preferences
		mergeInto(firefox_user_prefs, {
			"webgl.enable-webgl2": enableWebgl2,
			"webgl.force-enabled": true,
		});
	}

	// Every identity passes the whole-identity checks, whatever built it. The
	// pools are sampled independently, so a machine that never existed can be
	// assembled from parts that are each fine on their own. See coherence.ts.
	const incoherent = coherence.apply(config, targetOs);
	if (incoherent.length && debug) {
		for (const violation of incoherent) {
			console.log(
				`Incoherent identity (${violation.rule}): ${violation.detail}`,
			);
		}
	}

	// Render at the claimed display scale, so (resolution) and the CSS geometry
	// agree with devicePixelRatio as on the real machine (#933).
	const dpr = Number(config["window.devicePixelRatio"] || 1);
	if (dpr !== 1 && !("layout.css.devPixelsPerPx" in firefox_user_prefs)) {
		firefox_user_prefs["layout.css.devPixelsPerPx"] = String(dpr);
	}
	if (headless === false && !virtual_display && screenCons) {
		const fit = physicalScale(config, firefox_user_prefs);
		for (const [axis, cap] of [
			["Width", screenCons.maxWidth],
			["Height", screenCons.maxHeight],
		] as const) {
			const size = config[`window.outer${axis}`];
			if (cap && size && size * fit > cap) {
				throw new Error(
					`window.outer${axis} ${size} at display scale ${fit} needs ${size * fit} pixels, ` +
						`but this monitor has ${cap}. Pass a smaller screen and window, or run headless.`,
				);
			}
		}
	}

	setWebgpu(
		firefox_user_prefs,
		targetOs,
		[config["webGl:vendor"], config["webGl:renderer"]],
		host,
	);

	// Cache previous pages, requests, etc (uses more memory)
	if (enable_cache) {
		mergeInto(firefox_user_prefs, CACHE_PREFS);
	}

	// Print the config if debug is enabled
	if (debug) {
		console.log("[DEBUG] Config:");
		console.log(inspect(config, { depth: null, sorted: true }));
	}

	applyLaunchRules(
		targetOs,
		config,
		firefox_user_prefs,
		userPrefKeys,
		env,
		executable_path,
	);
	if (node) {
		// The holo prefs make local GL draw like Windows ANGLE; on a node, Windows ANGLE draws.
		for (const name of Object.keys(firefox_user_prefs)) {
			if (name.startsWith("webgl.holo-")) firefox_user_prefs[name] = false;
		}
		if (firefox_user_prefs["dom.webgpu.renderfarm.node"]) {
			// Likewise WebGPU's D3D12 limit table and NVIDIA emulation: the node reports its own.
			delete config["webGPU:d3d12Limits"];
			delete env.HOLO_WEBGPU_NV_ARCH;
		}
	}

	// Validate the config
	warnIfExecutablePredatesPlaywright(executable_path);
	utilsDeps.validateConfig(config, executable_path);

	// A SOCKS5 proxy's credentials go to Juggler, not through Playwright, which
	// refuses them (socks5Auth).
	const [launchProxy, proxyAuth] = socks5Auth(proxy);

	// Prepare environment variables to pass to Camoufox
	const envVars: EnvVars = {
		...utilsDeps.getEnvVars(config, targetOs, executable_path),
		...getPrefEnvVars(firefox_user_prefs),
		...(proxyAuth ? proxyAuthEnv(proxyAuth) : {}),
		...env,
	};

	Object.assign(envVars, proEnv);

	const result: Record<string, any> = {
		executablePath: resolvedExecutable,
		args,
		env: envVars,
		firefoxUserPrefs: firefox_user_prefs,
		headless,
		...passthrough,
	};
	// Only include proxy if it's set (Playwright validates this)
	// https://github.com/coryking/camoufox/commit/1336e8e509e8c12a896a09d9ee51f131f739f106
	// Thanks @coryking
	if (launchProxy != null) {
		result.proxy = launchProxy;
	}

	return result;
}
