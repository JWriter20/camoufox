/**
 * A managed proxy without a Camoufox Pro lease: what `managed` opens on a
 * standard Camoufox build (docs/pro.md, "Managed proxies").
 *
 * A Pro build gets its managed proxy as a section of its lease. A standard
 * build has no lease, so it asks the API for a proxy session instead: the same
 * exit and the same credentials, paid from the account's wallet, renewed while
 * the browser runs and released when it closes. Any account with a key can
 * open one, pay-as-you-go included.
 */

import type { Managed } from "./managed.js";
import * as pro from "./pro.js";

/** The lease's own default cadence, when the API states none. */
const HEARTBEAT_S = 60;
/** Local destinations never go through the managed proxy, which refuses them. */
export const BYPASS = "localhost,127.0.0.1,::1,*.local";

const LIVE = new Map<string, ProxySession>();

/**
 * One browser's managed proxy on a standard build. It stands in for a lease
 * where the launch path expects one (`claim`, `attachLease`, `launchFailed`).
 */
export class ProxySession {
	claimed = false;
	sessionId = "";
	egress: Record<string, any> = {};
	notices: Record<string, any>[] = [];
	/** What ends with this session: a render node opened alongside it. */
	onEnd: (() => Promise<void>)[] = [];
	#key = "";
	#base = "";
	#heartbeatS = HEARTBEAT_S;
	#timer: NodeJS.Timeout | null = null;
	#failures = 0;
	#released: Promise<void> | null = null;

	static async open(
		managed: Managed | boolean,
		key?: string | null,
	): Promise<ProxySession> {
		const session = new ProxySession();
		session.#key = pro.resolveKey(key);
		session.#base = pro.apiBase();
		const answer = await pro.post(
			"/api/v1/proxy/sessions",
			{ managed, client: pro.clientName() },
			{ key: session.#key, base: session.#base },
		);
		session.sessionId = String(answer.session_id);
		for (const name of [
			"server",
			"username",
			"password",
			"class",
			"country",
			"sticky",
			"exit_ip",
			"binding_id",
			"name",
			"expires_at",
		]) {
			session.egress[name] = answer[name];
		}
		session.notices = Array.isArray(answer.notices) ? answer.notices : [];
		pro.logNotices(session.notices);
		session.#heartbeatS = Number(answer.heartbeat_s) || HEARTBEAT_S;
		LIVE.set(session.username, session);
		watchExit();
		session.#schedule();
		return session;
	}

	get leaseId(): string {
		return this.sessionId;
	}

	get grants(): Record<string, any> {
		return { egress: this.egress };
	}

	get username(): string {
		return String(this.egress.username);
	}

	/** The launch's `proxy` option for this session. */
	proxy(): {
		server: string;
		username: string;
		password: string;
		bypass: string;
	} {
		return {
			server: String(this.egress.server),
			username: this.username,
			password: String(this.egress.password),
			bypass: BYPASS,
		};
	}

	#schedule(seconds?: number): void {
		// Jittered by a twelfth, so a fleet started together does not beat together.
		const wait =
			seconds ?? this.#heartbeatS * (11 / 12 + Math.random() * (2 / 12));
		this.#timer = setTimeout(() => void this.#beat(), wait * 1000);
		this.#timer.unref();
	}

	async #beat(): Promise<void> {
		if (this.#released) return;
		try {
			const answer = await pro.post(
				`/api/v1/proxy/sessions/${this.sessionId}/heartbeat`,
				{},
				{ key: this.#key, base: this.#base },
			);
			if (answer?.expires_at) this.egress.expires_at = answer.expires_at;
			pro.logNotices(answer?.notices);
			this.#failures = 0;
		} catch (error) {
			if (this.#released) return;
			const status = (error as { status?: number }).status ?? 0;
			if (!pro.transient(error) && status < 500) {
				console.error(
					`camoufox: the managed proxy session was refused a renewal and will lapse: ${(error as Error).message}`,
				);
				return;
			}
			const backoff = [5, 10, 20, 40, 60];
			const retry = (error as { retry_after?: number | null }).retry_after;
			this.#schedule(
				retry || backoff[Math.min(this.#failures, backoff.length - 1)],
			);
			this.#failures += 1;
			return;
		}
		this.#schedule();
	}

	close(): Promise<void> {
		return this.release();
	}

	/** End the session. Only the first call does anything; it lapses by itself if this fails. */
	release(reason = "clean_exit"): Promise<void> {
		this.#released ??= (async () => {
			LIVE.delete(this.username);
			if (this.#timer) clearTimeout(this.#timer);
			try {
				await pro.post(
					`/api/v1/proxy/sessions/${this.sessionId}/release`,
					{ reason },
					{ key: this.#key, base: this.#base },
				);
			} catch (error) {
				console.warn(
					`camoufox: releasing managed proxy session ${this.sessionId} failed: ${(error as Error).message}`,
				);
			}
			for (const end of this.onEnd) {
				try {
					await end();
				} catch (error) {
					console.warn(
						`camoufox: ending a session held with proxy session ${this.sessionId} failed: ${(error as Error).message}`,
					);
				}
			}
		})();
		return this.#released;
	}
}

/** The proxy session these launch options were built with, taken by the browser about to use them. */
export function claim(options: Record<string, any>): ProxySession | null {
	const username = options.proxy?.username;
	const session = typeof username === "string" ? LIVE.get(username) : undefined;
	if (!session) return null;
	if (session.claimed) {
		throw new Error(
			"These launch options' managed proxy session is already held by another browser. " +
				"Each browser needs its own: call launchOptions() once per browser.",
		);
	}
	session.claimed = true;
	return session;
}

let watching = false;

/** A browser the script never closed: its session is released when the process winds down, and
 * lapses by itself (its heartbeats stop) when it is killed. */
function watchExit(): void {
	if (watching) return;
	watching = true;
	process.on("beforeExit", () => void releaseAll());
}

/** Release every live session (the process is exiting). */
export async function releaseAll(): Promise<void> {
	await Promise.all(
		[...LIVE.values()].map((session) => session.release("driver_shutdown")),
	);
}
