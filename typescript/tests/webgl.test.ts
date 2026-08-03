import { describe, expect, it } from "vitest";
import { getPossiblePairs, sampleWebGL } from "../src/webgl/sample.js";

describe("sampleWebGL", () => {
	it("returns a vendor/renderer pair valid for the OS", async () => {
		for (const os of ["win", "mac", "lin"] as const) {
			const pairs = (await getPossiblePairs())[os];
			const fp = await sampleWebGL(os);
			expect(fp["webGl:vendor"]).toBeTruthy();
			expect(fp["webGl:renderer"]).toBeTruthy();
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

	it("carries the WebGL parameter payload alongside the pair", async () => {
		const fp = await sampleWebGL("win");
		expect(fp["webGl:supportedExtensions"]).toBeInstanceOf(Array);
		expect(fp).toHaveProperty("webGl:contextAttributes");
		expect(typeof fp.webGl2Enabled).toBe("boolean");
	});

	it("honours an explicit vendor/renderer pair", async () => {
		const [pair] = (await getPossiblePairs()).mac;
		const fp = await sampleWebGL("mac", pair.vendor, pair.renderer);
		expect(fp["webGl:vendor"]).toBe(pair.vendor);
		expect(fp["webGl:renderer"]).toBe(pair.renderer);
	});

	it("rejects a pair that does not occur on the target OS", async () => {
		// Apple silicon renderers never appear on Windows.
		const applePair = (await getPossiblePairs()).mac.find((p) =>
			p.vendor.includes("Apple"),
		);
		if (!applePair) throw new Error("no Apple pair in the mac catalogue");
		await expect(
			sampleWebGL("win", applePair.vendor, applePair.renderer),
		).rejects.toThrow(/not valid for win/);
	});

	it("rejects an unknown pair outright", async () => {
		await expect(sampleWebGL("lin", "Nope Inc.", "Nope")).rejects.toThrow(
			/No WebGL data found/,
		);
	});

	it("rejects an invalid OS", async () => {
		await expect(sampleWebGL("bsd" as any)).rejects.toThrow(/Invalid OS/);
	});

	it("does not hand callers a shared mutable record", async () => {
		const a = await sampleWebGL(
			"lin",
			...pairOf(await getPossiblePairs(), "lin"),
		);
		a["webGl:vendor"] = "mutated";
		const b = await sampleWebGL(
			"lin",
			...pairOf(await getPossiblePairs(), "lin"),
		);
		expect(b["webGl:vendor"]).not.toBe("mutated");
	});
});

function pairOf(
	pairs: Record<string, Array<{ vendor: string; renderer: string }>>,
	os: string,
): [string, string] {
	const first = pairs[os][0];
	return [first.vendor, first.renderer];
}
