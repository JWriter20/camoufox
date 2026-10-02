/**
 * Port of pythonlib/tests/test_launch_rules.py: what a build declares beside
 * its binary (launch.json, properties.json) is what the launcher applies.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import {
	BUNDLE,
	configOf,
	fingerprints,
	HOME,
	quietly,
	restoreDeps,
	SCRATCH,
	stubHost,
	utils,
	warnings,
} from "./launch-host.js";
import { prerequisite } from "./prereq.js";

const { ensureModel } = await import("../src/fpgen/index.js");

let modelReady = true;
try {
	await ensureModel();
} catch (e) {
	modelReady = prerequisite("fpgen-model", false, String(e));
}

const deps = utils.utilsDeps;

const RULES = {
	rules: [
		{ prefs: { "browser.sessionhistory.max_entries": 10 } },
		{
			target: ["win"],
			host: ["lin", "mac"],
			prefs: { "example.cross-os-feature": true },
			env: { EXAMPLE_CROSS_OS: "1" },
			envFromConfig: { EXAMPLE_ARCH: "example:arch" },
		},
		{
			target: ["win"],
			host: ["lin"],
			envPaths: { EXAMPLE_LIB: "lib/example.dll" },
		},
	],
};
const LIB = path.join(SCRATCH, "rules-build", "lib", "example.dll");

/** A copy of the fixture bundle that declares launch rules and a floored key. */
const BUILD = path.join(SCRATCH, "rules-build");
const BUILD_EXE = path.join(BUILD, "camoufox-bin");

beforeAll(() => {
	fs.cpSync(BUNDLE, BUILD, { recursive: true });
	const props = JSON.parse(
		fs.readFileSync(path.join(BUILD, "properties.json"), "utf-8"),
	);
	props.push({ property: "window.history.length", type: "uint", min: 2 });
	fs.writeFileSync(path.join(BUILD, "properties.json"), JSON.stringify(props));
	fs.writeFileSync(path.join(BUILD, "launch.json"), JSON.stringify(RULES));
	fs.mkdirSync(path.dirname(LIB));
	fs.writeFileSync(LIB, "MZ");
});

afterEach(() => restoreDeps());

function apply(
	targetOs: string,
	prefs: Record<string, any> = {},
	env: Record<string, any> = {},
	host: "lin" | "mac" | "win" = "lin",
	exe: string = BUILD_EXE,
	config: Record<string, any> = {},
) {
	deps.hostOsKey = () => host;
	utils.applyLaunchRules(
		targetOs,
		config,
		prefs,
		new Set(Object.keys(prefs)),
		env,
		exe,
	);
	return { prefs, env };
}

describe("applyLaunchRules", () => {
	it("applies an unconditional rule to every identity", () => {
		const { prefs, env } = apply("mac");
		expect(prefs).toEqual({ "browser.sessionhistory.max_entries": 10 });
		expect(env).toEqual({});
	});

	it("applies a conditional rule when target and host match", () => {
		const { prefs, env } = apply("win");
		expect(prefs["example.cross-os-feature"]).toBe(true);
		expect(env).toEqual({ EXAMPLE_CROSS_OS: "1", EXAMPLE_LIB: LIB });
	});

	it("sets env from config only when the identity has the key", () => {
		const { env } = apply("win", {}, {}, "mac", BUILD_EXE, {
			"example:arch": "blackwell",
		});
		expect(env).toEqual({ EXAMPLE_CROSS_OS: "1", EXAMPLE_ARCH: "blackwell" });
	});

	it("fails loudly on a missing env path", () => {
		fs.rmSync(LIB);
		try {
			expect(() => apply("win")).toThrow(/example\.dll/);
		} finally {
			fs.writeFileSync(LIB, "MZ");
		}
	});

	it("sets an optional env path only when the file exists, and fills config keys", () => {
		const launch = path.join(BUILD, "launch.json");
		fs.writeFileSync(
			launch,
			JSON.stringify({
				rules: [
					{
						target: ["win"],
						host: ["lin"],
						envPathsOptional: {
							EXAMPLE_PRESENT: "lib/example.dll",
							EXAMPLE_ABSENT: "lib/missing.dll",
						},
						config: { "example:on": true, "example:kept": true },
					},
				],
			}),
		);
		try {
			const config: Record<string, any> = { "example:kept": false };
			const { env } = apply("win", {}, {}, "lin", BUILD_EXE, config);
			expect(env).toEqual({ EXAMPLE_PRESENT: LIB });
			expect(config).toEqual({ "example:on": true, "example:kept": false });
			const other: Record<string, any> = {};
			apply("win", {}, {}, "win", BUILD_EXE, other);
			expect(other).toEqual({});
		} finally {
			fs.writeFileSync(launch, JSON.stringify(RULES));
		}
	});

	it("appends an existing library to LD_PRELOAD, keeping the caller's", () => {
		const launch = path.join(BUILD, "launch.json");
		fs.writeFileSync(
			launch,
			JSON.stringify({
				rules: [
					{
						target: ["win"],
						host: ["lin"],
						ldPreload: ["lib/example.dll", "lib/missing.so"],
					},
				],
			}),
		);
		try {
			expect(apply("win").env).toEqual({ LD_PRELOAD: LIB });
			expect(
				apply("win", {}, { LD_PRELOAD: "/opt/other.so" }).env.LD_PRELOAD,
			).toBe(`/opt/other.so:${LIB}`);
			expect(apply("win", {}, { LD_PRELOAD: LIB }).env.LD_PRELOAD).toBe(LIB);
			expect(apply("win", {}, {}, "win").env).toEqual({});
		} finally {
			fs.writeFileSync(launch, JSON.stringify(RULES));
		}
	});

	it("points a variable at a per-user cache directory it creates", () => {
		const launch = path.join(BUILD, "launch.json");
		const cacheHome = path.join(SCRATCH, "rules-cache");
		const prevXdg = process.env.XDG_CACHE_HOME;
		process.env.XDG_CACHE_HOME = cacheHome;
		fs.writeFileSync(
			launch,
			JSON.stringify({
				rules: [
					{
						target: ["win"],
						host: ["lin"],
						envCacheDirs: { EXAMPLE_CACHE: "example-cache" },
					},
				],
			}),
		);
		try {
			const dir = path.join(cacheHome, "camoufox-example-cache");
			expect(apply("win").env).toEqual({ EXAMPLE_CACHE: dir });
			expect(fs.statSync(dir).isDirectory()).toBe(true);
			expect(apply("win", {}, { EXAMPLE_CACHE: "/mine" }).env).toEqual({
				EXAMPLE_CACHE: "/mine",
			});
		} finally {
			if (prevXdg === undefined) delete process.env.XDG_CACHE_HOME;
			else process.env.XDG_CACHE_HOME = prevXdg;
			fs.writeFileSync(launch, JSON.stringify(RULES));
		}
	});

	it("skips a conditional rule on its own host", () => {
		const { prefs, env } = apply("win", {}, {}, "win");
		expect("example.cross-os-feature" in prefs).toBe(false);
		expect(env).toEqual({});
	});

	it("lets the caller's pref and environment win", () => {
		const { prefs, env } = apply(
			"win",
			{
				"browser.sessionhistory.max_entries": 50,
				"example.cross-os-feature": false,
			},
			{
				EXAMPLE_CROSS_OS: "0",
				EXAMPLE_LIB: "/elsewhere.dll",
				EXAMPLE_ARCH: "ampere",
			},
			"lin",
			BUILD_EXE,
			{ "example:arch": "blackwell" },
		);
		expect(prefs).toEqual({
			"browser.sessionhistory.max_entries": 50,
			"example.cross-os-feature": false,
		});
		expect(env).toEqual({
			EXAMPLE_CROSS_OS: "0",
			EXAMPLE_LIB: "/elsewhere.dll",
			EXAMPLE_ARCH: "ampere",
		});
	});

	it("gives a build without launch.json nothing", () => {
		const { prefs, env } = apply(
			"win",
			{},
			{},
			"lin",
			path.join(BUNDLE, "camoufox-bin"),
		);
		expect(prefs).toEqual({});
		expect(env).toEqual({});
	});
});

const EXCLUSIVE_RULES = {
	rules: [
		{
			target: ["win"],
			host: ["lin"],
			exclusive: true,
			envPaths: { EXAMPLE_DLL: "lib/example.dll" },
		},
		{
			target: ["win"],
			host: ["mac"],
			warn: "example.dll cannot load on a macOS host",
		},
	],
};
const EXCLUSIVE_BUILD = path.join(SCRATCH, "exclusive-build");
const EXCLUSIVE_EXE = path.join(EXCLUSIVE_BUILD, "camoufox-bin");
const EXCLUSIVE_DLL = path.join(EXCLUSIVE_BUILD, "lib", "example.dll");
const OSES = ["win", "mac", "lin"] as const;

/**
 * A feature that must exist on exactly one target/host pairing: here, a
 * Windows identity on a Linux host. The rule is `exclusive`, so the variable
 * cannot reach another pairing through the caller's environment, and the one
 * host that cannot support it is warned instead of degrading quietly.
 */
describe("exclusive rule, every target/host pairing", () => {
	beforeAll(() => {
		fs.cpSync(BUNDLE, EXCLUSIVE_BUILD, { recursive: true });
		fs.writeFileSync(
			path.join(EXCLUSIVE_BUILD, "launch.json"),
			JSON.stringify(EXCLUSIVE_RULES),
		);
		fs.mkdirSync(path.dirname(EXCLUSIVE_DLL), { recursive: true });
		fs.writeFileSync(EXCLUSIVE_DLL, "MZ");
	});

	for (const target of OSES) {
		for (const host of OSES) {
			const wanted = target === "win" && host === "lin";
			it(`${target} identity on a ${host} host ${wanted ? "sets" : "does not set"} the variable`, async () => {
				const { warnings: caught, result } = await warnings.recordWarnings(() =>
					apply(target, {}, {}, host, EXCLUSIVE_EXE),
				);
				expect(result.env).toEqual(
					wanted ? { EXAMPLE_DLL: EXCLUSIVE_DLL } : {},
				);
				expect(caught.map((w) => w.message)).toEqual(
					target === "win" && host === "mac"
						? ["example.dll cannot load on a macOS host"]
						: [],
				);
			});

			it(`${target} identity on a ${host} host ${wanted ? "keeps" : "strips"} an inherited variable`, async () => {
				const { result } = await warnings.recordWarnings(() =>
					apply(
						target,
						{},
						{ EXAMPLE_DLL: "/inherited.dll", UNRELATED: "1" },
						host,
						EXCLUSIVE_EXE,
					),
				);
				expect(result.env).toEqual(
					wanted
						? { EXAMPLE_DLL: "/inherited.dll", UNRELATED: "1" }
						: { UNRELATED: "1" },
				);
			});
		}
	}
});

describe("property floor", () => {
	it("lifts a value below min", () => {
		const config: Record<string, any> = { "window.history.length": 1 };
		utils.validateConfig(config, BUILD_EXE);
		expect(config).toEqual({ "window.history.length": 2 });
	});

	it("keeps a value at or above min", () => {
		const config: Record<string, any> = { "window.history.length": 4 };
		utils.validateConfig(config, BUILD_EXE);
		expect(config).toEqual({ "window.history.length": 4 });
	});

	it("leaves a property without min untouched", () => {
		const config: Record<string, any> = { "screen.width": 0 };
		utils.validateConfig(config, BUILD_EXE);
		expect(config).toEqual({ "screen.width": 0 });
	});
});

describe.runIf(modelReady)(
	"launchOptions with a build that declares rules",
	() => {
		it("applies the supplied build's rules and floors", async () => {
			stubHost();
			deps.hostOsKey = () => "lin";
			const opts = await quietly(() =>
				utils.launchOptions({
					env: { HOME },
					executable_path: BUILD_EXE,
					os: "windows",
					headless: true,
					i_know_what_im_doing: true,
					config: { "window.history.length": 1 },
					firefox_user_prefs: { "example.cross-os-feature": false },
				}),
			);
			expect(opts.firefoxUserPrefs["browser.sessionhistory.max_entries"]).toBe(
				10,
			);
			expect(opts.firefoxUserPrefs["example.cross-os-feature"]).toBe(false);
			expect(opts.env.EXAMPLE_CROSS_OS).toBe("1");
			expect(opts.env.EXAMPLE_LIB).toBe(LIB);
			expect(configOf(opts)["window.history.length"]).toBe(2);
		});
	},
);

describe("context screen avail rect", () => {
	it("passes the avail rect with the size", () => {
		const script = fingerprints.buildInitScript({
			screenWidth: 1920,
			screenHeight: 1080,
			screenAvailWidth: 1920,
			screenAvailHeight: 1040,
		});
		expect(script).toContain("w.setScreenDimensions(1920, 1080, 1920, 1040);");
	});

	it("passes the size alone without an avail rect", () => {
		const script = fingerprints.buildInitScript({
			screenWidth: 1920,
			screenHeight: 1080,
		});
		expect(script).toContain("w.setScreenDimensions(1920, 1080);");
	});
});
