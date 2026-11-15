/**
 * The display's ICC colour profile, which a page reads through every
 * ICC-tagged image it decodes. The TS twin of display_profile.py; see there
 * for the measurements.
 *
 * Firefox converts tagged images (gfx.color_management.mode 2) to the
 * display's profile: the OS's, or the file gfx.color_management.display_profile
 * names, which every platform reads first. Stock 158.0.1 on win-i9 (a
 * wide-gamut panel profile) decodes sRGB red as 217,54,41; a Linux host with no
 * _ICC_PROFILE keeps built-in sRGB (255,0,0). An identity of another OS
 * carries its display's profile: the caller's (`display_profile`), or one drawn
 * from display-profiles.json for its OS and device-pixel screen.
 */
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as random from "python-random";
import { loadDataFile } from "./paths.js";

export const DISPLAY_PROFILE_PREF = "gfx.color_management.display_profile";

/** ICC bytes or a path, false (the browser's own), or undefined/true (drawn). */
export type DisplayProfileOption =
	| Uint8Array
	| string
	| boolean
	| null
	| undefined;

interface ProfileEntry {
	name?: string;
	w?: number;
	icc: string;
	screens?: number[][];
	source?: string;
}

let tableCache: Record<string, any> | undefined;

/** display-profiles.json: OS key -> recorded profiles. */
export function loadDisplayProfiles(): Record<string, any> {
	tableCache ??= loadDataFile<Record<string, any>>("display-profiles.json");
	return tableCache;
}

/** Seams for tests: the table and where profiles are written. */
export const displayProfileDeps = {
	table: loadDisplayProfiles,
	cacheDirs: (installDir: string): string[] => [
		path.join(installDir, "display-profiles"),
		path.join(os.tmpdir(), "camoufox-display-profiles"),
	],
};

/** `data` if it is an ICC profile (its declared size fits, "acsp" at 36), else throws. */
export function checkIcc(data: Uint8Array): Uint8Array {
	const b = Buffer.from(data.buffer, data.byteOffset, data.byteLength);
	if (
		b.length < 132 ||
		b.toString("latin1", 36, 40) !== "acsp" ||
		b.readUInt32BE(0) > b.length
	)
		throw new Error("display_profile is not an ICC profile");
	return data;
}

/** The identity's screen in device pixels: CSS screen size times devicePixelRatio. */
export function deviceScreen(config: Record<string, any>): number[] | null {
	const w = config["screen.width"];
	const h = config["screen.height"];
	if (!w || !h) return null;
	const dpr = Number(config["window.devicePixelRatio"] || 1);
	// Python's round(): half to even.
	const pyRound = (v: number) => {
		const f = Math.floor(v);
		const d = v - f;
		return d > 0.5 || (d === 0.5 && f % 2 !== 0) ? f + 1 : f;
	};
	return [pyRound(w * dpr), pyRound(h * dpr)];
}

function weightedChoice(r: random.Random, items: ProfileEntry[]): ProfileEntry {
	const w = (item: ProfileEntry) =>
		Object.hasOwn(item, "w") ? Number(item.w) : 1;
	let x = r.random() * items.reduce((a, item) => a + w(item), 0);
	for (const item of items) {
		x -= w(item);
		if (x < 0) return item;
	}
	return items[items.length - 1];
}

/** The recorded profile an identity of `targetOs` with this screen draws, or null (built-in sRGB). */
export function drawDisplayProfile(
	config: Record<string, any>,
	seed: number,
	targetOs: string,
): Uint8Array | null {
	const entries: ProfileEntry[] = displayProfileDeps.table()[targetOs] ?? [];
	const screen = deviceScreen(config);
	const panels = entries.filter(
		(e) =>
			e.screens?.length &&
			screen &&
			e.screens.some((s) => s[0] === screen[0] && s[1] === screen[1]),
	);
	const pool = panels.length
		? panels
		: entries.filter((e) => !e.screens?.length);
	if (!pool.length) return null;
	const chosen = weightedChoice(new random.Random((seed ^ 0xc01c) >>> 0), pool);
	return Buffer.from(chosen.icc, "base64");
}

/** The profile's path in the cache (content-addressed, written once). */
function writeProfile(data: Uint8Array, installDir: string): string {
	const name = `${createHash("sha256").update(data).digest("hex").slice(0, 24)}.icc`;
	for (const base of displayProfileDeps.cacheDirs(installDir)) {
		const file = path.join(base, name);
		try {
			if (
				!fs.existsSync(file) ||
				!Buffer.from(data).equals(fs.readFileSync(file))
			) {
				fs.mkdirSync(base, { recursive: true });
				const tmp = path.join(base, `.${name}.${process.pid}`);
				fs.writeFileSync(tmp, data);
				fs.renameSync(tmp, file);
			}
			return file;
		} catch {
			// next directory
		}
	}
	throw new Error("no writable directory for the display profile");
}

/**
 * Point gfx.color_management.display_profile at the identity's display
 * profile. A caller's own pref wins. Returns the path set, or null.
 */
export function applyDisplayProfile(
	prefs: Record<string, any>,
	userPrefKeys: Set<string>,
	option: DisplayProfileOption,
	config: Record<string, any>,
	seed: number,
	targetOs: string,
	hostOs: string | null,
	installDir: string,
): string | null {
	if (userPrefKeys.has(DISPLAY_PROFILE_PREF) || option === false) return null;
	let data: Uint8Array | null;
	if (option === undefined || option === null || option === true) {
		if (targetOs === hostOs) return null;
		data = drawDisplayProfile(config, seed, targetOs);
		if (!data) return null;
	} else if (typeof option === "string") {
		data = fs.readFileSync(option.replace(/^~(?=$|\/)/, os.homedir()));
	} else {
		data = option;
	}
	const file = writeProfile(checkIcc(data), installDir);
	prefs[DISPLAY_PROFILE_PREF] = file;
	return file;
}
