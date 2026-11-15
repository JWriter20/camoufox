r"""
A headless window lives on the identity's screen.

Headless Firefox has one screen, 1366x768 unless MOZ_HEADLESS_WIDTH/HEIGHT say
otherwise. screen.width and the device-width media query are answered from the
config, so a page read the claimed screen from them, but the browser placed
and sized its windows on the 1366x768 one: a pop-up asked to be larger than the
screen came out 1366x768 whatever screen the identity claimed, where a window
on a desktop is fitted to the work area.

widget/headless (headless-claimed-screen.patch) gives the headless screen the
claimed screen.width/height and its work area screen.avail*.

    python browser/tests/playwright/patches/headless-claimed-screen.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

CONFIG = {"window.outerWidth": 1280, "window.outerHeight": 800, "window.screenX": 0, "window.screenY": 0,
          "screen.width": 1920, "screen.height": 1080, "screen.availWidth": 1920, "screen.availHeight": 1032,
          "screen.availLeft": 0, "screen.availTop": 0}
# Opened and measured from a click, as a page has to (the blocker stops a
# pop-up without a gesture).
PAGE = """<!doctype html><style>html,body,button{margin:0;width:100%;height:100%}</style>
<body><button>open</button><script>
  document.querySelector('button').onclick = async () => {
    const w = open('', '', 'width=5000,height=4000,left=0,top=0');
    if (!w) { document.body.dataset.geometry = 'blocked'; return; }
    await new Promise((r) => setTimeout(r, 1500));
    document.body.dataset.geometry = [w.outerWidth, w.outerHeight, w.screenX, w.screenY].join();
    w.close();
  };
</script></body>"""


def probe(binary, claimed):
    from camoufox.sync_api import Camoufox

    with Camoufox(executable_path=str(binary), headless=True, os=claimed, config=CONFIG,
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        page.wait_for_timeout(6000)  # past the launcher's startup size hold
        page.click("button")
        page.wait_for_function("document.body.dataset.geometry !== undefined", timeout=10000)
        return page.evaluate("document.body.dataset.geometry")


def main() -> int:
    binary = resolve_binary()
    failures = []
    for claimed in ("windows", "linux"):
        got = probe(binary, claimed)
        print(f"  {claimed:8} a 5000x4000 pop-up at 0,0: {got}")
        if got != "1920,1032,0,0":
            failures.append(f"claiming {claimed}: the pop-up is {got}, the claimed work area is 1920x1032 at 0,0")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: a headless pop-up larger than the screen is fitted to the claimed work area")
    return 0


if __name__ == "__main__":
    sys.exit(main())
