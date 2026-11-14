"""
A Windows identity's contextmenu follows the button's release, and its scrollbars have arrow buttons, as on Windows.

Launcher prefs (windowsBehaviourPrefs in launcher-constants.json), set when a
Linux host claims Windows:
- ui.context_menus.after_mouseup: Windows sends WM_CONTEXTMENU when the right
  button is released, so Windows Firefox 158.0.1 dispatches contextmenu after
  mouseup, with buttons 0; GTK dispatches it on the press
  (widget/gtk/nsWindow.cpp, OnButtonPressEvent). cfparity's input.mouse.click
  on win-i9: stock pointerup, mouseup, auxclick, contextmenu (buttons 0);
  Camoufox on Linux claiming Windows contextmenu (buttons 2) before pointerup.
- ui.scrollArrowStyle 4097 (eScrollArrowStyle_Single): Windows' LookAndFeel
  (widget/windows/nsLookAndFeel.cpp), so scrollbars have an arrow button at
  each end; GTK's Adwaita has none. cfparity's input.drag.scrollbar on win-i9:
  a press 10 CSS px below the scrollbar's top hit the up arrow and dragging
  scrolled nothing (scrollTop 0); Camoufox on Linux claiming Windows grabbed
  the thumb and scrolled 588 px.

The guard right-clicks with XTest and drags from the top of a scrollbar.
Claiming Windows: contextmenu after mouseup and scrollTop 0. Claiming Linux:
contextmenu before mouseup.

    python browser/tests/playwright/patches/windows-behaviour-click-scrollbar.py
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
<div id="t" style="position:absolute;left:400px;top:40px;width:160px;height:100px;background:#cde"></div>
<div id="sb" style="position:absolute;left:600px;top:300px;width:300px;height:200px;overflow-y:scroll"><div style="height:2000px"></div></div>
<script>const TYPES = ['mousedown', 'mouseup', 'contextmenu'];
addEventListener('contextmenu', (e) => e.preventDefault());</script>""" + LOGGER + "</body></html>"


def click_and_drag(page, inp):
    inp.move(480, 90)
    inp.button(3, True, 0.05)
    inp.button(3, False, 0.4)
    inp.move(896, 310, 0.3)
    inp.button(1, True)
    inp.move(896, 340)
    inp.move(896, 370)
    inp.button(1, False)
    inp.move(700, 650, 0.6)
    return page.evaluate("document.getElementById('sb').scrollTop")


def main() -> int:
    def checks(binary, claimed):
        seen, scroll_top, _ = launch(binary, claimed, PAGE, click_and_drag)
        right = [e[0] for e in seen if e[0] == "contextmenu" or (e[0] in ("mousedown", "mouseup") and e[1] == "t")]
        menu = next((e for e in seen if e[0] == "contextmenu"), None)
        print(f"  {claimed:8} {right} contextmenu {menu} scrollTop {scroll_top}")
        failures = []
        want = ["mousedown", "mouseup", "contextmenu"] if claimed == "windows" else ["mousedown", "contextmenu", "mouseup"]
        if right != want:
            failures.append(f"claiming {claimed}: a right click gives {right}, stock has {want}")
        if claimed == "windows" and (not menu or menu[4] != 0):
            failures.append(f"claiming windows: contextmenu buttons {menu and menu[4]}, Windows Firefox has 0")
        if claimed == "windows" and scroll_top != 0:
            failures.append(f"claiming windows: a drag from the scrollbar's top scrolled {scroll_top}: no up arrow, Windows has one")
        return failures

    return run(checks, "contextmenu follows mouseup and scrollbars have arrows claiming Windows")


if __name__ == "__main__":
    raise SystemExit(main())
