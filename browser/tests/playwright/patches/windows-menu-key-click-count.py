"""
A Windows identity's Menu-key contextmenu carries the mouse's last click count, as Windows does.

Windows Firefox gives every mouse event one static click count
(widget/windows/nsWindow.cpp, nsWindow::DispatchMouseEvent), and the
contextmenu it dispatches for WM_CONTEXTMENU, the Menu key's included, reports
it as `detail`: 0 before any click, 1 after a click, 2 after a double click,
3 after a triple click, and 0 again once the pointer has moved away from the
last release. Measured on win-i9, stock Firefox 158.0.1, three runs each
(SendInput; cfparity it4 menukey). GTK always says 1
(widget/gtk/nsGtkKeyUtils.cpp, MaybeDispatchContextMenuEvent).

The guard clicks with XTest, presses Menu on a focused element and reads the
contextmenu's detail. Claiming Windows: 0, 1, 2, 3, 0. Claiming Linux: 1
every time, as stock Linux Firefox has it.

    python browser/tests/playwright/patches/windows-menu-key-click-count.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

PAGE = """<!doctype html><html><body style="margin:0;width:1200px;height:800px">
<div id="t" tabindex="0" style="position:absolute;left:40px;top:40px;width:200px;height:100px;background:#cde"></div>
<script>
  const seen = [];
  addEventListener('contextmenu', (e) => {
    e.preventDefault();
    seen.push(e.detail);
    document.documentElement.dataset.seen = JSON.stringify(seen);
  }, true);
</script></body></html>"""

# win-i9, stock 158.0.1: the Menu key's contextmenu.detail after each history
WINDOWS = {"fresh": 0, "click": 1, "double": 2, "triple": 3, "click-move": 0}
LINUX = {k: 1 for k in WINDOWS}


class OSInput:
    """Native input through XTest, at page CSS coordinates."""

    def __init__(self, page):
        from Xlib import display
        self.d = display.Display()
        self.o = page.evaluate("({x: mozInnerScreenX, y: mozInnerScreenY, dpr: devicePixelRatio})")

    def fake(self, kind, wait=0.08, **kw):
        from Xlib.ext import xtest
        xtest.fake_input(self.d, kind, **kw)
        self.d.sync()
        time.sleep(wait)

    def move(self, x, y):
        from Xlib import X
        o = self.o
        self.fake(X.MotionNotify, x=round((o["x"] + x) * o["dpr"]), y=round((o["y"] + y) * o["dpr"]))

    def clicks(self, n):
        from Xlib import X
        for _ in range(n):
            self.fake(X.ButtonPress, detail=1)
            self.fake(X.ButtonRelease, detail=1)

    def key(self, name):
        from Xlib import X, XK
        code = self.d.keysym_to_keycode(XK.string_to_keysym(name))
        self.fake(X.KeyPress, 0.2, detail=code)
        self.fake(X.KeyRelease, 0.4, detail=code)


def histories(page, inp):
    """Each history, then the Menu key on a focused #t; the detail of its contextmenu."""
    def prep(name):
        if name == "fresh":
            inp.move(140, 90)  # a move, no press: the pointer is over the window for the keys
        elif name == "click-move":
            inp.move(140, 90)
            inp.clicks(1)
            inp.move(440, 390)
            inp.move(140, 600)
        else:
            inp.move(140, 90)
            inp.clicks({"click": 1, "double": 2, "triple": 3}[name])

    # The pointer over the window and a Shift tap first: without a WM the X focus follows the
    # pointer, and the first key of a new window can arrive before it is active.
    inp.move(600, 600)
    page.evaluate("document.getElementById('t').focus()")
    inp.key("Shift_L")
    out = {}
    for name in WINDOWS:
        time.sleep(0.7)  # past the double-click time: each history starts its own count
        prep(name)
        time.sleep(0.6)
        page.evaluate("document.getElementById('t').focus()")
        before = len(json.loads(page.evaluate("document.documentElement.dataset.seen || '[]'")))
        inp.key("Menu")
        inp.key("Escape")
        seen = json.loads(page.evaluate("document.documentElement.dataset.seen || '[]'"))
        out[name] = seen[before] if len(seen) > before else None
    return out


def probe(binary, claimed):
    from camoufox.sync_api import Camoufox
    with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1280, 900),
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        page.wait_for_timeout(500)
        return histories(page, OSInput(page))


def main() -> int:
    binary = resolve_binary()
    failures = []
    for claimed, want in (("windows", WINDOWS), ("linux", LINUX)):
        # A display of its own each: the second browser's first key raced its activation.
        with hidden_display("1920x1080"):
            got = probe(binary, claimed)
        print(f"  {claimed:8} {got}")
        if got != want:
            failures.append(f"claiming {claimed}: the Menu key's contextmenu.detail is {got}, stock has {want}")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: the Menu key's contextmenu reports Windows' last click count claiming Windows, 1 claiming Linux")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
