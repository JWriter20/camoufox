"""
A Windows identity stamps input at the Windows timer tick, as Windows does.

Windows Firefox 158.0.1 takes an input event's time from GetMessageTime, which
advances with the system timer (64 Hz, 15.625 ms): events arriving within one
tick share a time. Measured on win-i9 (it1 win-measure): 120 SendInput moves
8 ms apart gave pointerrawupdate timeStamp gaps of 0 (55) and 14-17 ms (64)
only, which cfparity's input.mouse.rate / driven.rate hz125 read as gapFrames
1; X stamps each event to the millisecond (gaps of 8, gapFrames 0.5).
widget/gtk/nsWindow.cpp GetEventTimeStamp quantizes under a Windows claim.

The guard moves the pointer 60 times 8 ms apart with XTest and reads the
pointerrawupdate timeStamps. Claiming Windows: no gap between 2 and 13 ms.
Claiming Linux: most gaps are.

    python browser/tests/playwright/patches/widget-event-time-claimed-os.py
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
<script>const TYPES = [];
const times = [];
addEventListener('pointerrawupdate', (e) => { times.push(e.timeStamp); document.documentElement.dataset.times = JSON.stringify(times); }, true);
document.addEventListener('guard-reset', () => { times.length = 0; document.documentElement.dataset.times = '[]'; });
</script>""" + LOGGER + "</body></html>"


def serve(html):
    """pointerrawupdate is for secure contexts only: the page comes from http://127.0.0.1."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def glide(page, inp):
    for i in range(60):
        inp.move(200 + 5 * i, 300, 0.008)
    page.wait_for_timeout(300)
    return json.loads(page.evaluate("document.documentElement.dataset.times || '[]'"))


def main() -> int:
    server = serve(PAGE)
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    def checks(binary, claimed):
        from camoufox.sync_api import Camoufox
        with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1280, 900),
                      i_know_what_im_doing=True) as browser:
            page = browser.new_page()
            page.goto(url)
            page.wait_for_timeout(300)
            inp = OSInput(page)
            inp.move(1100, 700)
            page.evaluate("document.dispatchEvent(new Event('guard-reset'))")
            times = glide(page, inp)
        gaps = [round(b - a, 1) for a, b in zip(times, times[1:])]
        sub_tick = [g for g in gaps if 2 <= g <= 13]
        print(f"  {claimed:8} {len(times)} pointerrawupdates, gaps {sorted(set(gaps))}")
        if len(times) < 30:
            return [f"claiming {claimed}: {len(times)} pointerrawupdate events for 60 moves"]
        if claimed == "windows" and sub_tick:
            return [f"claiming windows: {len(sub_tick)} gaps under a tick ({sub_tick[:8]}), Windows stamps 0 or 15-17 ms"]
        if claimed == "linux" and len(sub_tick) < len(gaps) / 2:
            return [f"claiming linux: only {len(sub_tick)} of {len(gaps)} gaps are the 8 ms the moves were apart"]
        return []

    return run(checks, "input is stamped at the Windows tick claiming Windows and to the millisecond claiming Linux")


if __name__ == "__main__":
    raise SystemExit(main())
