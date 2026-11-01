"""
A drag that begins after the pointer stopped finds no target, as stock Windows has it.

GTK's X11 drag source looks for a target only on a motion it gets through its
grab, so a drag that begins after the last motion ends with no dragenter,
dragover or drop. Stock Firefox 158.0.1 on win-i9 does the same: a selected
word dragged onto a textarea with the last move sent before the OLE drag loop
starts gives dragstart, dragend and no drop, at 0, 150 and 300 ms of dragstart
work (3/3 each, it6 batch M, LEAKS 266). A fix that synthesized a motion when
the drag began (windows-drag-begin-target.patch, it6) made Camoufox drop where
stock does not, and was dropped; this guard keeps it out.

The guard drags a selected word with XTest onto a textarea and stops there,
with a page dragstart handler that keeps the content process busy for 300 ms,
then releases. Claiming Windows or Linux: no drop.

    python browser/tests/playwright/patches/windows-drag-begin-target.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

PAGE = """<!doctype html><html><body style="margin:0">
<p id="p" style="position:absolute;left:40px;top:40px;margin:0;white-space:pre;font:16px/20px monospace">drag these words</p>
<textarea id="ta" style="position:absolute;left:40px;top:120px;width:300px;height:80px"></textarea>
<script>
document.body.dataset.ev = '[]';
for (const t of ['dragstart', 'dragenter', 'drop', 'dragend'])
  addEventListener(t, (e) => {
    const ev = JSON.parse(document.body.dataset.ev);
    ev.push(t);
    document.body.dataset.ev = JSON.stringify(ev);
    if (t === 'dragstart') { const end = performance.now() + 300; while (performance.now() < end); }
  }, true);
</script></body></html>"""


def probe(binary, claimed):
    from camoufox.sync_api import Camoufox
    from Xlib import X, display
    from Xlib.ext import xtest

    with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1280, 900),
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        ox, oy, dpr = page.evaluate("[mozInnerScreenX, mozInnerScreenY, devicePixelRatio]")
        word = page.evaluate("""(() => { const n = document.getElementById('p').firstChild; const r = document.createRange();
            r.setStart(n, 7); r.setEnd(n, 8); const b = r.getBoundingClientRect(); return [b.x + b.width / 2, b.y + b.height / 2]; })()""")
        ta = page.evaluate("(() => { const b = document.getElementById('ta').getBoundingClientRect(); "
                           "return [b.x + b.width / 2, b.y + b.height / 2]; })()")
        d = display.Display()

        def move(p):
            xtest.fake_input(d, X.MotionNotify, x=round((ox + p[0]) * dpr), y=round((oy + p[1]) * dpr))
            d.sync()
            time.sleep(0.05)

        def button(down):
            xtest.fake_input(d, X.ButtonPress if down else X.ButtonRelease, 1)
            d.sync()

        move(word)
        for _ in range(2):  # select the word
            button(True); time.sleep(0.03); button(False); time.sleep(0.08)
        time.sleep(0.8)  # past the double-click time
        page.evaluate("document.body.dataset.ev = '[]'")
        button(True); time.sleep(0.05)
        move((word[0] + 10, word[1] + 10))  # starts the drag; the page holds dragstart for 300 ms
        move(ta)  # the last motion, before the drag has begun
        time.sleep(0.8)
        button(False)
        time.sleep(1.0)
        return json.loads(page.evaluate("document.body.dataset.ev")), page.evaluate("document.getElementById('ta').value")


def main() -> int:
    binary = resolve_binary()
    failures = []
    with hidden_display("1920x1080"):
        for claimed, dropped in (("windows", False), ("linux", False)):
            events, value = probe(binary, claimed)
            print(f"  {claimed:8} {events} textarea={value!r}")
            got = "drop" in events and value == "these "
            if got != dropped:
                failures.append(f"claiming {claimed}: {'dropped' if got else 'no drop'} for a drag that began after "
                                f"the pointer stopped; stock {'drops' if dropped else 'does not drop'}")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: a drag that begins after the pointer stopped finds no target, claiming Windows or Linux, as stock does")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
