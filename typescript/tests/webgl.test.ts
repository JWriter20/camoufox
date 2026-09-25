/**
 * WebGL sampling: ports of pythonlib/tests/test_webgl_extension_filter.py and
 * test_webgl_screen_consistency.py, plus the TS-only contract checks. The
 * seeded draws themselves are pinned against Python in identity-golden.test.ts.
 */
import { describe, expect, it } from "vitest";
import {
	generateContextFingerprint,
	gpuScreenIsPlausible,
	isSoftwareRenderer,
	MODERN_SCREEN_FLOOR,
	raiseScreenToModernFloor,
	rendererBucket,
	sampleWebGLForScreen,
} from "../src/fingerprints.js";
import {
	getPossiblePairs,
	loadWebGLData,
	loadWebGLRecords,
	sampleWebGL,
	type WebGLData,
} from "../src/webgl/sample.js";
import { MODEL } from "./fpgen-setup.js";

function rowWith(ext: string, key = "webGl2:supportedExtensions", os = "win") {
	for (const row of loadWebGLRecords()) {
		if ((row as any)[os] > 0 && (row.data[key] ?? []).includes(ext)) {
			return row.data;
		}
	}
	throw new Error(`no ${os} row carries ${ext}`);
}

describe("sampleWebGL", () => {
	it("returns a vendor/renderer pair valid for the OS", () => {
		for (const os of ["win", "mac", "lin"] as const) {
			const pairs = getPossiblePairs()[os];
			const fp = sampleWebGL(os);
			expect(
				pairs.some(
					(p) =>
						p.vendor === fp["webGl:vendor"] &&
						p.renderer === fp["webGl:renderer"],
				),
				`${os} sampled a pair outside its own catalogue`,
			).toBe(true);
		}
	});

	it("is deterministic per seed", () => {
		for (const os of ["win", "mac", "lin"]) {
			for (const seed of [0, 1, 99, 2 ** 32 - 1]) {
				expect(sampleWebGL(os, null, null, seed)).toEqual(
					sampleWebGL(os, null, null, seed),
				);
			}
		}
	});

	it("carries the WebGL parameter payload alongside the pair", () => {
		const fp = sampleWebGL("win");
		expect(fp["webGl:supportedExtensions"]).toBeInstanceOf(Array);
		expect(fp).toHaveProperty("webGl:contextAttributes");
		expect(typeof fp.webGl2Enabled).toBe("boolean");
	});

	it("honours an explicit vendor/renderer pair", () => {
		const [pair] = getPossiblePairs().mac;
		const fp = sampleWebGL("mac", pair.vendor, pair.renderer);
		expect(fp["webGl:vendor"]).toBe(pair.vendor);
		expect(fp["webGl:renderer"]).toBe(pair.renderer);
	});

	it("rejects a pair that does not occur on the target OS", () => {
		const apple = getPossiblePairs().mac.find((p) =>
			p.vendor.includes("Apple"),
		);
		if (!apple) throw new Error("no Apple pair in the mac catalogue");
		expect(() => sampleWebGL("win", apple.vendor, apple.renderer)).toThrow(
			/not valid for Win/,
		);
	});

	it("rejects an unknown pair and an invalid OS", () => {
		expect(() => sampleWebGL("lin", "Nope Inc.", "Nope")).toThrow(
			/No WebGL data found/,
		);
		expect(() => sampleWebGL("bsd")).toThrow(/Invalid OS/);
	});

	it("does not hand callers a shared mutable record", () => {
		const [pair] = getPossiblePairs().lin;
		const a = sampleWebGL("lin", pair.vendor, pair.renderer);
		a["webGl:vendor"] = "mutated";
		expect(
			sampleWebGL("lin", pair.vendor, pair.renderer)["webGl:vendor"],
		).not.toBe("mutated");
	});

	it("never draws a GPU the OS cannot report", () => {
		for (let seed = 0; seed < 300; seed++) {
			const r = sampleWebGL("mac", null, null, seed)["webGl:renderer"];
			expect(r.includes("Intel(R) HD Graphics 400")).toBe(false);
			expect(r.includes("Radeon R9 200 Series")).toBe(false);
		}
	});
});

describe("extension filter (test_webgl_extension_filter.py)", () => {
	it("Windows keeps OVR_multiview2 on WebGL2", () => {
		const data = loadWebGLData(rowWith("OVR_multiview2"), "win");
		expect(data["webGl2:supportedExtensions"]).toContain("OVR_multiview2");
	});

	it("Linux filters OVR_multiview2", () => {
		const data = loadWebGLData(
			rowWith("OVR_multiview2", "webGl2:supportedExtensions", "lin"),
			"lin",
		);
		expect(data["webGl2:supportedExtensions"]).not.toContain("OVR_multiview2");
	});

	it("OVR_multiview2 is never on WebGL1 in the corpus", () => {
		for (const row of loadWebGLRecords()) {
			expect(row.data["webGl:supportedExtensions"] ?? []).not.toContain(
				"OVR_multiview2",
			);
		}
	});

	it("draft extensions are filtered on every OS", () => {
		const blob = {
			"webGl:supportedExtensions": [
				"ANGLE_instanced_arrays",
				"WEBGL_multi_draw",
			],
			"webGl2:supportedExtensions": [
				"EXT_texture_norm16",
				"WEBGL_clip_cull_distance",
				"OVR_multiview2",
			],
		} as unknown as WebGLData;
		for (const os of ["win", "mac", "lin"]) {
			const data = loadWebGLData(blob, os);
			expect(data["webGl:supportedExtensions"]).toEqual([
				"ANGLE_instanced_arrays",
			]);
			expect(data["webGl2:supportedExtensions"]).not.toContain(
				"EXT_texture_norm16",
			);
			expect(data["webGl2:supportedExtensions"]).not.toContain(
				"WEBGL_clip_cull_distance",
			);
		}
	});

	it("sampled Windows identities can carry it", () => {
		let hits = 0;
		for (let s = 0; s < 200; s++) {
			const exts = sampleWebGL("win", null, null, s)[
				"webGl2:supportedExtensions"
			];
			if ((exts ?? []).includes("OVR_multiview2")) hits++;
		}
		expect(hits).toBeGreaterThan(0);
	});
});

// The three spellings Gecko emits for one discrete-NVIDIA bucket.
const NV_ANGLE =
	"ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or similar";
const NV_PCIE = "NVIDIA GeForce GTX 980/PCIe/SSE2";
const NV_NOUVEAU = "GeForce GTX 980, or similar";
const AMD_IGP =
	"ANGLE (AMD, Radeon HD 3200 Graphics Direct3D11 vs_5_0 ps_5_0), or similar";
const INTEL =
	"ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0 ps_5_0), or similar";
const APPLE = "Apple M1, or similar";
const LLVMPIPE = "llvmpipe, or similar";

function scripted(draws: Array<{ "webGl:renderer": string }>) {
	let i = 0;
	const calls: unknown[] = [];
	const sampler = (...args: unknown[]) => {
		calls.push(args);
		const d = draws[Math.min(i, draws.length - 1)];
		i++;
		return { ...d, "webGl:vendor": "x" } as WebGLData;
	};
	return { sampler, calls };
}

describe("WebGL <-> screen coherence (test_webgl_screen_consistency.py)", () => {
	it("every spelling of one GPU reduces to one bucket", () => {
		for (const r of [NV_ANGLE, NV_PCIE, NV_NOUVEAU]) {
			expect(rendererBucket(r)).toBe("GeForce GTX 980");
		}
	});

	it("the ANGLE vendor field does not decide the bucket", () => {
		expect(rendererBucket(AMD_IGP)).toBe("Radeon HD 3200 Graphics");
		expect(rendererBucket("Radeon HD 3200 Graphics, or similar")).toBe(
			"Radeon HD 3200 Graphics",
		);
	});

	it("the ANGLE Vulkan form is unwrapped", () => {
		expect(rendererBucket("ANGLE (Samsung Xclipse 920) on Vulkan")).toBe(
			"Samsung Xclipse 920",
		);
	});

	it("a discrete GPU is rejected on netbook panels only", () => {
		for (const r of [NV_ANGLE, NV_PCIE, NV_NOUVEAU]) {
			expect(gpuScreenIsPlausible(r, 1024, 600)).toBe(false);
		}
		for (const [w, h] of [
			[1024, 768],
			[1280, 720],
			[1280, 800],
			[1366, 768],
			[1920, 1080],
		]) {
			expect(gpuScreenIsPlausible(NV_ANGLE, w, h)).toBe(true);
		}
		for (const [w, h] of [
			[1024, 600],
			[800, 480],
			[1024, 576],
		]) {
			expect(gpuScreenIsPlausible(NV_ANGLE, w, h)).toBe(false);
		}
	});

	it("integrated parts, Apple silicon, raw model names and missing values are unconstrained", () => {
		expect(gpuScreenIsPlausible(INTEL, 1024, 600)).toBe(true);
		expect(gpuScreenIsPlausible(AMD_IGP, 1024, 600)).toBe(true);
		expect(gpuScreenIsPlausible(APPLE, 1280, 800)).toBe(true);
		expect(
			gpuScreenIsPlausible(
				"ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0)",
				1024,
				600,
			),
		).toBe(true);
		expect(gpuScreenIsPlausible(null, 1920, 1080)).toBe(true);
		expect(gpuScreenIsPlausible(NV_ANGLE, null, null)).toBe(true);
	});

	it("software renderers are recognized and unconstrained", () => {
		for (const r of [
			LLVMPIPE,
			"ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11 vs_5_0 ps_5_0)",
			"ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)",
			"Generic Renderer",
		]) {
			expect(isSoftwareRenderer(r)).toBe(true);
			expect(gpuScreenIsPlausible(r, 1024, 600)).toBe(true);
		}
		for (const r of [NV_ANGLE, INTEL, APPLE, AMD_IGP]) {
			expect(isSoftwareRenderer(r)).toBe(false);
		}
	});

	it("a software first draw is resampled to hardware, kept only as a last resort", () => {
		let s = scripted([
			{ "webGl:renderer": LLVMPIPE },
			{ "webGl:renderer": INTEL },
		]);
		expect(
			sampleWebGLForScreen("lin", 1024, 600, 32, null, s.sampler)[
				"webGl:renderer"
			],
		).toBe(INTEL);
		s = scripted([{ "webGl:renderer": LLVMPIPE }]);
		expect(
			sampleWebGLForScreen("lin", 1920, 1080, 32, null, s.sampler)[
				"webGl:renderer"
			],
		).toBe(LLVMPIPE);
	});

	it("software draws are skipped while resampling", () => {
		const s = scripted([
			{ "webGl:renderer": NV_ANGLE },
			{ "webGl:renderer": LLVMPIPE },
			{ "webGl:renderer": INTEL },
		]);
		expect(
			sampleWebGLForScreen("lin", 1024, 600, 32, null, s.sampler)[
				"webGl:renderer"
			],
		).toBe(INTEL);
	});

	it("falls back to the first draw when nothing is coherent", () => {
		const s = scripted([{ "webGl:renderer": NV_ANGLE }]);
		expect(
			sampleWebGLForScreen("win", 800, 600, 4, null, s.sampler)[
				"webGl:renderer"
			],
		).toBe(NV_ANGLE);
		expect(s.calls.length).toBe(4);
	});

	it("a plausible first draw costs one query", () => {
		const s = scripted([{ "webGl:renderer": NV_ANGLE }]);
		sampleWebGLForScreen("win", 1920, 1080, 32, null, s.sampler);
		expect(s.calls.length).toBe(1);
	});

	it("resampling walks seed+1, seed+2, ... like Python", () => {
		const s = scripted([
			{ "webGl:renderer": NV_ANGLE },
			{ "webGl:renderer": NV_ANGLE },
			{ "webGl:renderer": INTEL },
		]);
		sampleWebGLForScreen("win", 1024, 600, 32, 10, s.sampler);
		expect(s.calls.map((c: any) => c[3])).toEqual([10, 11, 12]);
	});

	it("the sampled GPU is coherent with the screen, against the real pool", () => {
		for (const os of ["win", "mac", "lin"]) {
			for (let seed = 0; seed < 25; seed++) {
				const fp = sampleWebGLForScreen(os, 1280, 800, 32, seed);
				expect(gpuScreenIsPlausible(fp["webGl:renderer"], 1280, 800)).toBe(
					true,
				);
			}
		}
	});

	it("the screen floor lifts netbook geometry and keeps the taskbar gap", () => {
		const config: Record<string, number> = {
			"screen.width": 1024,
			"screen.height": 600,
			"screen.availWidth": 1024,
			"screen.availHeight": 560,
		};
		raiseScreenToModernFloor(config);
		expect([config["screen.width"], config["screen.height"]]).toEqual([
			...MODERN_SCREEN_FLOOR,
		]);
		expect(config["screen.height"] - config["screen.availHeight"]).toBe(40);
		expect(config["screen.width"] - config["screen.availWidth"]).toBe(0);
	});

	it("the screen floor leaves an adequate screen, or no screen, alone", () => {
		const config = {
			"screen.width": 1920,
			"screen.height": 1080,
			"screen.availWidth": 1920,
			"screen.availHeight": 1040,
		};
		const before = { ...config };
		raiseScreenToModernFloor(config);
		expect(config).toEqual(before);
		const empty = {};
		raiseScreenToModernFloor(empty);
		expect(empty).toEqual({});
	});

	it.skipIf(!MODEL.ok)("context fingerprints get the same treatment", () => {
		for (const os of ["windows", "macos", "linux"]) {
			for (let i = 0; i < 5; i++) {
				const { config } = generateContextFingerprint({ os });
				const r = config["webGl:renderer"];
				const w = config["screen.width"];
				const h = config["screen.height"];
				expect(r && w && h).toBeTruthy();
				expect(gpuScreenIsPlausible(r, w, h)).toBe(true);
				expect(w * h).toBeGreaterThan(1024 * 600);
			}
		}
	});
});
