/**
 * The identity's display ICC profile (display_profile.ts).
 * Twin of python/tests/test_display_profile.py.
 *
 * Stock Firefox converts ICC-tagged images to the display's profile, and a
 * page reads the result off a canvas: stock 158.0.1 on win-i9 decodes the
 * cfparity raster.color sRGB red as 217,54,41, a Linux host without
 * _ICC_PROFILE as 255,0,0. gfx.color_management.display_profile, read before
 * the OS profile on every platform, reproduces stock's values.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { afterAll, afterEach, beforeEach, describe, expect, it } from "vitest";
import {
	HOME,
	isolateLaunch,
	quietly,
	restoreDeps,
	SCRATCH,
	utils,
} from "./launch-host.js";

const dp = await import("../src/display_profile.js");

const WIN_UA =
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0";
const LIN_UA =
	"Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0";
const PREF = "gfx.color_management.display_profile";

/** A minimal byte string the launcher accepts as an ICC profile, told apart by `tag`. */
function icc(tag: string): Buffer {
	const body = Buffer.alloc(16);
	body.write(tag, "latin1");
	const header = Buffer.alloc(128);
	header.writeUInt32BE(128 + 4 + body.length, 0);
	header.write("acsp", 36, "latin1");
	return Buffer.concat([header, Buffer.alloc(4), body]);
}

const DEFAULT = icc("win-default");
const PANEL = icc("panel-3456");
const OTHER = icc("other");
const TABLE = {
	win: [
		{ name: "default", icc: DEFAULT.toString("base64") },
		{ name: "panel", screens: [[3456, 2160]], icc: PANEL.toString("base64") },
	],
	mac: [],
	lin: [],
};

const CACHE_DIR = path.join(SCRATCH, "display-profiles");
const ORIGINAL_DEPS = { ...dp.displayProfileDeps };

beforeEach(() => {
	dp.displayProfileDeps.table = () => TABLE;
	dp.displayProfileDeps.cacheDirs = () => [CACHE_DIR];
});
afterEach(() => {
	Object.assign(dp.displayProfileDeps, ORIGINAL_DEPS);
	restoreDeps();
});
afterAll(() => fs.rmSync(SCRATCH, { recursive: true, force: true }));

/** launchOptions on a Linux host; returns the prefs. */
async function launch(
	opts: { ua?: string; config?: Record<string, any>; [key: string]: any } = {},
): Promise<Record<string, any>> {
	const { ua = WIN_UA, config = {}, ...rest } = opts;
	isolateLaunch();
	utils.utilsDeps.hostOsKey = () => "lin";
	const options = (await quietly(() =>
		utils.launchOptions({
			env: { HOME },
			config: { "navigator.userAgent": ua, ...config },
			headless: true,
			i_know_what_im_doing: true,
			...rest,
		}),
	)) as Record<string, any>;
	return options.firefoxUserPrefs;
}

const profileOf = (prefs: Record<string, any>) => fs.readFileSync(prefs[PREF]);

describe("draw", () => {
	it("draws a panel profile only for its screen", () => {
		const hi = {
			"screen.width": 1382.4,
			"screen.height": 864,
			"window.devicePixelRatio": 2.5,
		};
		expect(dp.deviceScreen(hi)).toEqual([3456, 2160]);
		expect(Buffer.from(dp.drawDisplayProfile(hi, 1, "win")!)).toEqual(PANEL);
		expect(
			Buffer.from(
				dp.drawDisplayProfile(
					{ "screen.width": 1920, "screen.height": 1080 },
					1,
					"win",
				)!,
			),
		).toEqual(DEFAULT);
	});

	it("draws nothing for an OS without records", () => {
		expect(
			dp.drawDisplayProfile(
				{ "screen.width": 1440, "screen.height": 900 },
				1,
				"mac",
			),
		).toBeNull();
	});

	it("ships a table that holds only ICC profiles", () => {
		Object.assign(dp.displayProfileDeps, ORIGINAL_DEPS);
		for (const [osKey, entries] of Object.entries(dp.loadDisplayProfiles())) {
			if (osKey.startsWith("_")) continue;
			expect(["win", "mac", "lin"]).toContain(osKey);
			for (const e of entries as any[]) {
				dp.checkIcc(Buffer.from(e.icc, "base64"));
				expect(
					e.source,
					"every profile names the machine it was recorded on",
				).toBeTruthy();
			}
		}
	});
});

describe("launchOptions", () => {
	it("hands the caller's bytes to the pref", async () => {
		expect(profileOf(await launch({ display_profile: OTHER }))).toEqual(OTHER);
	});

	it("hands the caller's path to the pref", async () => {
		const f = path.join(SCRATCH, "display.icm");
		fs.writeFileSync(f, OTHER);
		expect(profileOf(await launch({ display_profile: f }))).toEqual(OTHER);
	});

	it("draws one for a Windows identity", async () => {
		const prefs = await launch({
			config: { "screen.width": 1920, "screen.height": 1080 },
		});
		expect(profileOf(prefs)).toEqual(DEFAULT);
	});

	it("writes the same file for the same profile", async () => {
		const a = (await launch({ display_profile: OTHER }))[PREF];
		const b = (await launch({ display_profile: OTHER }))[PREF];
		expect(a).toBe(b);
	});

	it("keeps the browser's own with false", async () => {
		expect(PREF in (await launch({ display_profile: false }))).toBe(false);
	});

	it("lets the caller's pref win", async () => {
		const prefs = await launch({
			display_profile: OTHER,
			firefox_user_prefs: { [PREF]: "/x.icc" },
		});
		expect(prefs[PREF]).toBe("/x.icc");
	});

	it("leaves a host-OS identity on the real display's profile", async () => {
		expect(PREF in (await launch({ ua: LIN_UA }))).toBe(false);
	});

	it("still takes the caller's profile for a host-OS identity", async () => {
		expect(
			profileOf(await launch({ ua: LIN_UA, display_profile: OTHER })),
		).toEqual(OTHER);
	});

	it("refuses bytes that are not an ICC profile", async () => {
		const bad = Buffer.from("\x89PNG not a profile".repeat(10), "latin1");
		await expect(launch({ display_profile: bad })).rejects.toThrow(
			/not an ICC profile/,
		);
	});
});
