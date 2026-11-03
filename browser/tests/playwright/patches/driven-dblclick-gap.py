"""
A driver's double click leaves the button up between its clicks as long as a person does.

Playwright's dblclick() sends both clicks back to back, so the page saw the
second press under 10 ms after the first release (cfparity driven.dblclick,
every Playwright variant; stock 158.0.1 Linux by OS input: tens of
milliseconds). Juggler now holds the second press a person's gap after the
release (PageHandler.js clickGap, 70-180 ms), with clickHold() before it, so
press to press stays inside the OS double-click time and dblclick still fires.

    python browser/tests/playwright/patches/driven-dblclick-gap.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

PAGE = '<!doctype html><html><body><button id="b" style="width:120px;height:40px">b</button></body></html>'
RECORD = """() => {
  window.__ev = [];
  const b = document.getElementById('b');
  if (b.__rec) return;
  b.__rec = true;
  for (const t of ['mousedown', 'mouseup', 'dblclick']) b.addEventListener(t, (e) => window.__ev.push([t, e.timeStamp]));
}"""


def gaps(events):
    downs = [ts for t, ts in events if t == "mousedown"]
    ups = [ts for t, ts in events if t == "mouseup"]
    dbl = sum(1 for t, _ in events if t == "dblclick")
    if len(downs) != 2 or len(ups) != 2:
        return None
    return {"upDown": round(downs[1] - ups[0], 1), "pressToPress": round(downs[1] - downs[0], 1), "dblclick": dbl}


def probe(binary):
    from camoufox.sync_api import Camoufox

    results = {}
    with Camoufox(executable_path=str(binary), headless=True, os="linux", i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        box = page.locator("#b").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        for name, act in (("locator.dblclick", lambda: page.locator("#b").dblclick()),
                          ("mouse.dblclick", lambda: page.mouse.dblclick(cx, cy)),
                          ("locator.dblclick again", lambda: page.locator("#b").dblclick())):
            page.evaluate(RECORD)
            act()
            page.wait_for_timeout(600)
            results[name] = gaps(page.evaluate("window.__ev"))
    return results


def main() -> int:
    results = probe(resolve_binary())
    failures = []
    for name, g in results.items():
        if not g:
            failures.append(f"{name}: not two presses and two releases")
        elif not 60 <= g["upDown"] <= 200:
            failures.append(f"{name}: second press {g['upDown']} ms after the first release, want 60-200")
        elif g["pressToPress"] > 450 or g["dblclick"] != 1:
            failures.append(f"{name}: presses {g['pressToPress']} ms apart, {g['dblclick']} dblclick, want <450 and 1")
    for f in failures:
        print(f"FAIL: {f}")
    if failures:
        return 1
    print(f"PASS: double clicks pause between clicks as a person's do: {results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
