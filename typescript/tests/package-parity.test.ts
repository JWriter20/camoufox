/**
 * The package ships what the Python package ships, at the same version.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { LIBRARY_VERSION } from "../src/__version__.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PKG = path.resolve(HERE, "..");
const PYLIB = path.resolve(PKG, "..", "pythonlib");
const DATA = path.join(PKG, "src", "data-files");
const pkg = JSON.parse(
	fs.readFileSync(path.join(PKG, "package.json"), "utf-8"),
);
const pyproject = fs.readFileSync(path.join(PYLIB, "pyproject.toml"), "utf-8");

describe("launcher data files are pythonlib's, byte for byte", () => {
	it.each([
		"territoryInfo.xml",
		"fonts.json",
		"repos.yml",
		"warnings.yml",
	])("%s", (name) => {
		expect(fs.readFileSync(path.join(DATA, name))).toEqual(
			fs.readFileSync(path.join(PYLIB, "camoufox", name)),
		);
	});
});

describe("package.json tracks pyproject.toml", () => {
	it("has the same version, and __version__.ts agrees", () => {
		const version = pyproject.match(/^version = "([^"]+)"/m)?.[1];
		expect(pkg.version).toBe(version);
		expect(LIBRARY_VERSION).toBe(version);
	});

	it("caps playwright-core where pyproject caps playwright", () => {
		const cap = pyproject.match(/^playwright = "([^"]+)"/m)?.[1];
		expect(cap).toBeDefined();
		expect(pkg.peerDependencies["playwright-core"]).toBe(cap);
		const installed = JSON.parse(
			fs.readFileSync(
				path.join(PKG, "node_modules", "playwright-core", "package.json"),
				"utf-8",
			),
		).version as string;
		const [major, minor] = installed.split(".").map(Number);
		const [capMajor, capMinor] = String(cap)
			.replace("<", "")
			.split(".")
			.map(Number);
		expect(major * 1000 + minor).toBeLessThan(capMajor * 1000 + capMinor);
	});

	it("copy-files ships every data file and the fpgen NOTICE", () => {
		const script = fs.readFileSync(
			path.join(PKG, "scripts", "copy-files.mjs"),
			"utf-8",
		);
		expect(script).toContain('"src/data-files"');
		expect(script).toContain('"src/fpgen/NOTICE"');
		expect(fs.existsSync(path.join(PKG, "src", "fpgen", "NOTICE"))).toBe(true);
	});
});
