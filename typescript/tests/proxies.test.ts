/**
 * Port of the catalogue half of python/tests/test_managed.py: the public
 * listing in one shape for every provider, followed page by page.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { TargetUnavailable } from "../src/exceptions.js";
import {
	ProxyDimension,
	ProxyNetwork,
	ProxyProvider,
	ProxyType,
} from "../src/managed.js";
import { listProxies, proxyTargets } from "../src/proxies.js";

const PRODUCT = {
	provider: "rayobyte",
	provider_name: "Rayobyte",
	network: "residential",
	type: "rotating",
	unit: "gb",
	retail_cents: 350,
	sell_cents: 333,
	available: true,
	profiles: false,
	targeting: {
		country: { mode: "open", known: 3 },
		asn: { mode: "open", known: 41 },
		new: {},
	},
	static_ips_available: null,
};

function answer(status: number, body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { "Content-Type": "application/json" },
	});
}

afterEach(() => vi.unstubAllGlobals());

describe("the catalogue", () => {
	it("is one shape for every provider, and pages are followed", async () => {
		const urls: string[] = [];
		vi.stubGlobal(
			"fetch",
			vi.fn(async (url: string) => {
				urls.push(url);
				if (url.endsWith("/api/v1/pro/proxies"))
					return answer(200, { products: [PRODUCT] });
				return url.includes("after=701")
					? answer(200, { options: [{ value: "7018" }], next: null })
					: answer(200, {
							options: [{ value: "701", name: "UUNET", country: "US" }],
							next: "701",
						});
			}),
		);
		const [product] = await listProxies();
		expect(product.provider).toBe(ProxyProvider.RAYOBYTE);
		expect(product.targeting.asn).toEqual({ mode: "open", known: 41 });
		expect(Object.keys(product.targeting)).toEqual(["country", "asn"]);
		const options = await proxyTargets(
			ProxyProvider.RAYOBYTE,
			ProxyNetwork.RESIDENTIAL,
			ProxyType.ROTATING,
			ProxyDimension.ASN,
			{ country: "US" },
		);
		expect(options.map((o) => o.value)).toEqual(["701", "7018"]);
		expect(urls[1]).toMatch(
			/\/api\/v1\/pro\/proxies\/rayobyte\/residential\/rotating\/asn\?limit=1000&country=US$/,
		);
	});

	it("a target the provider lacks is its own error", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () =>
				answer(409, {
					error: "target_unavailable",
					message: "No evomi exit",
					details: { listing: "/x" },
				}),
			),
		);
		await expect(
			proxyTargets(
				ProxyProvider.EVOMI,
				ProxyNetwork.RESIDENTIAL,
				ProxyType.ROTATING,
				ProxyDimension.CITY,
			),
		).rejects.toBeInstanceOf(TargetUnavailable);
	});
});
