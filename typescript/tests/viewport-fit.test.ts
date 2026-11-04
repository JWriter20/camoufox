/**
 * A viewport the caller asks for sits on a screen and in a window that can
 * hold it. Twin of python/tests/test_viewport_fit.py (which also derives
 * RECORDED_WINDOW_CHROME from fpgen's recorded windows).
 */

import * as fs from "node:fs";
import { generate } from "fpgen-js";
import { afterAll, afterEach, beforeEach, describe, expect, it } from "vitest";
import { MODEL } from "./fpgen-setup.js";
import {
	BUNDLE_EXE,
	configOf,
	fingerprints,
	HOME,
	quietly,
	restoreDeps,
	SCRATCH,
	stubHost,
	utils,
	warnings,
} from "./launch-host.js";

afterEach(() => restoreDeps());
afterAll(() => fs.rmSync(SCRATCH, { recursive: true, force: true }));

const LINUX_1366 = {
	os: "lin" as const,
	screen: [1366, 768] as [number, number],
	window: { x: 0, y: 0, width: 1366, height: 768 },
};
const VIEWPORT = { width: 1000, height: 700 };

async function warned(fn: () => unknown): Promise<string[]> {
	const { error, warnings: caught } = await warnings.recordWarnings(fn);
	if (error) throw error;
	return caught
		.filter((w) => w.category === "LeakWarning")
		.map((w) => w.message);
}

describe("screenFloor", () => {
	it("is the viewport in the recorded chrome plus the taskbar", () => {
		expect(fingerprints.RECORDED_WINDOW_CHROME).toEqual({
			win: [18, 130],
			mac: [0, 86],
			lin: [2, 86],
		});
		expect(fingerprints.screenFloor("lin", VIEWPORT)).toEqual([1002, 786]);
		expect(fingerprints.screenFloor("win", VIEWPORT)).toEqual([1018, 878]);
		expect(fingerprints.screenFloor("mac", VIEWPORT)).toEqual([1000, 862]);
	});

	it("holds the window too", () => {
		const window = { x: 100, y: 20, width: 1300, height: 700 };
		expect(fingerprints.screenFloor("lin", VIEWPORT, window)).toEqual([
			1400, 786,
		]);
		expect(fingerprints.screenFloor("lin", null, window)).toEqual([1400, 720]);
	});

	it.skipIf(!MODEL.ok)("draws a recorded screen that holds it", () => {
		for (const os of ["lin", "win", "mac"]) {
			const [w, h] = fingerprints.drawScreen(os, [1300, 1000]) as number[];
			expect(w).toBeGreaterThanOrEqual(1300);
			expect(h).toBeGreaterThanOrEqual(1000);
		}
		expect(fingerprints.drawScreen("lin", [20000, 20000])).toBeNull();
	});

	it.skipIf(!MODEL.ok)("holds the width bound with the height bound", () => {
		// fpgen gives screen.width and screen.height one node; a predicate per
		// field let the height one replace the width one, and 1280x1024 met
		// minWidth 1300 in about half these draws.
		const conditions = new fingerprints.Screen({
			minWidth: 1300,
			minHeight: 1000,
			maxHeight: 1024,
		}).asConditions();
		expect(conditions.screen({ width: 1280, height: 1024 })).toBe(false);
		for (let i = 0; i < 60; i++) {
			const screen = generate(
				{ browser: "Firefox", os: "Windows", ...conditions },
				{ target: "screen" },
			) as Record<string, number>;
			expect(screen.width).toBeGreaterThanOrEqual(1300);
			expect(screen.height).toBeGreaterThanOrEqual(1000);
			expect(screen.height).toBeLessThanOrEqual(1024);
		}
	});
});

describe.skipIf(!MODEL.ok)("fitContextScreen", () => {
	it("gives a viewport too tall for the screen a screen that holds it", async () => {
		let screen: [number, number] | null = null;
		expect(
			await warned(() => {
				screen = utils.fitContextScreen(LINUX_1366, { viewport: VIEWPORT });
			}),
		).toEqual([]);
		expect((screen as unknown as number[])[0]).toBeGreaterThanOrEqual(1366);
		expect((screen as unknown as number[])[1]).toBeGreaterThanOrEqual(786);
	});

	it("keeps a screen that holds it, and no viewport", () => {
		expect(
			utils.fitContextScreen(
				{ ...LINUX_1366, screen: [1920, 1080] },
				{ viewport: VIEWPORT },
			),
		).toBeNull();
		expect(utils.fitContextScreen(LINUX_1366, { viewport: null })).toBeNull();
	});

	it("counts Playwright's default viewport as asked for", () => {
		const screen = utils.fitContextScreen(
			{ os: "lin", screen: [1280, 720], window: null },
			{},
		) as number[];
		expect(screen[0]).toBeGreaterThanOrEqual(1282);
		expect(screen[1]).toBeGreaterThanOrEqual(806);
	});

	it("warns about a viewport larger than the window", async () => {
		const messages = await warned(() =>
			utils.fitContextScreen(LINUX_1366, {
				viewport: { width: 1300, height: 900 },
			}),
		);
		expect(messages.join("\n")).toMatch(
			/1300x900 viewport is larger than the 1366x768 browser window/,
		);
	});

	it("claims the caller's screen option, and warns when it cannot hold the viewport", async () => {
		expect(
			utils.fitContextScreen(LINUX_1366, {
				viewport: VIEWPORT,
				screen: { width: 2560, height: 1440 },
			}),
		).toEqual([2560, 1440]);
		const messages = await warned(() =>
			utils.fitContextScreen(LINUX_1366, {
				viewport: VIEWPORT,
				screen: { width: 1366, height: 700 },
			}),
		);
		expect(messages.join("\n")).toMatch(
			/1000x700 viewport does not fit the 1366x700 screen/,
		);
	});
});

function fakeBrowser() {
	const calls: any[] = [];
	const browser: any = {
		newContext: async (options: any) => {
			calls.push(options);
			const scripts: string[] = [];
			return {
				scripts,
				addInitScript: async (script: string) => {
					scripts.push(script);
				},
			};
		},
	};
	return { browser, calls };
}

describe.skipIf(!MODEL.ok)("attachViewportFit", () => {
	it("makes the context claim the fitted screen", async () => {
		const { browser, calls } = fakeBrowser();
		utils.attachViewportFit(browser, LINUX_1366);
		utils.attachViewportFit(browser); // once only
		const context = await browser.newContext({ viewport: VIEWPORT });
		expect(context.scripts).toHaveLength(1);
		const match = /setScreenDimensions\((\d+), (\d+)\)/.exec(
			context.scripts[0],
		) as RegExpExecArray;
		const [w, h] = match.slice(1).map(Number);
		expect(w).toBeGreaterThanOrEqual(1366);
		expect(h).toBeGreaterThanOrEqual(786);
		expect(calls).toEqual([{ viewport: VIEWPORT }]);
	});

	it("leaves a context that fits alone, and takes a handed-over identity", async () => {
		const { browser } = fakeBrowser();
		utils.attachViewportFit(browser, LINUX_1366);
		expect((await browser.newContext({ viewport: null })).scripts).toEqual([]);
		utils.handOverContextGeometry(browser, {
			config: { "navigator.platform": "Win32" },
			preset: { screen: { width: 1920, height: 1080 } },
		});
		const context = await browser.newContext({ viewport: null });
		expect(context.scripts).toEqual([]);
		expect(context[Symbol.for("camoufox.geometry")]).toEqual({
			os: "win",
			screen: [1920, 1080],
			window: LINUX_1366.window,
		});
		expect(browser[Symbol.for("camoufox.nextContext")]).toBeUndefined();
	});
});

describe.skipIf(!MODEL.ok)("NewContext identity", () => {
	it("makes the window the page, in a screen that holds it", () => {
		const bound = new fingerprints.Screen({ minWidth: 1600, minHeight: 1000 });
		for (let i = 0; i < 10; i++) {
			const fp = fingerprints.generateContextFingerprint({
				os: "linux",
				screen_bound: bound,
			});
			const opts = fp.context_options;
			// Playwright refuses deviceScaleFactor with a null viewport (system-ui-font-spoofing).
			if (opts.deviceScaleFactor !== undefined) {
				expect(opts.viewport).toEqual({
					width: Math.trunc(Number(fp.config["window.innerWidth"])),
					height: Math.trunc(Number(fp.config["window.innerHeight"])),
				});
				expect(opts.viewport.width).toBeLessThanOrEqual(
					fp.config["screen.width"],
				);
				expect(opts.viewport.height).toBeLessThanOrEqual(
					fp.config["screen.height"],
				);
			} else {
				expect(opts.viewport).toBeNull();
			}
			expect(fp.config["screen.width"]).toBeGreaterThanOrEqual(1600);
			expect(fp.config["screen.height"]).toBeGreaterThanOrEqual(1000);
		}
	});

	it("bounds the screen by the window and the viewport", () => {
		const { browser } = fakeBrowser();
		utils.attachViewportFit(browser, LINUX_1366);
		const bound = utils.contextScreenBound(browser, "windows", {
			viewport: VIEWPORT,
		}) as InstanceType<typeof fingerprints.Screen>;
		expect([bound.minWidth, bound.minHeight]).toEqual([1366, 878]);
		const any = utils.contextScreenBound(browser, null, {}) as InstanceType<
			typeof fingerprints.Screen
		>;
		expect([any.minWidth, any.minHeight]).toEqual([1366, 844]);
	});
});

describe("launchGeometry", () => {
	const options = (config: Record<string, any>) => ({
		env: { CAMOU_CONFIG_1: JSON.stringify(config) },
	});
	it("reads the window and the screen", () => {
		expect(
			utils.launchGeometry(
				options({
					"navigator.userAgent":
						"Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0",
					"screen.width": 1366,
					"screen.height": 768,
					"window.outerWidth": 1300,
					"window.outerHeight": 700,
					"window.screenX": 33,
				}),
			),
		).toEqual({
			os: "lin",
			screen: [1366, 768],
			window: { x: 33, y: 0, width: 1300, height: 700 },
		});
		expect(
			utils.launchGeometry(options({ "window.outerWidth": 1 })),
		).toBeNull();
	});
});

describe.skipIf(!MODEL.ok)(
	"persistent launch drawn around its viewport",
	() => {
		beforeEach(() => stubHost());
		it.each([
			["linux", "lin"],
			["windows", "win"],
			["macos", "mac"],
		] as const)("%s: the window and screen hold the viewport", async (os, key) => {
			const [chromeW, chromeH] = fingerprints.RECORDED_WINDOW_CHROME[key];
			const opts = await quietly(() =>
				utils.launchOptions({
					env: { HOME },
					headless: true,
					i_know_what_im_doing: true,
					executable_path: BUNDLE_EXE,
					os,
					viewport: VIEWPORT,
				} as any),
			);
			const config = configOf(opts);
			expect(opts.viewport).toEqual(VIEWPORT);
			expect([
				config["window.outerWidth"],
				config["window.outerHeight"],
			]).toEqual([1000 + chromeW, 700 + chromeH]);
			expect(config["screen.availWidth"]).toBeGreaterThanOrEqual(
				1000 + chromeW,
			);
			expect(config["screen.availHeight"]).toBeGreaterThanOrEqual(
				700 + chromeH,
			);
		});
	},
);

describe("setViewportSize", () => {
	it("warns about a size the screen cannot hold", async () => {
		const messages = await warned(() =>
			utils.checkViewportSize(LINUX_1366, VIEWPORT),
		);
		expect(messages.join("\n")).toMatch(
			/1000x700 viewport does not fit the 1366x768 screen/,
		);
		expect(
			await warned(() =>
				utils.checkViewportSize(
					{ ...LINUX_1366, screen: [1920, 1080] },
					VIEWPORT,
				),
			),
		).toEqual([]);
	});
});
