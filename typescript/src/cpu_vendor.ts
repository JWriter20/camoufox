/**
 * The CPU vendor an identity claims (cpu:vendor), as far as a page can tell
 * it. The TS twin of cpu_vendor.py; see there for the measurements.
 *
 * Skia's raster pipeline divides with the x86 reciprocal estimate (RCPPS),
 * whose low bits differ between Intel and AMD; a page reads them back from any
 * canvas blend that divides. The browser gives the claimed vendor's estimate
 * when it is not the host's (skia-cpu-vendor-estimates.patch). The claim is
 * set only where the identity implies a vendor: a caller's cpu:vendor, or a
 * WebGL renderer that is an integrated GPU. A discrete GPU, or an identity of
 * the host's own OS, leaves the host's vendor.
 */
import * as fs from "node:fs";
import * as os from "node:os";

export const CPU_VENDOR_KEY = "cpu:vendor";
export const VENDORS = ["intel", "amd"] as const;
export type CpuVendor = (typeof VENDORS)[number];

const INTEL_DISCRETE = /Arc\(TM\)\s+(Pro\s+)?[AB]\d{2,3}/i;
// AMD integrated graphics: every APU name in fpgen's corpus (cpu_vendor.py).
const AMD_INTEGRATED =
	/Radeon\s?(\(TM\))?\s+(Graphics\b|(RX\s+)?Vega\b|R[2-7]\s+Graphics\b|HD\s+\d{4}\s+Graphics\b|\d{3}M\b)|\b(RENOIR|REMBRANDT|RAVEN2?|PICASSO|CEZANNE|LUCIENNE|BARCELO|PHOENIX|MENDOCINO|VANGOGH)\b/i;

/** Seams for tests. */
export const cpuVendorDeps = {
	readCpuinfo: (): string | null => {
		try {
			return fs.readFileSync("/proc/cpuinfo", "latin1");
		} catch {
			return null;
		}
	},
	cpuModel: (): string => os.cpus()[0]?.model ?? "",
};

/** "intel" or "amd" for this machine's CPU, null for anything else. */
export function hostCpuVendor(): CpuVendor | null {
	const info = cpuVendorDeps.readCpuinfo();
	const text =
		info === null
			? cpuVendorDeps.cpuModel()
			: (info.split("\n").find((l) => l.startsWith("vendor_id")) ?? "");
	if (text.includes("GenuineIntel") || text.startsWith("Intel")) return "intel";
	if (text.includes("AuthenticAMD") || text.startsWith("AMD")) return "amd";
	return null;
}

/** The CPU vendor a WebGL renderer string implies, or null. */
export function impliedCpuVendor(
	renderer: string | null | undefined,
): CpuVendor | null {
	if (!renderer) return null;
	if (renderer.includes("Intel") && !INTEL_DISCRETE.test(renderer))
		return "intel";
	if (AMD_INTEGRATED.test(renderer)) return "amd";
	return null;
}

/** Validate a caller's cpu:vendor, or claim the one the identity's GPU implies. */
export function setCpuVendor(
	config: Record<string, any>,
	targetOs: string,
	hostOs: string | null,
	hostVendor: CpuVendor | null,
): void {
	if (CPU_VENDOR_KEY in config) {
		const value = String(config[CPU_VENDOR_KEY]).toLowerCase();
		if (!(VENDORS as readonly string[]).includes(value))
			throw new Error(
				`cpu:vendor must be one of ${VENDORS.join(", ")}, not ${JSON.stringify(config[CPU_VENDOR_KEY])}`,
			);
		config[CPU_VENDOR_KEY] = value;
		return;
	}
	if (targetOs === hostOs) return;
	const implied = impliedCpuVendor(config["webGl:renderer"]);
	// The host's own vendor needs no claim: the browser's estimate is already its.
	if (implied && implied !== hostVendor) config[CPU_VENDOR_KEY] = implied;
}
