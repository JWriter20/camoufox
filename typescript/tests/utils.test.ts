import { describe, expect, it } from "vitest";
import {
	checkValidOS,
	determineUAOS,
	getEnvVars,
	getTargetOS,
	isDomainSet,
	mergeInto,
	setInto,
	spoofsWindowDimensions,
	validateType,
} from "../src/utils.js";

const WINDOWS_UA =
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0";
const MAC_UA =
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) Gecko/20100101 Firefox/135.0";
const LINUX_UA =
	"Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0";

describe("determineUAOS", () => {
	it("maps each supported UA to its short OS name", () => {
		expect(determineUAOS(WINDOWS_UA)).toBe("win");
		expect(determineUAOS(MAC_UA)).toBe("mac");
		expect(determineUAOS(LINUX_UA)).toBe("lin");
	});

	it("falls back to lin for an unparseable UA, as the Python twin does", () => {
		// Python's ua_parser answers "Other" rather than nothing, so its `raise`
		// never fires. Throwing here instead would make
		// `config: {"navigator.userAgent": ...}` launch under one launcher and
		// hard-error under the other.
		expect(determineUAOS("X")).toBe("lin");
		expect(determineUAOS("")).toBe("lin");
	});
});

describe("getTargetOS", () => {
	it("derives the OS from the config's user agent", () => {
		expect(getTargetOS({ "navigator.userAgent": WINDOWS_UA })).toBe("win");
	});
});

describe("checkValidOS", () => {
	it("accepts the supported OS names, individually and as a list", () => {
		expect(() => checkValidOS("windows")).not.toThrow();
		expect(() => checkValidOS(["macos", "linux"])).not.toThrow();
	});

	it("rejects unsupported and non-lowercase names", () => {
		expect(() => checkValidOS("Windows")).toThrow(/lowercase/);
		expect(() => checkValidOS("solaris")).toThrow(/does not support/);
	});
});

describe("validateType", () => {
	it("matches properties.json type names to JS values", () => {
		expect(validateType("x", "str")).toBe(true);
		expect(validateType(1, "str")).toBe(false);
		expect(validateType(1, "int")).toBe(true);
		expect(validateType(1.5, "int")).toBe(false);
		expect(validateType(-1, "uint")).toBe(false);
		expect(validateType(1.5, "double")).toBe(true);
		expect(validateType(true, "bool")).toBe(true);
		expect(validateType([], "array")).toBe(true);
		expect(validateType({}, "dict")).toBe(true);
		expect(validateType([], "dict")).toBe(false);
		expect(validateType("x", "unknown-type")).toBe(false);
	});
});

describe("isDomainSet", () => {
	it("matches exact keys and dotted/colon prefixes", () => {
		const config = { "navigator.platform": "Win32", "locale:region": "US" };
		expect(isDomainSet(config, "navigator.platform")).toBe(true);
		expect(isDomainSet(config, "navigator.")).toBe(true);
		expect(isDomainSet(config, "locale:")).toBe(true);
		expect(isDomainSet(config, "screen.")).toBe(false);
		expect(isDomainSet(config, "timezone")).toBe(false);
	});
});

describe("mergeInto / setInto", () => {
	it("never overwrites an existing key", () => {
		const target: Record<string, unknown> = { a: 1 };
		mergeInto(target, { a: 2, b: 3 });
		expect(target).toEqual({ a: 1, b: 3 });

		setInto(target, "a", 9);
		setInto(target, "c", 9);
		expect(target).toEqual({ a: 1, b: 3, c: 9 });
	});
});

describe("getEnvVars", () => {
	it("chunks the config across CAMOU_CONFIG_<n> in index order", () => {
		// A payload comfortably larger than the 32767-char POSIX chunk size.
		const config = { "navigator.userAgent": "x".repeat(70_000) };
		const env = getEnvVars(config, "win");

		const keys = Object.keys(env)
			.filter((k) => k.startsWith("CAMOU_CONFIG_"))
			.sort((a, b) => Number(a.split("_").pop()) - Number(b.split("_").pop()));
		expect(keys.length).toBeGreaterThan(1);
		expect(keys[0]).toBe("CAMOU_CONFIG_1");

		const reassembled = keys.map((k) => env[k]).join("");
		expect(JSON.parse(reassembled)).toEqual(config);
	});
});

describe("spoofsWindowDimensions", () => {
	it("reassembles chunks before looking for a window dimension key", () => {
		const config = { "window.outerWidth": 1280 };
		expect(spoofsWindowDimensions({ env: getEnvVars(config, "win") })).toBe(
			true,
		);
	});

	it("is false when nothing spoofs a window dimension", () => {
		expect(
			spoofsWindowDimensions({
				env: getEnvVars({ "screen.width": 1920 }, "win"),
			}),
		).toBe(false);
		expect(spoofsWindowDimensions({})).toBe(false);
	});

	it("finds a key that straddles a chunk boundary", () => {
		// Pad so "window.outerHeight" is split across two CAMOU_CONFIG_<n> vars.
		const config = {
			pad: "x".repeat(32_750),
			"window.outerHeight": 720,
		};
		const env = getEnvVars(config, "win");
		expect(
			Object.keys(env).filter((k) => k.startsWith("CAMOU_CONFIG_")).length,
		).toBeGreaterThan(1);
		expect(spoofsWindowDimensions({ env })).toBe(true);
	});
});
