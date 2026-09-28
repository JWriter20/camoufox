"""
Verify per-channel humanize engines take effect, and that a seeded launch
replays the same input.

`humanize` is a per-channel setting (pythonlib/camoufox/humanize.py). The
browser resolves an engine for each input channel at every action
(additions/juggler/input/HumanizeSeam.js) and draws its randomness from a
stream seeded by `humanize:seed`, one stream per channel. With
CAMOU_HUMANIZE_TRACE set, every humanized action is written to that file with
its plan.

Run against a specific build:
    CAMOUFOX_EXECUTABLE_PATH=/path/to/camoufox-bin python tests/patches/humanize-seam.py
Against an unpackaged objdir build, run `make stage-fonts` first.

What PASS means:
    * mouse raw() with scroll notches(): a long move is one mousemove, and a
      wheel turn of 300px arrives as 3 line-mode notches;
    * mouse cursory() with scroll raw(): a long move is a trajectory, and a
      wheel turn arrives as the one pixel delta Playwright asked for;
    * two launches with the same seed plan the same moves and wheel turns, and
      the page receives the same mousemove coordinates; a different seed plans
      different moves.
"""

import asyncio
import json
import os
import sys
import tempfile

from camoufox.async_api import AsyncCamoufox
from camoufox.humanize import cursory, notches, raw

EXECUTABLE_PATH = os.environ.get("CAMOUFOX_EXECUTABLE_PATH")

BODY = '<body style="margin:0;width:1400px;height:5000px"></body>'
RECORDER = """
    window.moves = [];
    window.wheels = [];
    addEventListener("mousemove", e => moves.push([e.clientX, e.clientY]));
    addEventListener("wheel", e => wheels.push([e.deltaMode, e.deltaY]));
"""
TARGETS = [(900, 500), (120, 600), (700, 80), (400, 400)]


async def session(humanize, trace=None):
    """Moves between TARGETS with a wheel turn after each; what the page saw."""
    kwargs = dict(headless=True, os="linux", humanize=humanize)
    if EXECUTABLE_PATH:
        kwargs["executable_path"] = EXECUTABLE_PATH
    if trace:
        kwargs["env"] = {"CAMOU_HUMANIZE_TRACE": trace}
    async with AsyncCamoufox(**kwargs) as browser:
        page = await browser.new_page(viewport={"width": 1000, "height": 700})
        await page.set_content(BODY)
        await page.evaluate(RECORDER)
        await page.mouse.move(20, 20)
        moves_per_target = []
        for x, y in TARGETS:
            start = await page.evaluate("moves.length")
            await page.mouse.move(x, y)
            moves_per_target.append(await page.evaluate(f"moves.slice({start})"))
            await page.mouse.wheel(0, 300)
        wheels = await page.evaluate("wheels")
    return moves_per_target, wheels


def planned(trace):
    with open(trace) as f:
        records = [json.loads(line) for line in f]
    return [(r["channel"], r["engine"], r["seedStreamPos"], r["plan"]) for r in records]


async def main() -> int:
    failures = []

    moves, wheels = await session({"mouse": raw(), "scroll": notches()})
    if any(len(m) != 1 for m in moves):
        failures.append(f"mouse raw(): expected one mousemove per move, got {[len(m) for m in moves]}")
    # A notch is 3 lines; how many lines the page reports per notch is the
    # platform's wheel multiplier, so only the shape is checked.
    if len(wheels) != 3 * len(TARGETS) or len({tuple(w) for w in wheels}) != 1 or wheels[0][0] != 1:
        failures.append(f"scroll notches(): expected 3 equal line-mode notches per turn, got {wheels}")

    moves, wheels = await session({"mouse": cursory(), "scroll": raw()})
    if any(len(m) < 5 for m in moves):
        failures.append(f"mouse cursory(): expected a trajectory per move, got {[len(m) for m in moves]}")
    if wheels != [[0, 300]] * len(TARGETS):
        failures.append(f"scroll raw(): expected one pixel wheel event per turn, got {wheels}")

    with tempfile.TemporaryDirectory() as tmp:
        runs = {}
        for name, seed in (("a", 1234), ("b", 1234), ("c", 1235)):
            trace = os.path.join(tmp, f"{name}.jsonl")
            moves, wheels = await session({"mouse": cursory(), "scroll": notches(), "seed": seed}, trace)
            runs[name] = (planned(trace), moves, wheels)
        plans_a, moves_a, _ = runs["a"]
        plans_b, moves_b, _ = runs["b"]
        plans_c, _, _ = runs["c"]
        channels = sorted({(channel, engine) for channel, engine, _, _ in plans_a})
        print(f"seed 1234: {len(plans_a)} planned actions {channels}; "
              f"{sum(len(p) for *_, p in plans_a)} planned steps; "
              f"{sum(len(m) for m in moves_a)} mousemoves seen by the page")
        if channels != [("mouse", "cursory"), ("scroll", "notches")] or len(plans_a) != 2 * len(TARGETS) + 1:
            failures.append(f"trace: expected {2 * len(TARGETS) + 1} mouse and scroll plans, got {len(plans_a)} {channels}")
        if plans_a != plans_b:
            failures.append("same seed: the planned input differs between launches")
        if moves_a != moves_b:
            failures.append("same seed: the page saw different mousemove coordinates")
        if [p for p in plans_a if p[0] == "mouse"] == [p for p in plans_c if p[0] == "mouse"]:
            failures.append("a different seed planned the same moves")

    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: per-channel engines take effect, and a seeded launch replays the same input")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
