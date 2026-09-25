/**
 * Browser package management: version resolution, GitHub release discovery,
 * download/extract, and path lookup.
 *
 * TypeScript twin of python/src/pkgman.py.
 */
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { Writable } from "node:stream";
import { setTimeout as sleep } from "node:timers/promises";
import AdmZip from "adm-zip";
import cliProgress, { type Options as BarOptions } from "cli-progress";
import prettyBytes from "pretty-bytes";
import { CONSTRAINTS, LIBRARY_VERSION } from "./__version__.js";
import {
	CamoufoxNotInstalled,
	FileNotFoundError,
	MissingRelease,
	ProfileDirectoryError,
	UnsupportedArchitecture,
	UnsupportedOS,
	UnsupportedVersion,
} from "./exceptions.js";
import REPOS, {
	type BrowserRepoEntry,
	type BrowserVersionConstraint,
} from "./mappings/repos.config.js";
// pkgman and multiversion are mutually dependent, exactly as the Python twin's
// function-local imports are. Every use below sits inside a function body, so
// the ESM cycle resolves before any binding is read.
import {
	COMPAT_FLAG,
	getActivePath,
	getDefaultChannel,
	installVersioned,
	loadConfig,
} from "./multiversion.js";
import {
	ARCH_MAP,
	INSTALL_DIR,
	LAUNCH_FILE,
	OS_ARCH_MATRIX,
	OS_MAP,
	OS_NAME,
	rprint,
} from "./paths.js";

// Platform constants and install paths live in paths.ts so that the
// pkgman <-> multiversion cycle never needs them mid-evaluation. Re-exported
// here so pkgman stays the single public entry point for them.
export {
	ARCH_MAP,
	INSTALL_DIR,
	LAUNCH_FILE,
	LOCAL_DATA,
	OS_ARCH_MATRIX,
	OS_MAP,
	OS_NAME,
	rprint,
	userCacheDir,
} from "./paths.js";

function githubHeaders(url: string): Record<string, string> {
	const token = process.env.GITHUB_TOKEN;
	if (!token) return {};
	const host = new URL(url).hostname;
	if (host === "api.github.com" || host === "github.com") {
		return { Authorization: `Bearer ${token}` };
	}
	return {};
}

/**
 * Ensure Firefox's Linux application directory exists before startup.
 *
 * Firefox probes ~/.camoufox even when Playwright supplies a temporary profile.
 * On a read-only HOME -- the normal shape for a container that bakes the bundle
 * in as root and runs as a non-root user -- a missing directory makes startup
 * stall with no diagnostic, surfacing as a launch timeout rather than as a
 * permissions error. An existing directory may itself still be read-only, which
 * is fine: Firefox only needs it to be there.
 */
export function ensureBrowserProfileDir(
	env?: Record<string, unknown>,
): string | undefined {
	if (OS_NAME !== "lin") return undefined;

	const configuredHome = env?.HOME ?? process.env.HOME;
	const home = configuredHome ? String(configuredHome) : os.homedir();
	const profileDir = path.join(home, ".camoufox");
	if (fs.existsSync(profileDir) && fs.statSync(profileDir).isDirectory()) {
		return profileDir;
	}

	try {
		fs.mkdirSync(profileDir, { recursive: true, mode: 0o700 });
	} catch (error) {
		throw new ProfileDirectoryError(
			`Camoufox requires '${profileDir}' to exist before launch, but it could ` +
				"not be created. For a read-only runtime, create this directory " +
				"before making HOME read-only.",
			{ cause: error },
		);
	}

	if (!fs.statSync(profileDir).isDirectory()) {
		throw new ProfileDirectoryError(
			`Camoufox requires '${profileDir}' to be a directory before launch.`,
		);
	}
	return profileDir;
}

/**
 * Parse a semver string into a comparable tuple.
 */
function parseSemver(version: string): number[] {
	const parts = version.replace(/^[\^~]/, "").split(".");
	const out = parts.map((part) => {
		const n = Number.parseInt(part, 10);
		return Number.isNaN(n) ? 0 : n;
	});
	while (out.length < 3) out.push(0);
	return out;
}

function compareTuples(a: number[], b: number[]): number {
	const len = Math.max(a.length, b.length);
	for (let i = 0; i < len; i++) {
		const x = a[i] ?? 0;
		const y = b[i] ?? 0;
		if (x < y) return -1;
		if (x > y) return 1;
	}
	return 0;
}

/**
 * A comparable browser version string (up to 5 parts).
 */
export class Version {
	readonly build: string;
	readonly version?: string;
	readonly sortedRel: number[];

	constructor(build: string, version?: string) {
		this.build = build;
		this.version = version;
		// Mirrors the Python twin: digits stay numeric, a leading letter becomes
		// ord(c) - 1024 so "alpha" < "beta" < numeric builds sort sanely.
		const parts = build
			.split(".")
			.map((x) => (/^\d+$/.test(x) ? Number(x) : x.charCodeAt(0) - 1024));
		const padding = 5 - (build.split(".").length - 1);
		for (let i = 0; i < padding; i++) parts.push(0);
		this.sortedRel = parts;
	}

	get fullString(): string {
		return `${this.version}-${this.build}`;
	}

	/** Whether the build channel is alpha (like "alpha.26"). */
	get isAlpha(): boolean {
		return this.build.split(".")[0].toLowerCase() === "alpha";
	}

	compare(other: Version): number {
		return compareTuples(this.sortedRel, other.sortedRel);
	}

	equals(other: Version): boolean {
		return this.compare(other) === 0;
	}

	lessThan(other: Version): boolean {
		return this.compare(other) < 0;
	}

	isSupported(): boolean {
		return this.compare(VERSION_MIN) >= 0 && this.compare(VERSION_MAX) < 0;
	}

	/**
	 * Read the version from version.json at the given path.
	 */
	static fromPath(dir: string = INSTALL_DIR): Version {
		const versionPath = path.join(dir, "version.json");
		if (!fs.existsSync(versionPath)) {
			throw new FileNotFoundError(
				`Version information not found at ${versionPath}. ` +
					"Please run `camoufox fetch` to install.",
			);
		}
		const data = JSON.parse(fs.readFileSync(versionPath, "utf-8"));
		const build = data.build ?? data.release ?? data.tag;
		return new Version(build, data.version);
	}

	static isSupportedPath(dir: string): boolean {
		return Version.fromPath(dir).compare(VERSION_MIN) >= 0;
	}

	static buildMinMax(): [Version, Version] {
		return [
			new Version(CONSTRAINTS.MIN_VERSION),
			new Version(CONSTRAINTS.MAX_VERSION),
		];
	}
}

export const [VERSION_MIN, VERSION_MAX] = Version.buildMinMax();

/**
 * Find the browser build constraint for the current library version.
 */
function findVersionConstraints(
	versions: BrowserVersionConstraint[],
	libraryVersion: string,
): BrowserVersionConstraint["browser"] | undefined {
	const libParts = parseSemver(libraryVersion);
	let newest: BrowserVersionConstraint["browser"] | undefined;
	let newestMin: number[] | undefined;

	for (const entry of versions) {
		const pyLib = entry.python_library ?? {};
		const libMin = parseSemver(pyLib.min ?? "0");
		const libMax = parseSemver(pyLib.max ?? "999");
		if (
			compareTuples(libMin, libParts) <= 0 &&
			compareTuples(libParts, libMax) < 0
		) {
			return entry.browser;
		}
		if (newestMin === undefined || compareTuples(libMin, newestMin) > 0) {
			newestMin = libMin;
			newest = entry.browser;
		}
	}
	return newest;
}

/**
 * Get the min and max build bounds for a channel.
 */
function channelBounds(
	browser: BrowserVersionConstraint["browser"] | undefined,
	channel: "stable" | "prerelease",
): [string | undefined, string | undefined] {
	if (!browser) return [undefined, undefined];
	if ("stable" in browser || "prerelease" in browser) {
		const section = browser[channel] ?? {};
		return [section.min, section.max];
	}
	return [browser.min, browser.max];
}

/**
 * Configuration for a Camoufox repository.
 */
export class RepoConfig {
	repos: string[];
	name: string;
	pattern: string;
	stableMin?: string;
	stableMax?: string;
	prereleaseMin?: string;
	prereleaseMax?: string;

	constructor(init: {
		repos: string[];
		name: string;
		pattern: string;
		stableMin?: string;
		stableMax?: string;
		prereleaseMin?: string;
		prereleaseMax?: string;
	}) {
		this.repos = init.repos;
		this.name = init.name;
		this.pattern = init.pattern;
		this.stableMin = init.stableMin;
		this.stableMax = init.stableMax;
		this.prereleaseMin = init.prereleaseMin;
		this.prereleaseMax = init.prereleaseMax;
	}

	/** Primary GitHub repo. */
	get repo(): string {
		return this.repos[0];
	}

	static loadRepos(spoofLibraryVersion?: string): RepoConfig[] {
		return REPOS.browsers.map((r) =>
			RepoConfig.fromEntry(r, spoofLibraryVersion),
		);
	}

	static getDefaultName(): string {
		return REPOS.default?.browser ?? "Official";
	}

	static fromEntry(
		entry: BrowserRepoEntry,
		spoofLibraryVersion?: string,
	): RepoConfig {
		if (!entry.pattern) {
			throw new Error(
				`Repo '${entry.name ?? "unknown"}' missing required pattern`,
			);
		}

		let browser: BrowserVersionConstraint["browser"] | undefined;
		if (entry.versions?.length) {
			browser = findVersionConstraints(
				entry.versions,
				spoofLibraryVersion ?? LIBRARY_VERSION,
			);
		}
		const [stableMin, stableMax] = channelBounds(browser, "stable");
		const [prereleaseMin, prereleaseMax] = channelBounds(browser, "prerelease");

		// Parse comma separated repos list (primary + fallbacks)
		const repos = entry.repo.split(",").map((r) => r.trim());

		return new RepoConfig({
			repos,
			name: entry.name,
			pattern: entry.pattern,
			stableMin,
			stableMax,
			prereleaseMin,
			prereleaseMax,
		});
	}

	static getDefault(): RepoConfig {
		const found = RepoConfig.findByName(RepoConfig.getDefaultName());
		return found ?? RepoConfig.loadRepos()[0];
	}

	static findByName(name: string): RepoConfig | undefined {
		const lower = name.toLowerCase();
		return RepoConfig.loadRepos().find((r) => r.name.toLowerCase() === lower);
	}

	getOsName(spoofOs?: string): string {
		if (spoofOs) return spoofOs;
		const osName = OS_MAP[process.platform];
		if (!osName) {
			throw new UnsupportedOS(`OS ${process.platform} is not supported`);
		}
		return osName;
	}

	getArch(spoofArch?: string): string {
		if (spoofArch) return spoofArch;
		const platArch = os.arch().toLowerCase();
		const arch = ARCH_MAP[platArch];
		if (!arch) {
			throw new UnsupportedArchitecture(
				`Architecture ${platArch} is not supported`,
			);
		}
		return arch;
	}

	/**
	 * Build the asset regex from the config pattern string.
	 */
	buildPattern(spoofOs?: string, spoofArch?: string): RegExp {
		const replacements: Record<string, string> = {
			name: "(?<name>\\w+)",
			version: "(?<version>[^-]+)",
			build: "(?<build>[^-]+)",
			os: escapeRegExp(this.getOsName(spoofOs)),
			arch: escapeRegExp(this.getArch(spoofArch)),
		};
		const pattern = this.pattern.replace(/\./g, "\\.");
		const regex = pattern.replace(
			/\{(\w+)\}/g,
			(match, key) => replacements[key] ?? match,
		);
		return new RegExp(`^${regex}`);
	}

	/**
	 * Check if a build is within the supported range for its channel.
	 */
	isVersionSupported(version: Version, isPrerelease: boolean = false): boolean {
		const buildMin = isPrerelease ? this.prereleaseMin : this.stableMin;
		const buildMax = isPrerelease ? this.prereleaseMax : this.stableMax;
		if (buildMin === undefined || buildMax === undefined) {
			return true;
		}
		return (
			new Version(buildMin).compare(version) <= 0 &&
			version.compare(new Version(buildMax)) <= 0
		);
	}
}

function escapeRegExp(value: string): string {
	return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export interface GitHubAsset {
	name: string;
	browser_download_url: string;
	id?: number;
	size?: number;
	updated_at?: string;
	created_at?: string;
	digest?: string;
}

export interface GitHubRelease {
	prerelease?: boolean;
	assets: GitHubAsset[];
}

/**
 * Manages fetching GitHub releases with fallback repos.
 */
export class GitHubDownloader {
	githubRepos: string[];
	githubRepo: string;
	isPrerelease = false;

	constructor(githubRepos: string | string[]) {
		this.githubRepos =
			typeof githubRepos === "string" ? [githubRepos] : githubRepos;
		this.githubRepo = this.githubRepos[0];
	}

	/** Return truthy data if this is the desired asset, else null. */
	checkAsset(asset: GitHubAsset, _release?: GitHubRelease): any {
		return asset.browser_download_url;
	}

	missingAssetError(): never {
		throw new MissingRelease(
			`Could not find a release asset in ${this.githubRepo}.`,
		);
	}

	protected async getReleases(githubRepo: string): Promise<GitHubRelease[]> {
		const apiUrl = `https://api.github.com/repos/${githubRepo}/releases`;
		const response = await fetch(apiUrl, {
			headers: githubHeaders(apiUrl),
			signal: AbortSignal.timeout(20_000),
		});
		if (!response.ok) {
			throw new Error(
				`Failed to fetch releases from ${apiUrl}: ${response.status} ${response.statusText}`,
			);
		}
		return (await response.json()) as GitHubRelease[];
	}

	/**
	 * Fetch the first matching release asset, trying fallback repos on failure.
	 */
	async getAsset(): Promise<any> {
		let lastError: unknown;
		for (const repo of this.githubRepos) {
			try {
				const releases = await this.getReleases(repo);
				for (const release of releases) {
					for (const asset of release.assets ?? []) {
						const data = this.checkAsset(asset, release);
						if (data) {
							this.githubRepo = repo;
							this.isPrerelease = release.prerelease ?? false;
							return data;
						}
					}
				}
			} catch (error) {
				lastError = error;
			}
		}

		if (lastError) throw lastError;
		this.missingAssetError();
	}
}

/**
 * Information about an available Camoufox version on GitHub.
 */
export class AvailableVersion {
	version: Version;
	url: string;
	isPrerelease: boolean;
	assetId?: number;
	assetSize?: number;
	assetUpdatedAt?: string;
	sha256?: string;
	assetCreatedAt?: string;

	constructor(init: {
		version: Version;
		url: string;
		isPrerelease: boolean;
		assetId?: number;
		assetSize?: number;
		assetUpdatedAt?: string;
		sha256?: string;
		assetCreatedAt?: string;
	}) {
		Object.assign(this, init);
		this.version = init.version;
		this.url = init.url;
		this.isPrerelease = init.isPrerelease;
	}

	/** First 8 hex chars of the sha256, or empty when unknown. */
	get sha8(): string {
		return (this.sha256 ?? "").slice(0, 8);
	}

	get display(): string {
		const pre = this.isPrerelease ? " (prerelease)" : "";
		return `v${this.version.fullString}${pre}`;
	}

	toMetadata(): Record<string, any> {
		return {
			version: this.version.version,
			build: this.version.build,
			prerelease: this.isPrerelease,
			asset_id: this.assetId,
			asset_size: this.assetSize,
			asset_updated_at: this.assetUpdatedAt,
			sha256: this.sha256,
			created_at: this.assetCreatedAt,
		};
	}
}

/**
 * Handles fetching and installing Camoufox.
 */
export class CamoufoxFetcher extends GitHubDownloader {
	repoConfig: RepoConfig;
	arch: string;
	pattern: RegExp;
	installedSha256?: string;
	installedCreatedAt?: string;
	_versionObj?: Version;
	_selectedVersion?: AvailableVersion;
	_url?: string;

	constructor(repoConfig?: RepoConfig, selectedVersion?: AvailableVersion) {
		const config = repoConfig ?? RepoConfig.getDefault();
		super(config.repos);
		this.repoConfig = config;
		this.arch = this.getPlatformArch();
		this.pattern = this.repoConfig.buildPattern();

		if (selectedVersion) {
			this._selectedVersion = selectedVersion;
			this._versionObj = selectedVersion.version;
			this._url = selectedVersion.url;
			this.isPrerelease = selectedVersion.isPrerelease;
			this.installedSha256 = selectedVersion.sha256;
			this.installedCreatedAt = selectedVersion.assetCreatedAt;
		}
	}

	/**
	 * The Python constructor calls fetch_latest() inline; downloads are async in
	 * JS, so callers do `await new CamoufoxFetcher().init()` instead.
	 */
	async init(): Promise<this> {
		if (!this._versionObj) {
			await this.fetchLatest();
		}
		return this;
	}

	/** First 8 hex chars of the installed asset sha, or empty. */
	get installedSha8(): string {
		return (this.installedSha256 ?? "").slice(0, 8);
	}

	/**
	 * Match a release asset against version constraints, OS, and arch.
	 */
	checkAsset(
		asset: GitHubAsset,
		release?: GitHubRelease,
	): [Version, string] | null {
		const match = this.pattern.exec(asset.name);
		if (!match?.groups) return null;

		const version = new Version(match.groups.build, match.groups.version);
		const isPrerelease = Boolean(release?.prerelease) || version.isAlpha;
		if (!this.repoConfig.isVersionSupported(version, isPrerelease)) {
			return null;
		}

		const digest = asset.digest ?? "";
		if (digest.startsWith("sha256:")) {
			this.installedSha256 = digest.slice("sha256:".length);
		}
		this.installedCreatedAt = asset.created_at;

		return [version, asset.browser_download_url];
	}

	missingAssetError(): never {
		throw new MissingRelease(
			`No matching release found for ${OS_NAME} ${this.arch} in the ` +
				"supported range. Please update the library.",
		);
	}

	getPlatformArch(): string {
		const arch = (this.repoConfig ?? RepoConfig.getDefault()).getArch();
		if (!OS_ARCH_MATRIX[OS_NAME].includes(arch)) {
			throw new UnsupportedArchitecture(
				`Architecture ${arch} is not supported for ${OS_NAME}`,
			);
		}
		return arch;
	}

	/**
	 * Fetch the latest camoufox release for the current platform.
	 */
	async fetchLatest(): Promise<void> {
		const [versionObj, url] = await this.getAsset();
		this._versionObj = versionObj;
		this._url = url;
	}

	static async downloadFile(file: Writable, url: string): Promise<void> {
		rprint(`Downloading package: ${url}`);
		await webdl(url, "Downloading Camoufox", true, file);
	}

	static cleanup(): boolean {
		if (fs.existsSync(INSTALL_DIR)) {
			rprint(`Cleaning up cache: ${INSTALL_DIR}`);
			fs.rmSync(INSTALL_DIR, { recursive: true, force: true });
			return true;
		}
		return false;
	}

	/**
	 * Download and install camoufox to a versioned subdirectory.
	 */
	async install(replace: boolean = false): Promise<boolean> {
		const installed = await installVersioned(this, replace);
		ensureBrowserProfileDir();
		return installed;
	}

	get url(): string {
		if (!this._url) {
			throw new Error("Url is not available. Make sure to run init() first.");
		}
		return this._url;
	}

	get version(): string {
		if (!this._versionObj?.version) {
			throw new Error(
				"Version is not available. Make sure to run init() first.",
			);
		}
		return this._versionObj.version;
	}

	get build(): string {
		if (!this._versionObj) {
			throw new Error(
				"Build information is not available. Make sure to run init() first.",
			);
		}
		return this._versionObj.build;
	}

	get verstr(): string {
		if (!this._versionObj) {
			throw new Error(
				"Version is not available. Make sure to run init() first.",
			);
		}
		return this._versionObj.fullString;
	}
}

/**
 * Fetch all supported versions from GitHub for the current platform.
 */
export async function listAvailableVersions(
	repoConfig?: RepoConfig,
	includePrerelease: boolean = true,
	spoofOs?: string,
	spoofArch?: string,
): Promise<AvailableVersion[]> {
	const config = repoConfig ?? RepoConfig.getDefault();
	const pattern = config.buildPattern(spoofOs, spoofArch);

	const osName = spoofOs ?? OS_NAME;
	const arch = config.getArch(spoofArch);
	if (!(OS_ARCH_MATRIX[osName] ?? []).includes(arch)) {
		throw new UnsupportedArchitecture(
			`Architecture ${arch} is not supported for ${osName}`,
		);
	}

	let releases: GitHubRelease[] = [];
	let lastError: unknown;
	for (const repo of config.repos) {
		try {
			const apiUrl = `https://api.github.com/repos/${repo}/releases`;
			const resp = await fetch(apiUrl, {
				headers: githubHeaders(apiUrl),
				signal: AbortSignal.timeout(20_000),
			});
			if (!resp.ok) {
				throw new Error(`${resp.status} ${resp.statusText}`);
			}
			releases = (await resp.json()) as GitHubRelease[];
			break;
		} catch (error) {
			lastError = error;
		}
	}
	if (!releases.length && lastError) throw lastError;

	const versions: AvailableVersion[] = [];

	for (const release of releases) {
		const isPrerelease = release.prerelease ?? false;
		if (isPrerelease && !includePrerelease) continue;

		for (const asset of release.assets ?? []) {
			const match = pattern.exec(asset.name);
			if (!match?.groups) continue;

			const version = new Version(match.groups.build, match.groups.version);
			const assetPrerelease = isPrerelease || version.isAlpha;
			if (assetPrerelease && !includePrerelease) continue;
			if (!config.isVersionSupported(version, assetPrerelease)) continue;

			const digest = asset.digest ?? "";
			const sha256 = digest.startsWith("sha256:")
				? digest.slice("sha256:".length)
				: undefined;

			versions.push(
				new AvailableVersion({
					version,
					url: asset.browser_download_url,
					isPrerelease: assetPrerelease,
					assetId: asset.id,
					assetSize: asset.size,
					assetUpdatedAt: asset.updated_at,
					sha256,
					assetCreatedAt: asset.created_at,
				}),
			);
		}
	}

	versions.sort((a, b) => {
		const byVersion = b.version.compare(a.version);
		if (byVersion !== 0) return byVersion;
		return (b.assetCreatedAt ?? "").localeCompare(a.assetCreatedAt ?? "");
	});
	return versions;
}

/**
 * Get the full version string of the active install.
 */
export function installedVerStr(fromDir?: string): string {
	// An explicit directory (the folder holding a sandbox/alt-version
	// executable_path) reads that build's version.json instead of the active
	// install's. Mac-bundle aware: an executable inside
	// Camoufox.app/Contents/MacOS/ belongs to an install whose version.json sits
	// three levels up -- unless a deployment stamped one beside the binary.
	if (fromDir) {
		let dir = fromDir;
		if (
			path.basename(dir) === "MacOS" &&
			!fs.existsSync(path.join(dir, "version.json"))
		) {
			dir = path.dirname(path.dirname(path.dirname(dir)));
		}
		return Version.fromPath(dir).fullString;
	}

	const active = getActivePath();
	if (active === null) {
		const config = loadConfig();
		const pinned = config.pinned;
		const channel = config.channel ?? getDefaultChannel();
		const activeDisplay = pinned ? `${channel}/${pinned}` : channel;
		throw new CamoufoxNotInstalled(
			`${activeDisplay} is not installed. Please run \`camoufox fetch\` to install.`,
		);
	}
	return Version.fromPath(active).fullString;
}

/**
 * Full path to the active camoufox folder.
 *
 * Unlike the Python twin this never downloads: the fetch is asynchronous in JS
 * and this is called from synchronous path helpers. `ensureCamoufoxInstalled()`
 * is the async entry point that installs on demand; `launchOptions()` awaits it
 * before any path lookup, so first-run auto-download behaviour is preserved.
 */
export function camoufoxPath(): string {
	// Clean up incompatible old data directory
	if (
		fs.existsSync(INSTALL_DIR) &&
		fs.readdirSync(INSTALL_DIR).length > 0 &&
		!fs.existsSync(COMPAT_FLAG)
	) {
		rprint("Cleaning old data...", "yellow");
		fs.rmSync(INSTALL_DIR, { recursive: true, force: true });
	}

	const active = getActivePath();
	if (active && Version.fromPath(active).isSupported()) {
		return active;
	}

	if (fs.existsSync(INSTALL_DIR) && fs.readdirSync(INSTALL_DIR).length > 0) {
		try {
			if (Version.fromPath().isSupported()) {
				return INSTALL_DIR;
			}
		} catch {
			// No version.json at the top level: fall through to "not installed".
		}
		throw new UnsupportedVersion("Camoufox executable is outdated.");
	}

	const config = loadConfig();
	const pinned = config.pinned;
	const channel = config.channel ?? getDefaultChannel();
	const activeDisplay = pinned ? `${channel}/${pinned}` : channel;
	throw new CamoufoxNotInstalled(
		`${activeDisplay} is not installed. Please run \`camoufox fetch\` to install.`,
	);
}

/**
 * Resolve the active browser, downloading it first when nothing is installed.
 * The async counterpart to the Python twin's auto-installing camoufox_path().
 */
export async function ensureCamoufoxInstalled(): Promise<string> {
	try {
		return camoufoxPath();
	} catch (error) {
		if (
			!(error instanceof CamoufoxNotInstalled) &&
			!(error instanceof UnsupportedVersion)
		) {
			throw error;
		}
	}
	const fetcher = await new CamoufoxFetcher().init();
	await fetcher.install();
	return camoufoxPath();
}

/**
 * Get the path to a file in the camoufox directory.
 *
 * `baseDir` (the directory of an explicit/sandbox executable_path) resolves
 * resources relative to that build instead of the active install, so a sandbox
 * binary doesn't fall back to the cache dir.
 */
export function getPath(file: string, baseDir?: string): string {
	if (baseDir) {
		// Mac-bundle aware: an executable in Camoufox.app/Contents/MacOS/ keeps
		// its resources under ../Resources/.
		if (path.basename(baseDir) === "MacOS") {
			return path.join(baseDir, "..", "Resources", file);
		}
		return path.join(baseDir, file);
	}
	if (OS_NAME === "mac") {
		return path.resolve(
			camoufoxPath(),
			"Camoufox.app",
			"Contents",
			"Resources",
			file,
		);
	}
	return path.join(camoufoxPath(), file);
}

/**
 * Get the path to the camoufox executable.
 */
export function launchPath(browserPath?: string): string {
	let execPath: string;
	if (browserPath) {
		execPath =
			OS_NAME === "mac"
				? path.resolve(
						browserPath,
						"Camoufox.app",
						"Contents",
						"Resources",
						LAUNCH_FILE[OS_NAME],
					)
				: path.join(browserPath, LAUNCH_FILE[OS_NAME]);
	} else {
		execPath = getPath(LAUNCH_FILE[OS_NAME]);
	}

	if (!fs.existsSync(execPath)) {
		throw new CamoufoxNotInstalled(
			`Camoufox is not installed at ${browserPath ?? camoufoxPath()}. ` +
				"Please run `camoufox fetch` to install.",
		);
	}
	return execPath;
}

const formatBytes = (v: number, _: BarOptions, type: string) =>
	type === "total" || type === "value" ? prettyBytes(v) : String(v);

export type ProgressCallback = (downloaded: number, total: number) => void;

/**
 * Download a file from the given URL. Streams into `buffer` when one is given,
 * otherwise accumulates and returns the bytes.
 */
export async function webdl(
	url: string,
	desc: string = "",
	bar: boolean = true,
	buffer: Writable | null = null,
	{
		retries = 5,
		progressCallback,
	}: { retries?: number; progressCallback?: ProgressCallback } = {},
): Promise<Buffer> {
	let attempts = 0;
	let response: Response | undefined;

	while (attempts < retries) {
		try {
			response = await fetch(url, { headers: githubHeaders(url) });
			if (response.ok) break;
		} catch (e) {
			console.error(e, `retrying (${attempts + 1}/${retries})...`);
			await sleep(5e3);
		}
		attempts++;
	}

	if (!response?.ok) {
		throw new Error(`Failed to download from ${url} after ${retries} attempts`);
	}

	const totalSize = Number.parseInt(
		response.headers.get("content-length") || "0",
		10,
	);
	let progressBar: cliProgress.SingleBar | null = null;
	if (bar && !progressCallback && totalSize > 0) {
		progressBar = new cliProgress.SingleBar(
			{
				format: `${desc} [{bar}] {percentage}% | ETA: {eta_formatted} | {value}/{total}`,
				formatValue: formatBytes,
				noTTYOutput: true,
			},
			cliProgress.Presets.shades_classic,
		);
		progressBar.start(totalSize, 0);
	}

	const chunks: Uint8Array[] = [];
	let downloaded = 0;
	try {
		if (!response.body) {
			throw new Error(`Response from ${url} had no body`);
		}
		for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
			if (buffer) {
				buffer.write(chunk);
			} else {
				chunks.push(chunk);
			}
			downloaded += chunk.length;
			progressBar?.increment(chunk.length);
			progressCallback?.(downloaded, totalSize);
		}
	} finally {
		progressBar?.stop();
	}

	return Buffer.concat(chunks);
}

/**
 * Extract a zip file to the given path.
 */
export function unzip(
	zipFile: Buffer | string,
	extractPath: string,
	desc?: string,
	bar: boolean = true,
): void {
	const zip = new AdmZip(zipFile);
	const entries = zip.getEntries();

	if (bar && desc) {
		rprint(desc);
	}

	for (const entry of entries) {
		zip.extractEntryTo(entry, extractPath, true, true);
	}
}

/**
 * chmod -R 755 on POSIX so the freshly-extracted binaries are executable.
 * (The zip does not carry usable permission bits on every platform.)
 */
export function makeExecutable(dir: string): void {
	if (OS_NAME === "win") return;
	try {
		execFileSync("chmod", ["-R", "755", dir]);
	} catch (error) {
		rprint(`Warning: could not chmod ${dir}: ${error}`, "yellow");
	}
}

/**
 * Format an asset timestamp as "Mon D", or "Mon D, YYYY" when the year differs.
 */
export function formatAssetDate(iso?: string, now?: Date): string {
	if (!iso) return "";
	const dt = new Date(iso);
	if (Number.isNaN(dt.getTime())) return "";
	const currentYear = (now ?? new Date()).getFullYear();
	const month = dt.toLocaleString("en-US", { month: "short" });
	if (dt.getFullYear() === currentYear) {
		return `${month} ${dt.getDate()}`;
	}
	return `${month} ${dt.getDate()}, ${dt.getFullYear()}`;
}
