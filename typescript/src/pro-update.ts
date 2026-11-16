/**
 * Camoufox Pro: installing a release.
 *
 * `camoufox fetch pro` asks the Camoufox Pro API for a published release for
 * this machine, checks that it is the one the API signed, and installs it
 * beside the public builds. Mirrors `fetch` in
 * python/src/camoufox/pro_update.py. See docs/pro.md.
 */
import { createPublicKey, verify as verifySignature } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import type { Writable } from "node:stream";
import { UnsupportedArchitecture, UpdateRefused } from "./exceptions.js";
import {
	type InstalledVersion,
	listInstalled,
	removeVersion,
	setActive,
} from "./multiversion.js";
import { ARCH_MAP, rprint, userCacheDir } from "./paths.js";
import {
	AvailableVersion,
	CamoufoxFetcher,
	launchPath,
	RepoConfig,
	Version,
	webdl,
} from "./pkgman.js";
import { apiBase, call, HOST_OS, type ProBuild, resolveKey } from "./pro.js";

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
// A Pro build carries the public keys it verifies its lease with. A release is signed with the same
// keys, so the installed build is what says which signatures to believe.
const TRUST_MARKER = "camoufox-pro-lease-trust-v1 keys=";
const TRUSTED_KEYS = /^((?:[0-9a-f]+|-)(?:,(?:[0-9a-f]+|-))*) build=/;
export const ED25519_SPKI = Buffer.from("302a300506032b6570032100", "hex");
const LIBRARIES = ["libxul.so", "XUL", "xul.dll"];

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

/** The Ed25519 public keys the build beside `executable` trusts, as SPKI, by key id. */
export function trustedKeys(executable: string): Map<number, Buffer> {
	for (const name of LIBRARIES) {
		const library = path.join(path.dirname(executable), name);
		if (!fs.existsSync(library)) continue;
		const image = fs.readFileSync(library);
		const at = image.indexOf(TRUST_MARKER);
		const named =
			at < 0
				? null
				: TRUSTED_KEYS.exec(
						image
							.subarray(
								at + TRUST_MARKER.length,
								at + TRUST_MARKER.length + 4096,
							)
							.toString("latin1"),
					);
		if (named) {
			const keys = new Map<number, Buffer>();
			named[1].split(",").forEach((slot, kid) => {
				const spki = slot === "-" ? Buffer.alloc(0) : Buffer.from(slot, "hex");
				if (spki.subarray(0, ED25519_SPKI.length).equals(ED25519_SPKI))
					keys.set(kid, spki);
			});
			return keys;
		}
	}
	throw new UpdateRefused(
		`The Pro build at ${path.dirname(executable)} names no signing key, so no release can be checked.`,
	);
}

function verify(
	artifact: Record<string, any>,
	keys: Map<number, Buffer>,
): void {
	const kid = artifact.signature?.kid;
	const key = keys.get(kid);
	if (!key) {
		throw new UpdateRefused(
			`The release is signed by key ${JSON.stringify(kid)}, which this build does not trust.`,
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
 * `build` and `executable` are the Pro build already active on this machine,
 * if one is: the release is checked against the keys it carries before
 * anything is downloaded. Without one there is no key to check with until the
 * release is unpacked, so it is checked against its own.
 */
export async function fetchRelease(
	version: string | null,
	build: ProBuild | null,
	executable: string | null,
): Promise<InstalledVersion> {
	const key = resolveKey();
	const target = hostTarget();
	const artifact = await call(
		"GET",
		`/api/v1/artifacts/${encodeURIComponent(version ?? "latest")}/${target}`,
		null,
		{ key, base: apiBase(build) },
	);
	const keys = build && executable ? trustedKeys(executable) : null;
	if (keys) verify(artifact, keys);
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
	await new ProFetcher(artifact, named[1], named[2]).install();
	const installed = listInstalled().find(
		(iv) => iv.repoName === REPO && iv.sha256 === artifact.sha256,
	) as InstalledVersion;
	if (!keys) {
		try {
			verify(artifact, trustedKeys(launchPath(installed.path)));
		} catch (error) {
			if (error instanceof UpdateRefused) removeVersion(installed.path);
			throw error;
		}
		rprint(
			"No Camoufox Pro build is installed to check this release against, so its signature was " +
				"checked with the key the release itself carries. Later releases are checked with that key " +
				"before they are downloaded.",
		);
	}
	setActive(installed.relativePath);
	return installed;
}
