/**
 * Camoufox Pro: installing a release.
 *
 * `camoufox fetch pro` asks the Camoufox Pro API for a published release for
 * this machine, checks its signature against the public keys this package
 * ships (pro-release-public-keys.json) before it downloads anything, and
 * installs it beside the public builds. Mirrors `fetch` in
 * python/src/camoufox/pro_update.py. See docs/pro.md.
 */
import { createPublicKey, verify as verifySignature } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import type { Writable } from "node:stream";
import { LIBRARY_VERSION } from "./__version__.js";
import { UnsupportedArchitecture, UpdateRefused } from "./exceptions.js";
import {
	type InstalledVersion,
	listInstalled,
	setActive,
} from "./multiversion.js";
import { ARCH_MAP, LOCAL_DATA, rprint, userCacheDir } from "./paths.js";
import {
	AvailableVersion,
	CamoufoxFetcher,
	RepoConfig,
	Version,
	webdl,
} from "./pkgman.js";
import {
	apiBase,
	call,
	DEFAULT_API,
	HOST_OS,
	type ProBuild,
	resolveKey,
} from "./pro.js";

/** What the API signs of a release, and the domain it signs under. */
const DOMAIN = "cfp-artifact-v1\n";
const SIGNED_FIELDS = [
	"version",
	"target",
	"filename",
	"sha256",
	"size",
	"build_hash",
] as const;
/** A release's version as an install is named: `<Firefox version>-<build>`. */
const VERSION = /^(\d+(?:\.\d+)*)-([0-9A-Za-z]+(?:[.+][0-9A-Za-z]+)*)$/;
const SHA256 = /^[0-9a-f]{64}$/;
/** The folder of `browsers/` that Pro releases are installed in, and the name `camoufox list` shows. */
const REPO = "pro";
export const ED25519_SPKI = Buffer.from("302a300506032b6570032100", "hex");
/** The same file as the shipped one for another API (CAMOUFOX_PRO_API), whose releases other keys sign. */
export const KEYS_ENV = "CAMOUFOX_PRO_KEYS";

/**
 * Test seam, like browserPinDeps. `releaseKeys` is the public keys a release of
 * the default API is verified with: the body of that API's signing-keys
 * listing, public halves only. Both launchers ship this one file.
 */
export const proUpdateDeps = {
	releaseKeys: path.join(LOCAL_DATA, "pro-release-public-keys.json"),
	libraryVersion: LIBRARY_VERSION,
};

/** Where the download of a release is kept until it is whole. */
export function partials(): string {
	return path.join(userCacheDir("camoufox"), "pro", "downloads");
}

/** The release target this machine runs: `linux-x86_64`, `macos-arm64`, ... */
export function hostTarget(): string {
	const arch = ARCH_MAP[process.arch.toLowerCase()];
	if (!arch) {
		throw new UnsupportedArchitecture(
			`Architecture ${process.arch} is not supported`,
		);
	}
	return `${HOST_OS}-${arch}`;
}

/** The Ed25519 public keys that sign the releases of the API at `base`, as SPKI, by key id. */
export function releaseKeys(base: string): Map<number, Buffer> {
	let file = proUpdateDeps.releaseKeys;
	let missing =
		"This camoufox ships no Camoufox Pro release key yet. Update camoufox.";
	if (base !== DEFAULT_API) {
		file = (process.env[KEYS_ENV] ?? "").trim();
		missing = `${file} names no key.`;
		if (!fs.existsSync(file) || !fs.statSync(file).isFile()) {
			throw new UpdateRefused(
				`${base} is not the Camoufox Pro API this camoufox ships the release keys of. ` +
					`Set ${KEYS_ENV} to the file of that API's public signing keys.`,
			);
		}
	}
	const keys = new Map<number, Buffer>();
	for (const entry of JSON.parse(fs.readFileSync(file, "utf8")).keys) {
		const raw = Buffer.from(String(entry.public_key_b64url), "base64url");
		const key = raw.subarray(0, ED25519_SPKI.length).equals(ED25519_SPKI)
			? raw.subarray(ED25519_SPKI.length)
			: raw;
		if ((entry.algorithm ?? "ed25519") !== "ed25519" || key.length !== 32) {
			throw new UpdateRefused(
				`${file} holds a key that is not an Ed25519 public key.`,
			);
		}
		keys.set(Number(entry.kid), Buffer.concat([ED25519_SPKI, key]));
	}
	if (!keys.size) {
		throw new UpdateRefused(
			`No Camoufox Pro release can be checked: ${missing}`,
		);
	}
	return keys;
}

/** Whether this camoufox is at least the `min_driver_version` the release names. */
function launcherDrives(artifact: Record<string, any>): boolean {
	const numbers = (version: string) =>
		(/^\d+(?:\.\d+)*/.exec(version) as RegExpExecArray)[0]
			.split(".")
			.map(Number);
	if (!artifact.min_driver_version) return true;
	const mine = numbers(proUpdateDeps.libraryVersion);
	const needed = numbers(String(artifact.min_driver_version));
	for (let at = 0; at < Math.max(mine.length, needed.length); at++) {
		if (mine[at] !== needed[at]) return (mine[at] ?? -1) > (needed[at] ?? -1);
	}
	return true;
}

function verify(
	artifact: Record<string, any>,
	keys: Map<number, Buffer>,
): void {
	const kid = artifact.signature?.kid;
	const key = keys.get(kid);
	if (!key) {
		throw new UpdateRefused(
			`The release is signed by key ${JSON.stringify(kid)}, which is not a key trusted here.`,
		);
	}
	const message = Buffer.from(
		DOMAIN + SIGNED_FIELDS.map((name) => String(artifact[name])).join("\n"),
	);
	let signed = false;
	try {
		signed = verifySignature(
			null,
			message,
			createPublicKey({ key, format: "der", type: "spki" }),
			Buffer.from(String(artifact.signature.ed25519 ?? ""), "base64url"),
		);
	} catch {}
	if (!signed)
		throw new UpdateRefused("The release's signature does not verify.");
}

/** Installs the release the API resolved, as `camoufox fetch` installs a public build. */
class ProFetcher extends CamoufoxFetcher {
	readonly size: number;
	override partial: string;

	constructor(artifact: Record<string, any>, version: string, build: string) {
		super(
			new RepoConfig({ repos: [REPO], name: REPO, pattern: "" }),
			new AvailableVersion({
				version: new Version(build, version),
				url: artifact.url,
				isPrerelease: false,
				assetSize: Number(artifact.size),
				sha256: artifact.sha256,
			}),
		);
		this.size = Number(artifact.size);
		this.partial = path.join(partials(), `${artifact.sha256}.part`);
	}

	override async download(file: Writable): Promise<void> {
		// The URL is signed: it downloads the release for whoever holds it, so it is not printed.
		const have = fs.existsSync(this.partial)
			? fs.statSync(this.partial).size
			: 0;
		if (have >= this.size) return;
		rprint(
			`Downloading Camoufox Pro ${this.verstr}${have ? `, resuming at ${have} bytes` : ""}`,
		);
		await webdl(this.url, undefined, true, file, {
			offset: have,
			restart: () => fs.truncateSync(this.partial),
		});
	}

	override async install(replace: boolean = false): Promise<boolean> {
		fs.mkdirSync(partials(), { recursive: true });
		for (const other of fs.readdirSync(partials())) {
			if (
				other !== path.basename(this.partial) ||
				fs.statSync(this.partial).size > this.size
			)
				fs.rmSync(path.join(partials(), other));
		}
		try {
			return await super.install(replace);
		} finally {
			// A download that stopped short is resumed by the next fetch; a whole one is done with.
			if (
				fs.existsSync(this.partial) &&
				fs.statSync(this.partial).size >= this.size
			)
				fs.rmSync(this.partial);
		}
	}
}

/**
 * Install the newest published release for this machine, or `version`, and
 * make it the active build.
 *
 * `build` is the Pro build already active on this machine, if one is: its API
 * is the one asked. The answer is checked against that API's public keys
 * before anything is downloaded.
 */
export async function fetchRelease(
	version: string | null,
	build: ProBuild | null,
): Promise<InstalledVersion> {
	const key = resolveKey();
	const base = apiBase(build);
	// Needed to check the release, so their absence is found before the API is asked.
	const keys = releaseKeys(base);
	const target = hostTarget();
	const artifact = await call(
		"GET",
		`/api/v1/artifacts/${encodeURIComponent(version ?? "latest")}/${target}`,
		null,
		{ key, base },
	);
	verify(artifact, keys);
	const named = VERSION.exec(String(artifact.version));
	if (
		!named ||
		artifact.target !== target ||
		(version !== null && artifact.version !== version) ||
		!SHA256.test(String(artifact.sha256)) ||
		!String(artifact.url).startsWith("https://")
	) {
		throw new UpdateRefused(
			`Asked for ${version ?? "the newest release"} for ${target}; the API answered with ` +
				`${JSON.stringify(artifact.version)} for ${JSON.stringify(artifact.target)}, which is not a release to install.`,
		);
	}
	if (!launcherDrives(artifact)) {
		throw new UpdateRefused(
			`Camoufox Pro ${artifact.version} needs camoufox ${artifact.min_driver_version} or newer, ` +
				`and this is ${proUpdateDeps.libraryVersion}. Update the launcher: npm install camoufox@latest`,
		);
	}
	await new ProFetcher(artifact, named[1], named[2]).install();
	const installed = listInstalled().find(
		(iv) => iv.repoName === REPO && iv.sha256 === artifact.sha256,
	) as InstalledVersion;
	setActive(installed.relativePath);
	return installed;
}
