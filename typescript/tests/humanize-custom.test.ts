/**
 * Port of pythonlib/tests/test_humanize_custom.py: custom() humanize engines,
 * the launch config, the seeded streams (parity with the browser's), the page
 * wrappers and play()'s pacing. The scenario must produce the same sequence as
 * the Python launcher: tests/humanize/custom-sequence.json.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { HumanizeEngineUnavailable } from "../src/exceptions.js";
import {
	auto,
	CHANNELS,
	channelStream,
	custom,
	customEngines,
	engine,
	type HumanizeManifest,
	humanizeConfig,
	raw,
	seededRng,
	splitmix64,
} from "../src/humanize.js";
import { attach, validateSteps } from "../src/humanize-custom.js";
import { ValueError } from "../src/pycompat.js";

const REPO = path.resolve(
	path.dirname(fileURLToPath(import.meta.url)),
	"../..",
);
const read = (...parts: string[]) =>
	JSON.parse(fs.readFileSync(path.join(REPO, ...parts), "utf-8"));
const BASE_MANIFEST: HumanizeManifest = read(
	"settings",
	"humanize-engines.json",
);
const VECTORS = read("tests", "humanize", "rng-vectors.json");
const SEQUENCE = read("tests", "humanize", "custom-sequence.json");

const noop = () => undefined;
const plain = (config: Record<string, unknown>) =>
	JSON.parse(JSON.stringify(config));

describe("streams", () => {
	it("match the reference outputs", () => {
		expect(splitmix64(0n)).toBe(0xe220a8397b1dcdafn);
		expect(splitmix64(0x9e3779b97f4a7c15n)).toBe(0x6e789e6aa1b965f4n);
		const rng = seededRng(0);
		expect([rng(), rng(), rng()].map((x) => Math.round(x * 2 ** 32))).toEqual([
			1144304738, 1416247, 958946056,
		]);
	});

	it("match the browser's pin (tests/juggler/rng.test.mjs)", () => {
		const source = fs.readFileSync(
			path.join(REPO, "tests", "juggler", "rng.test.mjs"),
			"utf-8",
		);
		const body = /const PINNED = \{([^}]*)\}/.exec(source)?.[1] ?? "";
		const pinned = Object.fromEntries(
			[...body.matchAll(/(\w+): (\d+)/g)].map((m) => [m[1], Number(m[2])]),
		);
		expect(
			Object.fromEntries(
				CHANNELS.map((c) => [
					c,
					Math.round(channelStream(1234n, c)() * 2 ** 32),
				]),
			),
		).toEqual(pinned);
	});

	it("are the browser's, draw for draw (rng-vectors.json)", () => {
		for (const [seed, value] of VECTORS.splitmix64)
			expect(splitmix64(BigInt(seed))).toBe(BigInt(value));
		for (const entry of VECTORS.streams) {
			for (const channel of CHANNELS) {
				const stream = channelStream(BigInt(entry.seed), channel);
				const draws = entry[channel].map(() => stream() * 2 ** 32);
				expect(draws.every(Number.isInteger)).toBe(true);
				expect(draws).toEqual(entry[channel]);
				expect(stream.position).toBe(entry[channel].length);
			}
		}
	});

	it("uniform() is one draw", () => {
		const first = channelStream(7n, "mouse")();
		expect(channelStream(7n, "mouse").uniform(10, 20)).toBe(10 + 10 * first);
	});
});

describe("factory and config", () => {
	it("custom() takes a function", () => {
		expect(() => custom(42 as never)).toThrow(/custom\(\) takes the function/);
		expect(() => humanizeConfig({ mouse: "custom" }, BASE_MANIFEST)).toThrow(
			/needs its function, as custom\(fn\)/,
		);
		expect(() =>
			humanizeConfig({ mouse: engine("custom") }, BASE_MANIFEST),
		).toThrow(/needs its function/);
		expect(() =>
			humanizeConfig(
				{ mouse: { ...custom(noop), options: { speed: 1 } } },
				BASE_MANIFEST,
			),
		).toThrow(/custom\(\) takes no options/);
		expect(customEngines({ mouse: custom(noop), keyboard: raw() })).toEqual({
			mouse: noop,
		});
		expect(customEngines(true)).toEqual({});
	});

	it("custom channels run raw in the browser", () => {
		expect(
			plain(
				humanizeConfig(
					{
						mouse: custom(noop),
						keyboard: custom(noop),
						scroll: auto(),
						seed: 5,
					},
					BASE_MANIFEST,
				),
			),
		).toEqual({
			humanize: true,
			"humanize:mouse": "raw",
			"humanize:mouse:internal": "auto",
			"humanize:keyboard": "raw",
			"humanize:scroll": "auto",
			"humanize:seed": "5",
		});
		expect(
			plain(
				humanizeConfig(
					{ keyboard: custom(noop), scroll: custom(noop) },
					BASE_MANIFEST,
				),
			),
		).toEqual({
			humanize: true,
			"humanize:mouse": "auto",
			"humanize:keyboard": "raw",
			"humanize:scroll": "raw",
		});
		const all = humanizeConfig(
			{ mouse: custom(noop), keyboard: custom(noop), scroll: custom(noop) },
			BASE_MANIFEST,
		);
		expect(all.humanize).toBe(false);
		expect(all["humanize:mouse:internal"]).toBe("auto");
	});

	it("a build without the manifest", () => {
		expect(
			humanizeConfig(
				{ mouse: custom(noop), keyboard: custom(noop), scroll: custom(noop) },
				null,
			),
		).toEqual({});
		expect(
			humanizeConfig(
				{ mouse: custom(noop), keyboard: raw(), scroll: raw() },
				null,
			),
		).toEqual({});
		expect(() => humanizeConfig({ mouse: custom(noop) }, null)).toThrow(
			HumanizeEngineUnavailable,
		);
	});

	it("launchServer refuses custom()", async () => {
		const { launchServer } = await import("../src/server.js");
		await expect(
			launchServer({ humanize: { mouse: custom(noop) } }),
		).rejects.toThrow(/cannot run custom\(\) humanize engines/);
	});
});

describe("play()", () => {
	it.each([
		["move", "takes a list of steps"],
		[[["hop", 1, 2, 0]], "the kind must be"],
		[[["move", 1, 2]], "move takes 3 values"],
		[[["move", 1, 2, -1]], "t must be a finite number"],
		[
			[
				["move", 1, 2, 5],
				["move", 1, 2, 4],
			],
			"goes back in time",
		],
		[[["move", Number.NaN, 2, 0]], "finite numbers"],
		[[["down", "side", 0]], "the button must be"],
		[[["key", "a", "tap", 0]], 'key takes a key name and "down" or "up"'],
		[[["text", 5, 0]], "text takes a string"],
		[[["move", 1, 2, true]], "t must be"],
	])("rejects %j", (steps, message) => {
		expect(() => validateSteps(steps)).toThrow(ValueError);
		expect(() => validateSteps(steps)).toThrow(message);
	});

	it("names the bad step", () => {
		expect(() =>
			validateSteps([
				["down", "left", 0],
				["up", "side", 3],
			]),
		).toThrow('play() step 1 ["up","side",3]');
	});
});

// ---- the fake page ---------------------------------------------------------

type Event = unknown[];

function fakeBrowser() {
	const events: Event[] = [];
	const boxes: Record<string, [Record<string, number>, boolean]> = {
		html: [{ x: 0, y: 0, width: 1000, height: 700 }, true],
		"#far": [{ x: 100, y: 900, width: 80, height: 30 }, false],
		"#name": [{ x: 40, y: 120, width: 200, height: 24 }, true],
		"#near": [{ x: 500, y: 300, width: 50, height: 20 }, true],
	};
	const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

	// A class per browser, like Playwright's own, so patching one never leaks
	// into another test.
	class FakeLocator {
		constructor(
			readonly _page: FakePage,
			readonly selector: string,
		) {}
		page() {
			return this._page;
		}
		first() {
			return new FakeLocator(this._page, this.selector);
		}
		async log(name: string, ...args: unknown[]) {
			events.push([`locator.${name}`, this.selector, ...args]);
		}
		click(o?: { position?: object }) {
			return this.log("click", o?.position ?? null);
		}
		dblclick(o?: { position?: object }) {
			return this.log("dblclick", o?.position ?? null);
		}
		hover(o?: { position?: object }) {
			return this.log("hover", o?.position ?? null);
		}
		fill(value: string) {
			return this.log("fill", value);
		}
		type(text: string) {
			return this.log("type", text);
		}
		pressSequentially(text: string) {
			return this.log("pressSequentially", text);
		}
		press(key: string) {
			return this.log("press", key);
		}
		scrollIntoViewIfNeeded() {
			boxes[this.selector][1] = true;
			return this.log("scrollIntoViewIfNeeded");
		}
		selectText() {
			return this.log("selectText");
		}
		focus() {
			return this.log("focus");
		}
		async boundingBox() {
			return { ...boxes[this.selector][0] };
		}
		async evaluate(fn: (...a: unknown[]) => unknown) {
			if (String(fn).includes("clientLeft")) return [2, 3];
			return boxes[this.selector][1];
		}
	}

	class FakePage {
		latency = 0;
		mouse = {
			move: async (x: number, y: number) => {
				events.push(["mouse.move", x, y]);
				if (this.latency) await sleep(this.latency);
			},
			down: async (o?: { button?: string }) => {
				events.push(["mouse.down", o?.button ?? "left"]);
			},
			up: async (o?: { button?: string }) => {
				events.push(["mouse.up", o?.button ?? "left"]);
			},
			wheel: async (dx: number, dy: number) => {
				events.push(["mouse.wheel", dx, dy]);
			},
			click: async (x: number, y: number) => {
				events.push(["mouse.click", x, y]);
			},
			dblclick: async (x: number, y: number) => {
				events.push(["mouse.dblclick", x, y]);
			},
		};
		keyboard = {
			down: async (key: string) => {
				events.push(["keyboard.down", key]);
			},
			up: async (key: string) => {
				events.push(["keyboard.up", key]);
			},
			insertText: async (text: string) => {
				events.push(["keyboard.insertText", text]);
			},
			type: async (text: string) => {
				events.push(["keyboard.type", text]);
			},
			press: async (key: string) => {
				events.push(["keyboard.press", key]);
			},
		};
		constructor(readonly _context: FakeContext) {}
		context() {
			return this._context;
		}
		locator(selector: string) {
			return new FakeLocator(this, selector);
		}
		async click(selector: string) {
			events.push(["page.click", selector]);
		}
		async fill(selector: string, value: string) {
			events.push(["page.fill", selector, value]);
		}
	}

	class FakeContext {
		_pages: FakePage[] = [];
		handlers: ((p: FakePage) => void)[] = [];
		on(event: string, handler: (p: FakePage) => void) {
			expect(event).toBe("page");
			this.handlers.push(handler);
		}
		pages() {
			return this._pages;
		}
		/** A page the browser opened by itself, such as a popup. */
		openPage() {
			const page = new FakePage(this);
			this._pages.push(page);
			for (const handler of this.handlers) handler(page);
			return page;
		}
	}

	const _contexts: FakeContext[] = [];
	const browser = {
		contexts: () => _contexts,
		newContext: async () => {
			const context = new FakeContext();
			_contexts.push(context);
			return context;
		},
		newPage: async () => {
			const context = new FakeContext();
			_contexts.push(context);
			return context.openPage();
		},
	};
	return { browser, events };
}

// biome-ignore-start lint/suspicious/noExplicitAny: fakes stand in for Playwright
type Any = any;

async function attachedPage(humanize: Any) {
	const fake = fakeBrowser();
	attach(fake.browser as Any, humanize);
	const page: Any = await fake.browser.newPage();
	return { ...fake, page };
}

// ---- the reference engines (mirrored in test_humanize_custom.py) ----------

function referenceEngines(log: Event[]) {
	const at = { x: 0, y: 0 };
	const move = (_page: Any, x: number, y: number, { rng, play }: Any) => {
		log.push(["fn.mouse", x, y]);
		const n = 3 + Math.floor(rng() * 4);
		const [sx, sy] = [at.x, at.y];
		const steps = [];
		for (let i = 1; i <= n; i++) {
			const f = i / n;
			const jitter = (rng() - 0.5) * 4 * (1 - f);
			steps.push([
				"move",
				sx + (x - sx) * f + jitter,
				sy + (y - sy) * f - jitter,
				i * 2,
			]);
		}
		at.x = x;
		at.y = y;
		return play(steps);
	};
	const keys = (
		_page: Any,
		text: string,
		{ original, rng, play, kind }: Any,
	) => {
		log.push(["fn.keyboard", kind, text]);
		if (kind === "press") return original();
		const steps = [];
		let t = 0;
		for (const ch of text) {
			steps.push(["key", ch, "down", t]);
			t += 1 + Math.floor(rng() * 3);
			steps.push(["key", ch, "up", t]);
		}
		return play(steps);
	};
	const scroll = (_page: Any, target: Any, { original, rng, play }: Any) => {
		if (Array.isArray(target)) {
			log.push(["fn.scroll", target]);
			const n = 2 + Math.floor(rng() * 3);
			return play(
				Array.from({ length: n }, (_, i) => [
					"wheel",
					target[0] / n,
					target[1] / n,
					i,
				]),
			);
		}
		log.push(["fn.scroll", target.selector]);
		return original();
	};
	return { move, keys, scroll };
}

describe("wrappers", () => {
	it("the scenario matches the Python launcher's sequence", async () => {
		const fake = fakeBrowser();
		const { move, keys, scroll } = referenceEngines(fake.events);
		attach(fake.browser as Any, {
			mouse: custom(move),
			keyboard: custom(keys),
			scroll: custom(scroll),
			seed: 1234,
		});
		const page: Any = await fake.browser.newPage();
		await page.mouse.move(300, 200);
		await page.mouse.wheel(0, 480);
		await page.locator("#far").click();
		await page.locator("#near").hover({ position: { x: 5, y: 6 } });
		await page.locator("#name").fill("Hi there");
		await page.keyboard.press("Enter");
		await page.keyboard.type("ok");
		await page.mouse.click(10, 20);
		await page.click("#near");
		await page.fill("#name", "");
		await page.locator("#near").click({ trial: true });
		expect(plain({ e: fake.events }).e).toEqual(SEQUENCE.events);
	});

	it("a click calls the mouse engine once at the point", async () => {
		const seen: number[][] = [];
		const { events, page } = await attachedPage({
			mouse: custom((_p: Any, x: number, y: number, { original }: Any) => {
				seen.push([x, y]);
				return original();
			}),
		});
		await page.locator("#near").click();
		expect(seen).toEqual([[525, 310]]);
		expect(events).toEqual([
			["locator.scrollIntoViewIfNeeded", "#near"],
			["mouse.move", 525, 310],
			["locator.click", "#near", null],
		]);
	});

	it("an engine calling its own channel reaches Playwright", async () => {
		const { events, page } = await attachedPage({
			mouse: custom(async (p: Any, x: number, y: number) => {
				await p.mouse.move(x - 1, y - 1);
				return p.mouse.move(x, y);
			}),
		});
		await page.mouse.move(50, 60);
		expect(events).toEqual([
			["mouse.move", 49, 59],
			["mouse.move", 50, 60],
		]);
	});

	it("exceptions propagate", async () => {
		const { page } = await attachedPage({
			keyboard: custom(() => {
				throw new Error("engine failed");
			}),
		});
		await expect(page.keyboard.type("x")).rejects.toThrow("engine failed");
	});

	it("popups and new contexts are wrapped, other browsers are not", async () => {
		const calls: unknown[] = [];
		const mover = (_p: Any, x: number, y: number, { original }: Any) => {
			calls.push(["a", x, y]);
			return original();
		};
		const fake = fakeBrowser();
		attach(fake.browser as Any, { mouse: custom(mover) });
		const page: Any = await fake.browser.newPage();
		await page.context().openPage().mouse.move(1, 2);
		const context: Any = await fake.browser.newContext();
		await context.openPage().mouse.move(3, 4);

		const other = await attachedPage({ keyboard: custom(noop) });
		await other.page.mouse.move(5, 6);
		expect(calls).toEqual([
			["a", 1, 2],
			["a", 3, 4],
		]);
		expect(other.events).toEqual([["mouse.move", 5, 6]]);
		expect(() => attach(fake.browser as Any, { mouse: custom(mover) })).toThrow(
			"already attached",
		);
	});

	it("the streams follow the seed", async () => {
		const draws: number[] = [];
		for (const seed of [1, 1, 2]) {
			const { page } = await attachedPage({
				mouse: custom((_p: Any, _x: number, _y: number, { rng }: Any) => {
					draws.push(rng());
				}),
				seed,
			});
			await page.mouse.move(0, 0);
		}
		expect(draws[0]).toBe(draws[1]);
		expect(draws[0]).toBe(channelStream(1n, "mouse")());
		expect(draws[2]).not.toBe(draws[0]);
	});

	it("play() holds its schedule under latency", async () => {
		// 200 steps 10 ms apart against 5 ms of latency per event: a late event
		// delays only itself, so the whole plan ends within one interval of 1990 ms.
		let records: Any[] = [];
		const { page } = await attachedPage({
			mouse: custom(async (_p: Any, _x: number, _y: number, { play }: Any) => {
				records = await play(
					Array.from({ length: 200 }, (_, i) => ["move", i, i, i * 10]),
				);
			}),
		});
		page.latency = 5;
		const start = performance.now();
		await page.mouse.move(1, 1);
		const elapsed = performance.now() - start;
		const lag = records.map((r) => r.tActual - r.tPlanned);
		expect(records.length).toBe(200);
		expect(Math.max(...lag)).toBeLessThan(10);
		expect(elapsed).toBeGreaterThanOrEqual(1990);
		expect(elapsed).toBeLessThan(1990 + 10 + 5 + 50);
	});
});
// biome-ignore-end lint/suspicious/noExplicitAny: fakes stand in for Playwright
