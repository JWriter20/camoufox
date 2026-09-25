// Ship every non-TS file the package needs at runtime into dist/:
//   src/data-files/**  (presets, fonts, voices, WebGL, territoryInfo.xml,
//                       repos.yml, warnings.yml, ...)
//   src/fpgen/NOTICE   (Apache-2.0 attribution for the fpgen port)
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const copies = [
	["src/data-files", "dist/data-files"],
	["src/fpgen/NOTICE", "dist/fpgen/NOTICE"],
];
for (const [from, to] of copies) {
	const src = path.join(ROOT, from);
	const dst = path.join(ROOT, to);
	fs.rmSync(dst, { recursive: true, force: true });
	fs.mkdirSync(path.dirname(dst), { recursive: true });
	fs.cpSync(src, dst, { recursive: true });
	console.log(`copied ${from} -> ${to}`);
}
