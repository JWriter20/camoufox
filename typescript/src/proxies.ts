/**
 * Camoufox Pro's managed proxy catalogue: what each provider sells and what it
 * can be targeted at, in the same shape whatever the provider (docs/pro.md,
 * "Managed proxies"). Public: no key is needed. TypeScript twin of
 * python/src/camoufox/proxies.py.
 *
 * A value from `proxyTargets` is what `managed` takes:
 * `{ provider: ProxyProvider.EVOMI, country: "US", city: cities[0].value }`.
 */
import type {
	ProxyDimension,
	ProxyNetwork,
	ProxyProvider,
	ProxyType,
} from "./managed.js";
import { ProxyDimension as Dimensions } from "./managed.js";
import { call, resolveKey } from "./pro.js";

const PAGE = 1000;

/** How a product is targeted on one dimension. `listed`: the provider names its values; `open`: any well-formed value is tried, and the known ones are those Camoufox Pro has exited on. */
export interface Targeting {
	mode: "listed" | "open";
	known: number;
}

export interface ProxyProduct {
	provider: ProxyProvider;
	providerName: string;
	network: ProxyNetwork;
	type: ProxyType;
	/** "gb" for bandwidth, "ip_month" for a static IP's rent. */
	unit: string;
	sellCents: number;
	retailCents: number | null;
	/** Whether an exit for it can be had right now. */
	available: boolean;
	/** Whether a profile may use it (a profile keeps its IP: mobile, static ISP or CamouLink). */
	profiles: boolean;
	targeting: Partial<Record<ProxyDimension, Targeting>>;
	/** A static product's IPs in stock; null for bandwidth products. */
	staticIpsAvailable: number | null;
}

/** One value a product can be targeted at. */
export interface ProxyOption {
	value: string;
	name: string | null;
	country: string | null;
	region: string | null;
	/** Named by the provider's own listing; `observedAt` when Camoufox Pro's probes saw it. */
	listed: boolean;
	observedAt: string | null;
	/** For a static product, how many IPs in stock carry this value. */
	available: number | null;
}

const DIMENSIONS = new Set<string>(Object.values(Dimensions));

/** Every managed proxy product on sale, with its price and targeting. */
export async function listProxies(): Promise<ProxyProduct[]> {
	const body = await call("GET", "/api/v1/pro/proxies", null);
	return (body.products ?? []).map((entry: Record<string, any>) => ({
		provider: entry.provider,
		providerName: entry.provider_name ?? entry.provider,
		network: entry.network,
		type: entry.type,
		unit: entry.unit,
		sellCents: Number(entry.sell_cents),
		retailCents: entry.retail_cents ?? null,
		available: Boolean(entry.available),
		profiles: Boolean(entry.profiles),
		targeting: Object.fromEntries(
			Object.entries(entry.targeting ?? {})
				.filter(([name]) => DIMENSIONS.has(name))
				.map(([name, value]: [string, any]) => [
					name,
					{ mode: value.mode, known: Number(value.known ?? 0) },
				]),
		),
		staticIpsAvailable: entry.static_ips_available ?? null,
	}));
}

/** Every value of one dimension for one product, every page of it. An unsupported dimension is an empty list. */
export async function proxyTargets(
	provider: ProxyProvider,
	network: ProxyNetwork,
	type: ProxyType,
	dimension: ProxyDimension,
	{ country, query }: { country?: string; query?: string } = {},
): Promise<ProxyOption[]> {
	const options: ProxyOption[] = [];
	let after: string | null = null;
	for (;;) {
		const params = new URLSearchParams({ limit: String(PAGE) });
		if (country !== undefined) params.set("country", country);
		if (query) params.set("q", query);
		if (after !== null) params.set("after", after);
		const body = await call(
			"GET",
			`/api/v1/pro/proxies/${provider}/${network}/${type}/${dimension}?${params}`,
			null,
		);
		for (const entry of body.options ?? []) {
			options.push({
				value: String(entry.value),
				name: entry.name ?? null,
				country: entry.country ?? null,
				region: entry.region ?? null,
				listed: Boolean(entry.listed),
				observedAt: entry.observed_at ?? null,
				available: entry.available ?? null,
			});
		}
		after = body.next ?? null;
		if (!after) return options;
	}
}

/** A static IP the account rents under a name (`{ type: ProxyType.STATIC, name }`). */
export interface NamedStatic {
	name: string;
	provider: ProxyProvider;
	type: ProxyType;
	network: ProxyNetwork;
	country: string | null;
	exit_ip: string | null;
	/** When the rent paid so far runs out; it renews from the wallet until the IP is released. */
	paid_through: string | null;
	/** A month's rent, your plan's discount applied; null when the product has no monthly rent. */
	monthly_cents: number | null;
}

/** The static IPs this account rents under a name. Needs the account's key (`camoufox login`). */
export async function listStatics(
	options: { key?: string | null } = {},
): Promise<NamedStatic[]> {
	const body = await call("GET", "/api/v1/proxy/statics", null, {
		key: resolveKey(options.key),
	});
	return (body.statics ?? []) as NamedStatic[];
}

/** Stop renting the static IP held under `name`. The next launch naming it rents a new IP. */
export async function releaseStatic(
	name: string,
	options: { key?: string | null } = {},
): Promise<void> {
	if (typeof name !== "string" || !name) {
		throw new TypeError(
			`releaseStatic takes the static IP's name, not ${JSON.stringify(name)}`,
		);
	}
	await call(
		"DELETE",
		`/api/v1/proxy/statics/${encodeURIComponent(name)}`,
		null,
		{
			key: resolveKey(options.key),
		},
	);
}
