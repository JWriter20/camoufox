/**
 * What fonts are actually installed on THIS machine.
 *
 * TypeScript twin of pythonlib/camoufox/fontprobe.py.
 *
 * Camoufox ships its own font bundle and spoofs the claimed OS's font list from
 * it, so nothing in a normal launch depends on the host's fonts. This module is
 * the diagnostic half: it answers "what does this machine really have", which
 * is what a user needs to know when deciding whether a claimed identity is
 * plausible here.
 *
 * Enumeration is per platform because the authoritative list is:
 *
 *     Linux     fontconfig (`fc-list`), which is what Gecko itself asks
 *     Windows   the font registry, plus the per-user font directory that
 *               Windows 10 1809 and later install into without touching HKLM
 *     macOS     the three font directories CoreText reads
 *
 * The result is cached under the Camoufox cache directory and invalidated by
 * the (path, mtime, size) of every directory scanned, so a call after the first
 * costs a handful of stats. The cache file and its format are the Python
 * twin's, so the two launchers share it.
 *
 * The Python fallback reads name tables with fontTools; here a minimal sfnt
 * reader (TrueType/OpenType and their collections) does the same job. .dfont
 * and .pfb files carry no sfnt name table it can read and are skipped.
 */
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { INSTALL_DIR } from "./paths.js";

export const CACHE_VERSION = 1;
export const FONT_EXT = [".ttf", ".otf", ".ttc", ".otc", ".dfont", ".pfb"];

/** fc-list can be slow on a machine with thousands of fonts, and a launch
 *  should never hang on it. */
export const PROBE_TIMEOUT_S = 20;

export type HostOS = "linux" | "macos" | "windows";

/** 'linux', 'macos' or 'windows' -- the keys the font data is filed under. */
export function hostOs(): HostOS {
	return (
		({ linux: "linux", darwin: "macos", win32: "windows" } as const)[
			process.platform as "linux" | "darwin" | "win32"
		] ?? "linux"
	);
}

/**
 * The form two family names are compared in.
 *
 * Case and surrounding space only. Nothing clever: "Segoe UI" and
 * "Segoe UI Semibold" are different families to DirectWrite and must stay
 * different here, or a host with one would be credited with the other.
 */
export function normalise(name: string): string {
	return name.split(/\s+/).filter(Boolean).join(" ").toLowerCase();
}

// --------------------------------------------------------------------------
// per-platform enumeration
// --------------------------------------------------------------------------

function isDir(p: string): boolean {
	try {
		return fs.statSync(p).isDirectory();
	} catch {
		return false;
	}
}

function linuxFontDirs(): string[] {
	const home = os.homedir();
	return [
		"/usr/share/fonts",
		"/usr/local/share/fonts",
		path.join(home, ".local/share/fonts"),
		path.join(home, ".fonts"),
	].filter(isDir);
}

function windowsFontDirs(): string[] {
	const dirs = [path.join(process.env.WINDIR ?? "C:\\Windows", "Fonts")];
	const local = process.env.LOCALAPPDATA;
	if (local) dirs.push(path.join(local, "Microsoft", "Windows", "Fonts"));
	return dirs.filter(isDir);
}

function macosFontDirs(): string[] {
	return [
		"/System/Library/Fonts",
		"/Library/Fonts",
		path.join(os.homedir(), "Library/Fonts"),
		"/System/Library/Fonts/Supplemental",
	].filter(isDir);
}

export function fontDirs(): string[] {
	return {
		linux: linuxFontDirs,
		windows: windowsFontDirs,
		macos: macosFontDirs,
	}[hostOs()]();
}

/**
 * Ask fontconfig, which is the same source Gecko uses on Linux.
 *
 * Deliberately run with the caller's own environment stripped of Camoufox's
 * FONTCONFIG_FILE: that variable points at the bundle, and the question here
 * is what the HOST has.
 */
export function fromFcList(): Set<string> | null {
	const env = { ...process.env };
	delete env.FONTCONFIG_FILE;
	delete env.FONTCONFIG_PATH;
	const out = spawnSync("fc-list", ["--format", "%{family}\\n"], {
		env,
		timeout: PROBE_TIMEOUT_S * 1000,
	});
	if (out.error || out.status !== 0 || !out.stdout) return null;
	const families = new Set<string>();
	for (const line of out.stdout.toString("utf-8").split(/\r?\n/)) {
		// fontconfig prints every alias of a family, comma separated.
		for (const raw of line.split(",")) {
			const name = raw.trim();
			if (name) families.add(name);
		}
	}
	return families.size ? families : null;
}

const REG_FONT_KEYS = [
	"HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts",
	"HKCU\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts",
];

/** Parse `reg query` output into the value names it lists. */
export function parseRegQuery(output: string): string[] {
	const names: string[] = [];
	for (const line of output.split(/\r?\n/)) {
		const m = /^ {4}(.+?) {4}REG_[A-Z_]+( {4}.*)?$/.exec(line);
		if (m) names.push(m[1]);
	}
	return names;
}

/** Registry value names ("Segoe UI Bold (TrueType)", "MS Gothic & MS PGothic
 *  & MS UI Gothic (TrueType)") to family names. */
export function familiesFromRegistryNames(
	names: Iterable<string>,
): Set<string> {
	const families = new Set<string>();
	for (const name of names) {
		for (const raw of name.split("(")[0].split("&")) {
			const part = raw.trim();
			if (part) families.add(part);
		}
	}
	return families;
}

/** The font registry: what GDI and DirectWrite enumerate. */
export function fromWindowsRegistry(): Set<string> | null {
	if (process.platform !== "win32") return null;
	const names: string[] = [];
	for (const key of REG_FONT_KEYS) {
		const out = spawnSync("reg", ["query", key], {
			timeout: PROBE_TIMEOUT_S * 1000,
			windowsHide: true,
		});
		if (out.error || out.status !== 0 || !out.stdout) continue;
		names.push(...parseRegQuery(out.stdout.toString("utf-8")));
	}
	const families = familiesFromRegistryNames(names);
	return families.size ? families : null;
}

// --------------------------------------------------------------------------
// minimal sfnt name-table reader (the fontTools fallback)
// --------------------------------------------------------------------------

function decodeUtf16BE(buf: Buffer): string {
	const swapped = Buffer.from(buf);
	swapped.swap16();
	return swapped.toString("utf16le");
}

const MAC_ROMAN = new TextDecoder("macintosh");

/** The names of one sfnt font at `offset`, as fontTools' getDebugName(). */
function sfntNames(buf: Buffer, offset: number, nameIds: number[]): string[] {
	const numTables = buf.readUInt16BE(offset + 4);
	let nameOffset = -1;
	for (let i = 0; i < numTables; i++) {
		const rec = offset + 12 + i * 16;
		if (buf.toString("latin1", rec, rec + 4) === "name") {
			nameOffset = buf.readUInt32BE(rec + 8);
			break;
		}
	}
	if (nameOffset < 0) return [];
	const count = buf.readUInt16BE(nameOffset + 2);
	const storage = nameOffset + buf.readUInt16BE(nameOffset + 4);
	type Rec = {
		platform: number;
		encoding: number;
		lang: number;
		id: number;
		value: Buffer;
	};
	const records: Rec[] = [];
	for (let i = 0; i < count; i++) {
		const r = nameOffset + 6 + i * 12;
		const len = buf.readUInt16BE(r + 8);
		const off = storage + buf.readUInt16BE(r + 10);
		records.push({
			platform: buf.readUInt16BE(r),
			encoding: buf.readUInt16BE(r + 2),
			lang: buf.readUInt16BE(r + 4),
			id: buf.readUInt16BE(r + 6),
			value: buf.subarray(off, off + len),
		});
	}
	const decode = (rec: Rec): string | null => {
		if (
			rec.platform === 0 ||
			(rec.platform === 3 && [0, 1, 10].includes(rec.encoding))
		) {
			return decodeUtf16BE(rec.value);
		}
		if (rec.platform === 1 && rec.encoding === 0) {
			return MAC_ROMAN.decode(rec.value);
		}
		return null; // an encoding this reader does not carry
	};
	// fontTools' getDebugName(): the first English record (Mac 1/0 or
	// Windows 3/0x409) in table order, else the last decodable one.
	const out: string[] = [];
	for (const id of nameIds) {
		let someName: string | null = null;
		let englishName: string | null = null;
		for (const rec of records) {
			if (rec.id !== id) continue;
			const value = decode(rec);
			if (value === null) continue;
			someName = value;
			if (
				(rec.platform === 1 && rec.lang === 0) ||
				(rec.platform === 3 && rec.lang === 0x409)
			) {
				englishName = value;
				break;
			}
		}
		const name = englishName || someName;
		if (name) out.push(name);
	}
	return out;
}

/** Family names (nameID 16, then 1) of every font in a font file. */
export function fontFileFamilies(file: string): string[] {
	const buf = fs.readFileSync(file);
	const offsets: number[] = [];
	if (buf.toString("latin1", 0, 4) === "ttcf") {
		const n = buf.readUInt32BE(8);
		for (let i = 0; i < n; i++) offsets.push(buf.readUInt32BE(12 + i * 4));
	} else {
		offsets.push(0);
	}
	const names: string[] = [];
	for (const offset of offsets) {
		try {
			names.push(...sfntNames(buf, offset, [16, 1]));
		} catch {
			// A malformed face: skip it, as the Python twin does.
		}
	}
	return names;
}

/** Read the name table of every font file. The portable fallback. */
export function fromFontFiles(dirs: Iterable<string>, limit = 0): Set<string> {
	const families = new Set<string>();
	let seen = 0;
	const walk = (dir: string): boolean => {
		let entries: fs.Dirent[];
		try {
			entries = fs.readdirSync(dir, { withFileTypes: true });
		} catch {
			return true;
		}
		const files = entries
			.filter((e) => !e.isDirectory())
			.map((e) => e.name)
			.sort();
		for (const name of files) {
			const lower = name.toLowerCase();
			if (!FONT_EXT.some((ext) => lower.endsWith(ext))) continue;
			seen += 1;
			if (limit && seen > limit) return false;
			if (lower.endsWith(".dfont") || lower.endsWith(".pfb")) continue;
			// Parity with the Python twin, which reads nothing from a collection:
			// it closes the TTCollection before reading the fonts' name tables,
			// so every lookup fails and is swallowed. fontFileFamilies() can
			// read collections; this fallback deliberately does not, so the
			// two launchers report the same inventory.
			if (lower.endsWith(".ttc") || lower.endsWith(".otc")) continue;
			try {
				for (const value of fontFileFamilies(path.join(dir, name))) {
					families.add(value.trim());
				}
			} catch {
				// unreadable file
			}
		}
		// os.walk: top-down, not following symlinked directories.
		for (const e of entries) {
			if (e.isDirectory() && !walk(path.join(dir, e.name))) return false;
		}
		return true;
	};
	for (const dir of dirs) {
		if (!walk(dir)) break;
	}
	return families;
}

// --------------------------------------------------------------------------
// the cached inventory
// --------------------------------------------------------------------------

/**
 * Cheap invalidation: the directories and their mtimes.
 *
 * Installing or removing a font changes the mtime of the directory it is in,
 * which is enough -- and costs four stats instead of reading 3000 name tables.
 */
export function signature(dirs: Iterable<string>): string {
	const parts: string[] = [];
	for (const directory of [...dirs].sort()) {
		try {
			const stat = fs.statSync(directory, { bigint: true });
			parts.push(`${directory}:${stat.mtimeNs / 1_000_000_000n}:${stat.size}`);
		} catch {
			parts.push(`${directory}:-`);
		}
	}
	return parts.join("|");
}

function cachePath(): string | null {
	const base = INSTALL_DIR;
	try {
		fs.mkdirSync(path.join(base, "fontcache"), { recursive: true });
	} catch {
		return null;
	}
	return path.join(base, "fontcache", "host-fonts.json");
}

export interface FontInventory {
	version: number;
	signature: string;
	os: HostOS;
	source: "fontconfig" | "registry" | "files";
	dirs: string[];
	families: string[];
	seconds: number;
	cached: boolean;
}

/** Python's json.dump() layout (", " / ": ", ASCII-escaped), so the cache file
 *  the two launchers share is byte-for-byte what either one writes. */
function pyJsonDumps(value: unknown): string {
	if (value === null || value === undefined) return "null";
	if (typeof value === "boolean") return value ? "true" : "false";
	if (typeof value === "number") return String(value);
	if (typeof value === "string") {
		return JSON.stringify(value).replace(
			/[\u0080-\uffff]/g,
			(c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
		);
	}
	if (Array.isArray(value)) return `[${value.map(pyJsonDumps).join(", ")}]`;
	return `{${Object.entries(value as Record<string, unknown>)
		.map(([k, v]) => `${pyJsonDumps(k)}: ${pyJsonDumps(v)}`)
		.join(", ")}}`;
}

/**
 * Every font family this machine has, with where the answer came from.
 */
export function installedFamilies(
	refresh = false,
	useCache = true,
): FontInventory {
	const dirs = fontDirs();
	const sig = signature(dirs);
	const file = useCache ? cachePath() : null;

	if (file && !refresh && fs.existsSync(file)) {
		try {
			const cached = JSON.parse(fs.readFileSync(file, "utf-8"));
			if (cached.version === CACHE_VERSION && cached.signature === sig) {
				cached.cached = true;
				return cached;
			}
		} catch {
			// unreadable or corrupt cache: rebuild it
		}
	}

	const started = Date.now();
	let families: Set<string> | null = null;
	let source: FontInventory["source"] = "files";
	const system = hostOs();
	if (system === "linux") {
		families = fromFcList();
		source = "fontconfig";
	} else if (system === "windows") {
		families = fromWindowsRegistry();
		source = "registry";
	}
	if (!families?.size) {
		families = fromFontFiles(dirs);
		source = "files";
	}

	const seconds = Math.round(Date.now() - started) / 1000;
	const result: FontInventory = {
		version: CACHE_VERSION,
		signature: sig,
		os: system,
		source,
		dirs,
		families: [...families].sort(),
		seconds,
		cached: false,
	};
	if (file) {
		try {
			const tmp = `${file}.tmp${process.pid}`;
			// Python writes seconds as a float ("0.0", not "0").
			const text = pyJsonDumps(result).replace(
				/"seconds": (-?\d+)(,|})/,
				'"seconds": $1.0$2',
			);
			fs.writeFileSync(tmp, text);
			fs.renameSync(tmp, file);
		} catch {
			// cache is best-effort
		}
	}
	return result;
}

/** The installed families, normalised for comparison. */
export function familyIndex(inventory?: FontInventory): Set<string> {
	const inv = inventory ?? installedFamilies();
	return new Set((inv.families ?? []).map(normalise));
}

/** Split the families an identity claims into [on this host, not on it]. */
export function partition(
	claimed: Iterable<string>,
	inventory?: FontInventory,
): [string[], string[]] {
	const index = familyIndex(inventory);
	const real: string[] = [];
	const missing: string[] = [];
	for (const name of claimed) {
		(index.has(normalise(name)) ? real : missing).push(name);
	}
	return [real, missing];
}
