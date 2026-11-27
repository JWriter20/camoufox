/**
 * Mirrors python/tests/test_proxy_dns.py: who resolves names behind a proxy
 * (docs/proxy-dns.md).
 */
import { afterEach, expect, it, vi } from "vitest";
import {
	applyProxyDns,
	applyProxyOptimisticData,
	PROXY_DOH_PREFS,
	utilsDeps,
} from "../src/utils.js";

const SOCKS = {
	server: "socks5://proxy.example:1080",
	username: "u",
	password: "p",
};
const DOH = {
	...PROXY_DOH_PREFS,
	"network.trr.excluded-domains": "proxy.example",
};

async function apply(
	proxyDns: string | undefined,
	proxy: Record<string, string> | undefined,
	exitLookedUp = false,
	config: Record<string, any> = {},
	prefs: Record<string, any> = {},
) {
	await applyProxyDns(proxyDns, proxy, config, prefs, exitLookedUp);
	return [config, prefs];
}

afterEach(() => {
	vi.restoreAllMocks();
});

it.each([
	undefined,
	"doh",
])("a SOCKS5 proxy resolves by DoH (%s)", async (mode) => {
	expect(PROXY_DOH_PREFS["network.trr.mode"]).toBe(3);
	expect(await apply(mode, SOCKS)).toEqual([{}, DOH]);
});

it("a proxy named by address needs no exclusion", async () => {
	for (const server of [
		"socks5://127.0.0.1:1080",
		"socks5://[2001:db8::1]:1080",
	]) {
		expect(await apply(undefined, { server })).toEqual([{}, PROXY_DOH_PREFS]);
	}
});

it("exit mode leaves names to the proxy", async () => {
	expect(await apply("exit", SOCKS)).toEqual([{}, {}]);
});

it.each([
	undefined,
	{ server: "http://proxy.example:8080" },
	{ server: "proxy.example:8080" },
	{ server: "socks4://proxy.example:1080" },
])("only SOCKS5 resolves by DoH (%j)", async (proxy) => {
	expect(await apply(undefined, proxy)).toEqual([{}, {}]);
	expect(await apply("exit", proxy)).toEqual([{}, {}]);
	await expect(apply("doh", proxy)).rejects.toThrow(/socks5/);
});

it("an unknown mode throws", async () => {
	await expect(apply("system", SOCKS)).rejects.toThrow(/proxy_dns/);
});

it("the caller's prefs are kept", async () => {
	const mine = {
		"network.trr.uri": "https://dns.example/dns-query",
		"network.trr.excluded-domains": "a.example",
	};
	const [, prefs] = await apply(undefined, SOCKS, false, {}, { ...mine });
	expect(prefs).toEqual({ ...mine, "network.trr.mode": 3 });
});

it.each([
	[null, { "network:ipv4Only": true }],
	["2001:db8::7", {}],
])("an exit without IPv6 is an IPv4-only network (%s)", async (ipv6, expected) => {
	const asked = vi.spyOn(utilsDeps, "publicIpv6").mockResolvedValue(ipv6);
	expect((await apply(undefined, SOCKS, true))[0]).toEqual(expected);
	expect(asked.mock.calls).toEqual([["socks5://u:p@proxy.example:1080"]]);
});

it("the exit's IPv6 is not asked when already known", async () => {
	const asked = vi.spyOn(utilsDeps, "publicIpv6");
	expect((await apply(undefined, SOCKS, false))[0]).toEqual({});
	for (const known of [
		{ "webrtc:ipv6": "2001:db8::7" },
		{ "network:ipv4Only": false },
	]) {
		expect((await apply(undefined, SOCKS, true, { ...known }))[0]).toEqual(
			known,
		);
	}
	expect((await apply("exit", SOCKS, true))[0]).toEqual({});
	expect(asked).not.toHaveBeenCalled();
});

const OPTIMISTIC = { "network.proxy.socks5_optimistic_data": true };

it.each([SOCKS, undefined])("optimistic data sets the pref (%s)", (proxy) => {
	// With no launch proxy it is for a context's own SOCKS5 proxy.
	const prefs = {};
	applyProxyOptimisticData(true, proxy, prefs);
	expect(prefs).toEqual(OPTIMISTIC);
});

it.each([
	undefined,
	false,
])("optimistic data is off unless asked for (%s)", (enabled) => {
	const prefs = {};
	applyProxyOptimisticData(enabled, SOCKS, prefs);
	expect(prefs).toEqual({});
});

it.each([
	"http://proxy.example:8080",
	"socks4://proxy.example:1080",
])("optimistic data needs a SOCKS5 proxy (%s)", (server) => {
	expect(() => applyProxyOptimisticData(true, { server }, {})).toThrow(
		/socks5/,
	);
});
