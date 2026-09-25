/**
 * Locale + geolocation handling.
 *
 * TypeScript twin of python/src/locales.py and
 * python/src/geolocation.py, merged into one module (the two are only
 * ever used together, and the Geolocation type lives on the locale side).
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import tags from "language-tags";
import maxmind, { type Reader } from "maxmind";
import xml2js from "xml2js";
import {
	InvalidLocale,
	NotInstalledGeoIPExtra,
	UnknownIPLocation,
	UnknownLanguage,
	UnknownTerritory,
} from "./exceptions.js";
import { validateIP } from "./ip.js";
import REPOS, { type GeoIPRepoEntry } from "./mappings/repos.config.js";
import { INSTALL_DIR, LOCAL_DATA, rprint, unzip, webdl } from "./pkgman.js";
import { LeakWarning } from "./warnings.js";

/*
 * Data structures for locale and geolocation info
 */

/**
 * Stores locale, region, and script information.
 */
export class Locale {
	constructor(
		public language: string,
		public region?: string,
		public script?: string,
	) {}

	get asString(): string {
		if (this.region) {
			return `${this.language}-${this.region}`;
		}
		return this.language;
	}

	/**
	 * Converts the locale to an intl config object.
	 */
	asConfig(): Record<string, string> {
		if (!this.region) {
			throw new Error("Region is required for config");
		}
		const data: Record<string, string> = {
			"locale:region": this.region,
			"locale:language": this.language,
		};
		if (this.script) {
			data["locale:script"] = this.script;
		}
		return data;
	}
}

/**
 * Stores geolocation information.
 */
export class Geolocation {
	constructor(
		public locale: Locale,
		public longitude: number,
		public latitude: number,
		public timezone: string,
		public accuracy?: number,
	) {}

	asConfig(): Record<string, any> {
		const data: Record<string, any> = {
			"geolocation:longitude": this.longitude,
			"geolocation:latitude": this.latitude,
			timezone: this.timezone,
			...this.locale.asConfig(),
		};
		if (this.accuracy) {
			data["geolocation:accuracy"] = this.accuracy;
		}
		return data;
	}
}

/*
 * Helpers to validate and normalize locales
 */

/**
 * Verifies that a locale is valid. Takes either language-region or language.
 */
export function verifyLocale(loc: string): void {
	if (tags.check(loc)) {
		return;
	}
	throw InvalidLocale.invalidInput(loc);
}

/**
 * Normalizes and validates a locale code.
 */
export function normalizeLocale(locale: string): Locale {
	verifyLocale(locale);

	const parser = tags(locale);
	if (!parser.region()) {
		throw InvalidLocale.invalidInput(locale);
	}

	// Use the EXPLICIT script from the tag, not the language's suppress-script.
	//
	// The Python twin reads `record['Suppress-Script']`, which is the implicit
	// default script -- "Latn" for "en" -- that BCP-47 says to omit. Emitting it
	// produces a candidate like "en-Latn-US", and Firefox's
	// Intl.ComputeDefaultLocale runs that through ICU's available-locale set,
	// which contains "en-US" but not "en-Latn-US". The mismatch falls back to
	// "en", dropping the region: Intl then resolves "en" while
	// navigator.language stays "en-US" -- a detectable mismatch. Keeping only
	// the explicit script preserves real script-bearing locales ("zh-Hans-CN").
	return new Locale(
		parser.language()?.format() ?? "en",
		parser.region()?.format(),
		parser.script()?.format(),
	);
}

/**
 * Handles a locale input, normalizing it if necessary.
 */
export async function handleLocale(
	locale: string,
	ignoreRegion: boolean = false,
): Promise<Locale> {
	// If the caller passed `language-region` or `language-script-region`, normalize it.
	if (locale.length > 3) {
		return normalizeLocale(locale);
	}

	// Case: caller passed a `region` and needs a full locale
	try {
		return await SELECTOR.fromRegion(locale);
	} catch (e) {
		if (!(e instanceof UnknownTerritory)) throw e;
	}

	// Case: caller passed a `language` and doesn't care about the region
	if (ignoreRegion) {
		verifyLocale(locale);
		return new Locale(locale);
	}

	// Case: caller passed a `language` and wants a region
	try {
		const language = await SELECTOR.fromLanguage(locale);
		LeakWarning.warn("no_region");
		return language;
	} catch (e) {
		if (!(e instanceof UnknownLanguage)) throw e;
	}

	// Locale is not in a valid format.
	throw InvalidLocale.invalidInput(locale);
}

/**
 * Handles a list of locales, writing them into the config.
 */
export async function handleLocales(
	locales: string | string[],
	config: Record<string, any>,
): Promise<void> {
	const list =
		typeof locales === "string"
			? locales.split(",").map((loc) => loc.trim())
			: locales;

	// First, handle the leading locale. This is used for the Intl API.
	const intlLocale = (await handleLocale(list[0])).asConfig();
	for (const key of Object.keys(intlLocale)) {
		config[key] = intlLocale[key];
	}

	if (list.length < 2) {
		return;
	}

	// If additional locales were passed, validate them.
	// Note: in this case, we do not need the region.
	const resolved: string[] = [];
	for (const locale of list) {
		resolved.push((await handleLocale(locale, true)).asString);
	}
	config["locale:all"] = joinUnique(resolved);
}

/**
 * Joins a sequence of strings without duplicates.
 */
function joinUnique(seq: string[]): string {
	return [...new Set(seq)].join(", ");
}

/*
 * Gets a random language based on the territory code.
 */

interface TerritoryElement {
	$: Record<string, string>;
	languagePopulation?: Array<{ $: Record<string, string> }>;
}

let unicodeInfo: Promise<TerritoryElement[]> | null = null;

/**
 * Fetches supplemental data from the territoryInfo.xml file.
 * Source: https://raw.githubusercontent.com/unicode-org/cldr/master/common/supplemental/supplementalData.xml
 */
function getUnicodeInfo(): Promise<TerritoryElement[]> {
	if (!unicodeInfo) {
		unicodeInfo = (async () => {
			const data = await fs.promises.readFile(
				path.join(LOCAL_DATA, "territoryInfo.xml"),
			);
			const parsed = await new xml2js.Parser().parseStringPromise(data);
			const territories = parsed?.territoryInfo?.territory;
			if (!territories) {
				throw new Error("Failed to load territoryInfo.xml");
			}
			return territories as TerritoryElement[];
		})();
	}
	return unicodeInfo;
}

function asFloat(attrs: Record<string, string>, attr: string): number {
	return Number.parseFloat(attrs[attr] ?? "0") || 0;
}

/**
 * Selects a random locale based on statistical data. Takes either a territory
 * code or a language code, and generates a Locale.
 */
export class StatisticalLocaleSelector {
	/**
	 * Calculates a random language for a territory code, weighted by the
	 * probability that a person there speaks the language.
	 */
	private async loadTerritoryData(
		isoCode: string,
	): Promise<[string[], number[]]> {
		const territories = await getUnicodeInfo();
		const territory = territories.find((t) => t.$.type === isoCode);
		if (!territory) {
			throw new UnknownTerritory(`Unknown territory: ${isoCode}`);
		}

		const langPopulations = territory.languagePopulation;
		if (!langPopulations?.length) {
			throw new Error(`No language data found for region: ${isoCode}`);
		}

		const languages = langPopulations.map((lang) => lang.$.type);
		const percentages = langPopulations.map((lang) =>
			asFloat(lang.$, "populationPercent"),
		);

		return normalizeProbabilities(languages, percentages);
	}

	/**
	 * Calculates a random region for a language, weighted by the total number
	 * of speakers of that language in the region.
	 */
	private async loadLanguageData(
		language: string,
	): Promise<[string[], number[]]> {
		const territories = (await getUnicodeInfo()).filter((t) =>
			t.languagePopulation?.some((lp) => lp.$.type === language),
		);

		if (!territories.length) {
			throw new UnknownLanguage(
				`No region data found for language: ${language}`,
			);
		}

		const regions: string[] = [];
		const percentages: number[] = [];

		for (const terr of territories) {
			const region = terr.$.type;
			const langPop = terr.languagePopulation?.find(
				(lp) => lp.$.type === language,
			);
			if (!region || !langPop) continue;

			regions.push(region);
			percentages.push(
				((asFloat(langPop.$, "populationPercent") *
					asFloat(terr.$, "literacyPercent")) /
					10_000) *
					asFloat(terr.$, "population"),
			);
		}

		if (!regions.length) {
			throw new Error(`No valid region data found for language: ${language}`);
		}

		return normalizeProbabilities(regions, percentages);
	}

	/**
	 * Get a random locale based on the territory ISO code.
	 */
	async fromRegion(region: string): Promise<Locale> {
		const [languages, probabilities] = await this.loadTerritoryData(region);
		const language = weightedRandomChoice(languages, probabilities).replace(
			"_",
			"-",
		);
		return normalizeLocale(`${language}-${region}`);
	}

	/**
	 * Get a random locale based on the language.
	 */
	async fromLanguage(language: string): Promise<Locale> {
		const [regions, probabilities] = await this.loadLanguageData(language);
		const region = weightedRandomChoice(regions, probabilities);
		return normalizeLocale(`${language}-${region}`);
	}
}

function normalizeProbabilities(
	items: string[],
	freq: number[],
): [string[], number[]] {
	const total = freq.reduce((a, b) => a + b, 0);
	if (total === 0) {
		return [items, items.map(() => 1 / items.length)];
	}
	return [items, freq.map((f) => f / total)];
}

function weightedRandomChoice<T>(items: T[], weights: number[]): T {
	if (items.length === 0) {
		throw new Error("items must not be empty");
	}
	if (items.length !== weights.length) {
		throw new Error("items and weights must have the same length");
	}

	let total = 0;
	for (const w of weights) {
		if (w < 0) throw new Error("weights must be non-negative");
		total += w;
	}
	if (total === 0) {
		return items[Math.floor(Math.random() * items.length)];
	}

	const r = Math.random() * total;
	let acc = 0;
	for (let i = 0; i < items.length; i++) {
		acc += weights[i];
		if (r < acc) return items[i];
	}
	return items[items.length - 1];
}

export const SELECTOR = new StatisticalLocaleSelector();

/*
 * Helpers to fetch geolocation, timezone, and locale data given an IP
 */

export const GEOIP_DIR: string = path.join(INSTALL_DIR, "geoip");
export const MMDB_DIR: string = path.join(GEOIP_DIR, "mmdb");
export const GEOIP_CONFIG: string = path.join(GEOIP_DIR, "config.json");

/**
 * The Python twin gates this on the optional `maxminddb` import (`pip install
 * camoufox[geoip]`). The npm reader is a hard dependency, so geoip is always
 * available here -- the flag stays so `geoipAllowed()` keeps the same shape.
 */
export const ALLOW_GEOIP = true;

/**
 * Resolve a dotted path in a nested object.
 */
function findIn(data: any, key: string): any {
	let cursor = data;
	for (const part of key.split(".")) {
		if (typeof cursor !== "object" || cursor === null) return null;
		cursor = cursor[part];
		if (cursor === undefined || cursor === null) return null;
	}
	return cursor;
}

function validateGeoIPRepo(repo: GeoIPRepoEntry): GeoIPRepoEntry {
	if (!repo.urls) {
		throw new Error(`GeoIP repo '${repo.name}' missing required urls`);
	}
	if (!repo.paths) {
		throw new Error(`GeoIP repo '${repo.name}' missing required paths`);
	}
	return repo;
}

/**
 * Get a GeoIP config by name. When `name` is omitted, uses the default.
 */
export function getGeoIPConfigByName(name?: string): GeoIPRepoEntry {
	const repos = REPOS.geoip;
	const targetName = name ?? REPOS.default.geoip;

	for (const repo of repos) {
		if (repo.name.toLowerCase() === targetName.toLowerCase()) {
			return validateGeoIPRepo(repo);
		}
	}

	if (name) {
		const available = repos.map((r) => r.name);
		throw new Error(
			`GeoIP database '${name}' not found. Available: ${available.join(", ")}`,
		);
	}

	if (repos.length) return validateGeoIPRepo(repos[0]);
	throw new Error("No GeoIP repos configured");
}

/**
 * Load the active GeoIP config from disk, falling back to the default.
 */
export function loadGeoIPConfig(): GeoIPRepoEntry {
	if (fs.existsSync(GEOIP_CONFIG)) {
		let saved: { name?: string } = {};
		try {
			saved = JSON.parse(fs.readFileSync(GEOIP_CONFIG, "utf-8"));
		} catch {
			// Corrupt config: fall through to the default.
		}
		try {
			return getGeoIPConfigByName(saved.name);
		} catch {
			return getGeoIPConfigByName(undefined);
		}
	}
	return getGeoIPConfigByName(undefined);
}

/**
 * Save the active GeoIP source name to disk.
 */
export function saveGeoIPConfig(config: GeoIPRepoEntry): void {
	fs.mkdirSync(GEOIP_DIR, { recursive: true });
	fs.writeFileSync(GEOIP_CONFIG, JSON.stringify({ name: config.name }));
}

/**
 * Path to the mmdb file for the specified IP version.
 */
export function getMmdbPath(
	ipVersion: "ipv4" | "ipv6" = "ipv4",
	config?: GeoIPRepoEntry,
): string {
	const cfg = config ?? loadGeoIPConfig();
	const name = cfg.name.toLowerCase();
	if ("combined" in cfg.urls) {
		return path.join(MMDB_DIR, `${name}-combined.mmdb`);
	}
	return path.join(MMDB_DIR, `${name}-${ipVersion}.mmdb`);
}

/**
 * Checks that a GeoIP database reader is available.
 */
export function geoipAllowed(): void {
	if (!ALLOW_GEOIP) {
		throw new NotInstalledGeoIPExtra(
			"A GeoIP database reader is required to use this feature.",
		);
	}
}

/**
 * Downloads the GeoIP database(s) into geoip/mmdb/.
 */
export async function downloadMMDB(source?: string): Promise<void> {
	geoipAllowed();

	const config = source ? getGeoIPConfigByName(source) : loadGeoIPConfig();
	const name = config.name.toLowerCase();

	fs.mkdirSync(MMDB_DIR, { recursive: true });

	const extract = config.extract ?? false;

	for (const [ipVer, rawUrls] of Object.entries(config.urls)) {
		const urls = typeof rawUrls === "string" ? [rawUrls] : rawUrls;
		const suffix = "combined" in config.urls ? "" : ` (${ipVer})`;
		const desc = `Downloading ${config.name}${suffix}`;
		const mmdbPath = path.join(MMDB_DIR, `${name}-${ipVer}.mmdb`);

		let lastError: unknown;
		let done = false;
		for (const url of urls) {
			const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "camoufox-geoip-"));
			try {
				const buffer = await webdl(url, desc, true);
				if (extract) {
					unzip(buffer, tempDir, `Extracting ${config.name}${suffix}`, false);
					const found = findFirstMmdb(tempDir);
					if (!found) {
						throw new Error("No .mmdb file found in archive");
					}
					fs.copyFileSync(found, mmdbPath);
				} else {
					fs.writeFileSync(mmdbPath, buffer);
				}
				done = true;
				break;
			} catch (error) {
				lastError = error;
			} finally {
				fs.rmSync(tempDir, { recursive: true, force: true });
			}
		}
		if (!done) {
			throw lastError ?? new Error(`Failed to download ${ipVer}`);
		}
	}

	saveGeoIPConfig(config);
}

function findFirstMmdb(dir: string): string | null {
	for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
		const full = path.join(dir, entry.name);
		if (entry.isDirectory()) {
			const nested = findFirstMmdb(full);
			if (nested) return nested;
		} else if (entry.name.endsWith(".mmdb")) {
			return full;
		}
	}
	return null;
}

/**
 * Removes the GeoIP database and config.
 */
export function removeMMDB(): void {
	if (!fs.existsSync(GEOIP_DIR)) {
		rprint("GeoIP database not found.");
		return;
	}
	fs.rmSync(GEOIP_DIR, { recursive: true, force: true });
	rprint("GeoIP database removed.");
}

const UPDATE_AFTER_DAYS = 30;

/**
 * Check if the GeoIP database needs an update (older than 30 days).
 */
export function needsUpdate(config?: GeoIPRepoEntry): boolean {
	const cfg = config ?? loadGeoIPConfig();
	const ipv4Path = getMmdbPath("ipv4", cfg);
	if (!fs.existsSync(ipv4Path)) return true;

	const ageMs = Date.now() - fs.statSync(ipv4Path).mtimeMs;
	return ageMs > UPDATE_AFTER_DAYS * 24 * 60 * 60 * 1000;
}

// maxmind.open() memory-maps the database; keep one reader per file rather
// than re-opening it on every launch.
const readerCache = new Map<string, Promise<Reader<any>>>();

function openReader(mmdbPath: string): Promise<Reader<any>> {
	let reader = readerCache.get(mmdbPath);
	if (!reader) {
		reader = maxmind.open<any>(mmdbPath);
		readerCache.set(mmdbPath, reader);
	}
	return reader;
}

/**
 * Gets the geolocation for an IP address.
 */
export async function getGeolocation(
	ip: string,
	geoipDb?: string,
): Promise<Geolocation> {
	validateIP(ip);
	const ipVersion = ip.includes(":") ? "ipv6" : "ipv4";
	let mmdbPath = getMmdbPath(ipVersion);

	if (!fs.existsSync(mmdbPath) || needsUpdate()) {
		await downloadMMDB();
		mmdbPath = getMmdbPath(ipVersion);
	}

	const config = geoipDb ? getGeoIPConfigByName(geoipDb) : loadGeoIPConfig();
	const paths = config.paths;

	const reader = await openReader(mmdbPath);
	const resp = reader.get(ip);
	if (!resp) {
		throw new UnknownIPLocation(`IP not found in database: ${ip}`);
	}

	const isoCode = findIn(resp, paths.iso_code);
	const longitude = findIn(resp, paths.longitude);
	const latitude = findIn(resp, paths.latitude);
	const timezone = findIn(resp, paths.timezone);

	if (
		isoCode === null ||
		longitude === null ||
		latitude === null ||
		timezone === null
	) {
		throw new UnknownIPLocation(`Unknown IP location: ${ip}`);
	}

	const locale = await SELECTOR.fromRegion(String(isoCode).toUpperCase());

	return new Geolocation(
		locale,
		Number(longitude),
		Number(latitude),
		String(timezone),
	);
}
