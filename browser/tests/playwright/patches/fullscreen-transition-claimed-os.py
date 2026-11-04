"""
A Windows identity takes Windows' fullscreen fade: fullscreenchange comes ~200 ms after the request.

Firefox on Windows and macOS fades the screen to black before it toggles
fullscreen and back after (full-screen-api.transition-duration "200 200",
modules/libpref/init/all.js); GTK builds default to "0 0". The launcher sets
the Windows durations for a Windows identity, but the GTK widget only ran a
transition on a composited screen (widget/gtk/nsWindow.cpp,
PrepareForFullscreenTransition), so under Xvfb or any X server without a
compositor the pref did nothing and fullscreenchange came ~20 ms after the
click. On win-i9, stock 158.0.1 took 229-240 ms to enter and ~285 ms to leave
(fsblur, 11-02); a page can time requestFullscreen() and exitFullscreen().
In cfparity this showed as automation.end's trailing "blur:button": the
fullscreen check's button lost focus at fullscreen entry before the harness
removed it, where on Windows the removal came first.

The guard clicks a button (XTest) whose handler calls requestFullscreen() on
another element and times the fullscreenchange, then times exitFullscreen().
Claiming Windows: both at least 150 ms. Claiming Linux: both under 100 ms.

    python browser/tests/playwright/patches/fullscreen-transition-claimed-os.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

PAGE = """<!doctype html><html><body style="margin:0;width:1200px;height:800px">
<button id="b" style="position:absolute;left:40px;top:40px;width:160px;height:40px">go</button>
<div id="d" style="position:absolute;left:40px;top:140px;width:200px;height:100px;background:#ccc"></div>
<script>
  // The page's own timings, in a DOM attribute: page.evaluate runs in an isolated world.
  const t = {};
  const save = () => { document.documentElement.dataset.t = JSON.stringify(t); };
  document.addEventListener('fullscreenchange', () => {
    const now = performance.now();
    if (document.fullscreenElement) t.enter = now - t.asked;
    else t.leave = now - t.exitAsked;
    save();
  });
  document.addEventListener('fullscreenerror', () => { t.error = true; save(); });
  b.addEventListener('click', () => { t.asked = performance.now(); d.requestFullscreen(); });
  document.addEventListener('guard-exit', () => { t.exitAsked = performance.now(); document.exitFullscreen(); });
</script></body></html>"""


def xtest_click(page, x, y):
    from Xlib import X, display
    from Xlib.ext import xtest
    d = display.Display()
    o = page.evaluate("({x: mozInnerScreenX, y: mozInnerScreenY, dpr: devicePixelRatio})")
    for kind, kw in ((X.MotionNotify, dict(x=round((o["x"] + x) * o["dpr"]), y=round((o["y"] + y) * o["dpr"]))),
                     (X.ButtonPress, dict(detail=1)), (X.ButtonRelease, dict(detail=1))):
        xtest.fake_input(d, kind, **kw)
        d.sync()
        time.sleep(0.1)
    d.close()


def measure(binary, claimed):
    from camoufox.sync_api import Camoufox
    with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1280, 900),
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        page.wait_for_timeout(300)
        xtest_click(page, 120, 60)
        read = "JSON.parse(document.documentElement.dataset.t || '{}')"
        page.wait_for_function(f"(({read}).enter !== undefined) || ({read}).error", timeout=5000)
        page.wait_for_timeout(600)  # the fade back in
        # exitFullscreen() from the page's own world, so its clock times it.
        page.evaluate("document.dispatchEvent(new Event('guard-exit'))")
        page.wait_for_function(f"({read}).leave !== undefined", timeout=5000)
        return page.evaluate(read)


def main() -> int:
    binary = resolve_binary()
    failures = []
    with hidden_display("1920x1080"):
        for claimed, ok, want in (("windows", lambda ms: ms >= 150, ">= 150 ms (Windows' 200 ms fade)"),
                                  ("linux", lambda ms: ms < 100, "< 100 ms (GTK: no fade)")):
            t = measure(binary, claimed)
            print(f"  {claimed:8} enter {t.get('enter', 0):.0f} ms  leave {t.get('leave', 0):.0f} ms"
                  f"{'  fullscreenerror' if t.get('error') else ''}")
            if t.get("error"):
                failures.append(f"claiming {claimed}: requestFullscreen() after a real click failed")
                continue
            for what in ("enter", "leave"):
                if not ok(t[what]):
                    failures.append(f"claiming {claimed}: fullscreen {what} took {t[what]:.0f} ms, stock takes {want}")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: fullscreenchange keeps the claimed OS's fade timing (Windows ~200 ms, Linux none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
