"""
A Windows identity's intra-family fallback waits for character maps as Windows' does.

On Windows a DirectWrite family gets its faces on demand but no character
maps, so in a content process the first fallback within it finds it not fully
initialized: gfxFontGroup::FindFallbackFaceForChar(fontlist::Family*) starts
the parent's cmap pass and returns nothing, the character goes to the next
family, and the page sees the generic's glyph until the pass lands and the
fonts reflow. Stock Firefox 158.0.1 on win-i9 (3 launches): "Microsoft
JhengHei" with U+2211 and U+0436 (only its Light face has them) measured as
three different generic widths for 80-182 ms after its first lookup, then once
and for good as one width (the Light face) for all three generics. Families
whose regular face covers the string (Yu Gothic, Malgun Gothic, Segoe UI)
stayed constant from the start. Camoufox on Linux answered the Light face at
once.

As stock's shared font list, a landed pass holds for every process: in the
gate a cross-origin frame opened afterwards measured the Light face at once.

The guard polls the same canvas measurement every 2 ms for 2 s, then opens a
cross-origin frame (another content process) that measures it once.

    python browser/tests/playwright/patches/windows-lazy-cmaps.py
"""

import functools
import http.server
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

PAGE = """<!doctype html><meta charset=utf-8><title>run</title><script>
const TEST = 'mmMwWLliI0O&1@日本語∑ж';
const c = document.createElement('canvas').getContext('2d');
const w = (font) => { c.font = font; return c.measureText(TEST).width.toFixed(3); };
const cols = (n) => [w(`72px "${n}", monospace`), w(`72px "${n}", sans-serif`), w(`72px "${n}", serif`)].join(',');
async function poll(name) {
  const t0 = performance.now(); const seen = [[0, cols(name)]];
  while (performance.now() - t0 < 2000) {
    await new Promise((r) => setTimeout(r, 2));
    const v = cols(name); if (v !== seen[seen.length - 1][1]) seen.push([Math.round(performance.now() - t0), v]);
  }
  return seen;
}
(async () => { await new Promise((r) => setTimeout(r, 500)); const out = {};
  for (const n of ['Microsoft JhengHei', 'Yu Gothic', 'Malgun Gothic', 'Segoe UI']) out[n] = await poll(n);
  out.frame = await new Promise((res) => {
    addEventListener('message', (e) => res(e.data));
    const f = document.createElement('iframe');
    f.src = location.href.replace('127.0.0.1', 'localhost').replace('index.html', 'frame.html');
    document.body.append(f);
    setTimeout(() => res('timeout'), 20000);
  });
  document.documentElement.dataset.result = JSON.stringify(out); document.title = 'done';
})();
</script><body></body>"""
FRAME = """<!doctype html><meta charset=utf-8><script>
const TEST = 'mmMwWLliI0O&1@日本語∑ж';
const c = document.createElement('canvas').getContext('2d');
const w = (font) => { c.font = font; return c.measureText(TEST).width.toFixed(3); };
parent.postMessage([w('72px "Microsoft JhengHei", monospace'), w('72px "Microsoft JhengHei", sans-serif'),
  w('72px "Microsoft JhengHei", serif')].join(','), '*');
</script>"""

# Stock flipped at 80-182 ms; the launcher draws the pass time in that range.
# The upper bound leaves room for the reflow's own dispatch on a busy runner.
FLIP_MS = (40, 400)


def main() -> int:
    from camoufox.sync_api import Camoufox

    binary = resolve_binary()
    failures = []
    root = Path(tempfile.mkdtemp(prefix="windows-lazy-cmaps-"))
    (root / "index.html").write_text(PAGE)
    (root / "frame.html").write_text(FRAME)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with hidden_display("1920x1080"):
            with Camoufox(executable_path=str(binary), headless=False, os="windows", window=(1280, 900),
                          i_know_what_im_doing=True) as browser:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                page.wait_for_function("document.title === 'done'", timeout=60000)
                out = json.loads(page.evaluate("document.documentElement.dataset.result"))
    finally:
        server.shutdown()
    frame = out.pop("frame")
    for name, seen in out.items():
        print(f"  {name}: {seen}")
    print(f"  cross-origin frame afterwards: {frame}")
    jh = out["Microsoft JhengHei"]
    first = jh[0][1].split(",")
    if len(set(first)) != 3:
        failures.append(f"Microsoft JhengHei's first lookup {jh[0][1]} is not the three generics' widths "
                        "(stock: the family's cmaps are not loaded yet, so U+2211/U+0436 go to the generic)")
    if len(jh) != 2:
        failures.append(f"Microsoft JhengHei changed {len(jh) - 1} times; stock changes once")
    else:
        t, last = jh[1][0], jh[1][1].split(",")
        if not FLIP_MS[0] <= t <= FLIP_MS[1]:
            failures.append(f"Microsoft JhengHei flipped at {t} ms; stock 80-182 ms")
        if len(set(last)) != 1:
            failures.append(f"Microsoft JhengHei after the flip {jh[1][1]} is not one face for every generic")
        if frame != jh[1][1]:
            failures.append(f"a cross-origin frame after the flip measured {frame}; stock has the landed {jh[1][1]}")
    for name in ("Yu Gothic", "Malgun Gothic", "Segoe UI"):
        if len(out[name]) != 1 or len(set(out[name][0][1].split(","))) != 1:
            failures.append(f"{name} {out[name]}: stock is one width for every generic, constant from the start")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: the first fallback within an unloaded family goes to the generic until the cmap pass lands, once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
