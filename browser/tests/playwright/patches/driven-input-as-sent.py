r"""
Input the driver sends reaches the page as it was sent.

The browser adds no pacing and no shaping to driven input: it does not hold a
key or a button, wait between a move and the press that follows it, or split
a wheel scroll. Whatever timing a session should have is the driver's to send
(docs/input-dispatch.md).

Checked on one page, ten rounds each so that one slow frame decides nothing:

  wheel     mouse.wheel(0, 100) is one wheel event, deltaMode 0, deltaY 100
  insert    keyboard.insert_text("hello") pastes the text
            (driven-fill-paste.py) with the shortcut's four key events inside
            50 ms
  key       the fastest keydown -> keyup of keyboard.press("a") is under 50 ms
  button    the fastest mousedown -> mouseup of mouse.click() is under 50 ms
  hover     the fastest mousemove -> mousedown of mouse.click() is under 50 ms

50 ms is below the shortest hold or dwell a build that paces input ever
waited, so such a build fails every round and this one passes on its fastest.

    python browser/tests/playwright/patches/driven-input-as-sent.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

ROUNDS = 10
PACED_MS = 50

PAGE = """<!doctype html><html><body style="height:5000px"><input id=t><script>
// Guards read the page through the DOM: evaluate() runs in its own world.
const seen = [];
for (const type of ['mousemove', 'mousedown', 'mouseup', 'keydown', 'keyup', 'input', 'wheel'])
  addEventListener(type, e => {
    if (!e.isTrusted) return;
    seen.push([type, e.timeStamp, e.deltaY ?? null, e.deltaMode ?? null]);
    document.body.dataset.seen = JSON.stringify(seen);
  }, true);
new MutationObserver(() => {
  if (!document.body.dataset.seen) seen.length = 0;
}).observe(document.body, {attributes: true});
</script></body></html>"""


def fastest(seen, first, then):
    """The shortest time from a `first` event to the `then` event that follows it."""
    gaps = []
    start = None
    for kind, t, *_ in seen:
        if kind == first:
            start = t
        elif kind == then and start is not None:
            gaps.append(t - start)
            start = None
    return min(gaps, default=float("inf")), len(gaps)


def main() -> int:
    from camoufox.sync_api import Camoufox

    failures = []
    with Camoufox(headless=True, executable_path=str(resolve_binary()), os="linux", i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        page.wait_for_timeout(500)

        def take():
            page.wait_for_timeout(400)
            return json.loads(page.evaluate(
                "() => { const s = document.body.dataset.seen || '[]'; delete document.body.dataset.seen; return s; }"))

        page.mouse.move(300, 300)
        take()

        for _ in range(ROUNDS):
            page.mouse.wheel(0, 100)
            wheels = [(dy, mode) for kind, _, dy, mode in take() if kind == "wheel"]
            if wheels != [(100, 0)]:
                failures.append(f"wheel: wheel(0, 100) reached the page as {wheels} (deltaY, deltaMode)")
                break
        print(f"wheel: last wheel(0, 100) gave {wheels}")

        page.evaluate("() => scrollTo(0, 0)")
        page.click("#t")
        take()
        page.keyboard.insert_text("hello")
        seen = take()
        kinds = [kind for kind, *_ in seen]
        took = seen[-1][1] - seen[0][1] if seen else float("inf")
        value = page.evaluate("() => document.getElementById('t').value")
        print(f"insert: value {value!r}, events {kinds} in {took:.1f} ms")
        if value != "hello" or kinds != ["keydown", "keydown", "input", "keyup", "keyup"] or took >= PACED_MS:
            failures.append(f"insert: insert_text('hello') left {value!r} with events {kinds} over {took:.1f} ms")

        for _ in range(ROUNDS):
            page.keyboard.press("a")
        seen = take()
        gap, n = fastest(seen, "keydown", "keyup")
        print(f"key: {n} presses, fastest keydown -> keyup {gap:.1f} ms")
        if n != ROUNDS or gap >= PACED_MS:
            failures.append(f"key: {n} presses, none released in under {PACED_MS} ms (fastest {gap:.1f})")

        for i in range(ROUNDS):
            page.mouse.click(200 + 20 * i, 400)
        seen = take()
        for name, first, then in (("button", "mousedown", "mouseup"), ("hover", "mousemove", "mousedown")):
            gap, n = fastest(seen, first, then)
            print(f"{name}: {n} clicks, fastest {first} -> {then} {gap:.1f} ms")
            if n != ROUNDS or gap >= PACED_MS:
                failures.append(f"{name}: {n} clicks, no {first} -> {then} in under {PACED_MS} ms (fastest {gap:.1f})")

    for f in failures:
        print(f"FAIL: {f}")
    if failures:
        return 1
    print("PASS: wheel, inserted text, keys and clicks reach the page as the driver sent them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
