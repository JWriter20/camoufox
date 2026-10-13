/**
 * Camoufox Pro managed proxies: the `managed` launch option (docs/pro.md,
 * "Managed proxies"). TypeScript twin of python/src/camoufox/managed.py.
 *
 * The launcher checks the request before minting so a mistake fails here, with
 * a message naming the option, rather than as an API refusal. The API applies
 * the same rules and every default.
 */
import { ValueError } from "./pycompat.js";

/** Who runs the exit. `CAMOULINK` is Camoufox Pro's own Starlink network. */
export const ProxyProvider = {
	CAMOULINK: "camoulink",
	EVOMI: "evomi",
	RAYOBYTE: "rayobyte",
} as const;
export type ProxyProvider = (typeof ProxyProvider)[keyof typeof ProxyProvider];

/** `ROTATING`: a fresh exit each launch, kept for that browser's life. `STATIC`: an exit that stays. */
export const ProxyType = {
	ROTATING: "rotating",
	STATIC: "static",
} as const;
export type ProxyType = (typeof ProxyType)[keyof typeof ProxyType];

/** What kind of address the exit is. */
export const ProxyNetwork = {
	RESIDENTIAL: "residential",
	MOBILE: "mobile",
	ISP: "isp",
	DATACENTER: "datacenter",
} as const;
export type ProxyNetwork = (typeof ProxyNetwork)[keyof typeof ProxyNetwork];

/**
 * What an exit can be targeted by. Which a product takes, and their values,
 * are in the listing (`listProxies`, `proxyTargets` in proxies.ts).
 */
export const ProxyDimension = {
	COUNTRY: "country",
	REGION: "region",
	CITY: "city",
	ZIP: "zip",
	ISP: "isp",
	ASN: "asn",
} as const;
export type ProxyDimension =
	(typeof ProxyDimension)[keyof typeof ProxyDimension];

/**
 * The managed proxy a launch asks for. A field left out takes the API's
 * default: provider `CAMOULINK`; type `STATIC` with a profile, `ROTATING`
 * without; network `RESIDENTIAL`, or `ISP` for a static exit from a partner;
 * any country.
 *
 * Targeting beyond country: `region` and `city` take a name or its listing
 * value ("New York" and "new_york" are the same), `zip` a postal code, `isp`
 * the listing's value, `asn` 14593 or "AS14593". A region, city or ZIP needs a
 * country. A product that cannot be targeted by a field refuses it.
 */
export interface Managed {
	provider?: ProxyProvider;
	type?: ProxyType;
	network?: ProxyNetwork;
	country?: string;
	region?: string;
	city?: string;
	zip?: string;
	isp?: string;
	asn?: number | string;
}

// A profile keeps one IP for life, so it exits only through networks that can hold one.
const PROFILE_PARTNER_NETWORKS: readonly ProxyNetwork[] = [
	ProxyNetwork.MOBILE,
	ProxyNetwork.ISP,
];
const COUNTRY = /^[A-Z]{2}$/;
const ASN = /^(?:AS)?[0-9]{1,10}$/i;
const ZIP = /^[A-Za-z0-9]{2,10}$/;
// A place name is ambiguous across countries.
const NEEDS_COUNTRY = ["region", "city", "zip"] as const;
const FIELDS = [
	"provider",
	"type",
	"network",
	"country",
	"region",
	"city",
	"zip",
	"isp",
	"asn",
] as const;

function member(
	name: string,
	value: unknown,
	values: Record<string, string>,
	enumName: string,
): void {
	if (value !== undefined && !Object.values(values).includes(value as string)) {
		throw new TypeError(
			`managed ${name} must be a ${enumName} value (e.g. ${enumName}.${Object.keys(values)[0]}), not ${JSON.stringify(value)}`,
		);
	}
}

function nameOf(values: Record<string, string>, value: string): string {
	return Object.keys(values).find((key) => values[key] === value) ?? value;
}

/** The mint request's `managed` field, or undefined to leave it out. */
export function managedRequest(
	managed: Managed | boolean | undefined,
	{
		profile,
		proxy,
	}: { profile?: string | null; proxy?: Record<string, any> | null },
): Managed | boolean | undefined {
	if (
		managed !== undefined &&
		typeof managed !== "boolean" &&
		(managed === null || typeof managed !== "object")
	) {
		throw new TypeError(
			`managed must be a Managed object, true or false, not ${JSON.stringify(managed)}`,
		);
	}
	// The caller's own proxy is never replaced by a managed one, and asking for
	// both is a mistake worth naming rather than a request to drop one quietly.
	if (proxy != null) {
		if (managed !== undefined && managed !== false) {
			throw new ValueError(
				"proxy and managed conflict: with your own proxy there is no managed proxy. " +
					"Pass proxy to use your own, or managed (and no proxy) for a managed one.",
			);
		}
		return false;
	}
	if (typeof managed !== "object") return managed;
	const unknown = Object.keys(managed).filter(
		(key) => !(FIELDS as readonly string[]).includes(key),
	);
	if (unknown.length) {
		throw new TypeError(`managed has unknown keys: ${unknown.join(", ")}`);
	}
	member("provider", managed.provider, ProxyProvider, "ProxyProvider");
	member("type", managed.type, ProxyType, "ProxyType");
	member("network", managed.network, ProxyNetwork, "ProxyNetwork");
	if (
		managed.country !== undefined &&
		(typeof managed.country !== "string" || !COUNTRY.test(managed.country))
	) {
		throw new ValueError(
			`managed country must be an upper-case ISO 3166 code such as "US", not ${JSON.stringify(managed.country)}`,
		);
	}
	for (const name of ["region", "city", "isp"] as const) {
		const value = managed[name];
		if (
			value !== undefined &&
			(typeof value !== "string" || value.trim() === "")
		) {
			throw new ValueError(
				`managed ${name} must be a non-empty string, not ${JSON.stringify(value)}`,
			);
		}
	}
	if (
		managed.zip !== undefined &&
		(typeof managed.zip !== "string" || !ZIP.test(managed.zip))
	) {
		throw new ValueError(
			`managed zip must be 2-10 letters or digits, not ${JSON.stringify(managed.zip)}`,
		);
	}
	if (
		managed.asn !== undefined &&
		((typeof managed.asn !== "number" && typeof managed.asn !== "string") ||
			!ASN.test(String(managed.asn)))
	) {
		throw new ValueError(
			`managed asn must be a number such as 14593 or "AS14593", not ${JSON.stringify(managed.asn)}`,
		);
	}
	const place = NEEDS_COUNTRY.find((name) => managed[name] !== undefined);
	if (place !== undefined && managed.country === undefined) {
		throw new ValueError(
			`managed ${place} needs a country: a place name is ambiguous without one`,
		);
	}
	const hasProfile = profile != null;
	const kind =
		managed.type ?? (hasProfile ? ProxyType.STATIC : ProxyType.ROTATING);
	if (kind === ProxyType.STATIC && !hasProfile) {
		throw new ValueError(
			"A STATIC managed proxy belongs to a profile: pass profile too, or use ProxyType.ROTATING.",
		);
	}
	if (kind === ProxyType.ROTATING && hasProfile) {
		throw new ValueError(
			"A profile keeps its IP for life, so its managed proxy cannot be ProxyType.ROTATING.",
		);
	}
	if (
		hasProfile &&
		managed.provider !== undefined &&
		managed.provider !== ProxyProvider.CAMOULINK
	) {
		const network = managed.network ?? ProxyNetwork.ISP;
		if (!PROFILE_PARTNER_NETWORKS.includes(network)) {
			throw new ValueError(
				"A profile exits through ProxyProvider.CAMOULINK, or a partner's ProxyNetwork.MOBILE or " +
					`ProxyNetwork.ISP, so it matches its IP for life; not ${nameOf(ProxyNetwork, network)}.`,
			);
		}
	}
	const wire: Managed = {};
	for (const field of FIELDS) {
		if (managed[field] !== undefined) (wire as any)[field] = managed[field];
	}
	return wire;
}
