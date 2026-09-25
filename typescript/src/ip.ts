/**
 * Helpers to find the user's public IP address for geolocation.
 *
 * TypeScript twin of python/src/ip.py.
 */
import { Impit } from "impit";
import { InvalidIP, InvalidProxy } from "./exceptions.js";

export interface ProxyConfig {
	server: string;
	username?: string;
	password?: string;
	bypass?: string;
}

// biome-ignore lint/complexity/noStaticOnlyClass: these are the Python Proxy dataclass's methods; the data itself is ProxyConfig
export class ProxyHelper {
	/**
	 * Parses the proxy server string.
	 */
	static parseServer(server: string): {
		schema: string;
		url: string;
		port?: string;
	} {
		const proxyMatch = server.match(/^(?:(\w+):\/\/)?(.*?)(?::(\d+))?$/);
		if (!proxyMatch) {
			throw new InvalidProxy(`Invalid proxy server: ${server}`);
		}
		return {
			schema: proxyMatch[1] || "http",
			url: proxyMatch[2],
			port: proxyMatch[3],
		};
	}

	static asString(proxy: ProxyConfig): string {
		const { schema, url, port } = ProxyHelper.parseServer(proxy.server);
		let result = `${schema}://`;
		if (proxy.username) {
			result += proxy.username;
			if (proxy.password) {
				result += `:${proxy.password}`;
			}
			result += "@";
		}
		result += url;
		if (port) {
			result += `:${port}`;
		}
		return result;
	}
}

export function validIPv4(ip: string | false): boolean {
	if (!ip) {
		return false;
	}
	return /^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$/.test(ip);
}

export function validIPv6(ip: string | false): boolean {
	if (!ip) {
		return false;
	}
	return /^(([0-9a-fA-F]{0,4}:){1,7}[0-9a-fA-F]{0,4})$/.test(ip);
}

export function validateIP(ip: string): void {
	if (!validIPv4(ip) && !validIPv6(ip)) {
		throw new InvalidIP(`Invalid IP address: ${ip}`);
	}
}

// Impit has no close/dispose API: each instance's native client (Tokio
// runtime resources, one UDP resolver socket) is only reclaimed when the
// JS wrapper is GC'd -- and V8 rarely collects the tiny wrappers, so
// per-call instances leak fds in long-running processes. Reuse instances
// via a small LRU keyed by proxy URL; evicted entries are reclaimed by GC.
const IMPIT_CACHE_MAX = 8;
const impitCache = new Map<string, Impit>();

function getImpit(proxy?: string): Impit {
	const key = proxy ?? "";
	const cached = impitCache.get(key);
	if (cached) {
		impitCache.delete(key);
		impitCache.set(key, cached);
		return cached;
	}
	const impit = new Impit({ proxyUrl: proxy, timeout: 5000 });
	impitCache.set(key, impit);
	if (impitCache.size > IMPIT_CACHE_MAX) {
		impitCache.delete(impitCache.keys().next().value as string);
	}
	return impit;
}

const PUBLIC_IP_URLS = [
	// Prefers IPv4
	"https://api.ipify.org",
	"https://checkip.amazonaws.com",
	"https://ipinfo.io/ip",
	// IPv4 & IPv6
	"https://icanhazip.com",
	"https://ifconfig.co/ip",
	"https://ipecho.net/plain",
];

// The Python twin memoizes public_ip() with lru_cache; mirror that so repeated
// launches through the same proxy don't re-hit the API endpoints.
const publicIPCache = new Map<string, Promise<string>>();

/**
 * Sends a request to a public IP api.
 */
export function publicIP(proxy?: string): Promise<string> {
	const key = proxy ?? "";
	const cached = publicIPCache.get(key);
	if (cached) {
		return cached;
	}
	const pending = resolvePublicIP(proxy).catch((error) => {
		// Never cache a failure: the next launch should retry.
		publicIPCache.delete(key);
		throw error;
	});
	publicIPCache.set(key, pending);
	return pending;
}

async function resolvePublicIP(proxy?: string): Promise<string> {
	const errors: unknown[] = [];

	for (const url of PUBLIC_IP_URLS) {
		try {
			const response = await getImpit(proxy).fetch(url);
			if (!response.ok) {
				continue;
			}
			const ip = (await response.text()).trim();
			validateIP(ip);
			return ip;
		} catch (error) {
			errors.push(error);
			if (process.env.CAMOUFOX_DEBUG) {
				console.warn(
					new InvalidProxy(
						`camoufox(warn): Failed to fetch public IP from ${url}, retrying with another URL...`,
						{ cause: error },
					),
				);
			}
		}
	}

	throw new InvalidIP(
		"Failed to get a public IP address from any API endpoint.",
		{ cause: errors },
	);
}
