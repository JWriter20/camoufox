/**
 * Browser + GeoIP repository configuration.
 *
 * TypeScript twin of python/src/repos.yml. Kept as a module (rather
 * than a data-file) so the shape is type-checked at build time.
 */

export interface BrowserVersionConstraint {
	python_library?: { min?: string; max?: string };
	/** Absent means "assume every build is supported". */
	browser?: {
		stable?: { min?: string; max?: string };
		prerelease?: { min?: string; max?: string };
		min?: string;
		max?: string;
	};
}

export interface BrowserRepoEntry {
	/** Primary repo first, then fallbacks. */
	repo: string;
	name: string;
	pattern: string;
	versions?: BrowserVersionConstraint[];
}

export interface GeoIPRepoEntry {
	name: string;
	extract?: boolean;
	urls: Record<string, string | string[]>;
	paths: {
		iso_code: string;
		longitude: string;
		latitude: string;
		timezone: string;
	};
}

export interface ReposConfig {
	default: { browser: string; geoip: string };
	browsers: BrowserRepoEntry[];
	geoip: GeoIPRepoEntry[];
}

const REPOS: ReposConfig = {
	// Default configurations
	default: {
		browser: "Official",
		geoip: "MaxMind GeoLite2",
	},

	// Browser repositories
	browsers: [
		{
			// Fallback to camoufox org
			repo: "daijro/camoufox, camoufox/camoufox",
			name: "Official",
			pattern: "{name}-{version}-{build}-{os}.{arch}.zip",
			versions: [
				{
					python_library: { min: "0.5.0", max: "1" },
					browser: {
						// Stable channel
						stable: { min: "beta.19", max: "1" },
						// Prerelease channel (including "alpha.*" builds) matches all versions
					},
				},
			],
		},
		{
			repo: "coryking/camoufox",
			name: "CoryKing",
			pattern: "{name}-{version}-{build}-{os}.{arch}.zip",
			versions: [
				{
					python_library: { min: "0.5.0", max: "1" },
					// Assume all browsers
				},
			],
		},
		{
			repo: "JWriter20/camoufox",
			name: "JWriter20",
			pattern: "{name}-{version}-{build}-{os}.{arch}.zip",
			versions: [
				{
					python_library: { min: "0.5.0", max: "1" },
					// Assume all browsers
				},
			],
		},
	],

	// GeoIP database repositories
	geoip: [
		{
			// GeoLite2 City - Full city-level data with timezone
			name: "MaxMind GeoLite2",
			urls: {
				ipv4: [
					"https://cdn.jsdelivr.net/npm/@ip-location-db/geolite2-city-mmdb/geolite2-city-ipv4.mmdb",
					"https://raw.githubusercontent.com/sapics/ip-location-db/refs/heads/main/geolite2-city-mmdb/geolite2-city-ipv4.mmdb",
				],
				ipv6: [
					"https://cdn.jsdelivr.net/npm/@ip-location-db/geolite2-city-mmdb/geolite2-city-ipv6.mmdb",
					"https://raw.githubusercontent.com/sapics/ip-location-db/refs/heads/main/geolite2-city-mmdb/geolite2-city-ipv6.mmdb",
				],
			},
			paths: {
				iso_code: "country_code",
				longitude: "longitude",
				latitude: "latitude",
				timezone: "timezone",
			},
		},
		{
			// GeoIP All-in-One - Combined IPv4/IPv6 with all fields
			name: "GeoIP AIO by daijro",
			extract: true,
			urls: {
				combined: [
					"https://github.com/daijro/geoip-all-in-one/releases/latest/download/geoip-aio-all.mmdb.zip",
				],
			},
			paths: {
				iso_code: "country.iso_code",
				longitude: "location.longitude",
				latitude: "location.latitude",
				timezone: "location.time_zone",
			},
		},
	],
};

export default REPOS;
