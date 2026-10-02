/**
 * Client-side humanize engines: `custom(fn)` on a channel.
 * TypeScript twin of pythonlib/camoufox/_humanize_custom.py.
 *
 * The function runs in this process, never in the browser. It plans a
 * channel's input and plays it through Playwright's own input methods, so the
 * page sees the same trusted events as from any Playwright call. The browser
 * runs that channel raw (humanizeConfig), so input is never humanized twice.
 *
 * What is wrapped, per page of the browser (or persistent context) it is
 * attached to, and what the function is called for:
 *
 *     mouse     Mouse.move / click / dblclick; the move to the click point of
 *               Page/Locator click, dblclick and hover
 *     keyboard  Keyboard.type / press; Page/Locator fill, type, press, and
 *               Locator.pressSequentially
 *     scroll    Mouse.wheel; Locator.scrollIntoViewIfNeeded, and the scroll
 *               into view before a Page/Locator click, dblclick, hover, fill,
 *               type or pressSequentially when the element is not in view
 *
 * Playwright runs an action inside its own server, so a client wrapper cannot
 * reach into it. The wrappers act first: they scroll the element into view and
 * move to the point, so Playwright's own scroll finds nothing to do and its own
 * move has no distance to cover.
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { randomBytes } from "node:crypto";
import type { Browser, BrowserContext, Locator, Page } from "playwright-core";
import {
	type Channel,
	type CustomEngineFn,
	channelStream,
	customEngines,
	type HumanizeSetting,
	normalize,
	type SeededRng,
} from "./humanize.js";
import { ValueError } from "./pycompat.js";
import { LeakWarning } from "./warnings.js";

type AnyFn = (...args: any[]) => any;

/** A play() step. t is milliseconds from the start of the call, non-decreasing. */
export type PlayStep =
	| ["move", number, number, number]
	| ["down" | "up", "left" | "right" | "middle", number]
	| ["wheel", number, number, number]
	| ["key", string, "down" | "up", number]
	| ["text", string, number];

export interface PlayRecord {
	i: number;
	tPlanned: number;
	tActual: number;
}

export type Play = (steps: PlayStep[]) => Promise<PlayRecord[]>;

/** What a custom engine's function receives after its positional arguments. */
export interface CustomEngineContext {
	/** Playwright's own behaviour for this call; calling it is how a function declines. */
	original: AnyFn;
	/** The channel's seeded stream, bit for bit the browser's. */
	rng: SeededRng;
	/** Dispatch steps on this page on a schedule. */
	play: Play;
	/** keyboard only: "type", "press" or "fill". */
	kind?: "type" | "press" | "fill";
}

// The channels whose function is running in this async context. A call a
// function makes on its own channel (page.mouse.move inside a mouse engine)
// goes straight to Playwright instead of back into the function.
const active = new AsyncLocalStorage<ReadonlySet<Channel>>();

// Is the element's box inside its frame's viewport?
const IN_VIEW = (e: Element) => {
	const r = e.getBoundingClientRect();
	return (
		r.top >= 0 &&
		r.left >= 0 &&
		r.bottom <= innerHeight &&
		r.right <= innerWidth
	);
};

const BUTTONS = ["left", "right", "middle"];

interface Originals {
	move: AnyFn;
	down: AnyFn;
	up: AnyFn;
	wheel: AnyFn;
	click: AnyFn;
	dblclick: AnyFn;
	type: AnyFn;
	press: AnyFn;
	keyDown: AnyFn;
	keyUp: AnyFn;
	insertText: AnyFn;
}

class Engines {
	readonly rng: Partial<Record<Channel, SeededRng>> = {};

	constructor(
		readonly fns: Partial<Record<Channel, CustomEngineFn>>,
		readonly seed: bigint,
	) {
		for (const channel of Object.keys(fns) as Channel[])
			this.rng[channel] = channelStream(seed, channel);
	}

	/** The channel's function, or undefined when it has none or is already running. */
	fn(channel: Channel): CustomEngineFn | undefined {
		if (active.getStore()?.has(channel)) return undefined;
		return this.fns[channel];
	}
}

const pageEngines = new WeakMap<object, Engines>();
const pageOriginals = new WeakMap<object, Originals>();
const attachedTargets = new WeakSet<object>();
const locatorOriginals = new Map<object, Record<string, AnyFn>>();

/** Call the channel's function, with its own channel passed through. */
async function call(
	engines: Engines,
	channel: Channel,
	page: Page,
	args: unknown[],
	original: AnyFn,
	kind?: CustomEngineContext["kind"],
): Promise<unknown> {
	const fn = engines.fns[channel] as CustomEngineFn;
	const running = new Set(active.getStore() ?? []);
	running.add(channel);
	const context: CustomEngineContext = {
		original,
		rng: engines.rng[channel] as SeededRng,
		play: (steps) => play(page, steps),
	};
	if (kind !== undefined) context.kind = kind;
	return await active.run(running, () => fn(page, ...args, context));
}

// ---- play(): schedule-paced dispatch --------------------------------------

const isNumber = (v: unknown): v is number =>
	typeof v === "number" && Number.isFinite(v);

const ARITY: Record<string, number> = {
	move: 3,
	down: 2,
	up: 2,
	wheel: 3,
	key: 3,
	text: 2,
};

/** The steps of a play() call, or a ValueError naming the first bad step.
 *  Nothing is dispatched unless every step is valid. */
export function validateSteps(steps: unknown): PlayStep[] {
	if (!Array.isArray(steps)) {
		throw new ValueError(
			`play() takes a list of steps, got ${JSON.stringify(steps)}`,
		);
	}
	let last = 0;
	steps.forEach((step: unknown, i) => {
		const bad = (why: string) =>
			new ValueError(`play() step ${i} ${JSON.stringify(step)}: ${why}`);
		if (!Array.isArray(step) || typeof step[0] !== "string")
			throw bad('a step is an array such as ["move", x, y, t]');
		const [kind, ...args] = step;
		const arity = ARITY[kind];
		if (arity === undefined)
			throw bad("the kind must be move, down, up, wheel, key or text");
		if (args.length !== arity)
			throw bad(`${kind} takes ${arity} values after the kind`);
		const t = args[arity - 1];
		const values = args.slice(0, -1);
		if (!isNumber(t) || t < 0)
			throw bad("t must be a finite number of milliseconds, at least 0");
		if (t < last) throw bad(`t goes back in time (${t} after ${last})`);
		last = t;
		if ((kind === "move" || kind === "wheel") && !values.every(isNumber))
			throw bad(`${kind} takes finite numbers`);
		if ((kind === "down" || kind === "up") && !BUTTONS.includes(values[0]))
			throw bad("the button must be one of ('left', 'right', 'middle')");
		if (
			kind === "key" &&
			(typeof values[0] !== "string" ||
				!values[0] ||
				(values[1] !== "down" && values[1] !== "up"))
		)
			throw bad('key takes a key name and "down" or "up"');
		if (kind === "text" && typeof values[0] !== "string")
			throw bad("text takes a string");
	});
	return steps as PlayStep[];
}

const sleep = (ms: number) =>
	new Promise<void>((resolve) => setTimeout(resolve, ms));

/** Dispatch each step at start + t on a monotonic clock, so a late round trip
 *  delays only its own step and never accumulates. */
async function play(page: Page, steps: PlayStep[]): Promise<PlayRecord[]> {
	const plan = validateSteps(steps);
	const o = pageOriginals.get(page) as Originals;
	const record: PlayRecord[] = [];
	const start = performance.now();
	for (const [i, step] of plan.entries()) {
		const t = step[step.length - 1] as number;
		const wait = start + t - performance.now();
		if (wait > 0) await sleep(wait);
		const tActual = performance.now() - start;
		switch (step[0]) {
			case "move":
				await o.move(step[1], step[2]);
				break;
			case "down":
				await o.down({ button: step[1] });
				break;
			case "up":
				await o.up({ button: step[1] });
				break;
			case "wheel":
				await o.wheel(step[1], step[2]);
				break;
			case "key":
				await (step[2] === "down" ? o.keyDown : o.keyUp)(step[1]);
				break;
			case "text":
				await o.insertText(step[1]);
				break;
		}
		record.push({ i, tPlanned: t, tActual });
	}
	return record;
}

// ---- flows ----------------------------------------------------------------

function move(engines: Engines, page: Page, x: number, y: number) {
	const o = pageOriginals.get(page) as Originals;
	return call(engines, "mouse", page, [x, y], (...a: unknown[]) =>
		o.move(...(a.length ? a : [x, y])),
	);
}

function locatorOriginal(locator: Locator, name: string): AnyFn {
	return (
		locatorOriginals.get(Object.getPrototypeOf(locator)) as Record<
			string,
			AnyFn
		>
	)[name];
}

async function scrollIntoView(
	engines: Engines,
	locator: Locator,
	timeout: number | undefined,
	ifNeeded: boolean,
): Promise<unknown> {
	if (ifNeeded && (await locator.evaluate(IN_VIEW, undefined, { timeout })))
		return undefined;
	const scroll = locatorOriginal(locator, "scrollIntoViewIfNeeded");
	return call(
		engines,
		"scroll",
		locator.page(),
		[locator],
		(options?: object) => scroll.call(locator, { timeout, ...options }),
	);
}

type LocatorFlow = (
	engines: Engines,
	locator: Locator,
	original: AnyFn,
	args: unknown[],
) => Promise<unknown> | undefined;

/** click / dblclick / hover: scroll into view, move to the point, then
 *  Playwright's own action, whose move is then zero-length. */
const pointer: LocatorFlow = (engines, locator, original, args) => {
	const mouse = engines.fn("mouse");
	const scroll = engines.fn("scroll");
	const options: any = args[0] ?? {};
	if (!(mouse || scroll) || options.trial) return undefined;
	return (async () => {
		const timeout = options.timeout;
		if (scroll) await scrollIntoView(engines, locator, timeout, true);
		if (mouse) {
			if (!scroll) {
				// The point must be on screen; the browser's scroll engine plans this.
				await locatorOriginal(locator, "scrollIntoViewIfNeeded").call(locator, {
					timeout,
				});
			}
			const box = await locator.boundingBox({ timeout });
			if (box) {
				let x: number;
				let y: number;
				if (options.position) {
					// position is relative to the padding box
					const [left, top] = await locator.evaluate(
						(e) => [e.clientLeft, e.clientTop],
						undefined,
						{ timeout },
					);
					x = box.x + left + options.position.x;
					y = box.y + top + options.position.y;
				} else {
					x = box.x + box.width / 2;
					y = box.y + box.height / 2;
				}
				await move(engines, locator.page(), x, y);
			}
		}
		return original.apply(locator, args);
	})();
};

/** fill / type / pressSequentially / press on a locator. */
function keys(
	kind: "fill" | "type" | "press",
	focus: "select" | "focus",
): LocatorFlow {
	return (engines, locator, original, args) => {
		const keyboard = engines.fn("keyboard");
		const scroll = kind !== "press" && engines.fn("scroll");
		if (!(keyboard || scroll)) return undefined;
		const [text, options = {}] = args as [string, Record<string, unknown>?];
		return (async () => {
			const timeout = options.timeout as number | undefined;
			if (scroll) await scrollIntoView(engines, locator, timeout, true);
			if (!keyboard || text === "") return original.apply(locator, args);
			if (focus === "select") {
				// Typing then replaces what the field held, as fill() does.
				await locator.selectText({
					force: options.force as boolean | undefined,
					timeout,
				});
			} else {
				await locator.focus({ timeout });
			}
			return call(
				engines,
				"keyboard",
				locator.page(),
				[text],
				(value?: string) =>
					original.call(locator, value ?? text, ...args.slice(1)),
				kind,
			);
		})();
	};
}

const scrollLocator: LocatorFlow = (engines, locator, _original, args) => {
	if (!engines.fn("scroll")) return undefined;
	const options = (args[0] ?? {}) as { timeout?: number };
	return scrollIntoView(engines, locator, options.timeout, false);
};

const LOCATOR_FLOWS: Record<string, LocatorFlow> = {
	click: pointer,
	dblclick: pointer,
	hover: pointer,
	fill: keys("fill", "select"),
	type: keys("type", "focus"),
	pressSequentially: keys("type", "focus"),
	press: keys("press", "focus"),
	scrollIntoViewIfNeeded: scrollLocator,
};

/** Route a Locator prototype's input methods through the custom engines of the
 *  locator's page. A page with none (any other browser) gets the original. */
function patchLocatorPrototype(proto: Record<string, AnyFn>): void {
	if (locatorOriginals.has(proto)) return;
	const originals = Object.fromEntries(
		Object.keys(LOCATOR_FLOWS).map((name) => [name, proto[name]]),
	);
	locatorOriginals.set(proto, originals);
	for (const [name, original] of Object.entries(originals)) {
		proto[name] = function (this: Locator, ...args: unknown[]) {
			const engines = pageEngines.get(this.page());
			const flow =
				engines && LOCATOR_FLOWS[name](engines, this, original, args);
			return flow ?? original.apply(this, args);
		};
	}
}

// Page methods that take a selector, routed to the Locator method of the same
// name when one of these channels has a custom engine.
const PAGE_ROUTES: Record<string, Channel[]> = {
	click: ["mouse", "scroll"],
	dblclick: ["mouse", "scroll"],
	hover: ["mouse", "scroll"],
	fill: ["keyboard", "scroll"],
	type: ["keyboard", "scroll"],
	press: ["keyboard"],
};

function attachPage(page: Page, engines: Engines): void {
	if (pageEngines.has(page)) return;
	const mouse = page.mouse as any;
	const keyboard = page.keyboard as any;
	const o: Originals = {
		move: mouse.move.bind(mouse),
		down: mouse.down.bind(mouse),
		up: mouse.up.bind(mouse),
		wheel: mouse.wheel.bind(mouse),
		click: mouse.click.bind(mouse),
		dblclick: mouse.dblclick.bind(mouse),
		type: keyboard.type.bind(keyboard),
		press: keyboard.press.bind(keyboard),
		keyDown: keyboard.down.bind(keyboard),
		keyUp: keyboard.up.bind(keyboard),
		insertText: keyboard.insertText.bind(keyboard),
	};
	pageOriginals.set(page, o);
	pageEngines.set(page, engines);
	patchLocatorPrototype(Object.getPrototypeOf(page.locator("html")));

	const unless =
		(channel: Channel, original: AnyFn, flow: AnyFn) =>
		(...args: unknown[]) =>
			engines.fn(channel) ? flow(...args) : original(...args);

	if (engines.fns.mouse) {
		mouse.move = unless(
			"mouse",
			o.move,
			(x: number, y: number, options?: object) =>
				call(engines, "mouse", page, [x, y], (...a: unknown[]) =>
					o.move(...(a.length ? a : [x, y, options])),
				),
		);
		for (const name of ["click", "dblclick"] as const) {
			mouse[name] = unless(
				"mouse",
				o[name],
				async (x: number, y: number, options?: object) => {
					await move(engines, page, x, y);
					return o[name](x, y, options);
				},
			);
		}
	}
	if (engines.fns.scroll) {
		mouse.wheel = unless("scroll", o.wheel, (deltaX: number, deltaY: number) =>
			call(engines, "scroll", page, [[deltaX, deltaY]], (...a: unknown[]) =>
				o.wheel(...(a.length ? a : [deltaX, deltaY])),
			),
		);
	}
	if (engines.fns.keyboard) {
		for (const name of ["type", "press"] as const) {
			keyboard[name] = unless(
				"keyboard",
				o[name],
				(text: string, options?: object) =>
					call(
						engines,
						"keyboard",
						page,
						[text],
						(value?: string) => o[name](value ?? text, options),
						name,
					),
			);
		}
	}

	// page.click(selector) and friends: the same flow as the locator's.
	const target = page as any;
	for (const [name, channels] of Object.entries(PAGE_ROUTES)) {
		if (!channels.some((c) => engines.fns[c])) continue;
		const takesText = name === "fill" || name === "type" || name === "press";
		target[name] = (selector: string, ...rest: unknown[]) => {
			const args = [...rest];
			const at = takesText ? 1 : 0;
			const { strict, ...options } = (args[at] ?? {}) as {
				strict?: boolean;
			};
			args[at] = options;
			const locator = page.locator(selector);
			// Without strict, Page methods act on the first match.
			return ((strict ? locator : locator.first()) as any)[name](...args);
		};
	}
}

function attachContext(context: BrowserContext, engines: Engines): void {
	if (attachedTargets.has(context)) return;
	attachedTargets.add(context);
	context.on("page", (page) => attachPage(page, engines));
	for (const page of context.pages()) attachPage(page, engines);
}

const isBrowser = (target: Browser | BrowserContext): target is Browser =>
	typeof (target as Browser).newContext === "function";

/** The custom engines of a `humanize` setting, checked before a launch. */
export function check(
	humanize: HumanizeSetting,
): Partial<Record<Channel, CustomEngineFn>> {
	return customEngines(humanize);
}

/**
 * Run the custom engines of a `humanize` setting on every page of `target`, a
 * Browser or BrowserContext, including pages and contexts opened later. Other
 * browsers in this process are untouched.
 *
 * The streams use `humanize.seed`, or `seed`, or a random seed.
 */
export function attach<T extends Browser | BrowserContext>(
	target: T,
	humanize: HumanizeSetting,
	{
		seed,
		iKnowWhatImDoing,
	}: { seed?: bigint; iKnowWhatImDoing?: boolean } = {},
): T {
	const fns = check(humanize);
	if (!Object.keys(fns).length) return target;
	if (attachedTargets.has(target)) {
		throw new ValueError(
			"custom humanize engines are already attached to this browser",
		);
	}
	if ((target as any)._connection?.isRemote?.()) {
		LeakWarning.warn("humanize_custom_remote", iKnowWhatImDoing);
	}
	const given =
		normalize(humanize).seed ?? seed ?? randomBytes(8).readBigUInt64BE(0);
	const engines = new Engines(fns, given);

	if (!isBrowser(target)) {
		attachContext(target, engines);
		return target;
	}
	attachedTargets.add(target);
	for (const context of target.contexts()) attachContext(context, engines);
	const newContext = target.newContext.bind(target);
	const newPage = target.newPage.bind(target);
	const browser = target as any;
	browser.newContext = async (...args: unknown[]) => {
		const context = await newContext(...(args as []));
		attachContext(context, engines);
		return context;
	};
	browser.newPage = async (...args: unknown[]) => {
		const page = await newPage(...(args as []));
		attachContext(page.context(), engines);
		attachPage(page, engines);
		return page;
	};
	return target;
}
