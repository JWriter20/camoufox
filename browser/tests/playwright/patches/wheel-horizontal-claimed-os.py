"""
A Windows identity scrolls three lines for a notch of a horizontal wheel, as Windows does.

GTK turns a horizontal wheel notch (GDK_SCROLL_LEFT/RIGHT, X buttons 6 and 7)
into one line (widget/gtk/nsWindow.cpp, OnScrollEvent); Windows Firefox 158.0.1
gets WM_MOUSEHWHEEL and scrolls SPI_GETWHEELSCROLLCHARS, three by default.
cfparity's input.scroll.wheel on win-i9: stock's left/right notch was a wheel
event with deltaX 6 (deltaMode line) and scrolled 36 CSS px; Camoufox on Linux
claiming Windows gave deltaX 2 and scrolled 12.

The guard sends one left and one right notch with XTest over a scroller and
compares the wheel events' deltaX. Claiming Windows: three times what a Linux
identity gets, with the same sign.

    python browser/tests/playwright/patches/wheel-horizontal-claimed-os.py
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
        # Focus the page with a click on an empty spot, inside the viewport: the window follows the drawn
        # screen, and a short one (a 1280x720 screen gives a 712 px window) put a fixed y=700 below the
        # window, so the click and every later key reached the X root window and the page saw nothing.
        inp.move(*page.evaluate("[Math.min(1100, innerWidth - 40), Math.min(700, innerHeight - 40)]"))
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

PAGE = """<!doctype html><html><body style="margin:0;width:1200px;height:800px">
<div id="sc" style="position:absolute;left:40px;top:40px;width:300px;height:200px;overflow:auto">
<div style="width:4000px;height:300px"></div></div>
<script>const TYPES = ['wheel'];</script>""" + LOGGER + "</body></html>"


def notches(page, inp):
    inp.move(190, 140)
    for b in (6, 7):
        inp.button(b, True, 0.02)
        inp.button(b, False, 0.4)


def main() -> int:
    deltas = {}

    def checks(binary, claimed):
        seen, _, _ = launch(binary, claimed, PAGE, notches)
        deltas[claimed] = [e[3] for e in seen if e[0] == "wheel"]
        print(f"  {claimed:8} wheel deltaX: {deltas[claimed]}")
        if len(deltas[claimed]) != 2 or not (deltas[claimed][0] < 0 < deltas[claimed][1]):
            return [f"claiming {claimed}: want one left and one right wheel event, got {deltas[claimed]}"]
        return []

    rc = run(checks, "")
    if rc:
        return rc
    want = [3 * d for d in deltas["linux"]]
    if deltas["windows"] != want:
        print(f"FAIL: claiming Windows a horizontal notch gives deltaX {deltas['windows']}, Windows Firefox gives {want} (3 lines a notch)")
        return 1
    print("PASS: a horizontal notch scrolls three lines claiming Windows and one claiming Linux")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
