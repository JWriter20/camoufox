"""
A Windows identity's timers fire as late as Windows Firefox's do.

Windows Firefox's TimerThread sleeps on a waitable timer with a tolerance
(xpcom/threads/TimerThread.cpp TimerThread::Wait, XP_WIN), so a timer fires
a few hundred microseconds after its delay. Linux's wakes on time. A page sees
it in a nested setTimeout(0) chain (clamped to 4 ms after five levels) read at
its 1 ms jittered performance.now: the gaps round to 3, 4 or 5 ms in different
proportions, and cfparity's timing.timerClamp ("the last six gaps all >= 3.5
ms") reads 'unclamped' far more often on Linux.

Stock Firefox 158.0.1, 3 launches each (300 nested gaps; 30 timerClamp chains):
  win-i9 (Windows 11)  3: 6-13   4: 148-161  5: 114-143  6+: 3-11   unclamped 1-4/30
  Linux                3: 24-45  4: 173-208  5: 57-73              unclamped 10-19/30
Unclamped, win-i9's gaps are 4.40-4.76 ms with stretches of 4.04-4.35 ms;
Linux's 4.06-4.08 ms.
Camoufox before the fix, claiming Windows, had Linux's numbers.

The guard runs the same page (window, and the 300-gap run in a dedicated
worker) in 3 launches claiming Windows and 1 claiming Linux. The sums over the
Windows launches must fall inside Windows' ranges, widened only by the sampling
spread of 3 launches; the Linux launch must keep Linux's shape.

    python browser/tests/playwright/patches/timer-lateness-claimed-os.py
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

RUN = """
const once = () => new Promise((res) => { let n = 0; const ts = [];
  const tick = () => { ts.push(performance.now()); if (++n < 12) setTimeout(tick, 0); else res(ts.slice(6).map((t, i) => t - ts[i + 5]).every((x) => x >= 3.5) ? 'clamped-4ms' : 'unclamped'); };
  setTimeout(tick, 0); });
const nested = () => new Promise((res) => { const hist = {}; let n = 0, last = 0;
  const tick = () => { const t = performance.now(); if (n > 6) { const d = Math.round(t - last); hist[d] = (hist[d] || 0) + 1; } last = t; if (++n < 306) setTimeout(tick, 0); else res(hist); };
  setTimeout(tick, 0); });
"""
PAGE = f"""<!doctype html><meta charset=utf-8><title>run</title><script>{RUN}
(async () => {{
  await new Promise((r) => setTimeout(r, 1500));
  let unclamped = 0;
  for (let i = 0; i < 30; i++) unclamped += (await once()) === 'unclamped';
  const hist = await nested();
  const src = {json.dumps(RUN)} + 'nested().then((h) => postMessage(h));';
  const worker = await new Promise((res) => {{
    const w = new Worker(URL.createObjectURL(new Blob([src], {{type: 'text/javascript'}})));
    w.onmessage = (e) => res(e.data); w.onerror = () => res(null); }});
  document.documentElement.dataset.result = JSON.stringify({{unclamped, hist, worker}}); document.title = 'done';
}})();
</script>"""

# Windows' ranges as sums over 3 launches. win-i9 measured 3: 28, 4: 467, 5: 383,
# 6+: 19, unclamped 7; its unclamped gaps (privacy.reduceTimerPrecision off) put
# through the 1 ms jittered clock give 3: ~39, unclamped ~16. The bounds hold
# 99.9% of simulated runs of the fix's lateness on Linux's gaps (3: 20-53,
# 4: 417-483, 5: 347-405, unclamped 8-36). 6+ also holds the runner's own
# scheduling stalls. Linux sums 3: 90-139, 4: 524-624, 5: 168-219.
WINDOWS = {"3": (15, 56), "4": (410, 490), "5": (335, 415), "6+": (5, 65), "unclamped": (0, 38)}


def buckets(hist):
    out = {"3": 0, "4": 0, "5": 0, "6+": 0}
    for k, v in (hist or {}).items():
        k = int(k)
        if k >= 6:
            out["6+"] += v
        elif str(k) in out:
            out[str(k)] += v
    return out


def probe(binary, claimed, url):
    from camoufox.sync_api import Camoufox

    with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1280, 900),
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.goto(url)
        page.wait_for_function("document.title === 'done'", timeout=60000)
        return json.loads(page.evaluate("document.documentElement.dataset.result"))


def main() -> int:
    binary = resolve_binary()
    root = Path(tempfile.mkdtemp(prefix="timer-lateness-"))
    (root / "index.html").write_text(PAGE)
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/index.html"
    failures = []
    try:
        with hidden_display("1920x1080"):
            win = {"3": 0, "4": 0, "5": 0, "6+": 0, "unclamped": 0}
            wwin = {"3": 0, "4": 0, "5": 0, "6+": 0}
            for i in range(3):
                r = probe(binary, "windows", url)
                b, wb = buckets(r["hist"]), buckets(r["worker"])
                print(f"  windows #{i}: unclamped {r['unclamped']}/30 window {b} worker {wb}")
                for k in b:
                    win[k] += b[k]
                    wwin[k] += wb[k]
                win["unclamped"] += r["unclamped"]
            r = probe(binary, "linux", url)
            lin, wlin = buckets(r["hist"]), buckets(r["worker"])
            print(f"  linux:      unclamped {r['unclamped']}/30 window {lin} worker {wlin}")
    finally:
        server.shutdown()
    print(f"  windows sums: window {win} worker {wwin}")
    for k, (lo, hi) in WINDOWS.items():
        if not lo <= win[k] <= hi:
            failures.append(f"claiming windows, window {k}: {win[k]} over 3 launches, stock Windows {lo}-{hi}")
        if k != "unclamped" and not lo <= wwin[k] <= hi:
            failures.append(f"claiming windows, worker {k}: {wwin[k]} over 3 launches, stock Windows {lo}-{hi}")
    # Linux keeps its own timers: many 3s, few 5s.
    for name, b in (("window", lin), ("worker", wlin)):
        if b["3"] < 15 or b["5"] > 95:
            failures.append(f"claiming linux, {name}: {b} is not stock Linux's shape (3: 24-45, 5: 57-73)")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: claiming Windows, timers fire as late as Windows'; claiming Linux, on time")
    return 0


if __name__ == "__main__":
    sys.exit(main())
