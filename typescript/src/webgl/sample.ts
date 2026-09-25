/**
 * Weighted sampling of real WebGL vendor/renderer fingerprints.
 *
 * TypeScript twin of pythonlib/camoufox/webgl/sample.py. Python reads the
 * SQLite table webgl_data.db; the same rows, in the same (rowid) order, ship
 * here as data-files/webgl_data.json (scripts/sync-identity-data.py writes it)
 * so the package stays dependency-free. A seeded draw uses the same numpy
 * generator Python does (./nprandom.ts), so one seed draws one device in both.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { gpuFitsOs } from "../coherence.js";
import { LOCAL_DATA } from "../pkgman.js";
import { num, parsePyJson, pyRepr } from "../pycompat.js";
import { NumpyGenerator, npSum } from "./nprandom.js";

export const WEBGL_DATA_PATH = path.join(LOCAL_DATA, "webgl_data.json");

const WEBGL_OSES = ["win", "mac", "lin"] as const;

export type TargetOS = "win" | "mac" | "lin";

export interface WebGLData {
	"webGl:vendor": string;
	"webGl:renderer": string;
	webGl2Enabled?: boolean;
	[key: string]: any;
}

export interface WebGLRecord {
	vendor: string;
	renderer: string;
	win: number;
	mac: number;
	lin: number;
	data: WebGLData;
}

/**
 * Extensions a release Firefox never exposes (draft or mobile-only); a
 * spoofed list naming one is a tell on its own.
 */
export const NEVER_EXPOSED_EXTENSIONS: ReadonlySet<string> = new Set([
	"WEBGL_multi_draw",
	"WEBGL_clip_cull_distance",
	"EXT_texture_norm16",
	"WEBGL_compressed_texture_etc1",
]);

/**
 * Release extensions whose availability depends on the graphics backend:
 * ANGLE's D3D11 backend implements OVR_multiview2 on every Windows GPU, a
 * Linux host driver may not, so it is filtered off Windows only.
 */
export const HOST_DEPENDENT_EXTENSIONS: ReadonlySet<string> = new Set([
	"OVR_multiview2",
]);

export function filteredExtensions(os: string): ReadonlySet<string> {
	if (os === "win") return NEVER_EXPOSED_EXTENSIONS;
	return new Set([...NEVER_EXPOSED_EXTENSIONS, ...HOST_DEPENDENT_EXTENSIONS]);
}

/** A row's data with the extensions this OS never exposes removed (a fresh copy). */
export function loadWebGLData(data: WebGLData | string, os: string): WebGLData {
	const out: WebGLData =
		typeof data === "string" ? parsePyJson(data) : cloneData(data);
	const blocked = filteredExtensions(os);
	for (const key of [
		"webGl:supportedExtensions",
		"webGl2:supportedExtensions",
	]) {
		const exts = out[key];
		if (Array.isArray(exts)) out[key] = exts.filter((e) => !blocked.has(e));
	}
	return out;
}

/** structuredClone, keeping PyFloat instances (structuredClone drops classes). */
function cloneData<T>(value: T): T {
	if (Array.isArray(value)) return value.map(cloneData) as T;
	if (
		value !== null &&
		typeof value === "object" &&
		Object.getPrototypeOf(value) === Object.prototype
	) {
		const out: Record<string, unknown> = {};
		for (const [k, v] of Object.entries(value)) out[k] = cloneData(v);
		return out as T;
	}
	return value;
}

let records: WebGLRecord[] | null = null;

/**
 * Every row of the WebGL table, in rowid order. Parsed with parsePyJson so the
 * one integer past 2**53 in the table (a MAX_SERVER_WAIT_TIMEOUT of
 * 9223372034707292000) stays an exact bigint and reaches CAMOU_CONFIG as the
 * same digits Python writes, not as a float.
 */
export function loadWebGLRecords(): WebGLRecord[] {
	if (!records) {
		const rows = parsePyJson(
			fs.readFileSync(WEBGL_DATA_PATH, "utf-8"),
		) as WebGLRecord[];
		// The per-OS weights are plain probabilities (0.0 parses as a PyFloat).
		records = rows.map((row) => ({
			...row,
			win: num(row.win),
			mac: num(row.mac),
			lin: num(row.lin),
		}));
	}
	return records;
}

function title(s: string): string {
	return s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s;
}

function assertOs(os: string): asserts os is TargetOS {
	if (!(WEBGL_OSES as readonly string[]).includes(os)) {
		throw new Error(`Invalid OS: ${os}. Must be one of: win, mac, lin`);
	}
}

/**
 * Sample a WebGL vendor/renderer combination and its data by the OS's
 * real-world weights, or return the data of a specific vendor/renderer pair.
 * `seed` makes the draw reproducible (numpy's default_rng(seed), as Python).
 *
 * @throws when the OS is invalid or no data matches the OS/vendor/renderer.
 */
export function sampleWebGL(
	os: string,
	vendor?: string | null,
	renderer?: string | null,
	seed?: number | bigint | null,
): WebGLData {
	assertOs(os);
	const all = loadWebGLRecords();

	if (vendor && renderer) {
		const match = all.find(
			(row) => row.vendor === vendor && row.renderer === renderer,
		);
		if (!match) {
			throw new Error(
				`No WebGL data found for vendor "${vendor}" and renderer "${renderer}"`,
			);
		}
		if (match[os] <= 0) {
			const pairs = distinctPairs(all.filter((row) => row[os] > 0));
			throw new Error(
				`Vendor "${vendor}" and renderer "${renderer}" combination not valid for ${title(os)}.\n` +
					`Possible pairs: ${pairs.map((p) => `(${pyRepr(p.vendor)}, ${pyRepr(p.renderer)})`).join(", ")}`,
			);
		}
		return loadWebGLData(match.data, os);
	}

	let rows = all.filter((row) => row[os] > 0);
	if (rows.length === 0) throw new Error(`No WebGL data found for OS: ${os}`);

	// Drop pairs this OS cannot report before sampling (see coherence.gpuFitsOs).
	const coherent = rows.filter((row) => gpuFitsOs(row.renderer, os));
	if (coherent.length) rows = coherent;

	const probs = rows.map((row) => row[os]);
	const total = npSum(probs);
	const normalized = probs.map((p) => p / total);
	const idx = new NumpyGenerator(seed).choiceIndex(normalized);
	return loadWebGLData(rows[idx].data, os);
}

/** SELECT DISTINCT vendor, renderer: first occurrence of each pair, in row order. */
function distinctPairs(rows: WebGLRecord[]): WebGLRecord[] {
	const seen = new Set<string>();
	return rows.filter((row) => {
		const key = JSON.stringify([row.vendor, row.renderer]);
		if (seen.has(key)) return false;
		seen.add(key);
		return true;
	});
}

export interface VendorRenderer {
	vendor: string;
	renderer: string;
}

/**
 * All (vendor, renderer) pairs per OS with a probability above zero, most
 * likely first.
 */
export function getPossiblePairs(): Record<string, VendorRenderer[]> {
	const all = loadWebGLRecords();
	const result: Record<string, VendorRenderer[]> = {};
	for (const osType of WEBGL_OSES) {
		result[osType] = distinctPairs(
			all
				.filter((row) => row[osType] > 0)
				.sort((a, b) => b[osType] - a[osType]),
		).map(({ vendor, renderer }) => ({ vendor, renderer }));
	}
	return result;
}

/** Python name: sample_webgl. */
export const sampleWebgl = sampleWebGL;
