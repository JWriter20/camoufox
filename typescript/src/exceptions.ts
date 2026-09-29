/**
 * TypeScript twin of python/src/exceptions.py.
 *
 * The Python hierarchy leans on builtin bases (FileNotFoundError, ValueError,
 * ImportError) that have no JS analogue; those become plain Error subclasses
 * here. Every relationship that callers actually catch on -- LocaleError,
 * VirtualDisplayError -- is preserved.
 */

/** The Python twin relies on the builtin FileNotFoundError; JS has no such
 *  class, so version-lookup misses raise this instead. */
export class FileNotFoundError extends Error {
	constructor(message?: string) {
		super(message ?? "File couldn't be found.");
		this.name = "FileNotFoundError";
	}
}

export class UnsupportedVersion extends Error {
	constructor(message?: string) {
		super(message ?? "The Camoufox executable is outdated.");
		this.name = "UnsupportedVersion";
	}
}

export class MissingRelease extends Error {
	constructor(message?: string) {
		super(message ?? "A required GitHub release asset is missing.");
		this.name = "MissingRelease";
	}
}

/** Raised when a downloaded asset does not match its expected sha256 digest. */
export class CorruptedDownload extends Error {
	constructor(message?: string) {
		super(
			message ??
				"A downloaded asset does not match its expected sha256 digest.",
		);
		this.name = "CorruptedDownload";
	}
}

export class UnsupportedArchitecture extends Error {
	constructor(message?: string) {
		super(message ?? "The architecture is not supported.");
		this.name = "UnsupportedArchitecture";
	}
}

export class UnsupportedOS extends Error {
	constructor(message?: string) {
		super(message ?? "The OS is not supported.");
		this.name = "UnsupportedOS";
	}
}

export class InvalidPropertyType extends Error {
	constructor(message?: string) {
		super(message ?? "The property type is invalid.");
		this.name = "InvalidPropertyType";
	}
}

export class InvalidAddonPath extends FileNotFoundError {
	constructor(message?: string) {
		super(message ?? "The addon path is invalid.");
		this.name = "InvalidAddonPath";
	}
}

export class LocaleError extends Error {
	constructor(message?: string) {
		super(message ?? "The locale is invalid.");
		this.name = "LocaleError";
	}
}

export class InvalidIP extends Error {
	constructor(message?: string, options?: ErrorOptions) {
		super(message ?? "An IP address is invalid.", options);
		this.name = "InvalidIP";
	}
}

export class InvalidProxy extends Error {
	constructor(message?: string, options?: ErrorOptions) {
		super(message ?? "A proxy is invalid.", options);
		this.name = "InvalidProxy";
	}
}

export class UnknownIPLocation extends LocaleError {
	constructor(message?: string) {
		super(message ?? "The location of an IP is unknown.");
		this.name = "UnknownIPLocation";
	}
}

export class InvalidLocale extends LocaleError {
	constructor(message?: string) {
		super(message ?? "The locale input is invalid.");
		this.name = "InvalidLocale";
	}

	static invalidInput(locale: string): InvalidLocale {
		return new InvalidLocale(
			`Invalid locale: '${locale}'. Must be either a region, language, language-region, or language-script-region.`,
		);
	}
}

export class UnknownTerritory extends InvalidLocale {
	constructor(message?: string) {
		super(message ?? "The territory is unknown.");
		this.name = "UnknownTerritory";
	}
}

export class UnknownLanguage extends InvalidLocale {
	constructor(message?: string) {
		super(message ?? "The language is unknown.");
		this.name = "UnknownLanguage";
	}
}

export class NotInstalledGeoIPExtra extends Error {
	constructor(message?: string) {
		super(message ?? "The GeoIP database reader is not available.");
		this.name = "NotInstalledGeoIPExtra";
	}
}

export class NonFirefoxFingerprint extends Error {
	constructor(message?: string) {
		super(message ?? "A passed fingerprint is not a Firefox fingerprint.");
		this.name = "NonFirefoxFingerprint";
	}
}

export class InvalidOS extends Error {
	constructor(message?: string) {
		super(message ?? "The target OS is invalid.");
		this.name = "InvalidOS";
	}
}

export class VirtualDisplayError extends Error {
	constructor(message?: string) {
		super(message ?? "There is an error with the virtual display.");
		this.name = "VirtualDisplayError";
	}
}

export class CannotFindXvfb extends VirtualDisplayError {
	constructor(message?: string) {
		super(message ?? "Xvfb cannot be found.");
		this.name = "CannotFindXvfb";
	}
}

export class CannotExecuteXvfb extends VirtualDisplayError {
	constructor(message?: string) {
		super(message ?? "Xvfb cannot be executed.");
		this.name = "CannotExecuteXvfb";
	}
}

export class VirtualDisplayNotSupported extends VirtualDisplayError {
	constructor(message?: string) {
		super(
			message ?? "The user tried to use a virtual display on a non-Linux OS.",
		);
		this.name = "VirtualDisplayNotSupported";
	}
}

export class CamoufoxNotInstalled extends FileNotFoundError {
	constructor(message?: string) {
		super(message ?? "Camoufox is not installed.");
		this.name = "CamoufoxNotInstalled";
	}
}

/** Raised when Camoufox's required runtime directory cannot be prepared. */
export class ProfileDirectoryError extends Error {
	constructor(message?: string, options?: ErrorOptions) {
		super(
			message ?? "Camoufox's runtime directory could not be prepared.",
			options,
		);
		this.name = "ProfileDirectoryError";
	}
}

export interface ProErrorFields {
	code?: string | null;
	status?: number | null;
	resolution_url?: string | null;
	retry_after?: number | null;
	details?: Record<string, any> | null;
}

/**
 * Raised when Camoufox Pro cannot start a session. `code` is the API's stable
 * error code, `message` its explanation, and `resolution_url` the page that
 * fixes it, when there is one.
 */
export class ProError extends Error {
	readonly code: string | null;
	readonly status: number | null;
	readonly resolution_url: string | null;
	readonly retry_after: number | null;
	readonly details: Record<string, any>;
	readonly detail: string;

	constructor(message: string, fields: ProErrorFields = {}) {
		super(
			fields.resolution_url ? `${message} ${fields.resolution_url}` : message,
		);
		this.name = new.target.name;
		this.detail = message;
		this.code = fields.code ?? null;
		this.status = fields.status ?? null;
		this.resolution_url = fields.resolution_url ?? null;
		this.retry_after = fields.retry_after ?? null;
		this.details = fields.details ?? {};
	}
}

/** No Camoufox Pro key, or the API does not accept it. */
export class NotSignedIn extends ProError {}
/** The API rejected the lease request itself. */
export class InvalidRequest extends ProError {}
/** The account has no active Camoufox Pro subscription. */
export class SubscriptionRequired extends ProError {}
/** A metered allowance is used up and usage-based billing is off. */
export class AllowanceExhausted extends ProError {}
/** The account is suspended. */
export class AccountSuspended extends ProError {}
/** The browser is not a published Camoufox Pro release, or was revoked. */
export class BuildNotAllowlisted extends ProError {}
/** Every concurrent browser the plan includes is already running. */
export class LeaseLimitReached extends ProError {}
/** This machine cannot present the requested identity. */
export class CapabilityMismatch extends ProError {}
/** The API still refuses for rate after the retries. */
export class RateLimited extends ProError {}
/** The Camoufox Pro API cannot be reached, or fails, after the retries. */
export class ProUnavailable extends ProError {}

/** This machine's clock is so far from the API's that a fresh lease would
 * look expired to the browser. */
export class ProClockSkew extends ProError {
	readonly skew: number;

	constructor(skew: number) {
		const rounded = Math.round(skew);
		super(
			`This machine's clock is ${rounded >= 0 ? "+" : ""}${rounded} s off the Camoufox Pro API's. ` +
				"Lease expiry is evaluated on this machine's clock; fix NTP.",
			{ code: "clock_skew" },
		);
		this.skew = skew;
	}
}

/** A Camoufox Pro browser refused the lease it was started with. */
export class LeaseRefused extends ProError {
	readonly reason: string;

	constructor(reason: string) {
		super(`The browser refused its Camoufox Pro lease (${reason}).`, {
			code: "lease_refused",
		});
		this.reason = reason;
	}
}
