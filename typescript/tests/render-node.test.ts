/**
 * Port of python/tests/test_render_node.py: Camoufox Pro's render node, the
 * coordinator assignment a launch holds and hands to the build.
 *
 * The coordinator is a local fake answering as renderfarm's assignment API does
 * (coordinator/render/assignment_http.go); no test reaches the network.
 */
import * as fs from "node:fs";
import * as http from "node:http";
import type { AddressInfo } from "node:net";
import * as path from "node:path";
import { afterAll, afterEach, beforeEach, describe, expect, it } from "vitest";
import {
	BUNDLE,
	quietly,
	restoreDeps,
	SCRATCH,
	stubHost,
	utils,
} from "./launch-host.js";
import { BUILD, FakeApi, KEY } from "./pro-api.js";

const pro = await import("../src/pro.js");
const renderNode = await import("../src/render_node.js");
const { generateContextFingerprint } = await import("../src/fingerprints.js");
const { hasCanvasPlaceholder } = await import("../src/host_rendering.js");
const modelReady = (await import("./fpgen-setup.js")).MODEL.ok;

const TOKEN = "client-token";
const GPU =
	"ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or similar";

interface Call {
	method: string;
	path: string;
	body: Record<string, any>;
	auth: string | undefined;
}
type Answer = [number, Record<string, string>, unknown];

class FakeCoordinator {
	requests: Call[] = [];
	scripted: Answer[] = [];
	released = new Set<string>();
	routes: string[] | null = null;
	origin = "";
	config: Record<string, any> = {};
	#waiters: (() => void)[] = [];
	server = http.createServer((req, res) => {
		let data = "";
		req.on("data", (chunk) => {
			data += chunk;
		});
		req.on("end", () => {
			const body = data ? JSON.parse(data) : {};
			const method = req.method as string;
			const url = req.url as string;
			const [status, headers, answer] = this.answer(method, url, body);
			this.requests.push({
				method,
				path: url,
				body,
				auth: req.headers.authorization,
			});
			for (const wake of this.#waiters) wake();
			res.writeHead(status, headers);
			res.end(
				answer == null
					? ""
					: typeof answer === "string"
						? answer
						: JSON.stringify(answer),
			);
		});
	});

	calls(method: string, route: string) {
		const pattern = new RegExp(`^${route}$`);
		return this.requests.filter(
			(r) => r.method === method && pattern.test(r.path),
		);
	}

	waitFor(predicate: () => boolean, timeoutMs = 5000): Promise<void> {
		return new Promise((resolve, reject) => {
			const timer = setTimeout(
				() => reject(new Error(JSON.stringify(this.requests))),
				timeoutMs,
			);
			const check = () => {
				if (!predicate()) return;
				clearTimeout(timer);
				this.#waiters = this.#waiters.filter((w) => w !== check);
				resolve();
			};
			this.#waiters.push(check);
			check();
		});
	}

	answer(method: string, url: string, body: Record<string, any>): Answer {
		if (method === "POST" && url === "/assignments") {
			if (this.scripted.length) return this.scripted.shift() as Answer;
			return [
				201,
				{},
				{
					assignment_id: "a".repeat(32),
					browser_id: body.browser_id,
					renderer_id: "win-i9-angle-156.0.1-10de25a0-firefox",
					profile_digest: "d".repeat(64),
					hello: { gpu_summary: "NVIDIA GeForce RTX 3050 Ti Laptop GPU" },
					webgpu_ready: false,
					expires_at: "2026-10-26T17:14:09Z",
					route: "angle",
					...(this.routes ? { angle_routes: this.routes } : {}),
				},
			];
		}
		const match = /^\/assignments\/(\w+)(\/renew)?$/.exec(url);
		if (match && !this.released.has(match[1])) {
			if (method === "DELETE") {
				this.released.add(match[1]);
				return [204, {}, null];
			}
			if (method === "POST" && match[2]) {
				return [200, {}, { assignment_id: match[1], route: "angle" }];
			}
		}
		return [404, {}, null];
	}
}

let api: FakeApi;
let coordinator: FakeCoordinator;
const SAVED_ENV = { ...process.env };
const TIMING = { ...renderNode.renderNodeTiming };
const DIRS: Record<string, string> = {
	XDG_CONFIG_HOME: path.join(SCRATCH, "node-config"),
	// Short enough for a socket path, as /run/user/<uid> is.
	XDG_RUNTIME_DIR: fs.mkdtempSync("/tmp/cf-"),
};
const NAMES = Object.keys(DIRS);
const mode = (file: string) => fs.statSync(file).mode & 0o777;

beforeEach(async () => {
	for (const [name, dir] of Object.entries(DIRS)) {
		process.env[name] = dir;
		fs.rmSync(dir, { recursive: true, force: true });
		fs.mkdirSync(dir, { recursive: true });
	}
	const defaultFile = renderNode.renderNodeFiles.defaultPath;
	fs.rmSync(defaultFile, { force: true });
	fs.mkdirSync(path.dirname(defaultFile), { recursive: true });
	delete process.env[renderNode.CONFIG_ENV];
	delete process.env[pro.LEASE_FILE_ENV];
	process.env[pro.KEY_ENV] = KEY;
	api = new FakeApi();
	coordinator = new FakeCoordinator();
	for (const server of [api.server, coordinator.server]) {
		await new Promise<void>((resolve) =>
			server.listen(0, "127.0.0.1", resolve),
		);
	}
	process.env[pro.API_ENV] =
		`http://127.0.0.1:${(api.server.address() as AddressInfo).port}`;
	coordinator.origin = `http://127.0.0.1:${(coordinator.server.address() as AddressInfo).port}`;
	coordinator.config = {
		coordinator: coordinator.origin,
		token: TOKEN,
		gpu: GPU,
	};
	Object.assign(pro.proTiming, { mintRetryS: [0, 0, 0] });
});

afterEach(async () => {
	await pro.releaseAll();
	Object.assign(renderNode.renderNodeTiming, TIMING);
	restoreDeps();
	for (const server of [api.server, coordinator.server]) {
		await new Promise((resolve) => server.close(resolve));
	}
});

afterAll(() => {
	fs.rmSync(DIRS.XDG_RUNTIME_DIR, { recursive: true, force: true });
	for (const name of [...NAMES, pro.API_ENV, pro.KEY_ENV]) {
		if (SAVED_ENV[name] === undefined) delete process.env[name];
		else process.env[name] = SAVED_ENV[name];
	}
});

const writeDefault = () =>
	fs.writeFileSync(
		renderNode.renderNodeFiles.defaultPath,
		JSON.stringify(coordinator.config),
	);

describe("the configuration", () => {
	it("draws locally when nothing is configured", () => {
		expect(renderNode.resolve(undefined)).toBeNull();
		expect(renderNode.resolve(false)).toBeNull();
	});

	it("is found where documented", () => {
		writeDefault();
		expect(renderNode.resolve(undefined)?.coordinator).toBe(coordinator.origin);
		const named = path.join(SCRATCH, "other-node.json");
		fs.writeFileSync(
			named,
			JSON.stringify({ ...coordinator.config, gpu: null }),
		);
		process.env[renderNode.CONFIG_ENV] = named;
		expect(renderNode.resolve(undefined)?.gpu).toBeNull();
		expect(renderNode.resolve(false)).toBeNull();
	});

	it.each([
		[{ coordinator: "https://farm.example", token: "t" }, /https/],
		[{ coordinator: "http://farm.example/path", token: "t" }, /origin/],
		[{ coordinator: "http://farm.example" }, /token/],
		[
			{ coordinator: "http://farm.example", token: "t", adapter: "x" },
			/unknown/,
		],
	])("refuses a bad one (%j)", (config, error) => {
		expect(() => renderNode.resolve(config)).toThrow(
			renderNode.RenderNodeError,
		);
		expect(() => renderNode.resolve(config)).toThrow(error);
	});
});

describe.runIf(modelReady)("a launch", () => {
	let build: string;
	let proBuild: string;
	let config: Record<string, any>;

	beforeEach(() => {
		stubHost();
		config = {};
		utils.utilsDeps.getEnvVars = (drawn: Record<string, any>) => {
			Object.assign(config, drawn);
			return {};
		};
		const copy = (name: string) => {
			const dir = fs.mkdtempSync(path.join(SCRATCH, `${name}-`));
			fs.cpSync(BUNDLE, dir, { recursive: true });
			return path.join(dir, "camoufox-bin");
		};
		build = copy("build");
		proBuild = copy("pro-build");
		fs.writeFileSync(
			path.join(path.dirname(proBuild), "pro-build.json"),
			JSON.stringify(BUILD),
		);
	});

	const rules = (rule: Record<string, any>) =>
		fs.writeFileSync(
			path.join(path.dirname(proBuild), "launch.json"),
			JSON.stringify({ rules: [{ target: ["win"], ...rule }] }),
		);

	const launch = (options: Record<string, any> = {}) =>
		quietly(() =>
			utils.launchOptions({
				executable_path: proBuild,
				headless: true,
				os: "windows",
				i_know_what_im_doing: true,
				...options,
			}),
		);
	const heldLease = (opts: Record<string, any>) =>
		pro.LIVE.get(opts.env[pro.LEASE_FILE_ENV]) as InstanceType<
			typeof pro.Lease
		>;

	it("holds an assignment and hands the build its session", async () => {
		rules({ prefs: { "webgl.holo-point-sprites": true } });
		const opts = await launch({
			os: undefined,
			render_node: coordinator.config,
		});

		const [acquire, ...more] = coordinator.calls("POST", "/assignments");
		expect(more).toEqual([]);
		expect(acquire.auth).toBe(`Bearer ${TOKEN}`);
		const { browser_id, ...request } = acquire.body;
		expect(browser_id).toMatch(/^camoufox-[0-9a-f]{32}$/);
		expect(request).toEqual({
			browser: "firefox",
			version: "152.0.4",
			route: "angle",
			gpu: GPU,
		});
		const sessionFile = opts.env[renderNode.RELAY_ENV];
		expect(mode(sessionFile)).toBe(0o600);
		expect(JSON.parse(fs.readFileSync(sessionFile, "utf-8"))).toEqual({
			endpoint: `${coordinator.origin.replace("http", "ws")}/angle-relay`,
			token: TOKEN,
			assignment: "a".repeat(32),
			profile: "d".repeat(64),
			browser: "firefox",
			version: "152.0.4",
		});
		// The node renders, so nothing local is asked of the farm, masked or covered.
		expect(api.calls("/api/v1/leases")[0].body.gpu).toBe(false);
		expect(config["navigator.platform"]).toBe("Win32");
		expect(Object.keys(config).filter((k) => /^webGl2?:/.test(k))).toEqual([]);
		const prefs = opts.firefoxUserPrefs;
		expect(prefs["webgl.holo-point-sprites"]).toBe(false);
		expect(prefs["webgl.force-enabled"]).toBe(true);
		expect(prefs["webgl.enable-webgl2"]).toBe(true);
		expect(prefs["gfx.canvas.accelerated"]).toBe(true);
		expect(prefs["gfx.canvas.accelerated.force-enabled"]).toBe(true);
		expect(hasCanvasPlaceholder(prefs)).toBe(false);
		// This node serves no WebGPU, so WebGPU stays as a launch without one has it.
		expect("dom.webgpu.renderfarm.node" in prefs).toBe(false);
	});

	it.each([
		[null],
		[["text", "webgpu"]],
	])("runs WebGPU on a node that serves it (routes %j)", async (routes) => {
		rules({
			config: { "webGPU:d3d12Limits": true },
			env: { HOLO_WEBGPU_NV_ARCH: "ampere" },
		});
		coordinator.routes = routes;
		const opts = await launch({ render_node: coordinator.config });
		const prefs = opts.firefoxUserPrefs;
		expect(prefs["dom.webgpu.enabled"]).toBe(true);
		// The node's own adapter answers, so nothing local stands in for D3D12.
		const onNode = routes !== null;
		expect(prefs["dom.webgpu.renderfarm.node"] ?? false).toBe(onNode);
		expect("webGPU:d3d12Limits" in config).toBe(!onNode);
		expect("HOLO_WEBGPU_NV_ARCH" in opts.env).toBe(!onNode);
	});

	it.each([
		[null],
		[["webgpu"]],
		[["text"]],
	])("rasterizes text on a node that serves it (routes %j)", async (routes) => {
		coordinator.routes = routes;
		const opts = await launch({ render_node: coordinator.config });
		const sessionFile = opts.env[renderNode.RELAY_ENV];
		if (!routes?.includes("text")) {
			// The build keeps rasterizing with its bundled DirectWrite.
			expect(renderNode.TEXT_ENV in opts.env).toBe(false);
			return;
		}
		// The build binds it, in the session file's private directory.
		const socket = opts.env[renderNode.TEXT_ENV];
		expect(path.dirname(socket)).toBe(path.dirname(sessionFile));
		expect(mode(path.dirname(socket))).toBe(0o700);
		expect(fs.existsSync(socket)).toBe(false);
		fs.writeFileSync(socket, "");
		await heldLease(opts).release();
		expect(fs.existsSync(socket)).toBe(false);
	});

	it("refuses a socket path too long to bind", async () => {
		const runtime = path.join(SCRATCH, "r".repeat(80));
		fs.mkdirSync(runtime, { recursive: true });
		process.env.XDG_RUNTIME_DIR = runtime;
		coordinator.routes = ["text"];
		await expect(launch({ render_node: coordinator.config })).rejects.toThrow(
			/longer than a Unix socket path/,
		);
		expect(coordinator.calls("DELETE", ".*").map((r) => r.path)).toEqual([
			`/assignments/${"a".repeat(32)}`,
		]);
		expect(
			fs
				.readdirSync(path.join(runtime, "camoufox-pro"))
				.filter((name) => name.startsWith("render-node-")),
		).toEqual([]);
	});

	it("releases the assignment when the lease ends", async () => {
		const opts = await launch({ render_node: coordinator.config });
		const sessionFile = opts.env[renderNode.RELAY_ENV];
		await heldLease(opts).release();
		expect(coordinator.calls("DELETE", ".*").map((r) => r.path)).toEqual([
			`/assignments/${"a".repeat(32)}`,
		]);
		expect(fs.existsSync(sessionFile)).toBe(false);
	});

	it("renews the assignment while the browser runs", async () => {
		renderNode.renderNodeTiming.renewS = 0.02;
		const opts = await launch({ render_node: coordinator.config });
		await coordinator.waitFor(
			() => coordinator.calls("POST", "/assignments/\\w+/renew").length >= 2,
		);
		await heldLease(opts).release();
	});

	it("waits for a busy farm", async () => {
		coordinator.scripted.push([503, { "Retry-After": "0" }, "capacity full"]);
		const opts = await launch({ render_node: coordinator.config });
		expect(coordinator.calls("POST", "/assignments")).toHaveLength(2);
		expect(renderNode.RELAY_ENV in opts.env).toBe(true);
	});

	it("names a release no node serves, and releases the lease", async () => {
		coordinator.scripted.push([503, {}, "no compatible renderer\n"]);
		const launched = launch({ render_node: coordinator.config });
		await expect(launched).rejects.toBeInstanceOf(renderNode.RenderNodeError);
		await expect(launched).rejects.toThrow(/Firefox 152\.0\.4 on .*ANGLE pack/);
		expect(api.calls(".*/release").map((r) => r.body)).toEqual([
			{ reason: "error" },
		]);
	});

	it("uses the default file for a Pro build", async () => {
		writeDefault();
		const opts = await launch({ os: undefined });
		expect(renderNode.RELAY_ENV in opts.env).toBe(true);
	});

	it("uses a named node with a caller-managed lease", async () => {
		const ownLease = path.join(SCRATCH, "mine.json");
		process.env[pro.LEASE_FILE_ENV] = ownLease;
		const opts = await launch({ render_node: coordinator.config });
		expect(opts.env[pro.LEASE_FILE_ENV]).toBe(ownLease);
		const session = await pro.claim(opts);
		expect(session?.grants).toEqual({});
		await session?.close();
		expect(coordinator.calls("DELETE", ".*").map((r) => r.path)).toEqual([
			`/assignments/${"a".repeat(32)}`,
		]);
		expect(await pro.claim(opts)).toBeNull();
	});

	it("ignores the default file on a stock build, and refuses a named node there", async () => {
		writeDefault();
		const opts = await launch({ executable_path: build });
		expect(renderNode.RELAY_ENV in opts.env).toBe(false);
		await expect(
			launch({ executable_path: build, render_node: coordinator.config }),
		).rejects.toThrow(/Camoufox Pro build/);
		expect(coordinator.requests).toEqual([]);
	});

	it("draws another OS locally unless the node was named", async () => {
		writeDefault();
		const opts = await launch({ os: "macos" });
		expect(renderNode.RELAY_ENV in opts.env).toBe(false);
		expect(coordinator.requests).toEqual([]);
		await expect(
			launch({ os: "macos", render_node: coordinator.config }),
		).rejects.toThrow(/Windows/);
	});

	it("refuses a named GPU, which conflicts with the node's own", async () => {
		await expect(
			launch({
				render_node: coordinator.config,
				webgl_config: ["Google Inc. (NVIDIA)", GPU],
			}),
		).rejects.toThrow(/render_node: false/);
		expect(coordinator.calls("DELETE", ".*")).not.toEqual([]);
	});
});

describe.runIf(modelReady)("contexts of a node browser", () => {
	it("are Windows and claim no WebGL values", () => {
		const browser = { _camoufoxRenderNode: true };
		expect(utils.contextIdentity(browser, undefined, undefined)).toEqual([
			"windows",
			null,
		]);
		expect(() => utils.contextIdentity(browser, "linux", undefined)).toThrow(
			/render node/,
		);
		const fp = generateContextFingerprint({
			os: "windows",
			ff_version: "152",
			webgl: false,
		});
		expect(Object.keys(fp.config).filter((k) => /^webGl2?:/.test(k))).toEqual(
			[],
		);
	});
});
