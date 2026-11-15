/**
 * Port of python/tests/test_proxy_session.py and the name/CamouLink half of
 * test_managed.py: a standard build's managed proxy is a proxy session, opened,
 * claimed once, renewed and released; the launcher checks what it can before
 * any call; the API's notices are said.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PlanRequired } from "../src/exceptions.js";
import {
	managedRequest,
	ProxyNetwork,
	ProxyProvider,
	ProxyType,
} from "../src/managed.js";
import { logNotices } from "../src/pro.js";
import { listStatics, releaseStatic } from "../src/proxies.js";
import { claim, ProxySession } from "../src/proxy_session.js";

const SESSION = {
	session_id: "pxs_0191f3a2-7c1e-7b52-9a10-3f9e6d5a1c22",
	username: "pxs_0191f3a2-7c1e-7b52-9a10-3f9e6d5a1c22",
	server: "http://egress.camoufox.com:7190",
	password: "secret",
	class: "residential",
	country: "US",
	sticky: true,
	exit_ip: "203.0.113.5",
	binding_id: 7,
	name: null,
	expires_at: "2026-11-04T18:00:00Z",
	heartbeat_s: 60,
	notices: [{ code: "static_ip_replaced", message: "A new IP." }],
};

function answer(status: number, body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { "Content-Type": "application/json" },
	});
}

type Call = { url: string; method: string; body: any };

function fakeApi(
	reply: (call: Call) => Response = (call) =>
		call.url.endsWith("/api/v1/proxy/sessions")
			? answer(201, SESSION)
			: answer(200, {}),
): Call[] {
	const calls: Call[] = [];
	vi.stubGlobal(
		"fetch",
		vi.fn(async (url: string, init: RequestInit) => {
			const call = {
				url,
				method: String(init.method),
				body: init.body ? JSON.parse(String(init.body)) : null,
			};
			calls.push(call);
			return reply(call);
		}),
	);
	return calls;
}

beforeEach(() => vi.stubEnv("CAMOUFOX_PRO_KEY", "cfp_live_test"));
afterEach(() => {
	vi.unstubAllGlobals();
	vi.unstubAllEnvs();
	vi.useRealTimers();
	vi.restoreAllMocks();
});

describe("a proxy session", () => {
	it("opens with the request, gives the launch its proxy, and is claimed once", async () => {
		const calls = fakeApi();
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		const session = await ProxySession.open({ provider: "evomi" });
		expect(calls[0].body).toEqual({
			managed: { provider: "evomi" },
			client: expect.stringMatching(/^camoufox-js\//),
		});
		expect(session.proxy()).toEqual({
			server: SESSION.server,
			username: SESSION.username,
			password: "secret",
			bypass: "localhost,127.0.0.1,::1,*.local",
		});
		expect(warn).toHaveBeenCalledWith(
			"camoufox: A new IP. (static_ip_replaced)",
		);
		const options = { proxy: session.proxy() };
		expect(claim(options)).toBe(session);
		expect(() => claim(options)).toThrow(/already held by another browser/);
		await session.release();
	});

	it("is renewed while it lives and released once", async () => {
		vi.useFakeTimers();
		const calls = fakeApi();
		const session = await ProxySession.open(true);
		await vi.advanceTimersByTimeAsync(70_000);
		expect(
			calls.some((c) => c.url.endsWith(`${SESSION.session_id}/heartbeat`)),
		).toBe(true);
		await Promise.all([session.release(), session.release()]);
		const releases = calls.filter((c) => c.url.endsWith("/release"));
		expect(releases).toHaveLength(1);
		expect(releases[0].body).toEqual({ reason: "clean_exit" });
		expect(claim({ proxy: { username: SESSION.username } })).toBeNull();
	});

	it("is refused with the API's own error", async () => {
		fakeApi(() =>
			answer(402, {
				error: "plan_required",
				message: "CamouLink is part of Camoufox Pro.",
				details: { feature: "camoulink" },
			}),
		);
		await expect(
			ProxySession.open({ provider: "camoulink" }),
		).rejects.toBeInstanceOf(PlanRequired);
	});
});

describe("what the launcher checks before any call", () => {
	it("a name keeps a static exit, never a rotating one, and never beside a profile", () => {
		expect(
			managedRequest(
				{
					provider: ProxyProvider.EVOMI,
					network: ProxyNetwork.ISP,
					name: "shop-1",
				},
				{},
			),
		).toEqual({
			provider: "evomi",
			network: "isp",
			name: "shop-1",
		});
		expect(() => managedRequest({ type: ProxyType.STATIC }, {})).toThrow(
			/kept by a profile or a name/,
		);
		expect(() =>
			managedRequest({ type: ProxyType.ROTATING, name: "x" }, {}),
		).toThrow(/A name keeps a STATIC exit/);
		expect(() => managedRequest({ name: "has space" }, {})).toThrow(
			/managed name must be/,
		);
		expect(() => managedRequest({ name: "x" }, { profile: "shop" })).toThrow(
			/name and profile conflict/,
		);
	});

	it("CamouLink is residential Starlink, by country and AS14593 only", () => {
		const camoulink = ProxyProvider.CAMOULINK;
		expect(() =>
			managedRequest({ provider: camoulink, network: ProxyNetwork.ISP }, {}),
		).toThrow(/residential Starlink/);
		expect(() =>
			managedRequest(
				{ provider: camoulink, country: "US", city: "austin" },
				{},
			),
		).toThrow(/cannot be targeted by city/);
		expect(() =>
			managedRequest({ provider: camoulink, asn: 7018 }, {}),
		).toThrow(/AS14593/);
		expect(managedRequest({ provider: camoulink, asn: "AS14593" }, {})).toEqual(
			{
				provider: "camoulink",
				asn: "AS14593",
			},
		);
		// Unnamed, the provider is the API's to choose (a partner on a standard build).
		expect(
			managedRequest({ network: ProxyNetwork.ISP, name: "shop-ip" }, {}),
		).toEqual({
			network: "isp",
			name: "shop-ip",
		});
	});
});

describe("notices and named statics", () => {
	it("says each notice and skips what is not one", () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		logNotices([{ code: "x", message: "Said." }, { code: "y" }, "junk"]);
		logNotices(undefined);
		expect(warn).toHaveBeenCalledOnce();
		expect(warn).toHaveBeenCalledWith("camoufox: Said. (x)");
	});

	it("lists and releases the account's named statics", async () => {
		const held = {
			name: "shop/1",
			provider: "evomi",
			type: "static",
			network: "isp",
			country: "US",
			exit_ip: "203.0.113.10",
			paid_through: "2026-12-04T00:00:00Z",
			monthly_cents: 95,
		};
		const calls = fakeApi((call) =>
			call.method === "GET"
				? answer(200, { statics: [held] })
				: answer(200, { released: true }),
		);
		expect(await listStatics()).toEqual([held]);
		await releaseStatic("shop/1");
		expect(calls[1]).toMatchObject({ method: "DELETE" });
		expect(calls[1].url.endsWith("/api/v1/proxy/statics/shop%2F1")).toBe(true);
		await expect(releaseStatic("")).rejects.toThrow(TypeError);
	});
});
