"""
Verify custom() humanize engines: functions in the launcher's process plan the
input, and the page receives exactly what they play.

`custom(fn)` on a channel (pythonlib/camoufox/_humanize_custom.py) runs that
channel raw in the browser and wraps Playwright's input methods on the client:
the function gets the call, a stream seeded the browser's way, and `play()`,
which dispatches steps on a schedule through Playwright's own input.

Run against a specific build:
    CAMOUFOX_EXECUTABLE_PATH=/path/to/camoufox-bin python tests/patches/humanize-custom.py
Against an unpackaged objdir build, run `make stage-fonts` first. With the
TypeScript launcher built (typescript/dist) and Node on PATH, the same session
also runs through it (humanize-custom.mjs) and must match.

What PASS means:
    * every channel custom(): the page sees each planned move, key and wheel
      step, trusted, and nothing else: a locator click after the planned move
      adds no mousemove (Playwright's own move has no distance left), a fill
      types the text key by key, and an element out of view is brought in by
      the scroll function's wheel steps;
    * custom() mouse beside the build's own scroll engine: the browser scrolls
      the element into view with its engine and moves the cursor there with
      mouse:internal (a path, not a jump), while no move from the custom mouse
      is humanized a second time;
    * the same seed gives the same input; another seed does not;
    * the TypeScript launcher, given the same seed and functions, produces the
      same events (when it is built).
"""

import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from camoufox.async_api import AsyncCamoufox
from camoufox.humanize import auto, custom

EXECUTABLE_PATH = os.environ.get("CAMOUFOX_EXECUTABLE_PATH")
HERE = Path(__file__).resolve().parent
TS_DIST = HERE.parents[1] / "typescript" / "dist" / "index.js"

BODY = """<body style="margin:0;height:5000px">
  <input id="name" style="position:absolute;left:40px;top:120px;width:200px">
  <button id="near" style="position:absolute;left:500px;top:300px;width:50px;height:20px">near</button>
  <button id="far" style="position:absolute;left:100px;top:2600px;width:80px;height:30px">far</button>
  <div id="box" style="position:absolute;left:600px;top:380px;width:300px;height:200px;overflow:auto">
    <div style="height:1500px"><button id="deep" style="margin-top:1100px">deep</button></div>
  </div>
</body>"""
RECORDER = """() => {
  window.rec = [];
  const round = v => Math.round(v * 100) / 100;
  for (const type of ['mousemove', 'mousedown', 'mouseup', 'click', 'keydown', 'keyup', 'wheel'])
    addEventListener(type, e => rec.push([type, e.isTrusted, round(e.clientX ?? 0), round(e.clientY ?? 0),
                                          e.key ?? '', round(e.deltaY ?? 0)]), true);
}"""

# Wheel scrolling lands asynchronously; wait until the page stops moving so
# the next measurement does not depend on timing.
SETTLE = """() => new Promise(resolve => {
  let last = scrollY, same = 0;
  const tick = () => {
    if (scrollY === last) { if (++same >= 10) return resolve(scrollY); }
    else { same = 0; last = scrollY; }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
})"""


def reference_engines(log):
    """Mirrored in humanize-custom.mjs, draw for draw."""
    at = {"x": 0.0, "y": 0.0}
    planned = []

    async def move(page, x, y, *, original, rng, play):
        log.append(["mouse", x, y])
        n = 8 + math.floor(rng() * 8)
        planned.append(n)
        sx, sy = at["x"], at["y"]
        steps = []
        for i in range(1, n + 1):
            f = i / n
            ease = f * f * (3 - 2 * f)
            jitter = (rng() - 0.5) * 6 * (1 - f)
            steps.append(["move", sx + (x - sx) * ease + jitter, sy + (y - sy) * ease - jitter, i * 12])
        at.update(x=x, y=y)
        return await play(steps)

    async def keys(page, text, *, original, rng, play, kind):
        log.append(["keyboard", kind, text])
        if kind == "press":
            return await original()
        steps, t = [], 0
        for ch in text:
            steps.append(["key", ch, "down", t])
            t += 30 + math.floor(rng() * 40)
            steps.append(["key", ch, "up", t])
            t += 20 + math.floor(rng() * 60)
        return await play(steps)

    async def scroll(page, target, *, original, rng, play):
        if isinstance(target, tuple):
            log.append(["scroll", list(target)])
            n = 3 + math.floor(rng() * 3)
            return await play([["wheel", target[0] / n, target[1] / n, i * 16] for i in range(n)])
        # Wheel until the element's centre is near the middle, then let
        # Playwright finish (a no-op when it is already in view).
        log.append(["scroll", "locator"])
        distance = await target.evaluate(
            "e => { const r = e.getBoundingClientRect(); return r.top + r.height / 2 - innerHeight / 2; }"
        )
        steps, done, t = [], 0, 0
        while abs(distance - done) > 1:
            chunk = math.floor(max(-120, min(120, distance - done)) * (0.8 + rng() * 0.2) + 0.5)
            if chunk == 0:
                break
            steps.append(["wheel", 0, chunk, t])
            done += chunk
            t += 16 + math.floor(rng() * 10)
        await play(steps)
        await page.evaluate(SETTLE)
        return await original()

    return move, keys, scroll, planned


async def custom_session(seed, humanize_extra=None, trace=None, nested=False):
    log = []
    move, keys, scroll, planned = reference_engines(log)
    humanize = {"mouse": custom(move), "keyboard": custom(keys), "scroll": custom(scroll), "seed": seed}
    humanize.update(humanize_extra or {})
    kwargs = dict(headless=True, os="linux", humanize=humanize)
    if EXECUTABLE_PATH:
        kwargs["executable_path"] = EXECUTABLE_PATH
    if trace:
        kwargs["env"] = {"CAMOU_HUMANIZE_TRACE": trace}
    async with AsyncCamoufox(**kwargs) as browser:
        page = await browser.new_page(viewport={"width": 1000, "height": 700})
        await page.set_content(BODY)
        await page.evaluate(RECORDER)
        await page.mouse.move(300, 200)
        await page.mouse.wheel(0, 360)
        await page.evaluate(SETTLE)
        await page.mouse.wheel(0, -360)
        await page.evaluate(SETTLE)
        await page.locator("#near").click()
        await page.locator("#name").fill("Hi there")
        await page.keyboard.press("Tab")
        await page.locator("#far").click()
        if nested:
            # A scroller elsewhere on the page: the browser moves the cursor over it first.
            await page.locator("#deep").click()
        state = await page.evaluate(
            "() => [document.querySelector('#name').value, Math.round(scrollY),"
            " (r => r.top >= 0 && r.bottom <= innerHeight)(document.querySelector('#far').getBoundingClientRect())]"
        )
        events = await page.evaluate("rec")
    return log, events, state, planned


def run_typescript(seed):
    node = shutil.which("node")
    if not node or not TS_DIST.exists():
        return None
    args = [node, str(HERE / "humanize-custom.mjs"), str(TS_DIST), str(seed)]
    if EXECUTABLE_PATH:
        args.append(EXECUTABLE_PATH)
    out = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"humanize-custom.mjs exited {out.returncode}:\n{out.stderr}")
    return json.loads(out.stdout)


async def main() -> int:
    failures = []

    log, events, state, planned = await custom_session(1234)
    kinds = [e[0] for e in events]
    moves = [e for e in events if e[0] == "mousemove"]
    wheels = [e for e in events if e[0] == "wheel"]
    print(f"all custom, seed 1234: engine calls {[entry[:2] for entry in log]}")
    print(f"  page saw {len(moves)} mousemoves (planned {sum(planned)}), {kinds.count('keydown')} keydowns, {len(wheels)} wheels, "
          f"{kinds.count('click')} clicks; value/scrollY/far in view: {state}")
    if len(moves) != sum(planned):
        failures.append(f"the page saw {len(moves)} mousemoves for {sum(planned)} planned ones")
    if not all(e[1] for e in events):
        failures.append("an event was not trusted")
    if state[0] != "Hi there" or not state[2]:
        failures.append(f"expected the text typed and #far in view, got {state}")
    if [entry[0] for entry in log] != ["mouse", "scroll", "scroll", "mouse", "keyboard", "keyboard", "scroll", "mouse"]:
        failures.append(f"unexpected engine calls {log}")
    if [entry[1] for entry in log if entry[0] == "keyboard"] != ["fill", "press"]:
        failures.append("fill and press did not reach the keyboard function with their kinds")
    # Each click's point is the end of the planned path, and Playwright's own
    # move added nothing: the page saw only the planned moves.
    clicks = [e for e in events if e[0] == "click"]
    if len(clicks) != 2:
        failures.append(f"expected 2 clicks, got {clicks}")
    # Playwright holds Shift for an uppercase key of its own accord.
    typed = [e[4] for e in events if e[0] == "keydown" and e[4] != "Shift"]
    if typed != [*"Hi there", "Tab"]:
        failures.append(f"expected a keydown per character plus Tab, got {typed}")
    for click in clicks:
        before = [e for e in events[: events.index(click)] if e[0] == "mousemove"]
        if not before or before[-1][2:4] != click[2:4]:
            failures.append(f"the click at {click[2:4]} did not land where the planned path ended")

    # The same seed replays; another seed does not.
    log_b, events_b, _, _ = await custom_session(1234)
    _, events_c, _, _ = await custom_session(1235)
    same = [e[2:] for e in events_b] == [e[2:] for e in events]
    print(f"seed 1234 again: identical page events: {same}; seed 1235 differs: "
          f"{[e[2:] for e in events_c] != [e[2:] for e in events]}")
    if not same or log_b != log:
        failures.append("the same seed gave different input")
    if [e[2:] for e in events_c] == [e[2:] for e in events]:
        failures.append("another seed gave the same input")

    # Custom mouse beside the build's own scroll engine.
    with tempfile.TemporaryDirectory() as tmp:
        trace = os.path.join(tmp, "trace.jsonl")
        log_s, events_s, state_s, _ = await custom_session(77, {"scroll": auto()}, trace, nested=True)
        with open(trace) as f:
            records = [json.loads(line) for line in f]
    engines = sorted({(r["channel"], r["engine"]) for r in records})
    internal = [r for r in records if r["channel"] == "mouse:internal"]
    print(f"custom mouse + scroll auto(): browser plans {engines}; "
          f"{len([e for e in events_s if e[0] == 'click'])} of 3 clicks landed; "
          f"{len(internal)} mouse:internal moves {[(r['engine'], len(r['plan'])) for r in internal]}")
    if any(channel == "mouse" for channel, _ in engines):
        failures.append("the browser humanized a move the custom mouse had already planned")
    clicked = [e for e in events_s if e[0] == "click"]
    if not any(channel == "scroll" for channel, _ in engines) or len(clicked) != 3:
        failures.append(f"the build's scroll engine did not bring every target into view: {len(clicked)} clicks")
    if any(entry[0] == "scroll" for entry in log_s):
        failures.append("the custom scroll function ran although scroll was auto()")
    # Only an engine that plans scrolling into view (not the base notches)
    # moves the cursor over a scroller first.
    into_view = any(r["channel"] == "scroll" and r["command"] == "Page.scrollIntoViewIfNeeded" for r in records)
    if (into_view and not internal) or any(r["engine"] == "raw" or len(r["plan"]) < 2 for r in internal):
        failures.append(f"a cursor move the browser originated was a jump: {internal}")

    # The TypeScript launcher: same seed, same functions, same events.
    ts = run_typescript(1234)
    if ts is None:
        print("typescript: not built (typescript/dist) or no node; parity not checked")
    else:
        same_log = ts["log"] == json.loads(json.dumps(log))
        same_events = ts["events"] == json.loads(json.dumps(events))
        print(f"typescript, seed 1234: engine calls identical: {same_log}; "
              f"{len(ts['events'])} page events identical: {same_events}; state {ts['state']}")
        if not (same_log and same_events and ts["state"] == state):
            failures.append("the TypeScript launcher produced different input for the same seed")

    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: custom engines plan every channel client-side, beside built-in ones, and replay by seed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
