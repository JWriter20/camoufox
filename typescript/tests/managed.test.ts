/**
 * Port of python/tests/test_managed.py: the `managed` launch option
 * (docs/pro.md, "Managed proxies") -- what the launcher sends, and what it
 * refuses before minting. No network.
 */
import { describe, expect, it } from "vitest";
import {
	type Managed,
	managedRequest,
	ProxyNetwork,
	ProxyProvider,
	ProxyType,
} from "../src/managed.js";

const request = (
	managed: Managed | boolean | undefined,
	profile?: string,
	proxy?: Record<string, string>,
) => managedRequest(managed, { profile, proxy });

describe("the shapes docs/pro.md shows", () => {
	it("a one-off browser", () => {
		expect(
			request({
				provider: ProxyProvider.EVOMI,
				type: ProxyType.ROTATING,
				network: ProxyNetwork.RESIDENTIAL,
				country: "US",
			}),
		).toEqual({
			provider: "evomi",
			type: "rotating",
			network: "residential",
			country: "US",
		});
	});

	it("a CamouLink profile", () => {
		expect(request({ provider: ProxyProvider.CAMOULINK }, "shop-1")).toEqual({
			provider: "camoulink",
		});
	});

	it("a static ISP profile", () => {
		expect(
			request(
				{
					provider: ProxyProvider.EVOMI,
					type: ProxyType.STATIC,
					network: ProxyNetwork.ISP,
				},
				"shop-2",
			),
		).toEqual({ provider: "evomi", type: "static", network: "isp" });
	});

	it("true, false and absent", () => {
		expect(request(true)).toBe(true);
		expect(request(false)).toBe(false);
		expect(request(undefined)).toBeUndefined();
		expect(request(undefined, "shop-1")).toBeUndefined();
	});
});

describe("your own proxy", () => {
	it("asks for no managed one, and conflicts with one", () => {
		const proxy = { server: "http://my.proxy:8080" };
		expect(request(undefined, undefined, proxy)).toBe(false);
		expect(request(false, undefined, proxy)).toBe(false);
		for (const managed of [true, {}]) {
			expect(() => request(managed, undefined, proxy)).toThrow(
				/proxy and managed conflict/,
			);
		}
	});
});

describe("typing", () => {
	it.each([
		["provider", "Evomi", "ProxyProvider"],
		["type", "sticky", "ProxyType"],
		["network", "home", "ProxyNetwork"],
	])("refuses an unknown %s, naming the enum", (field, value, enumName) => {
		expect(() => request({ [field]: value } as Managed)).toThrow(
			new RegExp(enumName),
		);
	});

	it.each(["us", "USA", "U"])("refuses country %s", (country) => {
		expect(() => request({ country })).toThrow(/ISO 3166/);
	});

	it("refuses unknown keys and non-objects", () => {
		expect(() => request({ class: "isp" } as Managed)).toThrow(/unknown keys/);
		expect(() => request("evomi" as unknown as Managed)).toThrow(
			/Managed object, true or false/,
		);
	});
});

describe("what a profile may use", () => {
	it("a static proxy needs a profile", () => {
		expect(() => request({ type: ProxyType.STATIC })).toThrow(
			/belongs to a profile/,
		);
	});

	it("a profile cannot rotate", () => {
		expect(() => request({ type: ProxyType.ROTATING }, "shop-1")).toThrow(
			/cannot be ProxyType.ROTATING/,
		);
	});

	it.each([
		[ProxyNetwork.RESIDENTIAL, "RESIDENTIAL"],
		[ProxyNetwork.DATACENTER, "DATACENTER"],
	])("a partner profile cannot use %s", (network, name) => {
		expect(() =>
			request({ provider: ProxyProvider.EVOMI, network }, "shop-1"),
		).toThrow(new RegExp(name));
	});

	it.each([
		undefined,
		ProxyNetwork.MOBILE,
		ProxyNetwork.ISP,
	])("a partner profile may use %s", (network) => {
		expect(
			request({ provider: ProxyProvider.RAYOBYTE, network }, "shop-1"),
		).toMatchObject({ provider: "rayobyte" });
	});

	it("a CamouLink profile takes any network", () => {
		expect(request({ network: ProxyNetwork.RESIDENTIAL }, "shop-1")).toEqual({
			network: "residential",
		});
	});
});

describe("targeting beyond country (SPEC-EGRESS § 9)", () => {
	it("is sent as given", () => {
		expect(
			request({
				provider: ProxyProvider.EVOMI,
				country: "US",
				region: "New York",
				city: "new_york",
				isp: "comcast",
				asn: "AS7018",
			}),
		).toEqual({
			provider: "evomi",
			country: "US",
			region: "New York",
			city: "new_york",
			isp: "comcast",
			asn: "AS7018",
		});
		expect(request({ asn: 14593 }, "shop-1")).toEqual({ asn: 14593 });
	});

	it.each(["region", "city", "zip"] as const)("%s needs a country", (field) => {
		expect(() => request({ [field]: "10001" })).toThrow(/needs a country/);
	});

	it.each(["AS", "ASN7018", "70-18"])("asn %s is refused", (asn) => {
		expect(() => request({ asn })).toThrow(/asn/);
	});

	it("a zip is short and alphanumeric", () => {
		expect(() => request({ country: "US", zip: "1" })).toThrow(/zip/);
	});
});
