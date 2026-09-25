/**
 * The identity data files ship in the npm tarball (src/data-files ->
 * dist/data-files) and must be the SAME files pythonlib ships, or one seed
 * would draw different fonts / voices / devices / GPUs in the two launchers.
 * scripts/sync-identity-data.py copies them; this test fails when a copy
 * drifts. (webgl_data.json is an export of webgl_data.db, checked row by row
 * against the database in identity-golden.test.ts.)
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { LOCAL_DATA } from "../src/pkgman.js";
import { prerequisite } from "./prereq.js";

const PYTHONLIB = path.resolve(
	path.dirname(fileURLToPath(import.meta.url)),
	"..",
	"..",
	"pythonlib",
	"camoufox",
);

const SHARED = [
	"fonts.json",
	"font-bases.json",
	"font-groups.json",
	"voices.json",
	"voice-manifests.json",
	"voice-uris.json",
	"media-devices.json",
	"fingerprint-presets.json",
	"fingerprint-presets-v150.json",
];

describe.skipIf(
	!prerequisite("pythonlib-source", fs.existsSync(PYTHONLIB), PYTHONLIB),
)("identity data files", () => {
	for (const name of SHARED) {
		it(`${name} is byte-identical to pythonlib's`, () => {
			const ours = fs.readFileSync(path.join(LOCAL_DATA, name));
			const theirs = fs.readFileSync(path.join(PYTHONLIB, name));
			expect(
				ours.equals(theirs),
				`${name} drifted -- run .venv/bin/python typescript/scripts/sync-identity-data.py`,
			).toBe(true);
		});
	}
});
