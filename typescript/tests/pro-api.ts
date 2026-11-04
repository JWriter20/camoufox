/**
 * A local fake of the Camoufox Pro API, answering in the server's own shapes,
 * shared by the tests of the lease and of what a lease carries (pro.test.ts,
 * render-node.test.ts). No test reaches the network.
 */
import { randomBytes, randomUUID } from "node:crypto";
import * as http from "node:http";
import type { ProBuild } from "../src/pro.js";

export const BUILD_HASH = `sha256:${"ab".repeat(32)}`;
export const KEY = `cfp_live_${"k".repeat(43)}`;
export const BUILD: ProBuild = {
	build_hash: BUILD_HASH,
	version: "156.0.1-pro.1",
	target: "linux-x86_64",
};

export function tokenFor(leaseId: string, buildHash: string, expiresIn = 1800) {
	const now = Math.floor(Date.now() / 1000);
	const u64 = (n: number) => {
		const b = Buffer.alloc(8);
		b.writeBigUInt64BE(BigInt(n));
		return b;
	};
	const raw = Buffer.concat([
		Buffer.from([1, 0]),
		Buffer.from(leaseId.replaceAll("-", ""), "hex"),
		u64(42),
		Buffer.alloc(16),
		Buffer.from(buildHash.replace("sha256:", ""), "hex"),
		u64(now),
		u64(now + expiresIn),
		Buffer.from([0, 1]),
		randomBytes(64), // a signature: the launcher never checks it, the browser does
	]);
	return `cfl1_${raw.toString("base64url")}`;
}

interface Recorded {
	path: string;
	body: Record<string, any>;
	auth: string | undefined;
	status: number;
}

/** The routes the launcher calls, answering as the control plane does unless a test scripts otherwise. */
export class FakeApi {
	requests: Recorded[] = [];
	scripted: [RegExp, [number, Record<string, any>][]][] = [];
	clockOffsetMs = 0;
	heartbeatS = 60;
	grants: Record<string, any> = {};
	#waiters: (() => void)[] = [];
	server = http.createServer((req, res) => {
		let data = "";
		req.on("data", (chunk) => {
			data += chunk;
		});
		req.on("end", () => {
			const body = data ? JSON.parse(data) : {};
			const url = req.url as string;
			const [status, answer] = this.answer(url, body);
			this.requests.push({
				path: url,
				body,
				auth: req.headers.authorization,
				status,
			});
			for (const wake of this.#waiters) wake();
			res.writeHead(status, { "Content-Type": "application/json" });
			res.end(JSON.stringify(answer));
		});
	});

	script(route: string, ...answers: [number, Record<string, any>][]) {
		this.scripted.push([new RegExp(`^${route}$`), answers]);
	}

	calls(route: string) {
		const pattern = new RegExp(`^${route}$`);
		return this.requests.filter((r) => pattern.test(r.path));
	}

	waitFor(predicate: () => boolean, timeoutMs = 5000): Promise<void> {
		return new Promise((resolve, reject) => {
			const timer = setTimeout(
				() => reject(new Error(JSON.stringify(this.requests))),
				timeoutMs,
			);
			const check = () => {
				if (!predicate()) return;
				clearTimeout(timer);
				this.#waiters = this.#waiters.filter((w) => w !== check);
				resolve();
			};
			this.#waiters.push(check);
			check();
		});
	}

	answer(
		url: string,
		body: Record<string, any>,
	): [number, Record<string, any>] {
		for (const [pattern, queue] of this.scripted) {
			if (pattern.test(url) && queue.length) return queue.shift() as any;
		}
		const now = new Date(Date.now() + this.clockOffsetMs).toISOString();
		if (url === "/api/v1/leases") {
			const leaseId = randomUUID();
			return [
				201,
				{
					v: 1,
					lease_id: `lse_${leaseId}`,
					server_time: now,
					expires_at: now,
					token: tokenFor(leaseId, body.build_hash),
					limits: {
						ttl_s: 1800,
						heartbeat_s: this.heartbeatS,
						grace_s: 180,
						degrade_drain_s: 900,
					},
					host: {
						fingerprint: body.host.fingerprint,
						fidelity: "layout",
						capability_hash: "",
					},
					notices: [],
					...this.grants,
				},
			];
		}
		const match =
			/^\/api\/v1\/leases\/lse_([0-9a-f-]+)\/(heartbeat|release)$/.exec(url);
		if (match?.[2] === "heartbeat") {
			return [
				200,
				{
					lease_id: `lse_${match[1]}`,
					server_time: now,
					expires_at: now,
					token: tokenFor(match[1], BUILD_HASH),
					token_kid: 0,
					notices: [],
				},
			];
		}
		if (match) return [200, { released: true, already_released: false }];
		return [404, { error: "not_found", message: "No such route." }];
	}
}
