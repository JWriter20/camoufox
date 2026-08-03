/**
 * Invariants of launchOptions() that must hold without a real browser binary.
 *
 * Mirrors python/tests/test_launch_environment.py and
 * test_launch_geometry.py. Rather than stubbing the config/env assembly, we
 * point the launcher at a fixture "bundle" built from the repo's real
 * browser/settings/properties.json and browser/bundle/fontconfig -- so
 * validateConfig() and
 * getEnvVars() run for real.
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, it, vi } from "vitest";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BROWSER_DIR = path.resolve(HERE, "..", "..", "browser");

// Isolate the launcher's cache dir (INSTALL_DIR is resolved at module load, so
// this must be set before pkgman is imported below).
const TMP_ROOT = fs.mkdtempSync(path.join(os.tmpdir(), "camoufox-ts-test-"));
process.env.XDG_CACHE_HOME = path.join(TMP_ROOT, "cache");

// A minimal browser bundle: properties.json for validateConfig, and the
// per-OS fontconfig + fonts dir that getEnvVars needs on Linux.
const BUNDLE = path.join(TMP_ROOT, "bundle");
fs.mkdirSync(path.join(BUNDLE, "fonts"), { recursive: true });
fs.copyFileSync(
	path.join(BROWSER_DIR, "settings", "properties.json"),
	path.join(BUNDLE, "properties.json"),
);
for (const osDir of ["linux", "macos", "windows"]) {
	fs.mkdirSync(path.join(BUNDLE, "fontconfig", osDir), { recursive: true });
	fs.copyFileSync(
		path.join(BROWSER_DIR, "bundle", "fontconfig", osDir, "fonts.conf"),
		path.join(BUNDLE, "fontconfig", osDir, "fonts.conf"),
	);
}

vi.mock("../src/pkgman.js", async (importOriginal) => {
	const actual = await importOriginal<typeof import("../src/pkgman.js")>();
	return {
		...actual,
		ensureCamoufoxInstalled: vi.fn(async () => BUNDLE),
		installedVerStr: vi.fn(() => "152.0.4-beta.28"),
		launchPath: vi.fn(() => path.join(BUNDLE, "camoufox-bin")),
		getPath: vi.fn((file: string) => path.join(BUNDLE, file)),
	};
});

vi.mock("../src/addons.js", async (importOriginal) => {
	const actual = await importOriginal<typeof import("../src/addons.js")>();
	return { ...actual, addDefaultAddons: vi.fn(async () => undefined) };
});

const { launchOptions } = await import("../src/utils.js");
const { sampleWebGL } = await import("../src/webgl/sample.js");

/** launchOptions() with a caller-supplied (empty) env, as the Python tests do. */
async function launch(extra: Record<string, any> = {}) {
	return launchOptions({
		block_webgl: true,
		i_know_what_im_doing: true,
		env: {} as Record<string, string>,
		...extra,
	});
}

/** Reassemble the chunked CAMOU_CONFIG back into the config object. */
function configOf(options: Record<string, any>): Record<string, any> {
	const blob = Object.entries(options.env)
		.filter(([k]) => k.startsWith("CAMOU_CONFIG_"))
		.sort(
			(a, b) => Number(a[0].split("_").pop()) - Number(b[0].split("_").pop()),
		)
		.map(([, v]) => v)
		.join("");
	return JSON.parse(blob);
}

async function configFrom(extra: Record<string, any> = {}) {
	return configOf(await launch({ headless: true, ...extra }));
}

describe("launchOptions environment isolation", () => {
	beforeEach(() => {
		for (const key of ["DISPLAY", "GDK_BACKEND", "WAYLAND_DISPLAY"]) {
			delete process.env[key];
		}
	});

	it("does not mutate the process environment for a virtual display", async () => {
		await launch({ virtual_display: ":4242", headless: true });

		expect(process.env.DISPLAY).toBeUndefined();
		expect(process.env.GDK_BACKEND).toBeUndefined();
	});

	it("writes the virtual display into the launch env, not the parent's", async () => {
		const options = await launch({ virtual_display: ":4242", headless: true });

		expect(options.env.DISPLAY).toBe(":4242");
		expect(options.env.GDK_BACKEND).toBe("x11");
		expect(options.env.MOZ_ENABLE_WAYLAND).toBe("0");
		expect(options.env.WAYLAND_DISPLAY).toBeUndefined();
	});

	it("does not leak a caller-supplied env mapping back to the caller", async () => {
		const callerEnv: Record<string, string> = { FOO: "bar" };
		await launch({ virtual_display: ":99", headless: true, env: callerEnv });

		expect(callerEnv).toEqual({ FOO: "bar" });
	});
});

describe("launchOptions shape", () => {
	it("returns Playwright's camelCase launch options", async () => {
		const options = await launch({ headless: true });

		expect(options.executablePath).toBe(path.join(BUNDLE, "camoufox-bin"));
		expect(options.headless).toBe(true);
		expect(Array.isArray(options.args)).toBe(true);
		expect(typeof options.firefoxUserPrefs).toBe("object");
	});

	it("omits proxy entirely when none is given (Playwright 1.55+ validates it)", async () => {
		expect("proxy" in (await launch({ headless: true }))).toBe(false);
	});

	it("passes an explicit proxy straight through", async () => {
		const proxy = { server: "http://localhost:8080" };
		expect((await launch({ headless: true, proxy })).proxy).toEqual(proxy);
	});

	it("passes unknown options through to Playwright", async () => {
		expect((await launch({ headless: true, timeout: 12_345 })).timeout).toBe(
			12_345,
		);
	});

	it("disables WebGL when block_webgl is set", async () => {
		const options = await launch({ headless: true });
		expect(options.firefoxUserPrefs["webgl.disabled"]).toBe(true);
	});

	it("samples a WebGL fingerprint when WebGL is not blocked", async () => {
		const options = await launch({ headless: true, block_webgl: false });
		const config = configOf(options);
		expect(config["webGl:vendor"]).toBeTruthy();
		expect(config["webGl:renderer"]).toBeTruthy();
		expect(options.firefoxUserPrefs["webgl.force-enabled"]).toBe(true);
		// webGl2Enabled is consumed as a pref, never written into the config.
		expect(config.webGl2Enabled).toBeUndefined();
	});
});

describe("launchOptions validation", () => {
	it("rejects webgl_config without an os", async () => {
		await expect(
			launch({ headless: true, webgl_config: ["Vendor", "Renderer"] }),
		).rejects.toThrow(/OS must be set/);
	});

	it("rejects an unsupported os", async () => {
		await expect(launch({ headless: true, os: "solaris" })).rejects.toThrow(
			/does not support/,
		);
	});

	it("rejects a non-lowercase os", async () => {
		await expect(launch({ headless: true, os: "Windows" })).rejects.toThrow(
			/lowercase/,
		);
	});

	it("rejects custom_fonts_only with no fonts", async () => {
		await expect(
			launch({ headless: true, custom_fonts_only: true }),
		).rejects.toThrow(/No custom fonts were passed/);
	});

	it("rejects a config value of the wrong type", async () => {
		await expect(
			launch({
				headless: true,
				config: { "navigator.hardwareConcurrency": "eight" },
			}),
		).rejects.toThrow(/Invalid type for property/);
	});
});

describe("launchOptions geometry", () => {
	it("never emits an impossible window geometry", async () => {
		// The generated fingerprint is random, so assert the invariant over a
		// handful of draws rather than one.
		for (let i = 0; i < 5; i++) {
			const config = await configFrom();

			for (const axis of ["Width", "Height"] as const) {
				const screen = config[`screen.${axis.toLowerCase()}`];
				const avail = config[`screen.avail${axis}`];
				const outer = config[`window.outer${axis}`];
				const inner = config[`window.inner${axis}`];

				if (screen && avail) expect(avail).toBeLessThanOrEqual(screen);
				if (avail && outer) expect(outer).toBeLessThanOrEqual(avail);
				if (outer && inner) expect(inner).toBeLessThanOrEqual(outer);
			}

			// The window box must sit inside its own reported screen.
			for (const [axis, posKey] of [
				["Width", "window.screenX"],
				["Height", "window.screenY"],
			] as const) {
				const screen = config[`screen.${axis.toLowerCase()}`];
				const outer = config[`window.outer${axis}`];
				const pos = config[posKey];
				if (pos != null && screen && outer) {
					expect(pos).toBeGreaterThanOrEqual(0);
					expect(pos).toBeLessThanOrEqual(screen - outer);
				}
			}
		}
	});

	it("keeps availHeight below height so noTaskbar cannot flip", async () => {
		for (let i = 0; i < 5; i++) {
			const config = await configFrom();
			const sameWidth = config["screen.width"] === config["screen.availWidth"];
			const sameHeight =
				config["screen.height"] === config["screen.availHeight"];
			expect(sameWidth && sameHeight).toBe(false);
		}
	});

	it("does not touch geometry the caller set themselves", async () => {
		const config = await configFrom({
			config: {
				"screen.width": 800,
				"screen.height": 600,
				"screen.availWidth": 800,
				"screen.availHeight": 600,
			},
		});
		// User-set screen/window means the corrections are skipped wholesale.
		expect(config["screen.availHeight"]).toBe(600);
	});
});

describe("launchOptions config contents", () => {
	it("sets the three per-launch noise seeds to non-zero values", async () => {
		const config = await configFrom();
		for (const key of ["fonts:spacing_seed", "audio:seed", "canvas:seed"]) {
			expect(config[key]).toBeGreaterThan(0);
			expect(config[key]).toBeLessThanOrEqual(4_294_967_295);
		}
	});

	it("spoofs Accept-Encoding from the generated fingerprint", async () => {
		// `fingerprint-generator` returns the headers *beside* the fingerprint
		// and lowercased, so they were being dropped -- Firefox then sent its own
		// Accept-Encoding while every other header was spoofed. Python's
		// browserforge carries them on the fingerprint object, hence the
		// `headers.Accept-Encoding` mapping in browserforge.yml.
		const config = await configFrom();
		expect(config["headers.Accept-Encoding"]).toMatch(/gzip/);
	});

	it("ships a font list and a voice list for the target OS", async () => {
		const config = await configFrom();
		expect(config.fonts.length).toBeGreaterThan(0);
		expect(config.voices.length).toBeGreaterThan(0);
		expect(config.voices.filter((v: any) => v.isDefault)).toHaveLength(1);
	});

	it("defaults mediaDevices so enumerateDevices() is never empty", async () => {
		const config = await configFrom();
		expect(config["mediaDevices:enabled"]).toBe(true);
		expect(config["mediaDevices:micros"]).toBe(1);
		expect(config["mediaDevices:webcams"]).toBe(1);
	});

	it("honours a caller-supplied mediaDevices topology", async () => {
		const config = await configFrom({ config: { "mediaDevices:webcams": 2 } });
		expect(config["mediaDevices:webcams"]).toBe(2);
		expect(config["mediaDevices:enabled"]).toBeUndefined();
	});

	it("uses caller-supplied fonts verbatim", async () => {
		const config = await configFrom({ fonts: ["Arial", "Verdana"] });
		expect(config.fonts).toEqual(["Arial", "Verdana"]);
	});

	it("sets humanize without maxTime for `true`", async () => {
		const config = await configFrom({ humanize: true });
		expect(config.humanize).toBe(true);
		expect(config["humanize:maxTime"]).toBeUndefined();
	});

	it("sets maxTime as a number when given a duration", async () => {
		const config = await configFrom({ humanize: 2.5 });
		expect(config.humanize).toBe(true);
		expect(config["humanize:maxTime"]).toBe(2.5);
	});

	it("enables the main world only when asked", async () => {
		expect((await configFrom()).allowMainWorld).toBeUndefined();
		expect((await configFrom({ main_world_eval: true })).allowMainWorld).toBe(
			true,
		);
	});

	it("resolves the locale into the intl config keys", async () => {
		const config = await configFrom({ locale: "en-GB" });
		expect(config["locale:language"]).toBe("en");
		expect(config["locale:region"]).toBe("GB");
	});

	it("does not emit an implicit suppress-script for en-US", async () => {
		// "en-Latn-US" is not in ICU's available-locale set, so emitting the
		// implicit script makes Intl fall back to "en" while navigator.language
		// stays "en-US" -- a readable mismatch.
		const config = await configFrom({ locale: "en-US" });
		expect(config["locale:script"]).toBeUndefined();
	});

	it("keeps an explicit script when the tag carries one", async () => {
		const config = await configFrom({ locale: "zh-Hans-CN" });
		expect(config["locale:script"]).toBe("Hans");
	});

	it("joins multiple locales into locale:all without duplicates", async () => {
		const config = await configFrom({ locale: "en-US, fr-FR, en-US" });
		expect(config["locale:all"]).toBe("en-US, fr-FR");
	});
});

describe("launchOptions with a fingerprint preset", () => {
	it("uses a bundled preset when fingerprint_preset is true", async () => {
		const config = await configFrom({
			fingerprint_preset: true,
			os: "windows",
		});
		expect(config["navigator.userAgent"]).toContain("Firefox");
		expect(config.fonts.length).toBeGreaterThan(0);
	});

	it("accepts a preset object directly", async () => {
		const config = await configFrom({
			fingerprint_preset: {
				navigator: {
					userAgent:
						"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
					platform: "Win32",
					hardwareConcurrency: 12,
				},
				screen: { width: 1920, height: 1080, colorDepth: 24 },
			},
		});
		expect(config["navigator.platform"]).toBe("Win32");
		expect(config["navigator.hardwareConcurrency"]).toBe(12);
		expect(config["screen.width"]).toBe(1920);
		// oscpu is derived from the platform when the preset omits it.
		expect(config["navigator.oscpu"]).toBe("Windows NT 10.0; Win64; x64");
		// ff_version defaults to the installed browser's major.
		expect(config["navigator.userAgent"]).toContain("Firefox/152.0");
	});
});

describe("launchOptions WebGL for a preset GPU", () => {
	const WINDOWS_NAV = {
		userAgent:
			"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
		platform: "Win32",
		hardwareConcurrency: 12,
	};

	/** A vendor/renderer pair the bundled WebGL catalogue has parameters for. */
	const KNOWN = {
		unmaskedVendor: "Google Inc. (Intel)",
		unmaskedRenderer:
			"ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0 ps_5_0), or similar",
	};
	/** A real preset GPU that the catalogue has no parameters for. */
	const UNKNOWN = {
		unmaskedVendor: "Google Inc. (Intel)",
		unmaskedRenderer:
			"ANGLE (Intel, Intel(R) Arc(TM) A750 Graphics Direct3D11 vs_5_0 ps_5_0), or similar",
	};

	const withPreset = (webgl: Record<string, string>, extra = {}) =>
		configFrom({
			block_webgl: false,
			fingerprint_preset: { navigator: WINDOWS_NAV, webgl },
			...extra,
		});

	it("keeps the preset's GPU when the catalogue has parameters for it", async () => {
		const config = await withPreset(KNOWN);
		expect(config["webGl:vendor"]).toBe(KNOWN.unmaskedVendor);
		expect(config["webGl:renderer"]).toBe(KNOWN.unmaskedRenderer);
		expect(config["webGl:parameters"]).toBeDefined();
	});

	it("substitutes a sampled GPU when the catalogue has none, instead of throwing", async () => {
		// Roughly 10% of the bundled presets name a GPU with no recorded
		// parameters; this used to abort the launch outright.
		const config = await withPreset(UNKNOWN);
		expect(config["webGl:renderer"]).not.toBe(UNKNOWN.unmaskedRenderer);
		expect(config["webGl:vendor"]).toBeTruthy();
		expect(config["webGl:renderer"]).toBeTruthy();
	});

	it("keeps the substituted vendor, renderer and parameters coherent", async () => {
		// The whole point of substituting rather than patching around the missing
		// entry: the name must describe the GPU whose extensions and limits are
		// reported, or the mismatch is itself a tell.
		const config = await withPreset(UNKNOWN);
		const match = await sampleWebGL(
			"win",
			config["webGl:vendor"],
			config["webGl:renderer"],
		);
		expect(config["webGl:supportedExtensions"]).toEqual(
			match["webGl:supportedExtensions"],
		);
		expect(config["webGl:parameters"]).toEqual(match["webGl:parameters"]);
	});

	it("warns that the preset's GPU was swapped out", async () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		try {
			await withPreset(UNKNOWN, { i_know_what_im_doing: false });
			expect(warn.mock.calls.flat().join("\n")).toContain(
				"WebGL catalogue has no",
			);
		} finally {
			warn.mockRestore();
		}
	});

	it("still throws for a caller-supplied pair the catalogue lacks", async () => {
		// The fallback is for presets we ship. If the caller names a specific
		// pair, silently handing them a different GPU would be worse than failing.
		await expect(
			configFrom({
				block_webgl: false,
				os: "windows",
				webgl_config: [UNKNOWN.unmaskedVendor, UNKNOWN.unmaskedRenderer],
			}),
		).rejects.toThrow(/No WebGL data found/);
	});
});
