/**
 * Mirrors python/tests/test_fetch_pro.py: `camoufox fetch pro` installs a
 * Camoufox Pro release beside the public builds.
 *
 * The API and the storage its signed URLs point at are a stubbed fetch,
 * answering the download route in the server's shape. The API's public key
 * is in a keys file of the shape the launcher ships.
 *
 * The CLI runs on import, so each command imports __main__ afresh and waits
 * for it to finish or set an exit code.
 */
import {
	createHash,
	generateKeyPairSync,
	type KeyObject,
	randomBytes,
	sign,
} from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import AdmZip from "adm-zip";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const API = "https://api.camoufox.test";
const DEFAULT_API = "https://api.camoufox.com";
const KEY = `cfp_live_${"k".repeat(43)}`;
const FIRST = "159.0-pro.1";
const SECOND = "159.0-pro.2";
// The suite runs on Linux: a release here is a Linux build's files.
const target = `linux-${process.arch === "x64" ? "x86_64" : process.arch}`;
const ENV = [
	"XDG_CACHE_HOME",
	"XDG_CONFIG_HOME",
	"HOME",
	"CAMOUFOX_PRO_API",
	"CAMOUFOX_PRO_KEY",
	"CAMOUFOX_PRO_KEYS",
	"CAMOUFOX_EXECUTABLE_PATH",
];

let tmp: string;
let saved: Record<string, string | undefined>;
let savedArgv: string[];
let output: string;
let finished: boolean;
/** The key the gate signs with, and its public half, which the keys file names. */
let signer: KeyObject;
let trusted: KeyObject;
/** The keys file CAMOUFOX_PRO_KEYS names. */
let keysFile: string;
/** What the launcher is made to read as the keys file it ships, when a test replaces it. */
let shipped: string | null;
/** The version the launcher is made to be, when a test replaces it. */
let libraryVersion: string | null;
/** What the download gate answers: a status and a body. */
let gate: [number, Record<string, any>];
let asked: { path: string; auth: string | null }[];
let served: Map<string, Buffer>;
let ranges: (string | null)[];
/** The download drops after this many bytes of a response, once. */
let dropAfter: number | null;

beforeEach(() => {
	tmp = fs.mkdtempSync(path.join(os.tmpdir(), "camoufox-fetch-pro-"));
	saved = Object.fromEntries(ENV.map((name) => [name, process.env[name]]));
	savedArgv = process.argv;
	process.env.XDG_CACHE_HOME = path.join(tmp, "cache");
	process.env.XDG_CONFIG_HOME = path.join(tmp, "config");
	process.env.HOME = tmp;
	process.env.CAMOUFOX_PRO_API = API;
	process.env.CAMOUFOX_PRO_KEY = KEY;
	delete process.env.CAMOUFOX_EXECUTABLE_PATH;
	output = "";
	gate = [404, { error: "artifact_not_found", message: "No such release." }];
	asked = [];
	served = new Map();
	ranges = [];
	dropAfter = null;
	({ privateKey: signer, publicKey: trusted } = generateKeyPairSync("ed25519"));
	keysFile = path.join(tmp, "signing-keys.json");
	trust(trusted);
	process.env.CAMOUFOX_PRO_KEYS = keysFile;
	shipped = null;
	libraryVersion = null;
	vi.doMock("../src/pro-update.js", async (load) => {
		const actual = await load<typeof import("../src/pro-update.js")>();
		if (shipped) actual.proUpdateDeps.releaseKeys = shipped;
		if (libraryVersion) actual.proUpdateDeps.libraryVersion = libraryVersion;
		return actual;
	});
	vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
		const headers = new Headers(init?.headers);
		const api = [API, DEFAULT_API].find((base) => url.startsWith(base));
		if (api) {
			asked.push({
				path: url.slice(api.length),
				auth: headers.get("Authorization"),
			});
			return new Response(JSON.stringify(gate[1]), { status: gate[0] });
		}
		const body = served.get(url);
		if (!body)
			return new Response("", { status: 404, statusText: "Not Found" });
		const range = headers.get("Range");
		ranges.push(range);
		const rest = body.subarray(range ? Number(/\d+/.exec(range)) : 0);
		const drop = dropAfter;
		dropAfter = null;
		let sent = false;
		return new Response(
			new ReadableStream({
				// A chunk a reader has not taken is lost when the stream errors,
				// so the drop comes on the pull after the bytes that arrived.
				pull(controller) {
					if (sent) {
						controller.error(new Error("connection reset"));
					} else if (drop === null) {
						controller.enqueue(rest);
						controller.close();
					} else {
						controller.enqueue(rest.subarray(0, drop));
						sent = true;
					}
				},
			}),
			{
				status: range ? 206 : 200,
				headers: { "content-length": String(rest.length) },
			},
		);
	});
	vi.spyOn(process.stdout, "write").mockImplementation((chunk) => {
		output += String(chunk);
		return true;
	});
	vi.doMock("../src/geolocation.js", async (load) => ({
		...(await load<typeof import("../src/geolocation.js")>()),
		allowGeoip: () => false,
	}));
	vi.doMock("fpgen-js", async (load) => ({
		...(await load<typeof import("fpgen-js")>()),
		ensureModel: async () => {
			finished = true;
		},
	}));
});

afterEach(() => {
	for (const name of ENV) {
		if (saved[name] === undefined) delete process.env[name];
		else process.env[name] = saved[name];
	}
	process.argv = savedArgv;
	process.exitCode = undefined;
	vi.doUnmock("../src/pro-update.js");
	vi.doUnmock("../src/geolocation.js");
	vi.doUnmock("fpgen-js");
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	fs.rmSync(tmp, { recursive: true, force: true });
});

/** Write the keys file as the API lists its signing keys: `keys` in slots 1, 2, ... */
function trust(...keys: KeyObject[]): void {
	fs.writeFileSync(
		keysFile,
		JSON.stringify({
			keys: keys.map((key, at) => ({
				kid: at + 1,
				generation: 1,
				algorithm: "ed25519",
				public_key_b64url: key
					.export({ format: "der", type: "spki" })
					.toString("base64url"),
				state: at ? "standby" : "active",
			})),
		}),
	);
}

/** The archive of a release. The key its build carries is not one that signs releases here. */
function release(version: string): Buffer {
	const spki = generateKeyPairSync("ed25519")
		.publicKey.export({ format: "der", type: "spki" })
		.toString("hex");
	const zip = new AdmZip();
	zip.addFile("camoufox-bin", Buffer.from("#!/bin/sh\n"), "", 0o755);
	zip.addFile(
		"libxul.so",
		Buffer.concat([
			Buffer.from("\x7fELF"),
			Buffer.alloc(64),
			Buffer.from(
				`camoufox-pro-lease-trust-v1 keys=-,${spki},- build=${"ab".repeat(32)}`,
			),
			// Does not compress, so the archive is larger than one read of it.
			randomBytes(65536),
		]),
	);
	zip.addFile(
		"pro-build.json",
		Buffer.from(
			JSON.stringify({
				build_hash: `sha256:${"cd".repeat(32)}`,
				version,
				target,
			}),
		),
	);
	return zip.toBuffer();
}

/** Publish `version`; `signed` replaces fields of the answer before it is signed, `changed` after. */
function publish(
	version: string,
	{
		archive = release(version),
		key = signer,
		kid = 1,
		signed = {},
		changed = {},
	}: {
		archive?: Buffer;
		key?: KeyObject;
		kid?: number;
		signed?: Record<string, any>;
		changed?: Record<string, any>;
	} = {},
): Buffer {
	const artifact: Record<string, any> = {
		version,
		target,
		filename: "camoufox-pro.zip",
		sha256: createHash("sha256").update(archive).digest("hex"),
		size: archive.length,
		build_hash: `sha256:${"cd".repeat(32)}`,
		...signed,
	};
	const message = Buffer.from(
		"cfp-artifact-v1\n" +
			["version", "target", "filename", "sha256", "size", "build_hash"]
				.map((name) => String(artifact[name]))
				.join("\n"),
	);
	const answer = {
		...artifact,
		url: `https://releases.test/${version}.zip?signature=secret`,
		signature: {
			kid,
			ed25519: sign(null, message, key).toString("base64url"),
		},
		...changed,
	};
	gate = [200, answer];
	served.set(answer.url, archive);
	return archive;
}

/** Run a camoufox command; its exit code and what it printed. */
async function camoufox(...args: string[]) {
	vi.resetModules();
	process.argv = ["node", "camoufox", ...args];
	process.exitCode = undefined;
	finished = args[0] !== "fetch";
	const from = output.length;
	await import("../src/__main__.js");
	await vi.waitFor(() => {
		if (!finished && process.exitCode === undefined)
			throw new Error("the command is still running");
	});
	// A command that does not fetch has no end to wait for but its output.
	if (args[0] !== "fetch")
		await new Promise((resolve) => setTimeout(resolve, 50));
	return { code: process.exitCode ?? 0, output: output.slice(from) };
}

const fetchPro = (...args: string[]) => camoufox("fetch", "pro", ...args);

async function installed() {
	vi.resetModules();
	const { listInstalled } = await import("../src/multiversion.js");
	return new Map(listInstalled().map((iv) => [iv.version.fullString, iv]));
}

function partials(): string[] {
	const directory = path.join(tmp, "cache", "camoufox", "pro", "downloads");
	return fs.existsSync(directory)
		? fs.readdirSync(directory).map((name) => path.join(directory, name))
		: [];
}

const flat = (text: string) => text.split(/\s+/).join(" ");

it("says to sign in, and asks nothing, without a key", async () => {
	delete process.env.CAMOUFOX_PRO_KEY;

	const result = await fetchPro();

	expect(result.code).toBe(1);
	expect(result.output).toContain("camoufox login");
	expect(asked).toEqual([]);
});

it.each([
	[
		402,
		"subscription_required",
		"This account has no active Camoufox Pro subscription.",
	],
	[403, "account_suspended", "This account is suspended."],
])("tells an account that may not download where to fix it (%i)", async (status, error, message) => {
	gate = [
		status,
		{ error, message, resolution_url: "https://camoufox.test/billing" },
	];

	const result = await fetchPro();

	expect(result.code).toBe(1);
	expect(result.output).toContain(message);
	expect(result.output).toContain("https://camoufox.test/billing");
	expect((await installed()).size).toBe(0);
});

it("installs the newest release and makes it active like any build", async () => {
	publish(FIRST);

	const result = await fetchPro();

	expect(result.code, result.output).toBe(0);
	expect(asked).toEqual([
		{ path: `/api/v1/artifacts/latest/${target}`, auth: `Bearer ${KEY}` },
	]);
	// A signed URL is a credential for the release; it is not printed.
	expect(result.output).not.toContain("secret");
	const build = (await installed()).get(FIRST);
	expect(build?.repoName).toBe("pro");
	expect(build?.isActive).toBe(true);
	expect(path.dirname(build?.path as string)).toBe(
		path.join(tmp, "cache", "camoufox", "browsers", "pro"),
	);
	const { camoufoxPath, launchPath, buildFile } = await import(
		"../src/pkgman.js"
	);
	const { readBuild } = await import("../src/pro.js");
	const executable = launchPath(camoufoxPath());
	expect(executable).toBe(path.join(build?.path as string, "camoufox-bin"));
	expect(readBuild(buildFile(executable, "pro-build.json"))?.version).toBe(
		FIRST,
	);
	expect(partials()).toEqual([]);

	expect((await camoufox("list")).output).toContain(`v${FIRST}`);
	expect((await camoufox("active")).output).toContain(`pro/stable/${FIRST}`);
	expect((await camoufox("set", `pro/stable/${FIRST}`)).output).toContain(
		`Pinned: pro/stable/${FIRST} (installed)`,
	);
	expect((await camoufox("set", `pro/stable/${SECOND}`)).code).toBe(1);
	expect((await camoufox("remove", `pro/${FIRST}`, "-y")).code).toBe(0);
	expect((await installed()).size).toBe(0);
});

const stranger = () => generateKeyPairSync("ed25519");

it.each<[string, () => void]>([
	[
		"a key the launcher does not trust",
		() => publish(FIRST, { key: stranger().privateKey }),
	],
	["a key id the launcher has no key for", () => publish(FIRST, { kid: 0 })],
	["no signature", () => publish(FIRST, { changed: { signature: null } })],
	[
		"a digest that was not signed",
		() => publish(FIRST, { changed: { sha256: "00".repeat(32) } }),
	],
	[
		"another key in the signer's slot",
		() => {
			publish(FIRST);
			trust(stranger().publicKey);
		},
	],
])("refuses, before any download, a release signed by %s", async (_name, arrange) => {
	arrange();

	const result = await fetchPro();

	expect(result.code).toBe(1);
	expect(result.output).toMatch(
		/signature does not verify|not a key trusted here/,
	);
	expect(ranges).toEqual([]);
	expect((await installed()).size).toBe(0);
	expect(partials()).toEqual([]);
});

it("accepts a standby key the launcher ships, as after a rotation", async () => {
	const standby = stranger();
	trust(trusted, standby.publicKey);
	publish(FIRST, { key: standby.privateKey, kid: 2 });

	expect((await fetchPro()).code).toBe(0);
	expect((await installed()).get(FIRST)?.isActive).toBe(true);
});

it("checks the default API with the keys the launcher ships", async () => {
	delete process.env.CAMOUFOX_PRO_API;
	delete process.env.CAMOUFOX_PRO_KEYS;
	shipped = keysFile;
	publish(FIRST);

	expect((await fetchPro()).code).toBe(0);
	expect((await installed()).get(FIRST)?.isActive).toBe(true);
});

it("takes no other keys for the default API than the shipped ones", async () => {
	delete process.env.CAMOUFOX_PRO_API;
	shipped = path.join(tmp, "shipped.json");
	fs.writeFileSync(shipped, '{"keys": []}');
	publish(FIRST);

	// CAMOUFOX_PRO_KEYS names the key that signed the release, and is not read for this API.
	const result = await fetchPro();

	expect(result.code).toBe(1);
	expect(flat(result.output)).toContain("ships no Camoufox Pro release key");
	expect(asked).toEqual([]);
	expect((await installed()).size).toBe(0);
});

it("asks another API nothing without its keys", async () => {
	delete process.env.CAMOUFOX_PRO_KEYS;
	publish(FIRST);

	const result = await fetchPro();

	expect(result.code).toBe(1);
	expect(result.output).toContain("CAMOUFOX_PRO_KEYS");
	expect(asked).toEqual([]);
	expect((await installed()).size).toBe(0);
});

it("ships the signing-keys listing both launchers read", async () => {
	vi.doUnmock("../src/pro-update.js");
	vi.resetModules();
	const { proUpdateDeps, releaseKeys } = await import("../src/pro-update.js");
	expect(fs.realpathSync(proUpdateDeps.releaseKeys)).toBe(
		fs.realpathSync(
			path.join(
				import.meta.dirname,
				"../../python/src/camoufox/pro-release-public-keys.json",
			),
		),
	);
	const listing = JSON.parse(
		fs.readFileSync(proUpdateDeps.releaseKeys, "utf8"),
	);
	expect(Object.keys(listing)).toEqual(["keys"]);
	if (listing.keys.length) {
		expect([...releaseKeys(DEFAULT_API).keys()].sort()).toEqual(
			listing.keys.map((entry: { kid: number }) => entry.kid).sort(),
		);
	}
});

it("does not download a release this launcher is too old to drive", async () => {
	libraryVersion = "0.5.7";
	publish(FIRST, { changed: { min_driver_version: "0.6" } });

	const refused = await fetchPro();

	expect(refused.code).toBe(1);
	expect(flat(refused.output)).toContain(
		"needs camoufox 0.6 or newer, and this is 0.5.7",
	);
	expect(refused.output).toContain("npm install camoufox@latest");
	expect(ranges).toEqual([]);
	expect((await installed()).size).toBe(0);

	publish(FIRST, { changed: { min_driver_version: "0.5.7" } });
	expect((await fetchPro()).code).toBe(0);
});

it.each([
	["version", { signed: { version: "../../elsewhere" } }],
	["target", { signed: { target: "plan9-x86_64" } }],
	["url", { changed: { url: "http://releases.test/159.0-pro.1.zip" } }],
])("refuses a signed answer that is not the release asked for (%s)", async (_name, answer) => {
	publish(FIRST, answer);

	const result = await fetchPro();

	expect(result.code).toBe(1);
	expect(result.output).toContain("not a release to install");
	expect(ranges).toEqual([]);
	expect((await installed()).size).toBe(0);
});

it("does not install a download that is not the release", async () => {
	const archive = publish(FIRST);
	const tampered = Buffer.from(archive);
	tampered[tampered.length - 1] ^= 1;
	served.set(gate[1].url, tampered);

	const result = await fetchPro();

	expect(result.code).toBe(1);
	expect(result.output).toContain("Checksum mismatch");
	expect((await installed()).size).toBe(0);
	expect(partials()).toEqual([]);
});

it("resumes an interrupted download", async () => {
	const archive = publish(FIRST);
	dropAfter = 16384;

	const interrupted = await fetchPro();

	expect(interrupted.code).toBe(1);
	await vi.waitFor(() => {
		expect(partials().map((file) => fs.statSync(file).size)).toEqual([16384]);
	});
	expect((await installed()).size).toBe(0);

	const resumed = await fetchPro();

	expect(resumed.code, resumed.output).toBe(0);
	expect(ranges).toEqual([null, "bytes=16384-"]);
	expect((await installed()).get(FIRST)?.sha256).toBe(
		createHash("sha256").update(archive).digest("hex"),
	);
	expect(partials()).toEqual([]);
});

it("installs a named release, which stays the choice", async () => {
	const named = `/api/v1/artifacts/${FIRST}/${target}`;
	publish(FIRST);

	expect((await fetchPro(FIRST)).code).toBe(0);
	expect(asked.map((call) => call.path)).toEqual([named]);
	expect((await installed()).get(FIRST)?.isActive).toBe(true);

	// A release the API answers with in place of the one named is not installed.
	publish(SECOND);
	expect((await fetchPro(FIRST)).code).toBe(1);
	expect([...(await installed()).keys()]).toEqual([FIRST]);

	// `camoufox fetch` installs the chosen build, which is now that Pro release.
	publish(FIRST);
	expect((await camoufox("fetch")).code).toBe(0);
	expect(asked.map((call) => call.path)).toEqual([named, named, named]);
});

it("follows the Pro channel with `camoufox fetch` once it is chosen", async () => {
	publish(FIRST);
	expect((await fetchPro()).code).toBe(0);
	publish(SECOND);

	const result = await camoufox("fetch");

	expect(result.code, result.output).toBe(0);
	expect((await installed()).get(SECOND)?.isActive).toBe(true);
});

it("installs by name a release numbered after a plus", async () => {
	const numbered = "152.0.4-beta.31+pro.3";
	publish(numbered);

	expect((await fetchPro(numbered)).code).toBe(0);
	expect(asked.map((call) => call.path)).toEqual([
		`/api/v1/artifacts/152.0.4-beta.31%2Bpro.3/${target}`,
	]);
	expect((await installed()).get(numbered)?.isActive).toBe(true);
});
