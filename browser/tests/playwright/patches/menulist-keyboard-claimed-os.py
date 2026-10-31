"""
A Windows identity's open <select> takes the option the arrow keys move to, as Windows does.

On Windows, Firefox 158.0.1 selects an open drop-down's option as the arrow
keys move to it (dom/xul/XULMenuParentElement.cpp, SetActiveMenuChild under
XP_WIN), so Escape closes the list on the new value, and the keys stop at the
first and last option (layout/xul/nsXULPopupManager.cpp, no wrap on Windows).
On Linux only Enter selects and Escape reverts. cfparity's input.forms.select
on win-i9: stock's Down then Escape left the value "four" with input and
change; Camoufox on Linux claiming Windows reverted to "three" with no events.

The guard opens a <select> with an XTest click, presses Down and Escape, and
reads the value. Claiming Windows: the next option, with input and change.
Claiming Linux: unchanged.

    python browser/tests/playwright/patches/menulist-keyboard-claimed-os.py
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

PAGE = """<!doctype html><html><body style="margin:0;width:1200px;height:800px">
<select id="s" style="position:absolute;left:40px;top:40px;width:160px">
<option>one</option><option>two</option><option selected>three</option><option>four</option><option>five</option></select>
<script>const TYPES = ['input', 'change'];</script>""" + LOGGER + "</body></html>"


def down_escape(page, inp):
    inp.move(120, 50)
    inp.button(1, True, 0.05)
    inp.button(1, False, 0.5)
    inp.key("Down", None, 0.2)
    inp.key("Escape", None, 0.4)
    return page.evaluate("document.getElementById('s').value")


def main() -> int:
    want = {"windows": ("four", ["input", "change"]), "linux": ("three", [])}

    def checks(binary, claimed):
        seen, value, _ = launch(binary, claimed, PAGE, down_escape)
        got = (value, [e[0] for e in seen])
        print(f"  {claimed:8} {got}")
        if got != want[claimed]:
            return [f"claiming {claimed}: Down then Escape in an open <select> gives {got}, stock has {want[claimed]}"]
        return []

    return run(checks, "an open <select> takes the arrow keys' option claiming Windows and reverts on Escape claiming Linux")


if __name__ == "__main__":
    raise SystemExit(main())
