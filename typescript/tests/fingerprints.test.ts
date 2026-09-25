import { describe, expect, it } from "vitest";
import {
	buildInitScript,
	clampScreenToDisplay,
	clampWindowDimensions,
	clampWindowPosition,
	fixNavigatorArch,
	fixScreenNoTaskbar,
	generateContextFingerprint,
	generateRandomFontSubset,
	generateRandomVoiceSubset,
	setMediaDevicesDefaults,
} from "../src/fingerprints.js";

const LINUX_UA =
	"Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0";

describe("fixNavigatorArch", () => {
	it("forces platform and oscpu to match the UA arch on Linux", () => {
		const config: Record<string, any> = {
			"navigator.userAgent": LINUX_UA,
			"navigator.platform": "Linux armv81",
			"navigator.oscpu": "Linux armv81",
		};
		fixNavigatorArch(config, "lin");
		expect(config["navigator.platform"]).toBe("Linux x86_64");
		expect(config["navigator.oscpu"]).toBe("Linux x86_64");
	});

	it("leaves mac and windows fingerprints alone", () => {
		const config: Record<string, any> = {
			"navigator.userAgent": LINUX_UA,
			"navigator.platform": "MacIntel",
		};
		fixNavigatorArch(config, "mac");
		expect(config["navigator.platform"]).toBe("MacIntel");
	});
});

describe("fixScreenNoTaskbar", () => {
	it("shrinks availHeight so CreepJS's noTaskbar flag cannot flip", () => {
		const config: Record<string, any> = {
			"screen.width": 1920,
			"screen.height": 1080,
			"screen.availWidth": 1920,
			"screen.availHeight": 1080,
			"window.outerHeight": 1080,
			"window.innerHeight": 1000,
		};
		fixScreenNoTaskbar(config, "win");
		expect(config["screen.availHeight"]).toBe(1040);
		// The window is clamped to the new avail, keeping its chrome delta.
		expect(config["window.outerHeight"]).toBe(1040);
		expect(config["window.innerHeight"]).toBe(960);
	});

	it("does nothing when the screen already reports chrome", () => {
		const config: Record<string, any> = {
			"screen.width": 1920,
			"screen.height": 1080,
			"screen.availWidth": 1920,
			"screen.availHeight": 1040,
		};
		fixScreenNoTaskbar(config, "win");
		expect(config["screen.availHeight"]).toBe(1040);
	});
});

describe("clampWindowDimensions", () => {
	it("enforces inner <= outer <= avail <= screen on both axes", () => {
		const config: Record<string, any> = {
			"screen.width": 1280,
			"screen.height": 720,
			"screen.availWidth": 1400,
			"screen.availHeight": 800,
			"window.outerWidth": 1600,
			"window.outerHeight": 900,
			"window.innerWidth": 1700,
			"window.innerHeight": 880,
		};
		clampWindowDimensions(config);

		expect(config["screen.availWidth"]).toBeLessThanOrEqual(
			config["screen.width"],
		);
		expect(config["screen.availHeight"]).toBeLessThanOrEqual(
			config["screen.height"],
		);
		expect(config["window.outerWidth"]).toBeLessThanOrEqual(
			config["screen.availWidth"],
		);
		expect(config["window.outerHeight"]).toBeLessThanOrEqual(
			config["screen.availHeight"],
		);
		expect(config["window.innerWidth"]).toBeLessThanOrEqual(
			config["window.outerWidth"],
		);
		expect(config["window.innerHeight"]).toBeLessThanOrEqual(
			config["window.outerHeight"],
		);
	});
});

describe("clampScreenToDisplay", () => {
	it("shrinks the screen to the display, preserving the taskbar delta", () => {
		const config: Record<string, any> = {
			"screen.width": 2560,
			"screen.height": 1440,
			"screen.availWidth": 2560,
			"screen.availHeight": 1400, // 40px of chrome
		};
		clampScreenToDisplay(config, 1366, 768);
		expect(config["screen.width"]).toBe(1366);
		expect(config["screen.height"]).toBe(768);
		expect(config["screen.availHeight"]).toBe(728);
	});

	it("leaves a screen already inside the display alone", () => {
		const config: Record<string, any> = {
			"screen.width": 1280,
			"screen.height": 720,
		};
		clampScreenToDisplay(config, 1920, 1080);
		expect(config["screen.width"]).toBe(1280);
		expect(config["screen.height"]).toBe(720);
	});
});

describe("clampWindowPosition", () => {
	it("keeps the window box inside its own reported screen", () => {
		const config: Record<string, any> = {
			"screen.width": 1366,
			"screen.height": 768,
			"window.outerWidth": 1200,
			"window.outerHeight": 700,
			"window.screenX": 900,
			"window.screenY": -20,
		};
		clampWindowPosition(config);
		expect(config["window.screenX"]).toBe(166); // 1366 - 1200
		expect(config["window.screenY"]).toBe(0);
	});
});

describe("setMediaDevicesDefaults", () => {
	it("defaults to one mic and one camera", () => {
		const config: Record<string, any> = {};
		setMediaDevicesDefaults(config);
		expect(config["mediaDevices:enabled"]).toBe(true);
		expect(config["mediaDevices:micros"]).toBe(1);
		expect(config["mediaDevices:webcams"]).toBe(1);
		expect(config["mediaDevices:speakers"]).toBe(0);
	});

	it("never overrides a caller-supplied device topology", () => {
		const config: Record<string, any> = { "mediaDevices:webcams": 3 };
		setMediaDevicesDefaults(config);
		expect(config["mediaDevices:webcams"]).toBe(3);
		expect(config["mediaDevices:enabled"]).toBeUndefined();
	});
});

describe("generateRandomFontSubset", () => {
	it("always includes the OS marker fonts CreepJS probes for", () => {
		for (const [os, markers] of [
			["macos", ["Helvetica Neue", "PingFang SC"]],
			["windows", ["Segoe UI", "Cambria Math"]],
			["linux", ["Arimo", "Twemoji Mozilla"]],
		] as const) {
			const fonts = generateRandomFontSubset(os);
			for (const marker of markers) {
				expect(fonts, `${os} is missing ${marker}`).toContain(marker);
			}
		}
	});

	it("varies between draws but stays a subset of the OS list", () => {
		const a = generateRandomFontSubset("macos");
		const b = generateRandomFontSubset("macos");
		expect(a.length).toBeGreaterThan(0);
		// Two independent draws of 30-78% of a large pool should differ.
		expect(new Set(a)).not.toEqual(new Set(b));
	});
});

describe("generateRandomVoiceSubset", () => {
	it("emits MaskConfig voice objects with exactly one default", () => {
		const voices = generateRandomVoiceSubset("windows");
		expect(voices.length).toBeGreaterThan(0);
		for (const voice of voices) {
			expect(voice).toHaveProperty("name");
			expect(voice).toHaveProperty("lang");
			expect(voice).toHaveProperty("voiceUri");
			expect(voice).toHaveProperty("isDefault");
			expect(voice).toHaveProperty("isLocalService");
		}
		expect(voices.filter((v) => v.isDefault)).toHaveLength(1);
	});

	it("uses the real Firefox URI scheme for each backend", () => {
		expect(generateRandomVoiceSubset("windows")[0].voiceUri).toMatch(
			/^urn:moz-tts:sapi:/,
		);
		expect(generateRandomVoiceSubset("macos")[0].voiceUri).toMatch(
			/^urn:moz-tts:osx:/,
		);
		const linux = generateRandomVoiceSubset("linux")[0];
		expect(linux.voiceUri).toMatch(/^urn:moz-tts:speechd:/);
		// speechd URIs carry the language as a query suffix.
		expect(linux.voiceUri).toContain("?");
	});

	it("marks a default matching the spoofed locale prefix", () => {
		const voices = generateRandomVoiceSubset("windows", "en-GB");
		const def = voices.find((v) => v.isDefault);
		expect(def?.lang.split("-")[0].toLowerCase()).toBe("en");
	});
});

describe("buildInitScript", () => {
	it("guards every setter behind a typeof check", () => {
		const script = buildInitScript({
			canvasSeed: 42,
			navigatorPlatform: "Win32",
			screenWidth: 1920,
			screenHeight: 1080,
			screenColorDepth: 24,
			fontList: ["Arial", "Segoe UI"],
			speechVoices: [
				{
					name: "Microsoft David",
					lang: "en-US",
					voiceUri: "urn:moz-tts:sapi:microsoft.david",
					isDefault: true,
					isLocalService: true,
				},
			],
		});

		expect(script).toContain(
			'if (typeof w.setCanvasSeed === "function") w.setCanvasSeed(42);',
		);
		expect(script).toContain("w.setScreenDimensions(1920, 1080)");
		expect(script).toContain("w.setScreenColorDepth(24)");
		expect(script).toContain('w.setFontList("Arial,Segoe UI")');
		expect(script).toContain('w.setSpeechVoices("Microsoft David")');
		// Every call site must be guarded -- an unguarded call throws on a stock
		// Firefox where the per-context setters do not exist.
		for (const line of script.split("\n")) {
			if (line.includes("w.set")) {
				expect(line.trim().startsWith("if (typeof w.set")).toBe(true);
			}
		}
	});

	it("always clears the WebRTC IP, even when none is supplied", () => {
		expect(buildInitScript({})).toContain('w.setWebRTCIPv4("")');
		expect(buildInitScript({ webrtcIP: "1.2.3.4" })).toContain(
			'w.setWebRTCIPv4("1.2.3.4")',
		);
	});

	it("omits setters whose value is absent", () => {
		const script = buildInitScript({});
		expect(script).not.toContain("setCanvasSeed");
		expect(script).not.toContain("setScreenDimensions");
		expect(script).not.toContain("setTimezone");
	});
});

describe("generateContextFingerprint", () => {
	it("builds an init script and Playwright context options from a preset", async () => {
		const fp = await generateContextFingerprint({
			preset: {
				navigator: {
					userAgent:
						"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
					platform: "Win32",
					hardwareConcurrency: 8,
				},
				screen: { width: 1920, height: 1080, colorDepth: 24 },
				webgl: { unmaskedVendor: "Google Inc.", unmaskedRenderer: "ANGLE" },
				timezone: "Europe/London",
			},
			ff_version: "135",
		});

		// ff_version rewrites the UA in both the config and the context options.
		expect(fp.config["navigator.userAgent"]).toContain("Firefox/135.0");
		expect(fp.config["navigator.userAgent"]).toContain("rv:135.0");
		expect(fp.context_options.userAgent).toContain("Firefox/135.0");
		expect(fp.context_options.timezoneId).toBe("Europe/London");
		expect(fp.context_options.viewport).toEqual({ width: 1920, height: 1052 });

		expect(fp.init_script).toContain('w.setNavigatorPlatform("Win32")');
		expect(fp.init_script).toContain("w.setNavigatorHardwareConcurrency(8)");
		expect(fp.init_script).toContain('w.setTimezone("Europe/London")');

		// Per-context noise seeds must be present and non-zero (0 is a C++ no-op).
		for (const key of ["fonts:spacing_seed", "audio:seed", "canvas:seed"]) {
			expect(fp.config[key]).toBeGreaterThan(0);
		}
	});

	it("honours explicit locale and config overrides", async () => {
		const fp = await generateContextFingerprint({
			preset: { navigator: { platform: "MacIntel" }, screen: {} },
			locale: "en-GB",
			config_overrides: { "fonts:spacing_seed": 0 },
		});

		expect(fp.config["locale:language"]).toBe("en");
		expect(fp.config["locale:region"]).toBe("GB");
		expect(fp.config["navigator.language"]).toBe("en-GB");
		expect(fp.context_options.locale).toBe("en-GB");
		// The override lands before the script is rendered. 0 is still emitted --
		// it is a no-op in the C++ noise implementation, which is exactly how
		// callers disable perturbation.
		expect(fp.config["fonts:spacing_seed"]).toBe(0);
		expect(fp.init_script).toContain("w.setFontSpacingSeed(0)");
	});

	it("falls back to BrowserForge synthesis with no preset", async () => {
		const fp = await generateContextFingerprint({ os: "windows" });
		expect(fp.config["navigator.userAgent"]).toContain("Firefox");
		expect(fp.config.fonts?.length).toBeGreaterThan(0);
		expect(fp.config.voices?.length).toBeGreaterThan(0);
		expect(fp.init_script).toContain("w.setWebRTCIPv4");
	});
});
