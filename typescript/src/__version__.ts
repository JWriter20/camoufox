/**
 * Camoufox version constants.
 */

// biome-ignore lint/complexity/noStaticOnlyClass: mirrors the Python twin's CONSTRAINTS class so both launchers read the same
export class CONSTRAINTS {
	/**
	 * The minimum and maximum supported versions of the Camoufox browser.
	 */
	static readonly MIN_VERSION: string = "alpha.1";
	static readonly MAX_VERSION: string = "1";

	/**
	 * Returns the version range as a string.
	 */
	static asRange(): string {
		return `>=${CONSTRAINTS.MIN_VERSION}, <${CONSTRAINTS.MAX_VERSION}`;
	}
}

/** Version of this launcher library. Kept in step with package.json. */
export const LIBRARY_VERSION = "0.5.4";
