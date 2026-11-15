import re
from pathlib import Path

CFG = Path(__file__).resolve().parents[2] / "browser" / "settings" / "camoufox.cfg"


def test_reader_parse_on_load_stays_at_stock():
    # Stock Firefox parses every page for Reader View after its first paint
    # (all.js: reader.parse-on-load.enabled true). AboutReaderChild reads the
    # MozAfterPaint event's clientRects and the page's <p> nodes, which resolves
    # DOMRectList and HTMLParagraphElement on the page's window; with the check
    # off a page enumerating its window lacks both where stock has them
    # (cfparity cross-os against Windows Firefox 159, 2026-11-11).
    assert not re.search(r'Pref\("reader\.parse-on-load\.enabled"', CFG.read_text())
