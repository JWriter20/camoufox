/**
 * Playwright server mode.
 *
 * TypeScript twin of python/src/server.py. Python has to shell out to
 * the Node runtime bundled with its Playwright driver (and hand it a base64
 * config frame over stdin, via launchServer.js) because there is no Python
 * binding for BrowserServer. Here we already are that runtime, so this calls
 * playwright-core's launchServer() directly.
 */
import { type BrowserServer, firefox } from "playwright-core";
import { type LaunchOptions, launchOptions } from "./utils.js";
import { VirtualDisplay } from "./virtdisplay.js";

export interface LaunchServerOptions extends Omit<LaunchOptions, "headless"> {
	/** Port to listen on. Defaults to a random free port. */
	port?: number;
	/** Path of the websocket endpoint. Defaults to a random path. */
	ws_path?: string;
	/** Whether to run the browser headless. `"virtual"` spawns an Xvfb display. */
	headless?: boolean | "virtual";
}

/**
 * Launch a Playwright server. Takes the same options as `Camoufox()`.
 *
 * Note: persistent contexts are not servable. Playwright's `launchServer`
 * routes through `BrowserType.launch()`, and its `PlaywrightServer` only
 * accepts a pre-launched Browser -- there is no way to expose a persistent
 * BrowserContext over a websocket endpoint. Reject those options up front
 * rather than accepting them and silently launching a throwaway profile.
 */
export async function launchServer({
	port,
	ws_path,
	headless,
	...options
}: LaunchServerOptions = {}): Promise<BrowserServer> {
	for (const unsupported of ["persistent_context", "user_data_dir"] as const) {
		if (options[unsupported]) {
			throw new Error(
				`launchServer() does not support '${unsupported}': Playwright cannot ` +
					"serve a persistent context over a websocket endpoint. Use " +
					"Camoufox({ persistent_context: true, ... }) in-process instead.",
			);
		}
		delete options[unsupported];
	}

	let virtualDisplay: VirtualDisplay | null = null;
	let headlessBool: boolean | undefined;
	if (headless === "virtual") {
		virtualDisplay = new VirtualDisplay(options.debug ?? false);
		options.virtual_display = await virtualDisplay.get();
		headlessBool = false;
	} else {
		headlessBool = headless;
	}

	try {
		const server = await firefox.launchServer({
			...(await launchOptions({ ...options, headless: headlessBool })),
			port,
			wsPath: ws_path,
		});

		if (virtualDisplay) {
			// BrowserServer has no "disconnected" event; "close" fires on shutdown.
			const display = virtualDisplay;
			server.on("close", () => display.kill());
		}

		return server;
	} catch (error) {
		virtualDisplay?.kill();
		throw error;
	}
}
