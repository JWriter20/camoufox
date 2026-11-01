"""
A Windows identity reports a drag's end where Windows Firefox does: offset by the client area's screen origin.

Stock Firefox 158.0.1 on Windows reports the dragend of a drag dropped in its
own window at the drop point plus the window's client-area origin on the
screen: the point arrives in screen pixels where client pixels are expected
(widget/windows/nsDragService.cpp, GetMessagePos). Measured on win-i9 (dpr
2.5): dropped at clientX 320, dragend 326 with mozInnerScreenX 6.4 (the
cross-OS gate, every run of input.drag.element, input.drag.text, driven.drag)
and 328 with mozInnerScreenX 8 (it1 win-measure), 320 maximized (client area
at the screen's origin); those windows sat at the screen's top, so no vertical
offset. GTK reports the drop point (widget/gtk/nsDragServiceGtk.cpp,
ScheduleDropEvent).

The guard drags an element onto a target with XTest and compares dragend's
client point with drop's. Claiming Windows: (mozInnerScreenX, the client
area's top on the screen) further on. Claiming Linux: the same point.

    python browser/tests/playwright/patches/window-frame-dragend-claimed-os.py
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
                               " iw: innerWidth, ow: outerWidth, sy: screenY})")

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
<div id="src" draggable="true" style="position:absolute;left:40px;top:300px;width:80px;height:40px;background:#edc">drag</div>
<div id="dst" style="position:absolute;left:240px;top:300px;width:160px;height:100px;background:#dec"></div>
<script>const TYPES = ['drop', 'dragend'];
const dst = document.getElementById('dst');
dst.addEventListener('dragover', (e) => e.preventDefault());
dst.addEventListener('drop', (e) => e.preventDefault());</script>""" + LOGGER + "</body></html>"


def drag(page, inp):
    inp.move(80, 320)
    inp.button(1, True)
    for x, y in ((90, 325), (110, 330), (300, 350), (320, 350)):
        inp.move(x, y, 0.2)
    inp.button(1, False, 0.6)
    inp.move(700, 600, 0.4)


def main() -> int:
    def checks(binary, claimed):
        seen, _, o = launch(binary, claimed, PAGE, drag)
        drop = next((e for e in seen if e[0] == "drop"), None)
        end = next((e for e in seen if e[0] == "dragend"), None)
        if not drop or not end:
            return [f"claiming {claimed}: no drop/dragend: {seen}"]
        # The client area's screen origin: across, mozInnerScreenX (no chrome beside the page);
        # down, the window's top (a window that draws in its titlebar has no frame above it).
        want = (o["x"], o["sy"]) if claimed == "windows" else (0, 0)
        got = (end[2] - drop[2], end[3] - drop[3])
        print(f"  {claimed:8} drop {drop[2:4]} dragend {end[2:4]} offset {got}, want {want}")
        if abs(got[0] - want[0]) > 1 or abs(got[1] - want[1]) > 1:
            return [f"claiming {claimed}: dragend {got} from the drop, stock is {want}"]
        return []

    return run(checks, "a drag ends offset by the client area's screen origin claiming Windows and at the drop claiming Linux")


if __name__ == "__main__":
    raise SystemExit(main())
