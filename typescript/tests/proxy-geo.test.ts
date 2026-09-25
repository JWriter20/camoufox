/**
 * NewContext derives the WebRTC IP and timezone from the proxy's exit IP.
 * Twin of pythonlib/tests/test_proxy_geo.py: the lookup must go through the
 * proxy as Playwright would reach it (a scheme-less server is http), and a
 * failed lookup must raise rather than leave the context on the host's values.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const impit = vi.hoisted(() => ({
	proxyUrls: [] as (string | undefined)[],
	respond: async (): Promise<unknown> => ({}),
}));

vi.mock("impit", () => ({
	Impit: class {
		// ip.ts caches one client per proxy URL, so record each request.
		constructor(private options: { proxyUrl?: string }) {}
		async fetch() {
			impit.proxyUrls.push(this.options.proxyUrl);
			const body = await impit.respond();
			return { ok: true, status: 200, json: async () => body };
		}
	},
}));

const { NewContext } = await import("../src/sync_api.js");
const { InvalidIP } = await import("../src/exceptions.js");

const EXIT = {
	status: "success",
	query: "203.0.113.7",
	timezone: "Europe/Paris",
};

function fakeBrowser() {
	const calls: { options?: any; script?: string } = {};
	const browser = {
		newContext: async (options: any) => {
			calls.options = options;
			return {
				addInitScript: async (script: string) => {
					calls.script = script;
				},
			};
		},
	};
	return { browser: browser as any, calls };
}

beforeEach(() => {
	impit.proxyUrls.length = 0;
	impit.respond = async () => EXIT;
});

describe("NewContext proxy lookup", () => {
	it.each([
		["1.2.3.4:8080", "http://u:p@1.2.3.4:8080"],
		["proxy.example.com:8080", "http://u:p@proxy.example.com:8080"],
		["http://proxy.example.com:8080", "http://u:p@proxy.example.com:8080"],
		["socks5://proxy.example.com:1080", "socks5://u:p@proxy.example.com:1080"],
	])("%s is looked up through %s", async (server, expected) => {
		const { browser, calls } = fakeBrowser();
		await NewContext(browser, {
			os: "linux",
			proxy: { server, username: "u", password: "p" },
		});
		expect(impit.proxyUrls).toContain(expected);
		expect(calls.options.timezoneId).toBe("Europe/Paris");
		expect(calls.script).toContain("203.0.113.7");
	});

	it.each([
		[
			"an unreachable proxy",
			async () => {
				throw new Error("proxy refused");
			},
		],
		[
			"a failed lookup",
			async () => ({ status: "fail", message: "private range" }),
		],
	])("%s raises instead of launching without the values", async (_, respond) => {
		impit.respond = respond;
		const { browser } = fakeBrowser();
		const launched = NewContext(browser, {
			os: "linux",
			proxy: { server: "1.2.3.4:8080" },
		});
		await expect(launched).rejects.toThrow(InvalidIP);
		await expect(launched).rejects.toThrow(/webrtc_ip/);
	});

	it("skips the lookup when both values are given", async () => {
		const { browser } = fakeBrowser();
		await NewContext(browser, {
			os: "linux",
			proxy: { server: "1.2.3.4:8080" },
			webrtc_ip: "198.51.100.1",
			timezoneId: "UTC",
		} as any);
		expect(impit.proxyUrls).toEqual([]);
	});
});
