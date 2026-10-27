/**
 * Camoufox Pro: WebGL drawn on a render node.
 *
 * TypeScript twin of python/src/camoufox/render_node.py. A Pro build can send
 * every WebGL call to a renderfarm render node instead of drawing locally: a
 * Windows machine that runs the call on stock Firefox's own ANGLE (Direct3D 11)
 * on its real GPU and sends back the results. The page then sees what Windows
 * Firefox on that GPU shows it, parameters and pixels alike, so a Windows
 * identity needs no WebGL masking and no canvas noise.
 *
 * The node is reached through a renderfarm coordinator. One file names it:
 *
 *     {"coordinator": "http://192.168.1.20:9471", "token": "<client token>", "gpu": "<optional>"}
 *
 * found, in order, at the path launchOptions({ render_node }) passes, the path
 * in CAMOUFOX_RENDER_NODE, or ~/.config/camoufox/render-node.json. `gpu` picks
 * the node's GPU by the WebGL renderer Windows Firefox reports for it, for
 * example "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or
 * similar"; without it the coordinator picks any free GPU.
 *
 * Each browser holds its own assignment on the coordinator, renewed while the
 * browser runs and released when its Pro lease ends.
 */
import { randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { writePrivate } from "./pro.js";

export const CONFIG_ENV = "CAMOUFOX_RENDER_NODE";
/** Read by the build's relay client (additions/pro/angle-relay): the session to open. */
export const RELAY_ENV = "ANGLE_RELAY_CONFIG";
export const ROUTE = "/angle-relay";

/** Where the configuration is looked for last. Mutable for tests. */
export const renderNodeFiles = {
	defaultPath: path.join(
		os.homedir(),
		".config",
		"camoufox",
		"render-node.json",
	),
};

/** Seconds, and mutable for tests. */
export const renderNodeTiming = {
	// The coordinator's assignment lease is one minute; renewing at a third of
	// it survives one lost renewal.
	renewS: 20,
	// How long a launch waits for a GPU when every one is busy.
	busyWaitS: 30,
	timeoutS: 10,
};

export interface RenderNodeConfig {
	coordinator: string;
	token: string;
	gpu: string | null;
}

/** False draws locally; true or null/undefined looks for a configuration; a path or object names one. */
export type RenderNodeOption =
	| null
	| undefined
	| boolean
	| string
	| Record<string, any>;

/** The render node could not be reached or gave no session. */
export class RenderNodeError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "RenderNodeError";
	}
}

/**
 * The render-node configuration a launch uses, or null for local rendering.
 * `option` false turns it off; an object or path names it; null/undefined (or
 * true) looks in CAMOUFOX_RENDER_NODE and then the default file.
 */
export function resolve(option: RenderNodeOption): RenderNodeConfig | null {
	if (option === false) return null;
	if (option === true) option = null;
	if (option != null && typeof option === "object") {
		return validate({ ...option }, "render_node");
	}
	if (option == null) {
		const named = (process.env[CONFIG_ENV] ?? "").trim();
		if (named) option = named;
		else if (isFile(renderNodeFiles.defaultPath))
			option = renderNodeFiles.defaultPath;
		else return null;
	}
	const file = expandUser(option as string);
	let data: unknown;
	try {
		data = JSON.parse(fs.readFileSync(file, "utf-8"));
	} catch (error) {
		throw new RenderNodeError(
			`Cannot read the render node configuration ${file}: ${(error as Error).message}`,
		);
	}
	if (data == null || typeof data !== "object" || Array.isArray(data)) {
		throw new RenderNodeError(`${file} must hold a JSON object.`);
	}
	return validate(data as Record<string, any>, file);
}

function isFile(file: string): boolean {
	try {
		return fs.statSync(file).isFile();
	} catch {
		return false;
	}
}

function expandUser(file: string): string {
	return file === "~" || file.startsWith("~/")
		? path.join(os.homedir(), file.slice(1))
		: file;
}

function validate(data: Record<string, any>, where: string): RenderNodeConfig {
	const unknown = Object.keys(data)
		.filter((k) => !["coordinator", "token", "gpu"].includes(k))
		.sort();
	if (unknown.length) {
		throw new RenderNodeError(
			`${where}: unknown keys ${JSON.stringify(unknown)}`,
		);
	}
	const coordinator = String(data.coordinator || "").replace(/\/+$/, "");
	let parts: URL | null = null;
	try {
		parts = new URL(coordinator);
	} catch {
		parts = null;
	}
	if (
		!parts ||
		!["http:", "https:"].includes(parts.protocol) ||
		!parts.hostname ||
		// An origin only: no path, query or fragment.
		parts.origin !== coordinator
	) {
		throw new RenderNodeError(
			`${where}: coordinator must be an origin such as http://host:9471`,
		);
	}
	if (parts.protocol === "https:") {
		// The relay client speaks plain ws:// to a private-network coordinator only.
		throw new RenderNodeError(
			`${where}: an https coordinator is not supported yet; use the LAN http origin`,
		);
	}
	if (typeof data.token !== "string" || !data.token) {
		throw new RenderNodeError(`${where}: token is required`);
	}
	const gpu = data.gpu;
	if (gpu != null && (typeof gpu !== "string" || !gpu)) {
		throw new RenderNodeError(`${where}: gpu must be a renderer string`);
	}
	return { coordinator, token: data.token, gpu: gpu ?? null };
}

class HttpError extends Error {
	constructor(
		readonly status: number,
		readonly headers: Headers,
		readonly detail: string,
	) {
		super(`HTTP ${status} ${detail}`);
	}
}

const sleep = (ms: number) =>
	new Promise<void>((resolve) => setTimeout(resolve, ms));

/** One browser's assignment on the coordinator, and the file its relay client reads. */
export class Session {
	path = "";
	assignment: Record<string, any> = {};
	#timer: NodeJS.Timeout | null = null;
	#released: Promise<void> | null = null;
	#failures = 0;

	private constructor(
		readonly config: RenderNodeConfig,
		readonly version: string,
	) {}

	/** Take an assignment for Firefox `version` and write its session file in `directory`. */
	static async open(
		config: RenderNodeConfig,
		version: string,
		directory: string,
	): Promise<Session> {
		const session = new Session(config, version);
		session.assignment = await session.#acquire();
		const id: string = session.assignment.assignment_id;
		session.path = path.join(directory, `render-node-${id.slice(0, 16)}.json`);
		writePrivate(
			session.path,
			JSON.stringify({
				endpoint: `ws${config.coordinator.slice("http".length)}${ROUTE}`,
				token: config.token,
				assignment: id,
				profile: session.assignment.profile_digest,
				browser: "firefox",
				version,
			}),
		);
		session.#schedule();
		return session;
	}

	/** The node's routes beyond WebGL: "webgpu" and "text" where its renderer serves them. */
	get routes(): Set<string> {
		return new Set(this.assignment.angle_routes ?? []);
	}

	get gpuSummary(): string {
		return String(this.assignment.hello?.gpu_summary || "");
	}

	async #request(
		method: string,
		route: string,
		body?: Record<string, any>,
	): Promise<any> {
		const response = await fetch(this.config.coordinator + route, {
			method,
			body: body === undefined ? undefined : JSON.stringify(body),
			headers: {
				Authorization: `Bearer ${this.config.token}`,
				"Content-Type": "application/json",
			},
			signal: AbortSignal.timeout(renderNodeTiming.timeoutS * 1000),
		});
		const raw = await response.text();
		if (!response.ok) {
			throw new HttpError(response.status, response.headers, raw.trim());
		}
		return raw ? JSON.parse(raw) : null;
	}

	async #acquire(): Promise<Record<string, any>> {
		const request: Record<string, any> = {
			browser_id: `camoufox-${randomUUID().replaceAll("-", "")}`,
			browser: "firefox",
			version: this.version,
			route: "angle",
		};
		if (this.config.gpu) request.gpu = this.config.gpu;
		const deadline = performance.now() + renderNodeTiming.busyWaitS * 1000;
		for (;;) {
			let answer: any;
			try {
				answer = await this.#request("POST", "/assignments", request);
			} catch (error) {
				if (!(error instanceof HttpError)) {
					throw new RenderNodeError(
						`Cannot reach the render node coordinator: ${(error as Error).message}`,
					);
				}
				const retryAfter = error.headers.get("Retry-After");
				if (
					error.status === 503 &&
					retryAfter &&
					performance.now() < deadline
				) {
					await sleep(Math.min(Number(retryAfter), 5) * 1000);
					continue;
				}
				if (error.status === 503 && error.detail === "no compatible renderer") {
					throw new RenderNodeError(
						`No render node serves Firefox ${this.version}` +
							(this.config.gpu
								? ` on ${JSON.stringify(this.config.gpu)}`
								: "") +
							": the farm needs an ANGLE pack for this exact release.",
					);
				}
				throw new RenderNodeError(
					`The coordinator refused a render node: HTTP ${error.status} ${error.detail}`,
				);
			}
			if (
				answer == null ||
				typeof answer !== "object" ||
				answer.route !== "angle" ||
				!answer.assignment_id
			) {
				throw new RenderNodeError(
					"The coordinator answered with something other than an ANGLE assignment.",
				);
			}
			return answer;
		}
	}

	/** Renew on an unref'd timer, so a held assignment never keeps the process alive. */
	#schedule(): void {
		if (this.#released) return;
		this.#timer = setTimeout(
			() => void this.#renew(),
			renderNodeTiming.renewS * 1000,
		);
		this.#timer.unref();
	}

	async #renew(): Promise<void> {
		try {
			await this.#request(
				"POST",
				`/assignments/${this.assignment.assignment_id}/renew`,
			);
			this.#failures = 0;
		} catch (error) {
			if (this.#released) return;
			if (error instanceof HttpError && error.status === 404) {
				console.warn(
					"camoufox-pro: the render node assignment expired; WebGL in this browser has stopped",
				);
				return;
			}
			this.#failures += 1;
			if (this.#failures === 2) {
				console.warn(
					"camoufox-pro: renewing the render node assignment is failing; retrying",
				);
			}
		}
		this.#schedule();
	}

	/** End the assignment and delete the session file. Only the first call does anything. */
	release(): Promise<void> {
		this.#released ??= (async () => {
			if (this.#timer) clearTimeout(this.#timer);
			try {
				await this.#request(
					"DELETE",
					`/assignments/${this.assignment.assignment_id}`,
				);
			} catch {
				// It lapses by itself within a minute.
			}
			if (this.path) fs.rmSync(this.path, { force: true });
		})();
		return this.#released;
	}
}
