import { describe, expect, it } from "vitest";
import {
	formatAssetDate,
	OS_ARCH_MATRIX,
	OS_NAME,
	RepoConfig,
	Version,
} from "../src/pkgman.js";

describe("Version ordering", () => {
	it("orders alpha < beta < numeric builds", () => {
		const alpha = new Version("alpha.5");
		const beta = new Version("beta.5");
		expect(alpha.lessThan(beta)).toBe(true);
		expect(beta.lessThan(alpha)).toBe(false);
	});

	it("orders numerically within a channel", () => {
		expect(new Version("beta.9").lessThan(new Version("beta.20"))).toBe(true);
		expect(new Version("beta.20").lessThan(new Version("beta.9"))).toBe(false);
	});

	it("treats equal builds as equal", () => {
		expect(new Version("beta.20").equals(new Version("beta.20"))).toBe(true);
	});

	it("reports the full version string", () => {
		expect(new Version("beta.28", "152.0.4").fullString).toBe(
			"152.0.4-beta.28",
		);
	});

	it("detects the alpha channel", () => {
		expect(new Version("alpha.26").isAlpha).toBe(true);
		expect(new Version("beta.26").isAlpha).toBe(false);
	});

	it("accepts builds inside the supported range", () => {
		// CONSTRAINTS is alpha.1 <= v < 1
		expect(new Version("beta.28").isSupported()).toBe(true);
		expect(new Version("alpha.1").isSupported()).toBe(true);
	});
});

describe("RepoConfig", () => {
	it("parses the comma-separated fallback repo list", () => {
		const official = RepoConfig.findByName("Official");
		expect(official).toBeDefined();
		expect(official?.repos).toEqual(["daijro/camoufox", "camoufox/camoufox"]);
		expect(official?.repo).toBe("daijro/camoufox");
	});

	it("defaults to the repo named in the config", () => {
		expect(RepoConfig.getDefaultName()).toBe("Official");
		expect(RepoConfig.getDefault().name).toBe("Official");
	});

	it("builds an asset regex that captures name/version/build", () => {
		const config = RepoConfig.getDefault();
		const pattern = config.buildPattern("lin", "x86_64");
		const match = pattern.exec("camoufox-152.0.4-beta.28-lin.x86_64.zip");
		expect(match?.groups?.version).toBe("152.0.4");
		expect(match?.groups?.build).toBe("beta.28");
	});

	it("does not match another platform's asset", () => {
		const pattern = RepoConfig.getDefault().buildPattern("lin", "x86_64");
		expect(pattern.exec("camoufox-152.0.4-beta.28-win.x86_64.zip")).toBeNull();
	});

	it("applies the stable channel's build floor", () => {
		const official = RepoConfig.getDefault();
		// Official pins stable to beta.19+; prerelease is unbounded.
		expect(official.isVersionSupported(new Version("beta.28"), false)).toBe(
			true,
		);
		expect(official.isVersionSupported(new Version("beta.10"), false)).toBe(
			false,
		);
		expect(official.isVersionSupported(new Version("alpha.1"), true)).toBe(
			true,
		);
	});

	it("treats a repo with no browser constraints as unbounded", () => {
		const coryking = RepoConfig.findByName("CoryKing");
		expect(coryking?.isVersionSupported(new Version("beta.1"), false)).toBe(
			true,
		);
	});
});

describe("platform matrix", () => {
	it("knows the arches the current OS ships", () => {
		expect(OS_ARCH_MATRIX[OS_NAME].length).toBeGreaterThan(0);
	});
});

describe("formatAssetDate", () => {
	it("omits the year for the current year", () => {
		const now = new Date("2026-08-02T00:00:00Z");
		expect(formatAssetDate("2026-03-14T10:00:00Z", now)).toMatch(/^Mar 1[34]$/);
	});

	it("includes the year for another year", () => {
		const now = new Date("2026-08-02T00:00:00Z");
		expect(formatAssetDate("2024-03-14T10:00:00Z", now)).toMatch(
			/^Mar 1[34], 2024$/,
		);
	});

	it("returns empty for missing or unparseable input", () => {
		expect(formatAssetDate(undefined)).toBe("");
		expect(formatAssetDate("not-a-date")).toBe("");
	});
});
