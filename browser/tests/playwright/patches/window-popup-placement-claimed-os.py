r"""
A pop-up from a Linux build claiming Windows lands and sizes as Windows places it.

Stock Firefox 158 on win-i9 (dpr 2.5; screen 1382x864 CSS px, work area 816
tall above the taskbar), window.open from a window at 0,0:

  'popup'                  1280x816 at 22,0   -- 22 CSS px right of the window
                                                 it would cover (StaggerPosition),
                                                 clamped to the work area
  'width=10,height=5000'   130x816  at 22,0   -- no narrower than SM_CXMINTRACK
                                                 (325 device px at 250 %), no
                                                 taller than the work area

The Linux build put the first at 0,0 (Camoufox leaves an independent window to
the window manager: window-position-wm-placement.patch), made the second 113
wide, and sized both to the X screen's 864 because the widget layer kept the
host's work area while the page's screen.availHeight claimed 816.

    python browser/tests/playwright/patches/window-popup-placement-claimed-os.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

CONFIG = {"window.devicePixelRatio": 2.5, "window.outerWidth": 1280, "window.outerHeight": 816,
          "window.screenX": 0, "window.screenY": 0,
          "screen.width": 1382, "screen.height": 864, "screen.availWidth": 1382, "screen.availHeight": 816,
          "screen.availLeft": 0, "screen.availTop": 0}
# The page opens and measures the pop-up from a click, as a page has to (the
# blocker stops one without a gesture): Playwright's evaluate runs in an
# isolated world, which a new window's WindowProxy is cross-origin to. The
# features go in and the geometry comes out through the DOM.
PAGE = """<!doctype html><style>html,body,button{margin:0;width:100%;height:100%}</style>
<body><button>open</button><script>
  document.querySelector('button').onclick = async () => {
    const w = open('', '', document.body.dataset.features);
    if (!w) { document.body.dataset.geometry = 'null'; return; }
    await new Promise((r) => setTimeout(r, 1500));
    document.body.dataset.geometry = JSON.stringify([w.outerWidth, w.outerHeight, w.screenX, w.screenY]);
    w.close();
  };
</script></body>"""
ARM = "(features) => { delete document.body.dataset.geometry; document.body.dataset.features = features; }"


def probe(binary, claimed):
    from camoufox.sync_api import Camoufox

    with Camoufox(executable_path=str(binary), headless=False, os=claimed, config=CONFIG,
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        page.wait_for_timeout(6000)  # past the launcher's startup size hold
        opener = page.evaluate("[screenX, screenY]")
        got = {}
        for features in ("popup", "width=10,height=5000"):
            page.evaluate(ARM, features)
            page.click("button")
            page.wait_for_function("document.body.dataset.geometry !== undefined", timeout=10000)
            got[features] = json.loads(page.evaluate("document.body.dataset.geometry"))
            page.wait_for_timeout(500)
        return opener, got


def main() -> int:
    binary = resolve_binary()
    failures = []
    # The identity's screen at its devicePixelRatio: 1382x864 CSS px.
    with hidden_display("3455x2160"):
        for claimed in ("windows", "linux"):
            (ox, oy), got = probe(binary, claimed)
            print(f"  {claimed:8} opener {ox},{oy}  {got}")
            if None in got.values():
                failures.append(f"claiming {claimed}: a pop-up was blocked")
                continue
            pw, ph, px, py = got["popup"]
            tw, th, tx, ty = got["width=10,height=5000"]
            if claimed == "windows":
                if (px, py) != (ox + 22, 0):
                    failures.append(f"claiming windows: 'popup' at {px},{py}, stock staggers it to {ox + 22},0")
                if (tw, th) != (130, 816):
                    failures.append(f"claiming windows: a 10x5000 pop-up is {tw}x{th}, stock's is 130x816 "
                                    "(minimum track width, work area)")
                if ph != 816:
                    failures.append(f"claiming windows: 'popup' is {ph} tall, stock clamps it to the 816 work area")
            elif tw == 130 or th == 816:
                failures.append(f"claiming linux: a 10x5000 pop-up is {tw}x{th}, Windows's minimum width "
                                "or work area reached a Linux identity")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: a pop-up claiming Windows is staggered, clamped to the work area and kept to Windows's minimum width")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
