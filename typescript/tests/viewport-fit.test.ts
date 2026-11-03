/**
 * A viewport the caller asks for sits on a screen and in a window that can
 * hold it; one that cannot is clamped, since a browser is one machine with one
 * screen (LEAKS 619). Twin of python/tests/test_viewport_fit.py (which also derives
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

// One machine, one screen (LEAKS 619): a context keeps the screen, and a
// viewport whose window cannot fit it is clamped, with a LeakWarning.
describe("fitContextScreen", () => {
	it("clamps a viewport too tall for the screen to the largest that fits", async () => {
		let fitted: unknown;
		const messages = await warned(() => {
			fitted = utils.fitContextScreen(LINUX_1366, { viewport: VIEWPORT });
		});
		expect(fitted).toEqual([null, [1000, 768 - 86]]);
		expect(messages.join("\n")).toMatch(
			/1000x700 viewport does not fit this linux machine's 1366x768 screen[\s\S]*its page is\s+1000x682[\s\S]*Launch a browser with a larger screen/,
		);
	});

	it("clamps each axis on its own", async () => {
		let fitted: unknown;
		await warned(() => {
			fitted = utils.fitContextScreen(
				{
					os: "win",
					screen: [1366, 768],
					window: { x: 0, y: 0, width: 1366, height: 720 },
				},
				{ viewport: { width: 1600, height: 700 } },
			);
		});
		expect(fitted).toEqual([null, [1366 - 18, 768 - 48 - 130]]);
	});

	it("counts the window's position", async () => {
		let fitted: unknown;
		const messages = await warned(() => {
			fitted = utils.fitContextScreen(
				{
					os: "lin",
					screen: [1366, 768],
					window: { x: 66, y: 32, width: 1294, height: 736 },
				},
				{ viewport: VIEWPORT },
			);
		});
		expect(fitted).toEqual([null, [1000, 768 - 32 - 86]]);
		expect(messages.join("\n")).toMatch(/sits at 66,32/);
	});

	it("counts the available rect and its origin (LEAKS 635)", async () => {
		// A Linux machine with a 66 px dock on the left and a 32 px top bar.
		const docked = {
			os: "lin" as const,
			screen: [1366, 768] as [number, number],
			window: null,
			avail: [66, 32, 1300, 736] as [number, number, number, number],
		};
		let fitted: unknown;
		const messages = await warned(() => {
			fitted = utils.fitContextScreen(docked, {
				viewport: { width: 1400, height: 700 },
			});
		});
		expect(fitted).toEqual([null, [1300 - 2, 736 - 86]]);
		expect(messages.join("\n")).toMatch(/sits at 66,32/);
		await warned(() => {
			fitted = utils.fitContextScreen(
				{ ...docked, avail: [0, 0, 1300, 768] },
				{ viewport: { width: 1320, height: 600 } },
			);
		});
		expect(fitted).toEqual([null, [1298, 600]]);
		// A caller's screen is not the machine's: its rect does not apply.
		expect(
			await warned(() => {
				fitted = utils.fitContextScreen(docked, {
					viewport: VIEWPORT,
					screen: { width: 1920, height: 1080 },
				});
			}),
		).toEqual([]);
		expect(fitted).toEqual([[1920, 1080], null]);
	});

	it("leaves a viewport that fits, and no viewport", async () => {
		expect(
			await warned(() => {
				expect(
					utils.fitContextScreen(
						{ ...LINUX_1366, screen: [1920, 1080] },
						{ viewport: VIEWPORT },
					),
				).toEqual([null, null]);
				expect(utils.fitContextScreen(LINUX_1366, { viewport: null })).toEqual([
					null,
					null,
				]);
			}),
		).toEqual([]);
	});

	it("counts Playwright's default viewport as asked for", async () => {
		let fitted: unknown;
		const messages = await warned(() => {
			fitted = utils.fitContextScreen(
				{ os: "lin", screen: [1280, 720], window: null },
				{},
			);
		});
		expect(fitted).toEqual([null, [1278, 634]]);
		expect(messages.join("\n")).toMatch(/1280x720 viewport does not fit/);
	});

	// Juggler sizes the window to the page in its chrome
	// (juggler-viewport-window.py), so only the screen bounds the page.
	it("leaves a viewport larger than the launch window that fits the screen, quietly", async () => {
		let fitted: unknown;
		const messages = await warned(() => {
			fitted = utils.fitContextScreen(
				{ ...LINUX_1366, screen: [2560, 1440] },
				{ viewport: { width: 1300, height: 900 } },
			);
		});
		expect(fitted).toEqual([null, null]);
		expect(messages).toEqual([]);
	});

	it("claims the caller's screen option, and fits the viewport to it", async () => {
		expect(
			utils.fitContextScreen(LINUX_1366, {
				viewport: VIEWPORT,
				screen: { width: 2560, height: 1440 },
			}),
		).toEqual([[2560, 1440], null]);
		let fitted: unknown;
		const messages = await warned(() => {
			fitted = utils.fitContextScreen(LINUX_1366, {
				viewport: VIEWPORT,
				screen: { width: 1366, height: 700 },
			});
		});
		expect(fitted).toEqual([
			[1366, 700],
			[1000, 614],
		]);
		expect(messages.join("\n")).toMatch(
			/1000x700 viewport does not fit this linux machine's 1366x700 screen/,
		);
	});

	it("keeps the viewport, and warns, where the window leaves no room", async () => {
		let fitted: unknown;
		const messages = await warned(() => {
			fitted = utils.fitContextScreen(
				{ ...LINUX_1366, window: { x: 0, y: 700, width: 1366, height: 68 } },
				{ viewport: VIEWPORT },
			);
		});
		expect(fitted).toEqual([null, null]);
		expect(messages.join("\n")).toMatch(
			/no page fits, so the viewport is kept/,
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
	it("keeps the screen and gives the context the fitted viewport", async () => {
		const { browser, calls } = fakeBrowser();
		utils.attachViewportFit(browser, LINUX_1366);
		utils.attachViewportFit(browser); // once only
		let context: any;
		const messages = await warned(async () => {
			context = await browser.newContext({ viewport: VIEWPORT });
		});
		expect(
			messages.filter((m) => /viewport does not fit/.test(m)),
		).toHaveLength(1);
		expect(context.scripts).toEqual([]);
		expect(context[Symbol.for("camoufox.geometry")]).toEqual(LINUX_1366);
		expect(calls).toEqual([{ viewport: { width: 1000, height: 682 } }]);
	});

	it("sets a caller's screen through the screen setter", async () => {
		const { browser, calls } = fakeBrowser();
		utils.attachViewportFit(browser, LINUX_1366);
		const screen = { width: 2560, height: 1440 };
		const context = await browser.newContext({ viewport: VIEWPORT, screen });
		expect(context.scripts).toHaveLength(1);
		expect(context.scripts[0]).toContain("setScreenDimensions(2560, 1440)");
		expect(calls).toEqual([{ viewport: VIEWPORT, screen }]);
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
	it("carries the available rect, origin included (LEAKS 635)", () => {
		const config = {
			"navigator.userAgent":
				"Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0",
			"screen.width": 1366,
			"screen.height": 768,
			"screen.availLeft": 66,
			"screen.availTop": 32,
			"screen.availWidth": 1300,
			"screen.availHeight": 736,
		};
		expect(utils.launchGeometry(options(config))?.avail).toEqual([
			66, 32, 1300, 736,
		]);
		const { browser } = fakeBrowser();
		utils.attachViewportFit(browser, LINUX_1366);
		utils.handOverContextGeometry(browser, { config, preset: {} });
		expect(browser[Symbol.for("camoufox.nextContext")].avail).toEqual([
			66, 32, 1300, 736,
		]);
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
	it("clamps a size the screen cannot hold", async () => {
		let fitted: unknown;
		const messages = await warned(() => {
			fitted = utils.fitViewportSize(LINUX_1366, VIEWPORT);
		});
		expect(fitted).toEqual({ width: 1000, height: 682 });
		expect(messages.join("\n")).toMatch(
			/1000x700 viewport does not fit this linux machine's 1366x768 screen/,
		);
	});

	it("leaves a size that fits, quietly", async () => {
		expect(
			await warned(() => {
				expect(
					utils.fitViewportSize(
						{ ...LINUX_1366, screen: [1920, 1080] },
						VIEWPORT,
					),
				).toBe(VIEWPORT);
				expect(utils.fitViewportSize(null, VIEWPORT)).toBe(VIEWPORT);
			}),
		).toEqual([]);
	});

	it("warns about a persistent launch drawn without its viewport", async () => {
		const messages = await warned(() =>
			utils.checkViewportSize(LINUX_1366, VIEWPORT),
		);
		expect(messages.join("\n")).toMatch(
			/1000x700 viewport does not fit the 1366x768 screen/,
		);
	});
});
