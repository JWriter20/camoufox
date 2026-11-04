"""
A Windows identity serializes a selection with Windows' line break, CRLF.

With no explicit line-break flag the document encoder's serializers use the
platform's NS_LINEBREAK: "\\r\\n" on Windows, "\\n" on Linux. Selection.toString()
passes none, so stock Windows Firefox 158.0.1 reads a selection over three
lines of a white-space:pre <p> as "delta\\r\\necho ..." (cfparity
input.mouse.select, win-i9, 3/3), and Camoufox on Linux claiming Windows read
"delta\\necho". The same encoder makes copied and dragged text/plain.

The guard selects across the lines of a preformatted paragraph and reads
Selection.toString(). Claiming Windows: CRLF. Claiming Linux: LF, as stock
Linux has it.

    python browser/tests/playwright/patches/serializer-line-break-claimed-os.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

PAGE = """<!doctype html><html><body>
<p id="p" style="white-space:pre;font:16px/20px monospace">alpha bravo charlie delta
echo foxtrot golf hotel
india juliet kilo lima</p></body></html>"""

READ = """() => {
  const t = document.getElementById('p').firstChild;
  const r = document.createRange();
  r.setStart(t, 2);
  r.setEnd(t, t.data.indexOf('kilo') + 2);
  getSelection().removeAllRanges();
  getSelection().addRange(r);
  return getSelection().toString();
}"""


def probe(binary, claimed):
    from camoufox.sync_api import Camoufox

    with Camoufox(executable_path=str(binary), headless=False, os=claimed, i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        return page.evaluate(READ)


def main() -> int:
    binary = resolve_binary()
    failures = []
    with hidden_display("1920x1080"):
        for claimed, want in (("windows", "\r\n"), ("linux", "\n")):
            text = probe(binary, claimed)
            print(f"  {claimed:8} {text!r}")
            expected = want.join(["pha bravo charlie delta", "echo foxtrot golf hotel", "india juliet ki"])
            if text != expected:
                failures.append(f"claiming {claimed}: Selection.toString() gave {text!r}, stock {expected!r}")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: a selection breaks lines as the claimed OS's Firefox does")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
