"""
A Windows identity reports a lock key's new state on its keydown, and keypad 5 without Num Lock as "Clear".

Windows toggles Caps Lock / Num Lock / Scroll Lock when the key goes down, so
the keydown already reports the new state: stock Firefox 158.0.1 on win-i9
(cfparity input.keys.modifiers capsLock, input.keys.numpad, SendInput) gives
keydown CapsLock with getModifierState('CapsLock') false when the press turns
it off, the same as its keyup. XKB unlocks a locked modifier only on the
release, so GTK's keydown still says on (gate 10: Camoufox 'CapsLock+NumLock'
on the second Caps Lock keydown, stock 'NumLock'). Keypad 5 with Num Lock off is
VK_CLEAR on Windows (key "Clear", keyCode 12); GTK's KP_Begin has no DOM key
name ("Unidentified").

The guard presses Caps Lock and Num Lock twice each and keypad 5 with Num Lock off through
XTest. Claiming Windows: every lock keydown reports what its keyup reports, and
KP_Begin is "Clear". Claiming Linux: GTK's own behaviour (the press that turns a
lock off still reports it on; "Unidentified").

    python browser/tests/playwright/patches/windows-lock-keys-claimed-os.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

PAGE = """<!doctype html><html><body style="margin:0">
<input id="i" style="position:absolute;left:40px;top:40px;width:400px">
<script>
  const seen = [];
  for (const t of ['keydown', 'keyup'])
    addEventListener(t, (e) => seen.push([e.type, e.key, e.keyCode,
      ['CapsLock', 'NumLock', 'ScrollLock'].filter((m) => e.getModifierState(m)).join('+')]), true);
  window.take = () => seen.splice(0);
</script></body></html>"""


def scenario(binary, claimed):
    from camoufox.sync_api import Camoufox
    from Xlib import XK, X, display
    from Xlib.ext import xtest
    d = display.Display()

    def key(name):
        code = d.keysym_to_keycode(XK.string_to_keysym(name))
        for kind in (X.KeyPress, X.KeyRelease):
            xtest.fake_input(d, kind, detail=code)
            d.sync()
            time.sleep(0.08)

    with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1280, 900), main_world_eval=True,
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        page.wait_for_timeout(500)
        o = page.evaluate("({x: mozInnerScreenX, y: mozInnerScreenY, dpr: devicePixelRatio})")
        xtest.fake_input(d, X.MotionNotify, x=round((o["x"] + 240) * o["dpr"]), y=round((o["y"] + 51) * o["dpr"]))
        for kind in (X.ButtonPress, X.ButtonRelease):
            xtest.fake_input(d, kind, detail=1)
            d.sync()
            time.sleep(0.1)
        page.wait_for_timeout(300)
        page.evaluate("mw:take()")
        locks = []
        for name in ("Caps_Lock", "Caps_Lock", "Num_Lock", "Num_Lock"):
            key(name)
            page.wait_for_timeout(150)
            ev = page.evaluate("mw:take()")
            down = next((e for e in ev if e[0] == "keydown"), None)
            up = next((e for e in ev if e[0] == "keyup"), None)
            locks.append((name, down and down[3], up and up[3]))
        # Keypad 5 with Num Lock off.
        key("a")
        page.wait_for_timeout(150)
        if "NumLock" in page.evaluate("mw:take()")[0][3]:
            key("Num_Lock")
        page.wait_for_timeout(150)
        page.evaluate("mw:take()")
        key("KP_Begin")
        page.wait_for_timeout(150)
        kp = next((e for e in page.evaluate("mw:take()") if e[0] == "keydown"), None)
    d.close()
    return locks, kp


def main() -> int:
    binary = resolve_binary()
    failures = []
    with hidden_display("1920x1080"):
        for claimed in ("windows", "linux"):
            locks, kp = scenario(binary, claimed)
            print(f"  {claimed:8} locks (key, keydown state, keyup state) {locks}; KP_Begin {kp}")
            names = {"Caps_Lock": "CapsLock", "Num_Lock": "NumLock"}
            for name, down, up in locks:
                if down is None or up is None:
                    failures.append(f"claiming {claimed}: no keydown/keyup for {name}")
                    continue
                same = (names[name] in down.split("+")) == (names[name] in up.split("+"))
                # Linux: the press that turns a lock off still reports it on (XKB unlocks on release).
                turning_off = names[name] not in up.split("+")
                want_same = claimed == "windows" or not turning_off
                if same != want_same:
                    failures.append(f"claiming {claimed}: {name} keydown state {down!r}, keyup {up!r}")
            want_key = "Clear" if claimed == "windows" else "Unidentified"
            if not kp or kp[1] != want_key or kp[2] != 12:
                failures.append(f"claiming {claimed}: keypad 5 without Num Lock gives {kp}, stock {want_key}/12")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: claiming Windows a lock keydown reports the new state and keypad 5 is Clear; Linux keeps GTK's")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
