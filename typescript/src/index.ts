export type { DefaultAddon } from "./addons.js";
export { DefaultAddons } from "./addons.js";
export {
	generateContextFingerprint,
	getRandomPreset,
	loadPresets,
	type Preset,
	SUPPORTED_OS,
	type SupportedOS,
} from "./fingerprints.js";
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
	NewBrowser,
	type NewBrowserOptions,
	NewContext,
	type NewContextOptions,
} from "./sync_api.js";
export { type LaunchOptions, launchOptions } from "./utils.js";
export { VirtualDisplay } from "./virtdisplay.js";
