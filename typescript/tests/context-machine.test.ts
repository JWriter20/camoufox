/**
 * One browser is one machine: a NewContext() identity takes the launch
 * identity's machine-level values and draws only a user's own (LEAKS 619).
 * Mirrors python/tests/test_context_machine.py.
 */
import { describe, expect, it } from "vitest";
import * as fp from "../src/fingerprints.js";

const LAUNCH: Record<string, any> = {
	"navigator.userAgent":
		"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0",
	"navigator.platform": "Win32",
	"navigator.oscpu": "Windows NT 10.0; Win64; x64",
	"navigator.appVersion": "5.0 (Windows)",
	"navigator.hardwareConcurrency": 12,
	"navigator.language": "en-US",
	"screen.width": 2560,
	"screen.height": 1440,
	"screen.availWidth": 2560,
	"screen.availHeight": 1392,
	"screen.colorDepth": 24,
	"window.outerWidth": 1600,
	"window.outerHeight": 1000,
	"window.screenX": 66,
	"window.screenY": 32,
	"webGl:vendor": "Google Inc. (NVIDIA)",
	"webGl:renderer":
		"ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0, D3D11)",
	"webGl:parameters": { "3379": 16384, "33901": [1, 1024] },
	"webrtc:hostipv6": true,
	fonts: ["Arial", "Segoe UI"],
	voices: [{ name: "Microsoft David - English (United States)" }],
	timezone: "America/New_York",
	"locale:language": "en",
	"locale:region": "US",
	"webrtc:ipv4": "203.0.113.7",
	"geolocation:latitude": 40.7,
};

describe("a context on its browser's machine", () => {
	it("takes the machine's values from the launch", () => {
		const out = fp.generateContextFingerprint({ machine: LAUNCH });
		for (const key of [
			"navigator.userAgent",
			"navigator.platform",
			"screen.width",
			"window.screenX",
			"webGl:vendor",
			"webGl:renderer",
			"webGl:parameters",
			"webrtc:hostipv6",
			"fonts",
			"voices",
		])
			expect(out.config[key]).toEqual(LAUNCH[key]);
		// The machine's screen is the launch's own (LEAKS 635): no per-context setter.
		expect(out.init_script).not.toContain("setScreenDimensions");
		expect(out.init_script).not.toContain("setScreenColorDepth");
		expect(out.context_options.userAgent).toBe(LAUNCH["navigator.userAgent"]);
	});

	it("draws only a user's own values", () => {
		const out = fp.generateContextFingerprint({
			machine: LAUNCH,
			timezone: "Europe/Paris",
			locale: "fr-FR",
			webrtc_ip: "198.51.100.4",
		});
		expect(out.config.timezone).toBe("Europe/Paris");
		expect(out.config["navigator.language"]).toBe("fr-FR");
		expect(out.init_script).toContain('w.setWebRTCIPv4("198.51.100.4")');
		const machine = fp.machineConfig(LAUNCH);
		expect(
			Object.keys(machine).some((k) =>
				fp.CONTEXT_USER_KEYS.some((p) => k.startsWith(p)),
			),
		).toBe(false);
	});

	it("refuses another machine", () => {
		expect(() =>
			fp.generateContextFingerprint({ machine: LAUNCH, os: "linux" }),
		).toThrow(/claims windows, not linux/);
		expect(() =>
			fp.generateContextFingerprint({
				machine: LAUNCH,
				preset: { navigator: { platform: "Win32" } } as any,
			}),
		).toThrow(/preset is a whole machine/);
	});

	it("keeps the machine screen's available origin (LEAKS 635)", () => {
		const out = fp.generateContextFingerprint({
			machine: {
				...LAUNCH,
				"screen.availLeft": 66,
				"screen.availTop": 32,
				"screen.availWidth": 2494,
				"screen.availHeight": 1408,
			},
		});
		expect(out.init_script).not.toContain("setScreenDimensions");
		expect(out.config["screen.availLeft"]).toBe(66);
		expect(out.config["screen.availTop"]).toBe(32);
	});

	it("still sets a drawn identity's own screen", () => {
		const out = fp.generateContextFingerprint({ os: "windows" });
		expect(out.init_script).toContain("w.setScreenDimensions(");
	});

	it("claims no WebGL on a render node", () => {
		const out = fp.generateContextFingerprint({
			machine: LAUNCH,
			webgl: false,
		});
		expect(
			Object.keys(out.config).some(
				(k) => k.startsWith("webGl:") || k.startsWith("webGl2:"),
			),
		).toBe(false);
	});
});
