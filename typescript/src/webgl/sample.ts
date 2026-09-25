/**
 * Weighted sampling of real WebGL vendor/renderer fingerprints.
 *
 * TypeScript twin of python/src/webgl/sample.py. Python reads a SQLite
 * table; the same rows are shipped here as data-files/webgl_data.json (one
 * object per row) so the package stays dependency-free.
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { LOCAL_DATA, OS_ARCH_MATRIX } from "../pkgman.js";

const DATA_PATH = path.join(LOCAL_DATA, "webgl_data.json");

export type TargetOS = "win" | "mac" | "lin";

export interface WebGLData {
	"webGl:vendor": string;
	"webGl:renderer": string;
	webGl2Enabled?: boolean;
	[key: string]: any;
}

interface WebGLRecord {
	vendor: string;
	renderer: string;
	win: number;
	mac: number;
	lin: number;
	data: WebGLData;
}

let records: WebGLRecord[] | null = null;

async function loadRecords(): Promise<WebGLRecord[]> {
	if (!records) {
		records = JSON.parse(await fs.readFile(DATA_PATH, "utf-8"));
	}
	return records as WebGLRecord[];
}

/**
 * Sample a random WebGL vendor/renderer combination and its data based on OS
 * probabilities. Optionally use a specific vendor/renderer pair.
 *
 * @throws when the OS is invalid or no data matches the OS/vendor/renderer.
 */
export async function sampleWebGL(
	os: TargetOS,
	vendor?: string,
	renderer?: string,
): Promise<WebGLData> {
	if (!OS_ARCH_MATRIX[os]) {
		throw new Error(`Invalid OS: ${os}. Must be one of: win, mac, lin`);
	}

	const all = await loadRecords();

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
			const pairs = all.filter((row) => row[os] > 0);
			throw new Error(
				`Vendor "${vendor}" and renderer "${renderer}" combination not valid for ${os}.\n` +
					`Possible pairs: ${pairs
						.map((pair) => `(${pair.vendor}, ${pair.renderer})`)
						.join(", ")}`,
			);
		}
		return { ...match.data };
	}

	const rows = all.filter((row) => row[os] > 0);
	if (rows.length === 0) {
		throw new Error(`No WebGL data found for OS: ${os}`);
	}

	// Normalize, then walk the cumulative distribution (np.random.choice(p=...)).
	const probs = rows.map((row) => row[os]);
	const total = probs.reduce((a, b) => a + b, 0);
	const weights = probs.map((p) => p / total);
	const threshold = Math.random() * weights.reduce((acc, w) => acc + w, 0);
	let cumulative = 0;
	let idx = weights.length - 1; // Fallback in case of rounding errors
	for (let i = 0; i < weights.length; i++) {
		cumulative += weights[i];
		if (cumulative >= threshold) {
			idx = i;
			break;
		}
	}

	return { ...rows[idx].data };
}

export interface VendorRenderer {
	vendor: string;
	renderer: string;
}

/**
 * Get all possible (vendor, renderer) pairs per OS where the probability is
 * greater than zero.
 */
export async function getPossiblePairs(): Promise<
	Record<string, VendorRenderer[]>
> {
	const all = await loadRecords();
	const result: Record<string, VendorRenderer[]> = {};

	for (const osType of Object.keys(OS_ARCH_MATRIX) as TargetOS[]) {
		result[osType] = all
			.filter((row) => row[osType] > 0)
			.sort((a, b) => b[osType] - a[osType])
			.map(({ vendor, renderer }) => ({ vendor, renderer }));
	}

	return result;
}
