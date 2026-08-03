/**
 * Default Firefox addon download/extraction.
 *
 * TypeScript twin of python/src/addons.py.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { InvalidAddonPath } from "./exceptions.js";
import { INSTALL_DIR, unzip, webdl } from "./pkgman.js";
import { getAsBooleanFromENV } from "./utils.js";

/**
 * Default addons to be downloaded.
 */
export const DefaultAddons = {
	UBO: "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi",
} as const;

export type DefaultAddon = keyof typeof DefaultAddons;

// Addons are stored in a shared folder, not per-browser version
export const ADDONS_DIR: string = path.join(INSTALL_DIR, "addons");

/**
 * Confirms that the addon paths are valid.
 */
export function confirmPaths(paths: string[]): void {
	for (const addonPath of paths) {
		if (!fs.existsSync(addonPath) || !fs.statSync(addonPath).isDirectory()) {
			throw new InvalidAddonPath(addonPath);
		}
		if (!fs.existsSync(path.join(addonPath, "manifest.json"))) {
			throw new InvalidAddonPath(
				"manifest.json is missing. Addon path must be a path to an extracted addon.",
			);
		}
	}
}

/**
 * Adds default addons, minus any specified in excludeList, to addonsList.
 */
export async function addDefaultAddons(
	addonsList: string[],
	excludeList: DefaultAddon[] = [],
): Promise<void> {
	const addons: Record<string, string> = {};
	for (const [name, url] of Object.entries(DefaultAddons)) {
		if (!excludeList.includes(name as DefaultAddon)) {
			addons[name] = url;
		}
	}
	await maybeDownloadAddons(addons, addonsList);
}

/**
 * Downloads and extracts an addon from a given URL to a specified path.
 */
export async function downloadAndExtract(
	url: string,
	extractPath: string,
	name: string,
): Promise<void> {
	const buffer = await webdl(url, `Downloading addon (${name})`, false);
	unzip(buffer, extractPath, `Extracting addon (${name})`, false);
}

/**
 * Returns a path to the addon in the shared addons folder.
 */
export function getAddonPath(addonName: string): string {
	return path.join(ADDONS_DIR, addonName);
}

/**
 * Downloads and extracts addons from a given map into the given list.
 * Skips downloading if the addon is already downloaded.
 */
export async function maybeDownloadAddons(
	addons: Record<string, string>,
	addonsList?: string[],
): Promise<void> {
	if (getAsBooleanFromENV("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", false)) {
		console.log(
			"Skipping addon download due to PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD set!",
		);
		return;
	}

	for (const [addonName, url] of Object.entries(addons)) {
		const addonPath = getAddonPath(addonName);

		if (fs.existsSync(addonPath)) {
			addonsList?.push(addonPath);
			continue;
		}

		try {
			fs.mkdirSync(addonPath, { recursive: true });
			await downloadAndExtract(url, addonPath, addonName);
			addonsList?.push(addonPath);
		} catch (e) {
			console.error(`Failed to download and extract ${addonName}: ${e}`);
		}
	}
}
