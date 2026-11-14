"""
A Windows identity's open <select> lets a click outside it reach the page, and keeps the arrow keys' option, as Windows does.

Windows Firefox 158.0.1 marks the drop-down's popup consumeoutsideclicks=false
(toolkit/actors/SelectParent.sys.mjs, AppConstants.platform == "win"): a click
outside the open list closes it and still reaches the page. Measured on win-i9
(it1 win-measure): open, Down, click outside: value "four" with input and
change, then mousedown/mouseup on the page where the click landed. On Linux the
list swallows the click. With menulist-keyboard-claimed-os.patch the Down's
option is kept.

The guard opens a <select> with an XTest click, presses Down, clicks an empty
spot, and reads the value and the page's mousedowns. Claiming Windows: "four",
and a mousedown on the body. Claiming Linux: no mousedown on the body.

    python browser/tests/playwright/patches/select-popup-claimed-os.py
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
<select id="s" style="position:absolute;left:40px;top:40px;width:160px">
<option>one</option><option>two</option><option selected>three</option><option>four</option><option>five</option></select>
<script>const TYPES = ['mousedown', 'input', 'change'];</script>""" + LOGGER + "</body></html>"


def down_click_outside(page, inp):
    inp.move(120, 50)
    inp.button(1, True, 0.05)
    inp.button(1, False, 0.5)
    inp.key("Down", None, 0.3)
    inp.move(900, 600)
    inp.button(1, True, 0.05)
    inp.button(1, False, 0.5)
    return page.evaluate("document.getElementById('s').value")


def main() -> int:
    def checks(binary, claimed):
        seen, value, _ = launch(binary, claimed, PAGE, down_click_outside)
        outside = [e for e in seen if e[0] == "mousedown" and e[1] in ("BODY", "HTML")]
        print(f"  {claimed:8} value {value}, outside mousedowns {len(outside)}: {seen}")
        if claimed == "windows" and (value != "four" or not outside):
            return [f"claiming windows: value {value!r}, {len(outside)} outside mousedowns; Windows Firefox keeps 'four' and passes the click on"]
        if claimed == "linux" and outside:
            return [f"claiming linux: the open list let the outside click through ({outside})"]
        return []

    return run(checks, "an open <select> passes an outside click on and keeps the arrow key's option claiming Windows")


if __name__ == "__main__":
    raise SystemExit(main())
