"""
A Windows identity enters the window with its first mouse move, as Windows does.

Windows Firefox 158.0.1 synthesizes the mouse's entry into a window from the
first WM_MOUSEMOVE it dispatches, and drops a move to the point the last one
went to (widget/windows/nsWindow.cpp, LastMouseMoveData). A pointer that leaves
the window and comes back to the point it left from produces no move there, so
no enter: the page sees nothing until the pointer moves on. GTK dispatches the
enter from the X server's EnterNotify wherever it lands. cfparity's
input.mouse.leave on win-i9 (all three runs): back at the point it left from,
stock fired nothing, then pointerover/mouseover at the next point's element;
Camoufox on Linux claiming Windows fired pointerover/mouseover on <html> at
the return point first.

The guard moves the pointer with XTest to a point near the window's right
edge, out of the window, back to that point, then onto an element, and finds
the first mouseover after leaving. Claiming Windows: the element. Claiming
Linux: whatever is at the return point, at once.

    python browser/tests/playwright/patches/mousemove-same-point-enter-claimed-os.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

# Every event the page sees, as [type, target id or tag, details...].
LOGGER = """<script>
  const seen = [];
  const who = (t) => (t && (t.id || t.nodeName)) || String(t);
  for (const t of TYPES)
    addEventListener(t, (e) => {
      const o = [e.type, who(e.target)];
      if (e instanceof WheelEvent) o.push(e.deltaMode, e.deltaX, e.deltaY);
      else if (e instanceof KeyboardEvent) o.push(e.key);
      else if (e instanceof MouseEvent) o.push(e.clientX, e.clientY, e.buttons);
      seen.push(o);
      document.documentElement.dataset.seen = JSON.stringify(seen);
    }, true);
  document.addEventListener('guard-reset', () => { seen.length = 0; document.documentElement.dataset.seen = '[]'; });
</script>"""


class OSInput:
    """Native input through XTest, at page CSS coordinates."""

    def __init__(self, page):
        from Xlib import display
        self.d = display.Display()
        self.o = page.evaluate("({x: mozInnerScreenX, y: mozInnerScreenY, dpr: devicePixelRatio,"
                               " iw: innerWidth, ow: outerWidth})")

    def fake(self, kind, wait=0.15, **kw):
        from Xlib.ext import xtest
        xtest.fake_input(self.d, kind, **kw)
        self.d.sync()
        time.sleep(wait)

    def move(self, x, y, wait=0.15):
        from Xlib import X
        o = self.o
        self.fake(X.MotionNotify, wait, x=round((o["x"] + x) * o["dpr"]), y=round((o["y"] + y) * o["dpr"]))

    def button(self, b, down, wait=0.15):
        from Xlib import X
        self.fake(X.ButtonPress if down else X.ButtonRelease, wait, detail=b)

    def key(self, name, down=None, wait=0.15):
        from Xlib import X, XK
        code = self.d.keysym_to_keycode(XK.string_to_keysym(name))
        if down in (None, True):
            self.fake(X.KeyPress, wait, detail=code)
        if down in (None, False):
            self.fake(X.KeyRelease, wait, detail=code)


def launch(binary, claimed, page_html, scenario):
    from camoufox.sync_api import Camoufox
    with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1280, 900),
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(page_html)
        page.wait_for_timeout(300)
        inp = OSInput(page)
        # Focus the page with a click on an empty spot.
        inp.move(1100, 700)
        inp.button(1, True)
        inp.button(1, False)
        page.evaluate("document.dispatchEvent(new Event('guard-reset'))")
        result = scenario(page, inp)
        page.wait_for_timeout(400)
        return json.loads(page.evaluate("document.documentElement.dataset.seen || '[]'")), result, inp.o


def run(checks, passed):
    binary = resolve_binary()
    failures = []
    with hidden_display("1920x1080"):
        for claimed in ("windows", "linux"):
            failures += checks(binary, claimed)
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    if passed:
        print(f"PASS: {passed}")
    return 0

PAGE = """<!doctype html><html><body style="margin:0;width:100%;height:800px">
<div id="t" style="position:absolute;left:0;top:0;width:640px;height:420px;background:#cde"></div>
<script>const TYPES = ['mouseover', 'mouseout', 'mousemove'];</script>""" + LOGGER + "</body></html>"


def out_and_back(page, inp):
    edge = inp.o["iw"] - 10
    inp.move(300, 200)
    inp.move(edge, 200)
    inp.move(edge + 60, 200, 0.4)
    inp.move(edge, 200, 0.4)
    inp.move(300, 200)


def main() -> int:

    def checks(binary, claimed):
        seen, _, o = launch(binary, claimed, PAGE, out_and_back)
        # After the pointer left the window: the mouseout outside it.
        outs = [i for i, e in enumerate(seen) if e[0] == "mouseout" and e[2] >= o["iw"]]
        after = seen[outs[0] + 1:] if outs else []
        over = next((e[1] for e in after if e[0] == "mouseover"), None)
        print(f"  {claimed:8} first mouseover back in the window: {over}: {seen}")
        if claimed == "windows" and over != "t":
            return [f"claiming windows: back at the point it left from, the pointer entered {over}, Windows Firefox enters t with the next move"]
        if claimed == "linux" and over in (None, "t"):
            return [f"claiming linux: back at the point it left from, the pointer entered {over}, stock Linux enters the element there"]
        return []

    return run(checks, "back at the point it left from, the pointer enters with its next move claiming Windows and at once claiming Linux")


if __name__ == "__main__":
    raise SystemExit(main())
