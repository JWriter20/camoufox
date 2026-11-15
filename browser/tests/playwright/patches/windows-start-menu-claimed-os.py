"""
A Windows identity's Win key opens the Start menu: the window blurs and the key's keyup never arrives.

On Windows a Win key released with no other key in between opens Start, which
takes the foreground. Stock Firefox 158.0.1 on win-i9 (SendInput, the gate's
exact steps, it6): Super_L gives keydown Meta, then blur and hasFocus false,
no keyup; the next click into the window only dismisses Start (focus then blur
at once, no pointer events), so a Super_R tapped after it reaches nothing and
neither does Alt_L; the click after that activates the window and is
delivered. Win held around a click opens Start too (input.mouse.modifiers
super: keydown, the click, no keyup). Linux has no such shell: GTK delivers
both Win key events and the window keeps the focus (widget/gtk/nsWindow.cpp,
ClaimedShellKey).

The guard replays cfparity's input.keys.modifiers and input.mouse.modifiers
steps with XTest and reads every event the page sees. Claiming Linux the Win
keys still give keydown and keyup and the focus stays.

    python browser/tests/playwright/patches/windows-start-menu-claimed-os.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

PAGE = """<!doctype html><html><body style="margin:0;width:1200px;height:800px">
<input id="i" style="position:absolute;left:40px;top:40px;width:400px">
<div id="t" style="position:absolute;left:40px;top:120px;width:200px;height:120px;background:#cde"></div>
<script>
  const seen = [];
  const who = (t) => (t === window ? 'window' : t === document ? 'document' : (t && (t.id || t.nodeName.toLowerCase())) || String(t));
  for (const t of ['keydown', 'keyup', 'mousedown', 'click', 'focus', 'blur'])
    addEventListener(t, (e) => {
      if ((t === 'focus' || t === 'blur') && e.target !== window) return;
      seen.push(e.type + (e.key ? ':' + e.key : '') + '@' + who(e.target));
    }, true);
  window.take = () => { const out = seen.splice(0); return { events: out, hasFocus: document.hasFocus() }; };
</script></body></html>"""


class OSInput:
    """Native input through XTest at page CSS coordinates, with cfparity's step delays."""

    def __init__(self, page):
        from Xlib import display
        self.d = display.Display()
        self.o = page.evaluate("({x: mozInnerScreenX, y: mozInnerScreenY, dpr: devicePixelRatio})")

    def fake(self, kind, wait, **kw):
        from Xlib.ext import xtest
        xtest.fake_input(self.d, kind, **kw)
        self.d.sync()
        time.sleep(wait)

    def click(self, x, y):
        from Xlib import X
        o = self.o
        self.fake(X.MotionNotify, 0.05, x=round((o["x"] + x) * o["dpr"]), y=round((o["y"] + y) * o["dpr"]))
        self.fake(X.ButtonPress, 0.03, detail=1)
        self.fake(X.ButtonRelease, 0.13, detail=1)

    def key(self, name, down=None):
        from Xlib import X, XK
        code = self.d.keysym_to_keycode(XK.string_to_keysym(name))
        if down in (None, True):
            self.fake(X.KeyPress, 0.03, detail=code)
        if down in (None, False):
            self.fake(X.KeyRelease, 0.08, detail=code)


def scenario(binary, claimed):
    from camoufox.sync_api import Camoufox
    out = {}
    with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1280, 900), main_world_eval=True,
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        page.wait_for_timeout(500)
        inp = OSInput(page)

        def step(name, keys, then=None):
            # cfparity's typeInto: click the input, settle, then the keys.
            inp.click(240, 51)
            page.wait_for_timeout(400)
            page.evaluate("mw:take()")
            for k in keys:
                inp.key(k)
                if then:
                    time.sleep(then)
            page.wait_for_timeout(400)
            out[name] = page.evaluate("mw:take()")

        step("Super_L", ["Super_L"])
        step("Super_R", ["Super_R"])
        step("Alt_L", ["Alt_L", "Escape"], then=0.3)
        step("Alt_R", ["Alt_R", "Escape"], then=0.3)
        # input.mouse.modifiers super: Win held around a click, nothing focused in the page.
        page.evaluate("mw:document.activeElement.blur()")
        page.evaluate("mw:take()")
        inp.key("Super_L", True)
        inp.click(140, 180)
        inp.key("Super_L", False)
        page.wait_for_timeout(400)
        out["mouseSuper"] = page.evaluate("mw:take()")
        inp.click(140, 180)
        page.wait_for_timeout(400)
        out["clickAfter"] = page.evaluate("mw:take()")
    return out


WANT = {
    "windows": {
        "Super_L": (["keydown:Meta@i", "blur@window"], False),
        "Super_R": ([], False),
        "Alt_L": ([], False),
        "Alt_R": (["keydown:Alt@i", "keyup:Alt@i", "keyup:Escape@i"], True),
        "mouseSuper": (["keydown:Meta@body", "mousedown@t", "click@t", "blur@window"], False),
        "clickAfter": (["focus@window", "mousedown@t", "click@t"], True),
    },
    "linux": {
        "Super_L": (["keydown:Meta@i", "keyup:Meta@i"], True),
        "Super_R": (["keydown:Meta@i", "keyup:Meta@i"], True),
        "mouseSuper": (["keydown:Meta@body", "mousedown@t", "click@t", "keyup:Meta@body"], True),
    },
}


def main() -> int:
    binary = resolve_binary()
    failures = []
    with hidden_display("1920x1080"):
        for claimed, want in WANT.items():
            got = scenario(binary, claimed)
            for name, (events, focus) in want.items():
                g = got[name]
                print(f"  {claimed:8} {name:10} hasFocus={g['hasFocus']} {g['events']}")
                if g["events"] != events or g["hasFocus"] != focus:
                    failures.append(f"claiming {claimed}, {name}: {json.dumps(g)}; stock has {events}, "
                                    f"hasFocus {focus}")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: a Windows identity's Win key opens Start (blur, no keyup, the next click only dismisses it); "
          "Linux keeps both key events and the focus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
