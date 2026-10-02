/**
 * Public API. Mirrors pythonlib/camoufox/__init__.py (Camoufox, NewBrowser,
 * NewContext, their Async* twins, DefaultAddons, launch_options), plus the
 * package-management and server helpers the TS port has always exported.
 */
export type { DefaultAddon } from "./addons.js";
export { DefaultAddons } from "./addons.js";
export {
	AsyncCamoufox,
	AsyncNewBrowser,
	AsyncNewContext,
} from "./async_api.js";
export {
	generateContextFingerprint,
	getRandomPreset,
	loadPresets,
	Screen,
} from "./fingerprints.js";
export {
	auto,
	type CustomEngineFn,
	channelStream,
	cursory,
	custom,
	type Engine,
	engine,
	type HumanizeSetting,
	notches,
	raw,
	type SeededRng,
	seededRng,
	splitmix64,
} from "./humanize.js";
export {
	attach as attachCustom,
	type CustomEngineContext,
	type Play,
	type PlayRecord,
	type PlayStep,
} from "./humanize-custom.js";
export {
	findInstalledVersion,
	listInstalled,
	printTree,
} from "./multiversion.js";
export {
	CamoufoxFetcher,
	INSTALL_DIR,
	installedVerStr,
	OS_NAME,
	RepoConfig,
} from "./pkgman.js";
export { type LaunchServerOptions, launchServer } from "./server.js";
export {
	Camoufox,
	type Headless,
	NewBrowser,
	type NewBrowserOptions,
	NewContext,
	type NewContextOptions,
} from "./sync_api.js";
export {
	type LaunchOptions,
	launchOptions,
	launchOptions as launch_options,
} from "./utils.js";
export { VirtualDisplay } from "./virtdisplay.js";
export { FallbackWarning, LeakWarning } from "./warnings.js";
