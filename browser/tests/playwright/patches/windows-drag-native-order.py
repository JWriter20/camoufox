"""
A native drag under a Windows identity reaches the page in Windows' order.

The OS-input half of windows-drag-deferred-move: that guard drives the drag
through Playwright (Juggler); this one through XTest, the way a person's mouse
reaches GTK and the way cfparity's cf-none variant drives driven.drag.

Stock Windows Firefox 158.0.1 (win-i9, SendInput, cfparity driven.drag 3/3):
the move queued behind the one that started the drag waits out DoDragDrop's
modal OLE loop and reaches the page after dragend, button still held, stamped
when it was made, its movement from the move before; then a move at the drop
point with the button released, its movement from the deferred point; the
next move counts from the drop point. Nothing leaves the window.

GTK (pkg-it5 claiming Windows): the move after dragstart reached the page at
once, the drag's first motion took the pointer with a grab (a mouseleave up to
the root with no relatedTarget, which also reset the movement baseline), and
after the drop no move came until the next real one, which reported movement
[0, 0]. The fix holds a drag's moves in the content process until dragend
(BrowserChild) and turns GTK's grab leave and ungrab enter into Windows' none
and its move at the drop point (nsWindow).

    python browser/tests/playwright/patches/windows-drag-native-order.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

PAGE = """<!doctype html><html><body style="margin:0;width:1200px;height:800px">
<div id="stage" style="position:fixed;left:0;top:0;width:640px;height:420px">
<div id="src" draggable="true" style="position:absolute;left:40px;top:180px;width:81px;height:41px;background:#edc">drag</div>
<div id="dst" style="position:absolute;left:240px;top:180px;width:161px;height:101px;background:#dec"></div></div>
<script>
document.body.dataset.ev = '[]';
const who = (t) => t ? (t.id || (t.nodeName || 'win').toLowerCase()) : null;
for (const t of ['dragstart', 'drag', 'dragenter', 'dragover', 'dragleave', 'drop', 'dragend', 'pointerdown',
  'pointermove', 'pointerup', 'pointercancel', 'mousedown', 'mousemove', 'mouseup', 'mouseleave'])
  addEventListener(t, (e) => {
    const ev = JSON.parse(document.body.dataset.ev);
    const last = ev[ev.length - 1];
    // drag and dragover repeat on a timer while the pointer rests; a run counts once.
    if ((t === 'drag' || t === 'dragover') && last && last[0] === t) return;
    ev.push([t, who(e.target), e.buttons, e.movementX, e.movementY, e.clientX, e.clientY, e.timeStamp,
      who(e.relatedTarget)]);
    document.body.dataset.ev = JSON.stringify(ev);
  }, true);
const accept = (e) => e.preventDefault();
document.getElementById('dst').addEventListener('dragover', accept);
document.getElementById('dst').addEventListener('drop', accept);
</script></body></html>"""

# Stock Windows: (type, target, buttons), drag/dragover runs counted once.
WINDOWS = [
    ("pointermove", "src", 0), ("mousemove", "src", 0), ("pointerdown", "src", 1), ("mousedown", "src", 1),
    ("dragstart", "src", 1), ("pointercancel", "src", 1), ("mousemove", "src", 1),
    ("drag", "src", 0), ("dragenter", "dst", 1), ("dragover", "dst", 1), ("drag", "src", 0), ("dragover", "dst", 1),
    ("drop", "dst", 0), ("dragend", "src", 0),
    ("pointermove", "src", 1), ("mousemove", "src", 1),
    ("pointermove", "dst", 0), ("mousemove", "dst", 0),
    ("pointermove", "stage", 0), ("mousemove", "stage", 0),
]


def probe(binary, claimed):
    """cfparity's driven.drag through XTest: src centre, held, three moves, the target, released, away."""
    from camoufox.sync_api import Camoufox
    from Xlib import X, display
    from Xlib.ext import xtest

    with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1280, 900),
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        ox, oy, dpr = page.evaluate("[mozInnerScreenX, mozInnerScreenY, devicePixelRatio]")
        d = display.Display()

        def os_input(kind, at=None, **kw):
            if at:
                kw.update(x=round((ox + at[0]) * dpr), y=round((oy + at[1]) * dpr))
            xtest.fake_input(d, kind, **kw)
            d.sync()

        os_input(X.MotionNotify, at=(620.5, 400.5))
        time.sleep(0.3)
        page.evaluate("document.body.dataset.ev = '[]'")
        src, dst = (80.5, 200.5), (320.5, 230.5)
        steps = [("move", src), ("down",), ("move", (src[0] + 10, src[1] + 5)), ("move", (src[0] + 30, src[1] + 10)),
                 ("move", (dst[0] - 20, dst[1])), ("wait", 0.2), ("move", dst), ("wait", 0.2), ("up",),
                 ("move", (620.5, 400.5))]
        for s in steps:
            if s[0] == "move":
                os_input(X.MotionNotify, at=s[1])
            elif s[0] == "down":
                os_input(X.ButtonPress, detail=1)
            elif s[0] == "up":
                os_input(X.ButtonRelease, detail=1)
            else:
                time.sleep(s[1])
            time.sleep(0.05)
        time.sleep(0.6)
        return json.loads(page.evaluate("document.body.dataset.ev"))


def check_windows(events):
    failures = []
    order = [tuple(e[:3]) for e in events if e[0] != "mouseleave"]
    if order != WINDOWS:
        import difflib
        for line in difflib.unified_diff([str(e) for e in WINDOWS], [str(e) for e in order], "stock windows",
                                         "camoufox", lineterm="", n=1):
            print("  " + line)
        failures.append("the drag's moves and drag events differ from stock Windows' order")
    if any(e[0] == "mouseleave" and e[1] == "html" and e[8] is None for e in events):
        failures.append("the pointer left the window during the drag (a mouseleave at the root); stock Windows has none")
    moves = [e for e in events if e[0] == "mousemove"]
    end = next((e for e in events if e[0] == "dragend"), None)
    late = [e for e in moves if end and events.index(e) > events.index(end)]
    if end and late and late[0][2] == 1 and late[0][7] >= end[7]:
        failures.append(f"the deferred move is stamped {late[0][7] - end[7]:.0f} ms after dragend; stock stamps it "
                        "when it was made, before dragend")
    # Each move's movement is from the move before it: no baseline reset by a window exit.
    for prev, cur in zip(moves, moves[1:]):
        want = [cur[5] - prev[5], cur[6] - prev[6]]
        if [cur[3], cur[4]] != want and not (cur[0:3] == ["mousemove", "src", 1] and cur[3:5] == [0, 0]
                                             and prev[2] == 0):
            failures.append(f"mousemove at {cur[5:7]} reports movement {cur[3:5]}, stock {want} (from {prev[5:7]})")
    return failures


def main() -> int:
    binary = resolve_binary()
    failures = []
    with hidden_display("1920x1080"):
        events = probe(binary, "windows")
        if "-v" in sys.argv:
            for e in events:
                print("   ", e)
        failures += check_windows(events)
        # Claiming Linux keeps GTK's order, as stock Linux has it: the grab's exit at the drag's first motion.
        linux = probe(binary, "linux")
        if not any(e[0] == "mouseleave" and e[1] == "html" and e[8] is None for e in linux):
            failures.append("claiming Linux, the drag no longer leaves the window at its first motion as stock "
                            "Linux (GTK) does")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: a native drag claiming Windows reaches the page in Windows' order, and in GTK's claiming Linux")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
