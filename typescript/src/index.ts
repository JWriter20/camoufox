export type { DefaultAddon } from "./addons.js";
export { DefaultAddons } from "./addons.js";
// Safe to export eagerly: `captcha` resolves CaptchaKraken lazily, inside the
// call, so importing camoufox never requires the optional package to be present.
export {
	type CaptchaConfig,
	CaptchaCredentialsError,
	type CaptchaOption,
	CaptchaSolverUnavailable,
	clientTag,
	HOSTED_BASE_URL,
	resolveConfig,
	type SolveResult,
	solveCaptcha,
	watchCaptcha,
	verifyCredentials,
} from "./captcha.js";
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
