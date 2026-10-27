"""
fill() reaches the page as typing does in stock Firefox.

Playwright fills a text field with Page.insertText. Juggler inserted the whole
string through the field's editor: one beforeinput/input pair carrying all of
it, and not one key event. Typing cannot do that. In stock Firefox 158.0.1 on
Linux each character arrives as keydown, keypress, beforeinput and input
(insertText, that one character) and keyup, with Shift pressed around a
capital; measured with cfparity's driven.type (XTest keys on Xvfb, three runs,
identical) for the text below.

Key releases may overlap the next key's press (rollover), so they are compared
apart from the rest of the stream. The pace is a person's: keys held 50 to
250 ms, presses at least 60 ms apart (Dhakal et al., CHI 2018).

    python browser/tests/playwright/patches/driven-fill-keys.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

TEXT = "Hi there, 42!"
PAGE = '<!doctype html><html><body><input id="fill" style="width:201px"></body></html>'
RECORD = """() => {
  window.__events = [];
  const el = document.getElementById('fill');
  const mods = (e) => ['shift', 'ctrl', 'alt', 'meta'].filter((m) => e[m + 'Key']).join('+');
  for (const t of ['keydown', 'keypress', 'keyup', 'beforeinput', 'input', 'select']) {
    el.addEventListener(t, (e) => window.__events.push(e instanceof KeyboardEvent
      ? [t, e.key, e.code, e.keyCode, e.which, mods(e), e.isComposing, Math.round(e.timeStamp)]
      : [t, e.inputType ?? null, e.data ?? null, e.isComposing ?? null]));
  }
}"""


def stock_stream(text):
    """Stock's events for typing `text` on a US layout, as measured."""
    shifted = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ~!@#$%^&*()_+{}|:"<>?')
    digits = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0"}
    named = {" ": "Space", ",": "Comma", ".": "Period", "!": "Digit1"}
    out = []
    for ch in text:
        code = named.get(ch) or ("Digit" + digits.get(ch, ch) if ch.isdigit() or ch in digits else "Key" + ch.upper())
        down = ord(digits.get(ch, ch).upper()) if ch.isalnum() or ch in digits else {" ": 32, ",": 188}[ch]
        press = ord(ch) if not ch.isupper() else down
        mods = "shift" if ch in shifted else ""
        if mods:
            out.append(["keydown", "Shift", "ShiftLeft", 16, 16, "shift", False])
        out += [["keydown", ch, code, down, down, mods, False], ["keypress", ch, code, press, press, mods, False],
                ["beforeinput", "insertText", ch, False], ["input", "insertText", ch, False],
                ["keyup", ch, code, down, down, mods, False]]
        if mods:
            out.append(["keyup", "Shift", "ShiftLeft", 16, 16, "", False])
    return out


def probe(binary):
    from camoufox.sync_api import Camoufox

    with Camoufox(executable_path=str(binary), headless=True, os="linux") as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        page.evaluate(RECORD)
        page.locator("#fill").fill(TEXT, timeout=60000)
        return page.evaluate("window.__events"), page.locator("#fill").input_value()


def main() -> int:
    events, value = probe(resolve_binary())
    stock = stock_stream(TEXT)
    failures = []
    if value != TEXT:
        failures.append(f"the field holds {value!r}")
    got = [e[:-1] if e[0].startswith("key") else e for e in events]
    if [e for e in got if e[0] != "keyup"] != [e for e in stock if e[0] != "keyup"]:
        import difflib
        for line in difflib.unified_diff([str(e) for e in stock if e[0] != "keyup"],
                                         [str(e) for e in got if e[0] != "keyup"], "stock", "fill", lineterm="", n=1):
            print("  " + line)
        failures.append("fill's events differ from typing in stock")
    if sorted(map(str, (e for e in got if e[0] == "keyup"))) != sorted(map(str, (e for e in stock if e[0] == "keyup"))):
        failures.append("fill's key releases differ from typing in stock")
    downs = {}
    holds, gaps, last = [], [], None
    for e in events:
        if e[0] == "keydown" and e[1] != "Shift":
            downs[e[1]] = e[-1]
            if last is not None:
                gaps.append(e[-1] - last)
            last = e[-1]
        elif e[0] == "keyup" and e[1] in downs:
            holds.append(e[-1] - downs.pop(e[1]))
    if not holds or min(holds) < 40 or max(holds) > 260:
        failures.append(f"keys held {holds} ms: a person holds a key 50-250 ms")
    if not gaps or min(gaps) < 55:
        failures.append(f"key presses {gaps} ms apart: a person presses keys at least 60 ms apart")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print(f"PASS: fill types as a person does (holds {min(holds)}-{max(holds)} ms, gaps {min(gaps)}-{max(gaps)} ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
