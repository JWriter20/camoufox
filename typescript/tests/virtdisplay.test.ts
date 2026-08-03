/**
 * Mirrors python/tests/test_virtdisplay.py: the screen-geometry resolution
 * and the Xvfb argument vector, neither of which needs an X server.
 */
import { afterEach, describe, expect, it } from "vitest";
import { VirtualDisplayNotSupported } from "../src/exceptions.js";
import {
	COMPOSITE_ENV_VAR,
	DEFAULT_SCREEN,
	SCREEN_ENV_VAR,
	VirtualDisplay,
} from "../src/virtdisplay.js";

afterEach(() => {
	delete process.env[SCREEN_ENV_VAR];
	delete process.env[COMPOSITE_ENV_VAR];
});

describe("screen geometry", () => {
	it("defaults to 1x1x24", () => {
		expect(new VirtualDisplay().screen).toBe(DEFAULT_SCREEN);
		expect(DEFAULT_SCREEN).toBe("1x1x24");
	});

	it("accepts an explicit constructor override", () => {
		expect(new VirtualDisplay(false, "800x600x16").screen).toBe("800x600x16");
	});

	it("reads WxH from the env var and appends a depth of 24", () => {
		process.env[SCREEN_ENV_VAR] = "1920x1080";
		expect(new VirtualDisplay().screen).toBe("1920x1080x24");
	});

	it("reads WxHxD from the env var verbatim", () => {
		process.env[SCREEN_ENV_VAR] = "1920x1080x16";
		expect(new VirtualDisplay().screen).toBe("1920x1080x16");
	});

	it("falls back to the default for an empty env var", () => {
		process.env[SCREEN_ENV_VAR] = "   ";
		expect(new VirtualDisplay().screen).toBe(DEFAULT_SCREEN);
	});

	it("rejects malformed geometry rather than handing Xvfb junk", () => {
		for (const bad of ["1920", "1920x", "axb", "1920x1080x24x8", "0x1080"]) {
			process.env[SCREEN_ENV_VAR] = bad;
			expect(() => new VirtualDisplay()).toThrow(VirtualDisplayNotSupported);
		}
	});
});

describe("xvfbArgs", () => {
	it("passes the resolved screen through to -screen 0", () => {
		const args = new VirtualDisplay(false, "1280x720x24").xvfbArgs;
		const idx = args.indexOf("-screen");
		expect(idx).toBeGreaterThanOrEqual(0);
		expect(args[idx + 1]).toBe("0");
		expect(args[idx + 2]).toBe("1280x720x24");
	});

	it("disables COMPOSITE by default", () => {
		const args = new VirtualDisplay().xvfbArgs;
		expect(args[args.indexOf("COMPOSITE") - 1]).toBe("-extension");
	});

	it("enables COMPOSITE via the env escape hatch", () => {
		process.env[COMPOSITE_ENV_VAR] = "1";
		const args = new VirtualDisplay().xvfbArgs;
		expect(args[args.indexOf("COMPOSITE") - 1]).toBe("+extension");
	});

	it("enables COMPOSITE via the constructor", () => {
		const args = new VirtualDisplay(false, undefined, true).xvfbArgs;
		expect(args[args.indexOf("COMPOSITE") - 1]).toBe("+extension");
	});

	it("keeps GLX on and the cursor off", () => {
		const args = new VirtualDisplay().xvfbArgs;
		expect(args[args.indexOf("GLX") - 1]).toBe("+extension");
		expect(args).toContain("-nocursor");
		expect(args).toContain("-nolisten");
	});
});

describe("kill", () => {
	it("is safe to call on a display that was never started", () => {
		expect(() => new VirtualDisplay().kill()).not.toThrow();
	});
});
