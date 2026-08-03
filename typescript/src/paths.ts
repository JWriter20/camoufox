/**
 * Platform constants and install-directory paths.
 *
 * These are the leaves of the module graph: pkgman.ts and multiversion.ts are
 * mutually dependent (as their Python twins are, via function-local imports),
 * and both need these values *at module-evaluation time*. Keeping them here
 * means the cycle only ever involves function bodies, which ESM resolves
 * cleanly. Everything is re-exported from pkgman.ts, which stays the public
 * entry point for them.
 */
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { UnsupportedOS } from "./exceptions.js";

export const ARCH_MAP: Record<string, string> = {
	x64: "x86_64",
	amd64: "x86_64",
	x86: "x86_64",
	ia32: "i686",
	i686: "i686",
	i386: "i686",
	arm64: "arm64",
	aarch64: "arm64",
	arm: "arm64",
};

export const OS_MAP: Record<string, "mac" | "win" | "lin"> = {
	darwin: "mac",
	linux: "lin",
	win32: "win",
};

if (!(process.platform in OS_MAP)) {
	throw new UnsupportedOS(`OS ${process.platform} is not supported`);
}

export const OS_NAME: "mac" | "win" | "lin" = OS_MAP[process.platform];

const currentDir =
	import.meta.dirname ?? path.dirname(fileURLToPath(import.meta.url));

/**
 * platformdirs' user_cache_dir(appName), reimplemented so the TS and Python
 * launchers share one install directory. Hardcoding ~/.cache would diverge on
 * hosts that set XDG_CACHE_HOME, and on macOS/Windows entirely.
 */
export function userCacheDir(appName: string): string {
	if (OS_NAME === "win") {
		const localAppData = process.env.LOCALAPPDATA;
		const base =
			localAppData && path.isAbsolute(localAppData)
				? localAppData
				: path.join(os.homedir(), "AppData", "Local");
		return path.join(base, appName, appName, "Cache");
	}
	if (OS_NAME === "mac") {
		return path.join(os.homedir(), "Library", "Caches", appName);
	}
	const xdg = process.env.XDG_CACHE_HOME;
	const base =
		xdg && path.isAbsolute(xdg) ? xdg : path.join(os.homedir(), ".cache");
	return path.join(base, appName);
}

export const INSTALL_DIR: string = userCacheDir("camoufox");

/** Directory holding the bundled data-files (presets, fonts, territoryInfo...). */
export const LOCAL_DATA: string = path.join(currentDir, "data-files");

export const OS_ARCH_MATRIX: Record<string, string[]> = {
	win: ["x86_64", "i686"],
	mac: ["x86_64", "arm64"],
	lin: ["x86_64", "arm64", "i686"],
};

export const LAUNCH_FILE: Record<string, string> = {
	win: "camoufox.exe",
	mac: "../MacOS/camoufox",
	lin: "camoufox-bin",
};

const COLORS: Record<string, string> = {
	red: "\x1b[31m",
	green: "\x1b[32m",
	yellow: "\x1b[33m",
	blue: "\x1b[34m",
	cyan: "\x1b[36m",
	bright_black: "\x1b[90m",
};

/**
 * Print a styled message. The Python twin uses `rich`; keeping the same helper
 * name means the ported call sites read identically.
 */
export function rprint(msg: string, fg?: string, nl: boolean = true): void {
	const useColor = process.stdout.isTTY && !process.env.NO_COLOR;
	const prefix = useColor ? `\x1b[1m${(fg && COLORS[fg]) || ""}` : "";
	const suffix = useColor ? "\x1b[0m" : "";
	process.stdout.write(`${prefix}${msg}${suffix}${nl ? "\n" : ""}`);
}
