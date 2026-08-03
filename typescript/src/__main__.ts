#!/usr/bin/env node
/**
 * The `camoufox` CLI.
 *
 * TypeScript twin of python/src/__main__.py. Every command is present
 * except `gui`, which drives a PySide6 desktop app with no Node equivalent, and
 * the interactive (inquirer-driven) pickers -- `set` and `remove` take the same
 * specifiers, just non-interactively.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { Command } from "commander";
import { LIBRARY_VERSION } from "./__version__.js";
import { DefaultAddons, maybeDownloadAddons } from "./addons.js";
import {
	ALLOW_GEOIP,
	downloadMMDB,
	GEOIP_DIR,
	getMmdbPath,
	loadGeoIPConfig,
	removeMMDB,
} from "./locale.js";
import {
	BROWSERS_DIR,
	type CachedVersion,
	COMPAT_FLAG,
	CONFIG_FILE,
	findInstall,
	findInstalledVersion,
	getDefaultChannel,
	type InstalledVersion,
	latestPerBuild,
	listInstalled,
	loadConfig,
	loadRepoCache,
	printTree,
	REPO_CACHE_FILE,
	type RepoCache,
	removeVersion,
	saveConfig,
	saveRepoCache,
} from "./multiversion.js";
import {
	AvailableVersion,
	CamoufoxFetcher,
	INSTALL_DIR,
	installedVerStr,
	listAvailableVersions,
	OS_NAME,
	RepoConfig,
	rprint,
	Version,
} from "./pkgman.js";
import { launchServer } from "./server.js";
import { Camoufox } from "./sync_api.js";

/** Find an installed version by path, build, channel path, or repo/channel. */
function findInstalledEntry(specifier: string): InstalledVersion | null {
	const installed = listInstalled();
	const lower = specifier.toLowerCase();

	for (const v of installed) {
		if (
			v.channelPath.toLowerCase() === lower ||
			v.relativePath === specifier ||
			v.version.build.toLowerCase() === lower ||
			v.version.fullString.toLowerCase() === lower
		) {
			return v;
		}
	}

	const parts = lower.split("/");
	if (parts.length === 2) {
		const [repo, ctype] = parts;
		if (ctype === "stable" || ctype === "prerelease") {
			const isPre = ctype === "prerelease";
			for (const v of installed) {
				if (v.repoName === repo && v.isPrerelease === isPre) return v;
			}
		}
	}

	const byPath = findInstalledVersion(specifier);
	if (byPath) {
		return installed.find((v) => v.path === byPath) ?? null;
	}
	return null;
}

/** Name of the active GeoIP source. */
function getGeoIPSourceName(): string {
	try {
		return loadGeoIPConfig().name ?? "Default";
	} catch {
		return "Default";
	}
}

/**
 * Sync available versions from the remote repositories. Returns true on success.
 */
async function doSync(spoofOs?: string, spoofArch?: string): Promise<boolean> {
	rprint("Syncing repositories...", "yellow");

	const cache: RepoCache = {
		repos: [],
		spoof_os: spoofOs,
		spoof_arch: spoofArch,
	};

	for (const repoConfig of RepoConfig.loadRepos()) {
		rprint(`  ${repoConfig.name}...`, "cyan", false);
		try {
			const versions = await listAvailableVersions(
				repoConfig,
				true,
				spoofOs,
				spoofArch,
			);
			cache.repos?.push({
				name: repoConfig.name,
				versions: versions.map((v) => ({
					version: v.version.version as string,
					build: v.version.build,
					url: v.url,
					is_prerelease: v.isPrerelease,
					asset_id: v.assetId,
					asset_size: v.assetSize,
					asset_updated_at: v.assetUpdatedAt,
					sha256: v.sha256,
					created_at: v.assetCreatedAt,
				})),
			});
			rprint(` ${versions.length} versions`, "green");
		} catch (e) {
			rprint(` Error: ${e}`, "red");
		}
	}

	saveRepoCache(cache);
	const total = (cache.repos ?? []).reduce(
		(sum, r) => sum + (r.versions?.length ?? 0),
		0,
	);
	const platformStr = spoofOs ? ` (${spoofOs}/${spoofArch})` : "";
	rprint(
		`\nSynced ${total} versions from ${cache.repos?.length ?? 0} repos${platformStr}.`,
		"green",
	);

	return true;
}

/** Ensure a repo cache exists. Returns true if synced. */
function ensureSynced(): boolean {
	if (!fs.existsSync(REPO_CACHE_FILE)) {
		rprint("No repo cache found. Run 'camoufox sync' first.", "red");
		return false;
	}
	return true;
}

/** Cache block for a repo by name. */
function repoData(
	cache: RepoCache,
	repoName: string,
): { name: string; versions?: CachedVersion[] } | null {
	return (
		(cache.repos ?? []).find(
			(r) => r.name.toLowerCase() === repoName.toLowerCase(),
		) ?? null
	);
}

/**
 * Resolve a version-build or version-build-sha8 spec against the cache.
 * Returns the specific dated asset, the latest of that build, or null.
 */
function resolveSpec(
	repo: { versions?: CachedVersion[] },
	spec: string,
): [CachedVersion | null, string | null] {
	const versions = repo.versions ?? [];
	for (const v of versions) {
		const sha = v.sha256 ?? "";
		if (sha && spec === `${v.version}-${v.build}-${sha.slice(0, 8)}`) {
			return [v, sha];
		}
	}
	for (const v of latestPerBuild(versions)) {
		if (spec === `${v.version}-${v.build}`) return [v, null];
	}
	return [null, null];
}

/** The cache entry a pin resolves to: the sha'd asset, or the build's latest. */
function pinTarget(
	repo: { versions?: CachedVersion[] },
	pinned: string,
	pinnedSha?: string,
): CachedVersion | null {
	const versions = repo.versions ?? [];
	if (pinnedSha) {
		return versions.find((v) => v.sha256 === pinnedSha) ?? null;
	}
	return (
		latestPerBuild(versions).find(
			(v) => `${v.version}-${v.build}` === pinned,
		) ?? null
	);
}

function setChannel(repoName: string, channelType: string): void {
	const config = loadConfig();
	config.channel = `${repoName.toLowerCase()}/${channelType}`;
	delete config.pinned;
	delete config.pinned_sha;
	delete config.active_version;
	saveConfig(config);
	rprint(`Channel: ${repoName.toLowerCase()}/${channelType}`, "cyan");
	rprint("Run 'camoufox fetch' to install latest.", "yellow");
}

/** Pin a version-build, optionally to a specific dated asset by sha. */
function setPinned(
	repoName: string,
	channelType: string,
	verData: CachedVersion,
	inst: InstalledVersion | null,
	sha?: string | null,
): void {
	const config = loadConfig();
	config.channel = `${repoName.toLowerCase()}/${channelType}`;
	config.pinned = `${verData.version}-${verData.build}`;
	if (sha) {
		config.pinned_sha = sha;
	} else {
		delete config.pinned_sha;
	}
	const tag = sha ? ` (${sha.slice(0, 8)})` : "";
	const display = `${repoName.toLowerCase()}/${channelType}/${verData.version}-${verData.build}${tag}`;
	if (inst) {
		config.active_version = inst.relativePath;
		saveConfig(config);
		rprint(`Pinned: ${display} (installed)`, "green");
	} else {
		delete config.active_version;
		saveConfig(config);
		rprint(`Pinned: ${display}`, "cyan");
		rprint("Run 'camoufox fetch' to install.", "yellow");
	}
}

/**
 * Checks & updates Camoufox.
 */
class CamoufoxUpdate extends CamoufoxFetcher {
	currentVerStr: string | null = null;

	async init(): Promise<this> {
		await super.init();
		try {
			this.currentVerStr = installedVerStr();
		} catch {
			this.currentVerStr = null;
		}
		return this;
	}

	isUpdateNeeded(): boolean {
		return this.currentVerStr === null || this.currentVerStr !== this.verstr;
	}

	async update(replace: boolean = false): Promise<void> {
		if (!this.isUpdateNeeded() && !replace) {
			rprint("Camoufox binaries up to date!", "green");
			rprint(`Current version: v${this.currentVerStr}`, "green");
			return;
		}

		if (this.isPrerelease) {
			rprint(`Warning: v${this.verstr} is a prerelease version!`, "yellow");
		}

		const action = this.currentVerStr ? "Installing" : "Fetching";
		rprint(`${action} Camoufox v${this.verstr}...`, "yellow");
		await this.install(replace);
	}
}

const program = new Command();
program
	.name("camoufox")
	.description("Camoufox browser manager")
	.version(LIBRARY_VERSION);

program
	.command("sync")
	.description("Sync available versions from remote repositories")
	.option("--spoof-os <os>", "Spoof OS: auto | mac | win | lin")
	.option(
		"--spoof-arch <arch>",
		"Spoof architecture: auto | x86_64 | i686 | arm64",
	)
	.action(async ({ spoofOs, spoofArch }) => {
		await doSync(
			spoofOs === "auto" ? undefined : spoofOs,
			spoofArch === "auto" ? undefined : spoofArch,
		);
	});

program
	.command("fetch")
	.description("Install the active version, or a specific version")
	.argument("[version]", "e.g. official/135.0-beta.25")
	.action(async (version?: string) => {
		// Clean up an incompatible old data directory
		if (
			fs.existsSync(INSTALL_DIR) &&
			fs.readdirSync(INSTALL_DIR).length > 0 &&
			!fs.existsSync(COMPAT_FLAG)
		) {
			rprint("Cleaning old data...", "yellow");
			fs.rmSync(INSTALL_DIR, { recursive: true, force: true });
		}

		await doSync();

		const cache = loadRepoCache();
		const config = loadConfig();

		let repoName: string | null = null;
		let repo: { name: string; versions?: CachedVersion[] } | null = null;
		let verData: CachedVersion | null = null;

		if (version) {
			const parts = version.toLowerCase().split("/");
			let spec: string;
			if (parts.length === 1) {
				repoName = RepoConfig.getDefaultName();
				spec = parts[0];
			} else if (parts.length === 2 || parts.length === 3) {
				repoName = parts[0];
				spec = parts[parts.length - 1];
			} else {
				rprint(
					"Format: version-build, repo/version-build, or repo/channel/version-build",
					"red",
				);
				return;
			}
			repo = repoData(cache, repoName);
			if (repo) [verData] = resolveSpec(repo, spec.replace(/^v/, ""));
		} else if (config.pinned) {
			const channel = config.channel ?? "";
			repoName = channel.includes("/") ? channel.split("/")[0] : channel;
			repo = repoData(cache, repoName);
			if (repo) verData = pinTarget(repo, config.pinned, config.pinned_sha);
		} else {
			const channel = config.channel ?? getDefaultChannel();
			const [name, ctype = "stable"] = channel.split("/");
			repoName = name;
			repo = repoData(cache, repoName);
			if (repo) {
				const isPre = ctype === "prerelease";
				const latest = latestPerBuild(repo.versions ?? []).filter(
					(v) => (v.is_prerelease ?? false) === isPre,
				);
				verData = latest[0] ?? null;
			}
		}

		if (!verData || !repo) {
			rprint(
				`Version '${version ?? repoName}' not found in cache. Run 'camoufox sync'.`,
				"red",
			);
			return;
		}

		const selected = new AvailableVersion({
			version: new Version(verData.build, verData.version),
			url: verData.url,
			isPrerelease: verData.is_prerelease ?? false,
			sha256: verData.sha256,
			assetCreatedAt: verData.created_at,
		});
		const repoConfig = RepoConfig.findByName(repo.name);

		try {
			const updater = new CamoufoxUpdate(repoConfig, selected);
			await updater.init();
			await updater.update();
		} catch (e) {
			const msg = String(e);
			if (msg.includes("404") || msg.includes("Not Found")) {
				rprint("Release not found (404). Asset may have been removed.", "red");
				rprint("Run 'camoufox sync' to refresh available versions.", "yellow");
			} else {
				rprint(`Error: ${msg}`, "red");
			}
			return;
		}

		if (ALLOW_GEOIP) {
			await downloadMMDB();
		}
		await maybeDownloadAddons({ ...DefaultAddons });
	});

program
	.command("set")
	.description("Set the active Camoufox version to use & fetch")
	.argument(
		"[specifier]",
		"version-build, repo/channel, or repo/channel/version-build",
	)
	.option("--geoip <name>", "Set the GeoIP source instead")
	.action((specifier: string | undefined, { geoip }: { geoip?: string }) => {
		if (geoip) {
			const config = loadConfig();
			config.geoip = geoip;
			saveConfig(config);
			rprint(`GeoIP source: ${geoip}`, "cyan");
			rprint("Run 'camoufox fetch' to download it.", "yellow");
			return;
		}

		if (!specifier) {
			rprint(
				"A specifier is required. Use: version-build, repo/channel, or repo/channel/version-build",
				"red",
			);
			rprint("Run 'camoufox list all' to see available versions.", "yellow");
			return;
		}

		const parts = specifier.toLowerCase().split("/");

		// 2 parts sets a channel, like official/stable
		if (parts.length === 2) {
			const [repoName, ctype] = parts;
			if (ctype !== "stable" && ctype !== "prerelease") {
				rprint(
					`Unknown channel type '${ctype}'. Use 'stable' or 'prerelease'.`,
					"red",
				);
				return;
			}
			setChannel(repoName, ctype);
			return;
		}

		// 1 part pins in the default repo; 3 parts names the repo and channel
		let repoName: string;
		let spec: string;
		if (parts.length === 1) {
			repoName = RepoConfig.getDefaultName();
			spec = parts[0];
		} else if (parts.length === 3) {
			const ctype = parts[1];
			if (ctype !== "stable" && ctype !== "prerelease") {
				rprint(
					`Unknown channel type '${ctype}'. Use 'stable' or 'prerelease'.`,
					"red",
				);
				return;
			}
			repoName = parts[0];
			spec = parts[2];
		} else {
			rprint(`Invalid specifier '${specifier}'.`, "red");
			rprint(
				"Use: version-build, repo/channel, or repo/channel/version-build",
				"yellow",
			);
			return;
		}

		if (!ensureSynced()) return;
		const repo = repoData(loadRepoCache(), repoName);
		if (!repo) {
			rprint(
				`Repo '${repoName.toLowerCase()}' not in cache. Run 'camoufox sync'.`,
				"red",
			);
			return;
		}
		const [verData, sha] = resolveSpec(repo, spec.replace(/^v/, ""));
		if (!verData) {
			rprint(
				`Version '${spec}' not found in ${repoName.toLowerCase()}.`,
				"red",
			);
			return;
		}
		const ctype = verData.is_prerelease ? "prerelease" : "stable";
		const vb = `${verData.version}-${verData.build}`;
		const count = (repo.versions ?? []).filter(
			(x) => `${x.version}-${x.build}` === vb,
		).length;
		const inst = findInstall(vb, verData.sha256, listInstalled(), count);
		setPinned(repo.name, ctype, verData, inst, sha);
	});

program
	.command("list")
	.description("List Camoufox versions")
	.argument("[mode]", "installed (default) | all", "installed")
	.option("--path", "Show full paths")
	.action((mode: string, { path: showPaths }: { path?: boolean }) => {
		if (mode === "all") {
			listAll();
		} else {
			listInstalledVersions(Boolean(showPaths));
		}
	});

function listInstalledVersions(showPaths: boolean): void {
	printTree(true, showPaths);

	rprint("");
	rprint("geoip/", "cyan", false);
	if (showPaths && fs.existsSync(GEOIP_DIR)) {
		rprint(` -> ${GEOIP_DIR}`, "bright_black");
	} else {
		rprint("");
	}

	if (fs.existsSync(GEOIP_DIR)) {
		const mmdb = getMmdbPath();
		if (fs.existsSync(mmdb)) {
			rprint(`    └── ${path.basename(mmdb)} `, undefined, false);
			rprint(`(${getGeoIPSourceName()})`, "green");
		} else {
			rprint("    └── Not downloaded", "yellow");
		}
	} else {
		rprint("    └── Not configured", "yellow");
	}
}

function listAll(): void {
	if (!ensureSynced()) return;

	const cache = loadRepoCache();
	const installed = new Set(listInstalled().map((v) => v.version.build));

	rprint("Available versions:\n", "yellow");

	for (const repo of cache.repos ?? []) {
		rprint(`${repo.name}/`, "cyan");
		const versions = latestPerBuild(repo.versions ?? []);
		for (let i = 0; i < versions.length; i++) {
			const v = versions[i];
			const branch = i === versions.length - 1 ? "└── " : "├── ";
			const marker = installed.has(v.build) ? " (installed)" : "";
			const channel = v.is_prerelease ? " (prerelease)" : " (stable)";
			rprint(`    ${branch}v${v.version}-${v.build}`, undefined, false);
			rprint(channel, v.is_prerelease ? "yellow" : "blue", false);
			rprint(marker, "green");
		}
	}
}

program
	.command("remove")
	.description("Remove downloaded data. By default, this removes everything.")
	.argument("[version_path]", "A specific installed version to remove")
	.action((versionPath?: string) => {
		// Specific version: remove just that one
		if (versionPath) {
			const target = findInstalledEntry(versionPath);
			if (!target) {
				rprint(`Version '${versionPath}' not found.`, "red");
				return;
			}
			removeVersion(target.path);
			rprint(`Removed ${target.channelPath}`, "green");
			return;
		}

		// Default: remove everything
		if (
			!fs.existsSync(INSTALL_DIR) ||
			fs.readdirSync(INSTALL_DIR).length === 0
		) {
			rprint("Nothing to remove.", "yellow");
			return;
		}

		fs.rmSync(INSTALL_DIR, { recursive: true, force: true });
		rprint("Removed camoufox data directory.", "green");
	});

program
	.command("test")
	.description("Open the Playwright inspector")
	.argument("[url]", "URL to open")
	.option("--executable-path <path>", "Path to the Camoufox executable")
	.action(async (url: string | undefined, { executablePath }) => {
		const browser = await Camoufox({
			headless: false,
			env: process.env as Record<string, string>,
			config: { showcursor: false },
			executable_path: executablePath,
		});
		const page = await browser.newPage();
		if (url) {
			await page.goto(url);
		}
		await page.pause();
		await browser.close();
	});

program
	.command("server")
	.description("Launch a Playwright server")
	.action(async () => {
		const server = await launchServer();
		rprint(`Camoufox server started at ${server.wsEndpoint()}`);
		rprint("");
		rprint(
			"You can connect to it using Playwright's BrowserType.connect() method.",
		);
		rprint("To stop the server, press Ctrl+C or close this terminal.");
	});

program
	.command("version")
	.description("Display version, package, browser, and storage info")
	.action(() => {
		const row = (label: string, value: string) => {
			console.log(`  ${label.padEnd(28)}${value}`);
		};

		rprint("Packages", "yellow");
		row("Launcher (npm)", `v${LIBRARY_VERSION}`);
		row("Platform", `${OS_NAME}/${process.arch}`);

		rprint("\nBrowser", "yellow");
		const active = listInstalled().find((v) => v.isActive);
		if (active) {
			row("Current browser", `v${active.version.fullString}`);
			row("Channel", active.channelPath);
			if (active.sha256) row("SHA256", active.sha256.slice(0, 12));
			row("Installed", "Yes");
		} else {
			row("Current browser", "Not installed");
			row("Installed", "No");
		}
		row(
			"Last Sync",
			fs.existsSync(REPO_CACHE_FILE)
				? new Date(fs.statSync(REPO_CACHE_FILE).mtimeMs).toISOString()
				: "Never",
		);

		rprint("\nGeoIP", "yellow");
		const mmdbPath = getMmdbPath();
		if (fs.existsSync(mmdbPath)) {
			row("Database", getGeoIPSourceName());
			row("Updated", new Date(fs.statSync(mmdbPath).mtimeMs).toISOString());
		} else {
			row("Database", "Not installed");
		}

		rprint("\nStorage", "yellow");
		row("Install path", INSTALL_DIR);
		row("Browser(s) directory size", dirSize(BROWSERS_DIR));
		row("GeoIP database size", dirSize(GEOIP_DIR));
		row("Config file", CONFIG_FILE);
		row("Repo cache", REPO_CACHE_FILE);
	});

function dirSize(dir: string): string {
	if (!fs.existsSync(dir)) return "Nothing here";
	let total = 0;
	const walk = (current: string) => {
		for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
			const full = path.join(current, entry.name);
			if (entry.isDirectory()) walk(full);
			else if (entry.isFile()) total += fs.statSync(full).size;
		}
	};
	walk(dir);
	let size = total;
	for (const unit of ["B", "KB", "MB"]) {
		if (size < 1024) {
			return unit === "B" ? `${size} B` : `${size.toFixed(1)} ${unit}`;
		}
		size /= 1024;
	}
	return `${size.toFixed(1)} GB`;
}

program
	.command("active")
	.description("Print the current active version")
	.action(() => {
		const config = loadConfig();
		const pinned = config.pinned;
		const channel = config.channel ?? getDefaultChannel();
		const installed = listInstalled();

		const label = (v: InstalledVersion) => {
			const sha8 = (v.sha256 ?? "").slice(0, 8);
			return sha8 ? `${v.channelPath} (${sha8})` : v.channelPath;
		};

		if (pinned) {
			const target = config.pinned_sha
				? (installed.find((v) => v.sha256 === config.pinned_sha) ?? null)
				: findInstalledEntry(`${channel.toLowerCase()}/${pinned}`);
			if (target) {
				console.log(label(target));
			} else {
				rprint(`${channel.toLowerCase()}/${pinned} `, undefined, false);
				rprint("(not fetched)", "yellow");
			}
			return;
		}

		const activeEntry = installed.find((v) => v.isActive);
		if (activeEntry) {
			console.log(label(activeEntry));
			return;
		}
		rprint(`${channel.toLowerCase()} `, undefined, false);
		rprint("(not fetched)", "yellow");
	});

program
	.command("path")
	.description("Print the install directory path")
	.action(() => {
		console.log(INSTALL_DIR);
	});

program
	.command("remove-geoip")
	.description("Remove the downloaded GeoIP database")
	.action(() => {
		removeMMDB();
	});

program.parseAsync(process.argv).catch((error) => {
	rprint(`Error: ${error}`, "red");
	process.exitCode = 1;
});
