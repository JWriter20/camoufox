/**
 * Launch-option assembly: turns Camoufox's high-level options into the
 * Playwright Firefox launch options plus the CAMOU_CONFIG environment.
 *
 * TypeScript twin of python/src/utils.py.
 */
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import type {
	Fingerprint,
	FingerprintGeneratorOptions,
} from "fingerprint-generator";
import type { LaunchOptions as PlaywrightLaunchOptions } from "playwright-core";
import { UAParser } from "ua-parser-js";
import { addDefaultAddons, confirmPaths, type DefaultAddon } from "./addons.js";
import { hasDisplay, largestDisplay } from "./display.js";
import {
	FileNotFoundError,
	InvalidOS,
	InvalidPropertyType,
	NonFirefoxFingerprint,
} from "./exceptions.js";
import {
	clampScreenToDisplay,
	clampWindowDimensions,
	clampWindowPosition,
	fixNavigatorArch,
	fixScreenNoTaskbar,
	fromBrowserforge,
	fromPreset,
	generateFingerprint,
	generateRandomFontSubset,
	generateRandomVoiceSubset,
	getRandomPreset,
	type Preset,
	SUPPORTED_OS,
	type SupportedOS,
	setMediaDevicesDefaults,
} from "./fingerprints.js";
import {
	type ProxyConfig,
	ProxyHelper,
	publicIP,
	validIPv4,
	validIPv6,
} from "./ip.js";
import { geoipAllowed, getGeolocation, handleLocales } from "./locale.js";
import {
	ensureBrowserProfileDir,
	ensureCamoufoxInstalled,
	getPath,
	INSTALL_DIR,
	installedVerStr,
	LOCAL_DATA,
	launchPath,
	OS_NAME,
} from "./pkgman.js";
import type { VirtualDisplay } from "./virtdisplay.js";
import { LeakWarning } from "./warnings.js";
import { sampleWebGL, type TargetOS } from "./webgl/sample.js";

type Screen = NonNullable<FingerprintGeneratorOptions["screen"]>;

export type ListOrString = string | string[];

// Camoufox preferences to cache previous pages and requests
const CACHE_PREFS = {
	"browser.sessionhistory.max_entries": 10,
	"browser.sessionhistory.max_total_viewers": -1,
	"browser.cache.memory.enable": true,
	"browser.cache.disk_cache_ssl": true,
	"browser.cache.disk.smart_size.enabled": true,
};

/**
 * Generates a runtime fontconfig that resolves bundled font paths absolutely.
 *
 * The bundled fonts.conf uses prefix="cwd" relative paths which break when
 * Playwright's working directory differs from the browser install directory.
 * Writes a patched copy to the user cache dir (deterministic, only regenerated
 * when the content changes). This must not live inside the versioned browser
 * bundle: the bundle is commonly baked into an image as root and run as a
 * non-root user, so it is read-only at launch time.
 */
function generateFontconfig(fontconfigPath: string, fontsDir: string): string {
	const src = path.join(fontconfigPath, "fonts.conf");
	let content = fs.readFileSync(src, "utf-8");
	content = content.replace(
		'<dir prefix="cwd">fonts</dir>',
		`<dir>${fontsDir}</dir>`,
	);

	const cacheDir = path.join(INSTALL_DIR, "fontconfig");
	fs.mkdirSync(cacheDir, { recursive: true });

	const hash = createHash("sha256").update(content).digest("hex").slice(0, 12);
	const runtimeConf = path.join(cacheDir, `fonts-${hash}.conf`);
	if (!fs.existsSync(runtimeConf)) {
		fs.writeFileSync(runtimeConf, content);
	}
	return runtimeConf;
}

export type EnvVars = Record<string, string | number | boolean>;

/**
 * Gets the environment variables for Camoufox: the chunked CAMOU_CONFIG, plus
 * the Linux fontconfig pointer.
 */
export function getEnvVars(
	configMap: Record<string, any>,
	userAgentOS: string,
	executablePath?: string,
): EnvVars {
	const envVars: EnvVars = {};

	const configStr = JSON.stringify(configMap);

	// Split the config into chunks
	const chunkSize = OS_NAME === "win" ? 2047 : 32767;
	for (let i = 0; i < configStr.length; i += chunkSize) {
		const chunk = configStr.slice(i, i + chunkSize);
		envVars[`CAMOU_CONFIG_${Math.floor(i / chunkSize) + 1}`] = chunk;
	}

	if (OS_NAME === "lin") {
		// https://github.com/coryking/camoufox/commit/f21eeb2850a74cc104fb57e17e0a2fa27b7a2a28
		// Thanks @coryking
		// userAgentOS is 'lin' | 'mac' | 'win' but the fontconfig directories are
		// 'linux' | 'macos' | 'windows'.
		const directoryMap: Record<string, string> = {
			lin: "linux",
			mac: "macos",
			win: "windows",
		};
		const osDir = directoryMap[userAgentOS] ?? userAgentOS;

		// v150+ uses "fontconfig/" (matching the Go launcher); older bundles
		// shipped "fontconfigs/".
		let fontconfigPath = getPath(
			path.join("fontconfig", osDir),
			executablePath,
		);
		if (!fs.existsSync(path.join(fontconfigPath, "fonts.conf"))) {
			fontconfigPath = getPath(path.join("fontconfigs", osDir), executablePath);
		}

		if (!fs.existsSync(path.join(fontconfigPath, "fonts.conf"))) {
			throw new FileNotFoundError(
				`fonts.conf not found in ${fontconfigPath}! Something ain't right with your camoufox bundle.`,
			);
		}

		const fontsDir = getPath("fonts", executablePath);
		envVars.FONTCONFIG_FILE = generateFontconfig(fontconfigPath, fontsDir);
	}

	return envVars;
}

export function getAsBooleanFromENV(
	name: string,
	defaultValue?: boolean,
): boolean {
	const value = process.env[name];
	if (value === "false" || value === "0") return false;
	if (value) return true;
	return Boolean(defaultValue);
}

interface PropertyEntry {
	property: string;
	type: string;
}

/**
 * Loads the properties.json file shipped alongside the browser.
 */
function loadProperties(executablePath?: string): Record<string, string> {
	// The Python twin resolves this as `executable_path.parent /
	// properties.json`; getPath() does the same but is also mac-bundle aware
	// (resources live in ../Resources/, not beside the MacOS/ binary).
	const propFile = getPath("properties.json", executablePath);
	const propDict: PropertyEntry[] = JSON.parse(
		fs.readFileSync(propFile, "utf-8"),
	);
	return propDict.reduce<Record<string, string>>((acc, prop) => {
		acc[prop.property] = prop.type;
		return acc;
	}, {});
}

/**
 * Validates the config map against the browser's properties.json.
 */
export function validateConfig(
	configMap: Record<string, any>,
	executablePath?: string,
): void {
	const propertyTypes = loadProperties(executablePath);

	for (const [key, value] of Object.entries(configMap)) {
		const expectedType = propertyTypes[key];
		if (!expectedType) {
			// Property not supported by this browser version; skip silently.
			console.log(`Skipping unknown patch ${key} : ${value}`);
			continue;
		}

		if (!validateType(value, expectedType)) {
			throw new InvalidPropertyType(
				`Invalid type for property ${key}. Expected ${expectedType}, got ${typeof value}`,
			);
		}
	}
}

export function validateType(value: any, expectedType: string): boolean {
	switch (expectedType) {
		case "str":
			return typeof value === "string";
		case "int":
			return Number.isInteger(value);
		case "uint":
			return Number.isInteger(value) && value >= 0;
		case "double":
			return typeof value === "number";
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

/**
 * Gets the OS from the config if the user agent is set, otherwise returns the
 * OS of the current system.
 */
export function getTargetOS(config: Record<string, any>): TargetOS {
	if (config["navigator.userAgent"]) {
		return determineUAOS(config["navigator.userAgent"]);
	}
	return OS_NAME;
}

/**
 * Determines the OS from the user agent string.
 */
export function determineUAOS(userAgent: string): TargetOS {
	// An unrecognised UA must not throw: Python's ua_parser answers with the
	// literal "Other" rather than nothing, so its `raise` never fires and the
	// tail `return "lin"` is what actually runs. ua-parser-js returns undefined
	// in the same situation, so normalise it to keep the launchers in step --
	// otherwise `config: {"navigator.userAgent": ...}` with an unparseable UA
	// launches under Python and hard-errors here.
	const parsedUA = new UAParser(userAgent).getOS().name || "Other";
	// ua-parser-js reports "macOS"; the Python ua_parser reports "Mac OS X".
	if (parsedUA.startsWith("Mac") || parsedUA.startsWith("macOS")) return "mac";
	if (parsedUA.startsWith("Windows")) return "win";
	return "lin";
}

/**
 * Determines a sane viewport size for Camoufox when running headful.
 *
 * Bounds are CSS pixels, the unit Firefox lays its windows out in -- see
 * display.ts for why that differs from the monitor's physical size.
 */
export function getScreenCons(headless?: boolean): Screen | undefined {
	if (headless === false) {
		return undefined; // Skip if headless
	}
	const display = largestDisplay();
	if (display === null) {
		return undefined; // Skip if the display can't be probed
	}
	return { maxWidth: display.width, maxHeight: display.height };
}

/**
 * Updates the fonts for the target OS from the bundled full list.
 */
export function updateFonts(
	config: Record<string, any>,
	targetOs: string,
): void {
	const fontsPath = path.join(LOCAL_DATA, "fonts.json");
	const fonts: string[] = JSON.parse(fs.readFileSync(fontsPath, "utf-8"))[
		targetOs
	];

	// Merge with existing fonts (np.unique sorts, so match that)
	if (config.fonts) {
		config.fonts = [...new Set([...fonts, ...config.fonts])].sort();
	} else {
		config.fonts = fonts;
	}
}

/**
 * Asserts that the passed BrowserForge fingerprint is a valid Firefox
 * fingerprint, and warns that passing one is not recommended.
 */
export function checkCustomFingerprint(fingerprint: Fingerprint): void {
	const browserName =
		new UAParser(fingerprint.navigator.userAgent).getBrowser().name ||
		"Non-Firefox";
	if (browserName !== "Firefox") {
		throw new NonFirefoxFingerprint(
			`"${browserName}" fingerprints are not supported in Camoufox. ` +
				"Using fingerprints from a browser other than Firefox WILL lead to detection. " +
				"If this is intentional, pass `i_know_what_im_doing: true`.",
		);
	}

	LeakWarning.warn("custom_fingerprint", false);
}

/**
 * Checks if the target OS is valid.
 */
export function checkValidOS(os: ListOrString): void {
	if (typeof os !== "string") {
		for (const osName of os) checkValidOS(osName);
		return;
	}
	// Assert that the OS is lowercase
	if (os !== os.toLowerCase()) {
		throw new InvalidOS(`OS values must be lowercase: '${os}'`);
	}
	// Assert that the OS is supported by Camoufox
	if (!(SUPPORTED_OS as readonly string[]).includes(os)) {
		throw new InvalidOS(`Camoufox does not support the OS: '${os}'`);
	}
}

/**
 * Merges new keys/values from source into target, given that the key does not
 * already exist in target.
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
 * Sets a key/value into target, given that the key does not already exist.
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
	return properties.some((prop) => {
		// With a "." or ":" suffix, check if the domain prefixes any config key
		if (prop.endsWith(".") || prop.endsWith(":")) {
			return Object.keys(config).some((key) => key.startsWith(prop));
		}
		return prop in config;
	});
}

/**
 * Warns when the caller is manually setting properties Camoufox already sets.
 */
export function warnManualConfig(config: Record<string, any>): void {
	// Manual locale setting
	if (
		isDomainSet(
			config,
			"navigator.language",
			"navigator.languages",
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
	"document.body.clientWidth",
	"document.body.clientHeight",
];

/**
 * Whether the CAMOU_CONFIG in a set of launch options spoofs any window
 * dimension. The config is chunked across CAMOU_CONFIG_<n> env vars, so
 * reassemble it in index order before looking.
 */
export function spoofsWindowDimensions(
	fromOptions: Record<string, any>,
): boolean {
	const env = fromOptions.env ?? {};
	const chunks: Array<[number, string]> = Object.entries(env)
		.filter(([k]) => k.startsWith("CAMOU_CONFIG_"))
		.map(([k, v]) => [
			Number.parseInt(k.slice(k.lastIndexOf("_") + 1), 10),
			String(v),
		]);
	if (!chunks.length) return false;
	chunks.sort((a, b) => a[0] - b[0]);
	const blob = chunks.map(([, v]) => v).join("");
	return WINDOW_DIM_KEYS.some((key) => blob.includes(key));
}

/**
 * Default newPage()/newContext() to `noViewport: true`.
 *
 * Playwright applies a 1280x720 viewport by default, which makes Juggler ask
 * the content window to become 1280x720 (TargetRegistry.updateViewportSize).
 * When Camoufox is pinning the window to a spoofed size, that request can never
 * be satisfied, and awaitViewportDimensions has no timeout -- so the second
 * newPage() hangs forever (daijro/camoufox#666).
 *
 * Without a viewport, Juggler measures the window instead of resizing it, so
 * the handshake resolves immediately and the page reports the spoofed
 * dimensions exactly. An explicit viewport from the caller always wins.
 *
 * Note the API spelling: Playwright-Python takes `no_viewport=True`, but the
 * JS API expresses the same thing as `viewport: null`. A caller-supplied
 * `noViewport` is accepted (it is what the Python docs teach) and translated.
 */
export function attachNoViewportDefault<T>(target: T): T {
	for (const name of ["newPage", "newContext"] as const) {
		const original = (target as any)[name];
		if (typeof original !== "function") continue;

		(target as any)[name] = (options?: Record<string, any>, ...rest: any[]) => {
			const opts = { ...options };
			return original.call(target, applyNoViewport(opts), ...rest);
		};
	}
	return target;
}

/**
 * Normalise a context-options object onto the JS API's `viewport: null`,
 * defaulting to it when the caller expressed no preference.
 */
export function applyNoViewport(
	opts: Record<string, any>,
): Record<string, any> {
	if ("noViewport" in opts) {
		const noViewport = opts.noViewport;
		delete opts.noViewport;
		if (noViewport && !("viewport" in opts)) opts.viewport = null;
		return opts;
	}
	if (!("viewport" in opts)) opts.viewport = null;
	return opts;
}

/**
 * Attaches the virtual display to the browser's cleanup.
 */
export function attachVirtualDisplay<T>(
	browser: T,
	virtualDisplay?: VirtualDisplay | null,
): T {
	if (!virtualDisplay) return browser; // Skip if no virtual display

	const target = browser as any;
	const originalClose = target.close.bind(target);

	target.close = async (...args: any[]) => {
		try {
			return await originalClose(...args);
		} finally {
			virtualDisplay.kill();
		}
	};

	// close() never runs on an unexpected disconnect, so wire teardown directly;
	// kill() is idempotent, so a later close() is harmless.
	target.on?.("disconnected", () => virtualDisplay.kill());

	target._virtualDisplay = virtualDisplay;

	return browser;
}

export interface LaunchOptions {
	/** Camoufox properties to use.
	 * (read https://github.com/daijro/camoufox/blob/main/README.md) */
	config?: Record<string, any>;

	/** Operating system to use for the fingerprint generation.
	 * Can be "windows", "macos", "linux", or a list to randomly choose from.
	 * Default: ["windows", "macos", "linux"] */
	os?: SupportedOS | SupportedOS[];

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
	 * address. Pass the target IP address to use, or `true` to find it
	 * automatically. */
	geoip?: string | boolean;

	/** Name of the GeoIP database to use (e.g. "MaxMind GeoLite2"). */
	geoip_db?: string;

	/** Humanize the cursor movement. Takes either `true`, or the MAX duration in
	 * seconds of the cursor movement. The cursor typically takes up to 1.5
	 * seconds to move across the window. */
	humanize?: boolean | number;

	/** Locale(s) to use. The first listed locale is used for the Intl API. */
	locale?: string | string[];

	/** List of Firefox addons to use. */
	addons?: string[];

	/** Fonts to load into Camoufox (in addition to the default fonts for the
	 * target `os`). Takes font family names installed on the system. */
	fonts?: string[];

	/** If enabled, OS-specific system fonts will not be passed to Camoufox. */
	custom_fonts_only?: boolean;

	/** Default addons to exclude. */
	exclude_addons?: DefaultAddon[];

	/** Constrains the screen dimensions of the generated fingerprint. */
	screen?: Screen;

	/** Set a fixed window size instead of generating a random one. */
	window?: [number, number];

	/** Use a custom BrowserForge fingerprint. If not provided, a random
	 * fingerprint is generated from the `os` & `screen` constraints. */
	fingerprint?: Fingerprint;

	/** Opt into using real fingerprint presets instead of BrowserForge. Pass
	 * `true` for a random bundled preset, or a preset object directly. By
	 * default BrowserForge is used for infinite unique fingerprints. */
	fingerprint_preset?: boolean | Preset;

	/** Firefox version to use. Defaults to the current Camoufox version.
	 * To prevent leaks, only use this for special cases. */
	ff_version?: number;

	/** Whether to run the browser in headless mode. Defaults to `false`.
	 * On Linux, `"virtual"` (passed to Camoufox/NewBrowser) uses Xvfb. */
	headless?: boolean;

	/** Whether to enable running scripts in the main world.
	 * To use it, prepend "mw:" to the script: page.evaluate("mw:" + script). */
	main_world_eval?: boolean;

	/** Whether to let addons open new tabs. Defaults to false. */
	allow_addon_new_tab?: boolean;

	/** Custom Camoufox browser executable path. */
	executable_path?: string;

	/** Select a specific installed browser version. Can be a repo/build like
	 * "official/beta.20", a build alone like "beta.20", or a full version like
	 * "134.0.2-beta.20". Defaults to the active version. */
	browser?: string;

	/** Firefox user preferences to set. */
	firefox_user_prefs?: Record<string, any>;

	/** Proxy to use for the browser. Note: if `geoip` is true, a request is sent
	 * through this proxy to find the target IP. */
	proxy?: ProxyConfig;

	/** Cache previous pages, requests, etc (uses more memory). */
	enable_cache?: boolean;

	/** Arguments to pass to the browser. */
	args?: string[];

	/** Environment variables to set. */
	env?: EnvVars;

	/** Suppress leak warnings for options you are deliberately overriding. */
	i_know_what_im_doing?: boolean;

	/** Prints the config being sent to Camoufox. */
	debug?: boolean;

	/** Virtual display number, e.g. ":99". Handled by Camoufox & NewBrowser. */
	virtual_display?: string;

	/** Additional Firefox launch options passed straight through to Playwright. */
	[key: string]: any;
}

/**
 * Builds the Playwright Firefox launch options for Camoufox.
 *
 * Accepts all Playwright Firefox launch options, along with the Camoufox ones
 * documented on {@link LaunchOptions}.
 */
export async function launchOptions({
	config,
	os,
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
	...launch_options
}: LaunchOptions = {}): Promise<Record<string, any>> {
	ensureBrowserProfileDir(env);

	// Build the config
	config ??= {};

	// Set default values for optional arguments
	headless ??= false;
	addons ??= [];
	args ??= [];
	firefox_user_prefs ??= {};
	custom_fonts_only ??= false;
	i_know_what_im_doing ??= false;

	// Keep per-launch overrides isolated from the process environment and from
	// mappings supplied by callers. In particular, DISPLAY must not outlive the
	// virtual display that owns it.
	env = { ...(env ?? (process.env as EnvVars)) };

	if (typeof executable_path === "string") {
		executable_path = path.resolve(executable_path);
	}

	// Handle virtual display
	if (virtual_display) {
		env.DISPLAY = virtual_display;
		// The virtual display uses Xvfb (X11). If the host session forces Wayland
		// via env vars, GTK/Firefox may try Wayland and ignore DISPLAY entirely,
		// putting the "virtual" browser on the physical screen.
		env.GDK_BACKEND = "x11";
		delete env.WAYLAND_DISPLAY;
		env.MOZ_ENABLE_WAYLAND = "0";
	}

	// Warn the user for manual config settings
	if (!i_know_what_im_doing) {
		warnManualConfig(config);
	}

	// Snapshot which domains the USER set before fingerprint generation fills in
	// the rest. The post-generation BrowserForge corrections below must only
	// touch generated values, never override what the caller passed.
	const userSetNavigator = isDomainSet(config, "navigator.");
	const userSetScreenWindow = isDomainSet(config, "screen.", "window.");
	const userSetMediaDevices = isDomainSet(config, "mediaDevices:");

	// Assert the target OS is valid
	if (os) {
		checkValidOS(os);
	} else if (webgl_config) {
		// webgl_config requires OS to be set
		throw new Error("OS must be set when using webgl_config");
	}

	// Resolve the browser install up front. camoufoxPath() is synchronous and
	// cannot await a download, so the auto-install the Python twin performs
	// inline happens here instead -- before any getPath()/launchPath() call.
	if (!executable_path && !browser) {
		await ensureCamoufoxInstalled();
	}
	const executableDir =
		typeof executable_path === "string"
			? path.dirname(executable_path)
			: undefined;

	// Add the default addons
	await addDefaultAddons(addons, exclude_addons);

	// Confirm all addon paths are valid
	if (addons.length) {
		confirmPaths(addons);
		config.addons = addons;
	}

	// Get the Firefox version
	let ffVersionStr: string;
	if (ff_version) {
		ffVersionStr = String(ff_version);
		LeakWarning.warn("ff_version", i_know_what_im_doing);
	} else {
		ffVersionStr = installedVerStr(executableDir).split(".", 1)[0];
	}

	// Generate a fingerprint
	let usedPreset = false;
	if (fingerprint) {
		// The caller passed a custom BrowserForge fingerprint
		if (!i_know_what_im_doing) {
			checkCustomFingerprint(fingerprint);
		}
	} else if (fingerprint_preset) {
		// The caller opted into real fingerprint presets
		const preset =
			typeof fingerprint_preset === "object"
				? fingerprint_preset
				: getRandomPreset(os, ffVersionStr);
		if (preset) {
			mergeInto(config, fromPreset(preset, ffVersionStr));
			usedPreset = true;
		}
	}

	// Bound the geometry to the real display. BrowserForge only honours this
	// when its pool has a match, so it is re-applied after generation as well.
	const screenCons = screen ?? getScreenCons(headless || hasDisplay(env));

	if (!usedPreset && !fingerprint) {
		// Default: BrowserForge synthetic generation (infinite unique fingerprints)
		fingerprint = generateFingerprint(window, {
			screen: screenCons,
			operatingSystems: os
				? ((Array.isArray(os) ? os : [os]) as any)
				: undefined,
		});
	}

	if (!usedPreset && fingerprint) {
		// Inject the BrowserForge fingerprint into the config
		mergeInto(config, fromBrowserforge(fingerprint, ffVersionStr));
	}

	const targetOS = getTargetOS(config);

	// Correct BrowserForge fingerprint inconsistencies that leak as headless /
	// impossible-geometry tells, unless the caller is driving these themselves.
	if (!userSetNavigator) {
		fixNavigatorArch(config, targetOS);
	}
	if (!userSetScreenWindow) {
		// Headful on a real monitor only: this bound exists so the window fits
		// the screen it is drawn on. Headless has no window to overflow, and
		// headless="virtual" reaches here as headless=false (see sync_api) with a
		// 1x1 Xvfb (virtdisplay.ts) that is not a real screen.
		if (headless === false && !virtual_display && screenCons) {
			clampScreenToDisplay(config, screenCons.maxWidth, screenCons.maxHeight);
		}
		fixScreenNoTaskbar(config, targetOS);
		clampWindowDimensions(config);
		clampWindowPosition(config);
	}

	// Deliberately NOT setting window.history.length. It used to be pinned to a
	// random 1-5 because browser.sessionhistory.max_entries=0 left the real
	// session history empty, so the honest value was 0 -- an impossible number,
	// since the HTML spec guarantees a browsing context always keeps its current
	// entry. settings/camoufox.cfg now runs Firefox's stock max_entries, so the
	// real value starts at 1 and grows with each navigation.
	//
	// Pinning it on top of that is strictly worse than leaving it alone: the
	// value would no longer move across navigations, and a fresh tab would claim
	// a depth of, say, 4 while history.back() -- which reads the real session
	// history -- does nothing. Any page can check that pair. The property stays
	// in properties.json for callers who want to override it by hand.

	// Update fonts list
	if (fonts) {
		config.fonts = fonts;
	}

	if (custom_fonts_only) {
		firefox_user_prefs["gfx.bundled-fonts.activate"] = 0;
		if (fonts) {
			LeakWarning.warn("custom_fonts_only");
		} else {
			throw new Error(
				"No custom fonts were passed, but `custom_fonts_only` is enabled.",
			);
		}
	} else if (!config.fonts?.length) {
		// Generate a unique random font subset from the OS font list
		const osName =
			{ win: "windows", mac: "macos", lin: "linux" }[targetOS] ?? "macos";
		try {
			config.fonts = generateRandomFontSubset(osName);
		} catch {
			updateFonts(config, targetOS);
		}
	}

	// Generate a unique random voice subset
	if (!("voices" in config)) {
		const osNameV =
			{ win: "windows", mac: "macos", lin: "linux" }[targetOS] ?? "macos";
		try {
			config.voices = generateRandomVoiceSubset(osNameV);
		} catch {
			// Voice generation is best-effort; the browser falls back to its own.
		}
	}

	// Default mediaDevices to one mic + one camera so headless contexts don't
	// expose an empty enumerateDevices() list (a headless tell).
	if (!userSetMediaDevices) {
		setMediaDevicesDefaults(config);
	}

	// Set random seeds for fingerprint noise (per launch)
	setInto(config, "fonts:spacing_seed", randomSeed());
	setInto(config, "audio:seed", randomSeed());
	setInto(config, "canvas:seed", randomSeed());

	// Set geolocation
	if (geoip) {
		geoipAllowed(); // Assert that geoip is allowed

		if (geoip === true) {
			// Find the user's IP address
			geoip = proxy
				? await publicIP(ProxyHelper.asString(proxy))
				: await publicIP();
		}

		// Spoof WebRTC if not blocked
		if (!block_webrtc) {
			if (validIPv4(geoip)) {
				setInto(config, "webrtc:ipv4", geoip);
				firefox_user_prefs["network.dns.disableIPv6"] = true;
			} else if (validIPv6(geoip)) {
				setInto(config, "webrtc:ipv6", geoip);
			}
		}

		const geolocation = await getGeolocation(geoip as string, geoip_db);
		for (const [key, value] of Object.entries(geolocation.asConfig())) {
			// Manual locale/timezone config is authoritative; geoip only fills
			// the gaps. Coordinates always come from the resolved IP.
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
	} else if (
		// Raise a warning when a proxy is being used without spoofing geolocation.
		// This is a very bad idea; it cannot be silenced with i_know_what_im_doing.
		proxy &&
		!proxy.server?.includes("localhost") &&
		!isDomainSet(config, "geolocation")
	) {
		LeakWarning.warn("proxy_without_geoip");
	}

	// Set locale
	if (locale) {
		await handleLocales(locale, config);
	}

	// Pass the humanize option
	if (humanize) {
		setInto(config, "humanize", true);
		// MaskConfig expects maxTime to be a JSON number.
		if (typeof humanize === "number") {
			setInto(config, "humanize:maxTime", humanize);
		}
	}

	// Enable the main world context creation
	if (main_world_eval) {
		setInto(config, "allowMainWorld", true);
	}

	// Allow addons to open new tabs
	if (allow_addon_new_tab) {
		setInto(config, "allowAddonNewtab", true);
	}

	// Set Firefox user preferences
	if (block_images) {
		LeakWarning.warn("block_images", i_know_what_im_doing);
		firefox_user_prefs["permissions.default.image"] = 2;
	}
	if (block_webrtc) {
		firefox_user_prefs["media.peerconnection.enabled"] = false;
	}
	if (disable_coop) {
		LeakWarning.warn("disable_coop", i_know_what_im_doing);
		firefox_user_prefs["browser.tabs.remote.useCrossOriginOpenerPolicy"] =
			false;
	}

	// Allow the allow_webgl parameter for backwards compatibility
	const allowWebgl = launch_options.allow_webgl;
	delete launch_options.allow_webgl;
	if (block_webgl || allowWebgl === false) {
		firefox_user_prefs["webgl.disabled"] = true;
		LeakWarning.warn("block_webgl", i_know_what_im_doing);
	} else {
		let webglFp: Record<string, any>;
		if (webgl_config) {
			// The caller provided a specific WebGL vendor/renderer pair
			webglFp = await sampleWebGL(targetOS, ...webgl_config);
		} else if (config["webGl:vendor"] && config["webGl:renderer"]) {
			// A preset already set vendor/renderer -- sample matching WebGL params
			try {
				webglFp = await sampleWebGL(
					targetOS,
					config["webGl:vendor"],
					config["webGl:renderer"],
				);
			} catch {
				// The preset's GPU is not in the WebGL catalogue (roughly 10% of the
				// bundled presets), so there are no parameters recorded for it.
				// Leaving the vendor/renderer strings in place and merging another
				// GPU's parameters around them is worse than the substitution -- the
				// reported extensions, limits and shader precisions would contradict
				// the name. Drop the pair so the sampled one below is used for the
				// whole WebGL identity.
				LeakWarning.warn("preset_webgl_unavailable", i_know_what_im_doing);
				delete config["webGl:vendor"];
				delete config["webGl:renderer"];
				webglFp = await sampleWebGL(targetOS);
			}
		} else {
			webglFp = await sampleWebGL(targetOS);
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

	// Cache previous pages, requests, etc (uses more memory)
	if (enable_cache) {
		mergeInto(firefox_user_prefs, CACHE_PREFS);
	}

	// Print the config if debug is enabled
	if (debug) {
		console.debug("[DEBUG] Config:");
		console.debug(config);
	}

	// Validate the config
	validateConfig(config, executableDir);

	// Prepare environment variables to pass to Camoufox
	const envVars: EnvVars = {
		...getEnvVars(config, targetOS, executableDir),
		...env,
	};

	// Prepare the executable path
	let resolvedExecutable: string;
	if (executable_path) {
		resolvedExecutable = String(executable_path);
	} else if (browser) {
		// Select a specific installed browser version
		const { findInstalledVersion } = await import("./multiversion.js");
		const browserPath = findInstalledVersion(browser);
		if (!browserPath) {
			throw new Error(
				`Browser version '${browser}' not found. Run \`camoufox list\` to see installed versions.`,
			);
		}
		resolvedExecutable = launchPath(browserPath);
	} else {
		resolvedExecutable = launchPath();
	}

	const result: Record<string, any> = {
		executablePath: resolvedExecutable,
		args,
		env: envVars,
		firefoxUserPrefs: firefox_user_prefs,
		headless,
		...launch_options,
	};
	// Only include proxy when it is set (Playwright 1.55+ validates this)
	// https://github.com/coryking/camoufox/commit/1336e8e509e8c12a896a09d9ee51f131f739f106
	// Thanks @coryking
	if (proxy) {
		result.proxy = proxy;
	}

	return result;
}

/** Random seed in [1, 2^32-1]; 0 is a no-op in the C++ noise implementations. */
function randomSeed(): number {
	return Math.floor(Math.random() * 4_294_967_295) + 1;
}

export type { PlaywrightLaunchOptions };
