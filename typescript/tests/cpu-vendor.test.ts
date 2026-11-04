/**
 * The CPU vendor an identity claims (cpu_vendor.ts,
 * skia-cpu-vendor-estimates.patch). Twin of python/tests/test_cpu_vendor.py.
 */
import * as fs from "node:fs";
import { afterAll, afterEach, describe, expect, it } from "vitest";
import {
	configOf,
	HOME,
	isolateLaunch,
	quietly,
	restoreDeps,
	SCRATCH,
	utils,
} from "./launch-host.js";

const cv = await import("../src/cpu_vendor.js");

const INTEL_IGPU =
	"ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0 ps_5_0), or similar";
const INTEL_ARC_IGPU =
	"ANGLE (Intel, Intel(R) Arc(TM) Graphics Direct3D11 vs_5_0 ps_5_0), or similar";
const INTEL_ARC_DISCRETE =
	"ANGLE (Intel, Intel(R) Arc(TM) A770 Graphics Direct3D11 vs_5_0 ps_5_0), or similar";
const AMD_APU =
	"ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0), or similar";
const AMD_780M =
	"ANGLE (AMD, AMD Radeon 780M Graphics Direct3D11 vs_5_0 ps_5_0), or similar";
const AMD_DISCRETE =
	"ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0), or similar";
const NVIDIA =
	"ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or similar";

const ORIGINAL = { ...cv.cpuVendorDeps };
afterEach(() => {
	Object.assign(cv.cpuVendorDeps, ORIGINAL);
	restoreDeps();
});
afterAll(() => fs.rmSync(SCRATCH, { recursive: true, force: true }));

describe("implied", () => {
	it.each([
		[INTEL_IGPU, "intel"],
		[INTEL_ARC_IGPU, "intel"],
		[INTEL_ARC_DISCRETE, null],
		[AMD_APU, "amd"],
		[AMD_780M, "amd"],
		[AMD_DISCRETE, null],
		[NVIDIA, null],
		[null, null],
		["Apple M1, or similar", null],
		["ANGLE (AMD, AMD Radeon (TM) Graphics (0x000015E7) Direct3D11 vs_5_0 ps_5_0, D3D11)", "amd"],
		["ANGLE (AMD, AMD Radeon(TM) R4 Graphics (0x00009851) Direct3D11 vs_5_0 ps_5_0, D3D11)", "amd"],
		["ANGLE (AMD, AMD Radeon(TM) RX Vega 11 Graphics (0x000015D8) Direct3D11 vs_5_0 ps_5_0, D3D11)", "amd"],
		["ANGLE (AMD, Radeon HD 3200 Graphics Direct3D11 vs_5_0 ps_5_0)", "amd"],
		["ANGLE (AMD, Radeon(TM) RX 460 Graphics (0x000067EF) Direct3D11 vs_5_0 ps_5_0, D3D11)", null],
		["ANGLE (AMD, AMD Radeon 610 Series (0x00006665) Direct3D11 vs_5_0 ps_5_0, D3D11)", null],
		["ANGLE (AMD, AMD Radeon R7 M265 Series (0x00006604) Direct3D11 vs_5_0 ps_5_0, D3D11)", null],
		["ANGLE (AMD, REMBRANDT (rembrandt LLVM 15.0.7 DRM 3.61 6.14.0-37-generic), OpenGL 4.6)", "amd"],
		["ANGLE (AMD, NAVI23 (navi23 LLVM 15.0.7 DRM 3.61 6.14.0-37-generic), OpenGL ES 3.2)", null],
	])("only an integrated GPU names the CPU: %s", (renderer, vendor) => {
		expect(cv.impliedCpuVendor(renderer)).toBe(vendor);
	});
});

describe("setCpuVendor", () => {
	it("claims intel for an Intel iGPU on an AMD host", () => {
		const config: Record<string, any> = { "webGl:renderer": INTEL_IGPU };
		cv.setCpuVendor(config, "win", "lin", "amd");
		expect(config["cpu:vendor"]).toBe("intel");
	});
	it("needs no claim for the host's own vendor", () => {
		const config: Record<string, any> = { "webGl:renderer": INTEL_IGPU };
		cv.setCpuVendor(config, "win", "lin", "intel");
		expect("cpu:vendor" in config).toBe(false);
	});
	it("leaves the host vendor for a discrete GPU", () => {
		const config: Record<string, any> = { "webGl:renderer": NVIDIA };
		cv.setCpuVendor(config, "win", "lin", "amd");
		expect("cpu:vendor" in config).toBe(false);
	});
	it("claims nothing for an identity of the host OS", () => {
		const config: Record<string, any> = { "webGl:renderer": INTEL_IGPU };
		cv.setCpuVendor(config, "lin", "lin", "amd");
		expect("cpu:vendor" in config).toBe(false);
	});
	it("keeps and checks the caller's value", () => {
		const config: Record<string, any> = {
			"cpu:vendor": "Intel",
			"webGl:renderer": AMD_APU,
		};
		cv.setCpuVendor(config, "win", "lin", "intel");
		expect(config["cpu:vendor"]).toBe("intel");
		expect(() =>
			cv.setCpuVendor({ "cpu:vendor": "via" }, "win", "lin", "amd"),
		).toThrow(/cpu:vendor/);
	});
});

it("reads the host vendor from /proc/cpuinfo", () => {
	for (const [text, want] of [
		["vendor_id\t: AuthenticAMD\n", "amd"],
		["vendor_id\t: GenuineIntel\n", "intel"],
		["processor\t: 0\n", null],
	] as const) {
		cv.cpuVendorDeps.readCpuinfo = () => text;
		expect(cv.hostCpuVendor()).toBe(want);
	}
});

it("launchOptions claims the identity's vendor", async () => {
	const realEnv = utils.utilsDeps.getEnvVars;
	isolateLaunch();
	utils.utilsDeps.getEnvVars = realEnv;
	utils.utilsDeps.hostOsKey = () => "lin";
	utils.utilsDeps.hostCpuVendor = () => "amd";
	const ua =
		"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0";
	const options = (await quietly(() =>
		utils.launchOptions({
			env: { HOME },
			config: { "navigator.userAgent": ua, "cpu:vendor": "AMD" },
			headless: true,
			i_know_what_im_doing: true,
		}),
	)) as Record<string, any>;
	expect(configOf(options)["cpu:vendor"]).toBe("amd");
});
