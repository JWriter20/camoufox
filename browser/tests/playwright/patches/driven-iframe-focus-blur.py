"""
A driven page fires blur when focus moves into one of its own iframes, as in stock.

Upstream Playwright keeps every driven page focused (docShell.overrideHasFocus)
by never blurring a document focus leaves, which also swallowed the blur when
focus moves from the page into its own iframe. In stock Firefox a click into
an iframe blurs the focused element and then the top window, while
document.hasFocus() stays true. 0-playwright.patch keeps the override for
focus that leaves the tab, and not for focus that moves into the page's frames.

The page logs its events into data-ev on <html>, since a driver's evaluate runs
in an isolated world.

    python browser/tests/playwright/patches/driven-iframe-focus-blur.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

PAGE = """<!doctype html><html><body><input id=top>
<iframe srcdoc="<input id=inner>" style="width:300px;height:100px"></iframe><script>
const log = e => { const d = document.documentElement;
  const t = e.target === window ? 'window' : e.target === document ? 'document' : e.target.id;
  d.dataset.ev = (d.dataset.ev || '') + e.type + ':' + t + ' '; };
for (const t of ['focus', 'blur']) addEventListener(t, log, true);
</script></body></html>"""

READ = "() => ({events: (document.documentElement.dataset.ev || '').trim(), hasFocus: document.hasFocus()})"


def main():
    from camoufox.sync_api import Camoufox

    with Camoufox(executable_path=str(resolve_binary()), headless=True, os="linux",
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        page.click("#top")
        page.frame_locator("iframe").locator("#inner").click()
        page.wait_for_timeout(200)
        seen = page.evaluate(READ)

    print(seen)
    events = seen["events"].split()
    failures = []
    for wanted in ("blur:top", "blur:window"):
        if wanted not in events:
            failures.append(f"no {wanted} after the click into the iframe (stock fires it)")
    if not seen["hasFocus"]:
        failures.append("top document lost focus; in stock the focused iframe keeps it focused")
    for f in failures:
        print("FAIL", f)
    print("FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
