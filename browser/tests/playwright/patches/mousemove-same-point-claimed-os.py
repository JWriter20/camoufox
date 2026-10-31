"""
A Windows identity drops a native mouse move to the point the last one went to, as Windows does.

Windows Firefox 158.0.1 dispatches no mousemove for a native move that lands
on the screen point the previous one did (widget/windows/nsWindow.cpp,
LastMouseMoveData): a mouse that did not move did not move. GTK dispatches
every motion event the X server sends, and X sends one for a move to where
the pointer already is (XTest, or a device whose motion rounds to the same
pixel). cfparity's driven.hover on win-i9: stock's click after a hover made no
second move; Camoufox on Linux claiming Windows fired pointermove and mousemove
with movement [0, 0] before mousedown.

The guard moves the pointer with XTest to a point, again to the same point,
then presses and releases there, and counts the page's moves. Claiming
Windows: one. Claiming Linux: two, as stock Linux Firefox has it.

    python browser/tests/playwright/patches/mousemove-same-point-claimed-os.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

PAGE = """<!doctype html><html><body style="margin:0;width:1200px;height:800px"><script>
  const seen = [];
  for (const t of ['pointermove', 'mousemove', 'pointerdown', 'mousedown'])
    addEventListener(t, (e) => { seen.push([t, e.movementX, e.movementY]); document.body.dataset.seen = JSON.stringify(seen); }, true);
  addEventListener('keydown', () => { seen.length = 0; document.body.dataset.seen = '[]'; }, true);
</script></body></html>"""


def probe(binary, claimed):
    from camoufox.sync_api import Camoufox
    from Xlib import X, display
    from Xlib.ext import xtest

    with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1280, 900),
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        # Where the page's (300, 200) is on the X screen.
        x, y = page.evaluate("[(mozInnerScreenX + 300) * devicePixelRatio, (mozInnerScreenY + 200) * devicePixelRatio]")
        d = display.Display()

        def os_input(kind, **kw):
            xtest.fake_input(d, kind, **kw)
            d.sync()
            time.sleep(0.25)

        os_input(X.MotionNotify, x=round(x) - 40, y=round(y) - 30)
        page.keyboard.press("Shift")  # the page starts counting afresh
        os_input(X.MotionNotify, x=round(x), y=round(y))
        os_input(X.MotionNotify, x=round(x), y=round(y))
        os_input(X.ButtonPress, detail=1)
        os_input(X.ButtonRelease, detail=1)
        page.wait_for_timeout(300)
        return json.loads(page.evaluate("document.body.dataset.seen || '[]'"))


def main() -> int:
    binary = resolve_binary()
    failures = []
    with hidden_display("1920x1080"):
        for claimed, want in (("windows", 1), ("linux", 2)):
            seen = probe(binary, claimed)
            moves = [e for e in seen if e[0] == "mousemove"]
            print(f"  {claimed:8} {len(moves)} mousemove(s): {seen}")
            if len(moves) != want:
                failures.append(f"claiming {claimed}: {len(moves)} mousemoves for two moves to one point, stock has {want}")
            if [e[0] for e in seen if e[0] in ("pointermove", "mousemove")].count("pointermove") != len(moves):
                failures.append(f"claiming {claimed}: pointermoves and mousemoves differ in number")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: a move to the same point is dropped as Windows drops it, and kept on Linux")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
