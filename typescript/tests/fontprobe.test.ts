/**
 * fontprobe.ts against pythonlib/camoufox/fontprobe.py.
 */
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
	familiesFromRegistryNames,
	fontFileFamilies,
	fromFontFiles,
	normalise,
	parseRegQuery,
	partition,
	signature,
} from "../src/fontprobe.js";

const PY = path.join(import.meta.dirname, "../../.venv/bin/python");

describe("pure helpers", () => {
	it("normalises case and whitespace only", () => {
		expect(normalise("  Segoe   UI\tSemibold ")).toBe("segoe ui semibold");
	});

	it("splits registry value names into families", () => {
		const out = parseRegQuery(
			[
				"",
				"HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts",
				"    Segoe UI Bold (TrueType)    REG_SZ    segoeuib.ttf",
				"    MS Gothic & MS PGothic & MS UI Gothic (TrueType)    REG_SZ    msgothic.ttc",
				"",
			].join("\r\n"),
		);
		expect([...familiesFromRegistryNames(out)].sort()).toEqual([
			"MS Gothic",
			"MS PGothic",
			"MS UI Gothic",
			"Segoe UI Bold",
		]);
	});

	it("partitions claimed families against an inventory", () => {
		const inv = {
			version: 1,
			signature: "",
			os: "linux" as const,
			source: "files" as const,
			dirs: [],
			families: ["DejaVu Sans", "Arial"],
			seconds: 0,
			cached: false,
		};
		expect(partition(["arial", "Helvetica", " DejaVu  Sans"], inv)).toEqual([
			["arial", " DejaVu  Sans"],
			["Helvetica"],
		]);
	});

	it("signs directories as path:mtime:size, sorted", () => {
		const dir = fs.mkdtempSync(path.join(os.tmpdir(), "camoufox-sig-"));
		try {
			const st = fs.statSync(dir);
			expect(signature([dir, "/nonexistent-dir"])).toBe(
				`/nonexistent-dir:-|${dir}:${Math.trunc(st.mtimeMs / 1000)}:${st.size}`,
			);
		} finally {
			fs.rmSync(dir, { recursive: true, force: true });
		}
	});
});

// The host's own fonts, read two ways. fontTools is the Python reference.
const bundledFonts = path.join(
	os.homedir(),
	".cache/camoufox/browsers/local/152.0.4-beta.31-frompatches-20260914/fonts",
);
// Whichever interpreter has fontTools (the venv may not carry it).
const FONTTOOLS_PY = [PY, "python3"].find((py) => {
	try {
		execFileSync(py, ["-c", "import fontTools"], { stdio: "ignore" });
		return true;
	} catch {
		return false;
	}
});
const hasFontTools = FONTTOOLS_PY !== undefined;
// Load fontprobe.py by path: it is stdlib-only, but the camoufox package's
// __init__ pulls in Playwright.
const LOAD_FONTPROBE = `
import importlib.util
spec = importlib.util.spec_from_file_location("fontprobe", ${JSON.stringify(path.join(import.meta.dirname, "../../pythonlib/camoufox/fontprobe.py"))})
fontprobe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fontprobe)
`;

describe.skipIf(!hasFontTools || !fs.existsSync(bundledFonts))(
	"name-table reader vs fontTools",
	() => {
		it("reads the same families as the Python fallback", () => {
			const dir = path.join(bundledFonts, "linux");
			const script = `${LOAD_FONTPROBE}
import json
print(json.dumps(sorted(fontprobe._from_font_files([${JSON.stringify(dir)}]))))
`;
			const expected = JSON.parse(
				execFileSync(FONTTOOLS_PY as string, ["-c", script], {
					encoding: "utf-8",
					maxBuffer: 64 * 1024 * 1024,
				}),
			);
			expect(expected.length).toBeGreaterThan(10);
			expect([...fromFontFiles([dir])].sort()).toEqual(expected);
		}, 60_000);
	},
);

describe("installedFamilies cache", () => {
	let tmp: string;
	let savedXdg: string | undefined;
	beforeEach(() => {
		tmp = fs.mkdtempSync(path.join(os.tmpdir(), "camoufox-fc-"));
		savedXdg = process.env.XDG_CACHE_HOME;
		process.env.XDG_CACHE_HOME = tmp;
		vi.resetModules();
	});
	afterEach(() => {
		if (savedXdg === undefined) delete process.env.XDG_CACHE_HOME;
		else process.env.XDG_CACHE_HOME = savedXdg;
		fs.rmSync(tmp, { recursive: true, force: true });
	});

	it("writes the shared cache file and reads it back", async () => {
		const fp = await import("../src/fontprobe.js");
		const first = fp.installedFamilies();
		expect(first.cached).toBe(false);
		const file = path.join(tmp, "camoufox", "fontcache", "host-fonts.json");
		expect(fs.existsSync(file)).toBe(true);
		// Python json.dump layout.
		expect(fs.readFileSync(file, "utf-8")).toMatch(
			/^\{"version": 1, "signature": /,
		);
		const second = fp.installedFamilies();
		expect(second.cached).toBe(true);
		expect(second.families).toEqual(first.families);
		expect(fp.installedFamilies(true).cached).toBe(false);
	});

	it.skipIf(!fs.existsSync(PY))("is readable by the Python twin", async () => {
		const fp = await import("../src/fontprobe.js");
		const ours = fp.installedFamilies();
		const script = `
import sys, json
sys.path.insert(0, ${JSON.stringify(path.join(import.meta.dirname, "../../pythonlib"))})
from camoufox.fontprobe import installed_families
r = installed_families()
print(json.dumps({"cached": r["cached"], "n": len(r["families"]), "source": r["source"]}))
`;
		const py = JSON.parse(
			execFileSync(PY, ["-c", script], {
				encoding: "utf-8",
				env: { ...process.env, XDG_CACHE_HOME: tmp },
			}),
		);
		expect(py).toEqual({
			cached: true,
			n: ours.families.length,
			source: ours.source,
		});
	});
});

it("fontFileFamilies reads a collection header without throwing on junk", () => {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "camoufox-ff-"));
	try {
		const junk = path.join(dir, "junk.ttf");
		fs.writeFileSync(junk, Buffer.alloc(64));
		expect(fontFileFamilies(junk)).toEqual([]);
	} finally {
		fs.rmSync(dir, { recursive: true, force: true });
	}
});
