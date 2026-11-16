/**
 * Mirrors python/tests/test_fetch_pro.py: `camoufox fetch pro` installs a
 * Camoufox Pro release beside the public builds.
 *
 * The API and the storage its signed URLs point at are a stubbed fetch,
 * answering the download route in the server's shape. A release is a zip
 * signed with a key its own build carries, as a published one is.
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
	"CAMOUFOX_EXECUTABLE_PATH",
];

let tmp: string;
let saved: Record<string, string | undefined>;
let savedArgv: string[];
let output: string;
let finished: boolean;
/** The key the published builds trust, and the gate signs with. */
let signer: KeyObject;
let trusted: KeyObject;
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
	vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
		const headers = new Headers(init?.headers);
		if (url.startsWith(API)) {
			asked.push({
				path: url.slice(API.length),
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
	vi.doUnmock("../src/geolocation.js");
	vi.doUnmock("fpgen-js");
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	fs.rmSync(tmp, { recursive: true, force: true });
});

/** The archive of a release whose build trusts `key`. */
function release(version: string, key: KeyObject = trusted): Buffer {
	const spki = key.export({ format: "der", type: "spki" }).toString("hex");
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

/** Publish `version`; `changed` replaces fields of the answer after it is signed. */
function publish(
	version: string,
	{
		archive = release(version),
		key = signer,
		changed = {},
	}: { archive?: Buffer; key?: KeyObject; changed?: Record<string, any> } = {},
): Buffer {
	const artifact: Record<string, any> = {
		version,
		target,
		filename: "camoufox-pro.zip",
		sha256: createHash("sha256").update(archive).digest("hex"),
		size: archive.length,
		build_hash: `sha256:${"cd".repeat(32)}`,
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
			kid: 1,
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

it("says what a first install's signature was checked against", async () => {
	publish(FIRST);

	const result = await fetchPro();

	expect(flat(result.output)).toContain("No Camoufox Pro build is installed");
});

it("removes a first install whose own key did not sign it", async () => {
	publish(FIRST, { key: generateKeyPairSync("ed25519").privateKey });

	const result = await fetchPro();

	expect(result.code).toBe(1);
	expect(result.output).toContain("signature does not verify");
	expect((await installed()).size).toBe(0);
});

it("checks a later release against the installed build before it is downloaded", async () => {
	publish(FIRST);
	expect((await fetchPro()).code).toBe(0);
	const stranger = generateKeyPairSync("ed25519");
	publish(SECOND, {
		archive: release(SECOND, stranger.publicKey),
		key: stranger.privateKey,
	});
	const downloads = ranges.length;

	const refused = await fetchPro();

	expect(refused.code).toBe(1);
	expect(refused.output).toContain("signature does not verify");
	expect(ranges.length).toBe(downloads);
	expect([...(await installed()).keys()]).toEqual([FIRST]);

	publish(SECOND);
	const accepted = await fetchPro();

	expect(accepted.code, accepted.output).toBe(0);
	expect(flat(accepted.output)).not.toContain(
		"No Camoufox Pro build is installed",
	);
	const builds = await installed();
	expect(builds.get(SECOND)?.isActive).toBe(true);
	expect(builds.get(FIRST)?.isActive).toBe(false);
});

it.each([
	["version", { version: "../../elsewhere" }],
	["target", { target: "plan9-x86_64" }],
	["url", { url: "http://releases.test/159.0-pro.1.zip" }],
])("refuses an answer that is not the release asked for (%s)", async (_name, changed) => {
	publish(FIRST, { changed });

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
