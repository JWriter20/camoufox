"""
A Windows identity opens the context menu when the Menu key is released, as Windows does.

GTK dispatches the contextmenu event for the Menu (Application) key on its
key press (widget/gtk/nsGtkKeyUtils.cpp, MaybeDispatchContextMenuEvent from
HandleKeyPressEvent); Windows sends WM_CONTEXTMENU when VK_APPS is released,
so Windows Firefox 158.0.1 dispatches keydown, keyup, then contextmenu.
cfparity's input.window.menukey on win-i9: stock keydown@t, keyup@t (key
ContextMenu), contextmenu@t; Camoufox on Linux claiming Windows keydown@t,
contextmenu@t and no ContextMenu keyup (the menu took it).

The guard focuses an element, presses and releases Menu with XTest, and reads
the order. Claiming Windows: keydown, keyup, contextmenu. Claiming Linux:
keydown, contextmenu, as stock Linux Firefox has it.

    python browser/tests/playwright/patches/context-menu-key-claimed-os.py
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
<div id="t" tabindex="0" style="position:absolute;left:40px;top:40px;width:200px;height:100px;background:#cde"></div>
<script>const TYPES = ['keydown', 'keyup', 'contextmenu'];
addEventListener('contextmenu', (e) => e.preventDefault());</script>""" + LOGGER + "</body></html>"


def menu_key(page, inp):
    page.evaluate("document.getElementById('t').focus()")
    page.wait_for_timeout(200)
    inp.key("Menu", True, 0.3)
    inp.key("Menu", False, 0.3)


def main() -> int:
    want = {"windows": ["keydown", "keyup", "contextmenu"], "linux": ["keydown", "contextmenu"]}

    def checks(binary, claimed):
        seen, _, _ = launch(binary, claimed, PAGE, menu_key)
        order = [e[0] for e in seen if e[0] == "contextmenu" or (len(e) > 2 and e[2] == "ContextMenu")]
        print(f"  {claimed:8} {order}: {seen}")
        if order[:len(want[claimed])] != want[claimed]:
            return [f"claiming {claimed}: the Menu key gives {order}, stock has {want[claimed]}"]
        return []

    return run(checks, "the Menu key's contextmenu follows its keyup claiming Windows and its keydown claiming Linux")


if __name__ == "__main__":
    raise SystemExit(main())
