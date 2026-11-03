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

Those sums move with the runner's load: a busy host delays some wakeups, which
adds 5s and 6+s to both claims (LEAKS 258, 278: window 417 and worker 419 in
gate runs beside autoupdate's parity-warm work, where the same build reads
379/373 on a quiet host; the model's own 3-launch sum is 378, p99 397-402,
as win-i9's raw gaps give 378, p99 401). So one launch per claim also records
the raw gaps (privacy.reduceTimerPrecision off, 1200 window + 1200 worker),
which decide on their own, unmoved by load: a Windows claim's p10, median and
share of the low state (4.04-4.35 ms) inside win-i9's (3 launches: 4.14, 4.48,
0.36-0.38; the model on this box: 4.16-4.20, 4.44, 0.30-0.33, also under 14
niced CPU burners), a Linux claim's median at Linux's 4.06. The Linux claim's
raw gaps also tell a loaded host: a quiet one has at most 3 of 1200 over
4.3 ms (14 niced burners: 8-109). On a loaded host a bucket-sum or Linux-shape
miss is reported as the load's, and the guard still fails (exit 2): nothing is
loosened, the raw check still judges the model.

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
# Recalibrated 11-01 (LEAKS 278) to stock's own spread: win-i9's raw gaps (order kept) through the
# jittered clock in 20000 simulated guards give 3: 21-60, 4: 425-502, 5: 345-412 (p0.05-p99.95); the
# earlier 4 <= 490 and 3 <= 56 failed stock's own timers in 0.9% and 0.3% of guards (a model run read
# 4: 499 on a quiet host), and 5 <= 415 let through what stock reaches in fewer than 0.05%.
# Recalibrated again 11-01 (LEAKS 342): those simulations drew each 1 ms bucket's midpoint uniformly and
# independently. Stock's are not: RandomMidpoint (nsRFPService.cpp:626) takes the first output of an
# XorShift128+ seeded with (timeline mixin ^ seed, clamped ^ seed), so within one realm (one document or
# worker, one mixin) the midpoints follow the clamped time's low bits and some realms read nearly
# unjittered (stock 158.0.1 Linux, 150 workers of one launch: 3 of 300 gaps = 1, 1, 1, 4, 9, 13, 14).
# The same model reproduces stock's gtest vectors (test_reduceprecision.cpp:440-452). Under it the old
# bounds failed stock's own timers in 7.9% of window or worker sums; win-i9's raw gaps give 3: 10-69,
# 4: 406-527, 5: 322-424, 6+: 4-43 (p0.05-p99.95, 20000 simulated guards), unclamped 5-39 of 90.
# A Linux-timed Windows claim still fails 5 (its 3-launch sum stays under 270).
WINDOWS = {"3": (10, 70), "4": (405, 528), "5": (321, 425), "6+": (4, 65), "unclamped": (0, 40)}

# Raw gaps, per launch (window and worker each): win-i9 stock p10 4.14, median 4.48, low share 0.36-0.38;
# stock Linux 4.06 / 4.06 / 1.00.
RAW_WINDOWS = {"p10": (4.10, 4.24), "median": (4.40, 4.52), "low": (0.22, 0.45)}
RAW_LINUX_MEDIAN = (4.04, 4.08)
QUIET_SLOW = 6  # a quiet host's Linux claim: at most this many of 1200 raw gaps over 4.3 ms

RAW = """
const raw = (n) => new Promise((res) => { const ts = [];
  const tick = () => { ts.push(performance.now()); if (ts.length < n) setTimeout(tick, 0);
    else res(ts.slice(7).map((t, i) => t - ts[i + 6])); };
  setTimeout(tick, 0); });
"""
RAW_PAGE = f"""<!doctype html><meta charset=utf-8><title>raw</title><script>{RAW}
(async () => {{
  await new Promise((r) => setTimeout(r, 1500));
  const win = await raw(1207);
  const src = {json.dumps(RAW)} + 'raw(1207).then((g) => postMessage(g));';
  const worker = await new Promise((res) => {{
    const w = new Worker(URL.createObjectURL(new Blob([src], {{type: 'text/javascript'}})));
    w.onmessage = (e) => res(e.data); w.onerror = () => res(null); }});
  document.documentElement.dataset.result = JSON.stringify({{win, worker}}); document.title = 'done';
}})();
</script>"""


def raw_shape(gaps):
    gaps = sorted(g for g in gaps or [] if g < 40)
    if not gaps:
        return None
    return {"p10": round(gaps[len(gaps) // 10], 3), "median": round(gaps[len(gaps) // 2], 3),
            "low": round(sum(4.0 <= g < 4.36 for g in gaps) / len(gaps), 3), "slow": sum(g > 4.3 for g in gaps)}


def buckets(hist):
    out = {"3": 0, "4": 0, "5": 0, "6+": 0}
    for k, v in (hist or {}).items():
        k = int(k)
        if k >= 6:
            out["6+"] += v
        elif str(k) in out:
            out[str(k)] += v
    return out


def probe(binary, claimed, url, prefs=None):
    from camoufox.sync_api import Camoufox

    with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1280, 900),
                  i_know_what_im_doing=True, firefox_user_prefs=prefs or {}) as browser:
        page = browser.new_page()
        page.goto(url)
        page.wait_for_function("document.title === 'done'", timeout=60000)
        return json.loads(page.evaluate("document.documentElement.dataset.result"))


def main() -> int:
    binary = resolve_binary()
    root = Path(tempfile.mkdtemp(prefix="timer-lateness-"))
    (root / "index.html").write_text(PAGE)
    (root / "raw.html").write_text(RAW_PAGE)
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/index.html"
    failures, load_failures = [], []
    raw = {}
    try:
        with hidden_display("1920x1080"):
            for claimed in ("windows", "linux"):
                r = probe(binary, claimed, url.replace("index.html", "raw.html"), {"privacy.reduceTimerPrecision": False})
                raw[claimed] = {k: raw_shape(r.get(k)) for k in ("win", "worker")}
                print(f"  raw {claimed}: window {raw[claimed]['win']} worker {raw[claimed]['worker']}")
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
    # The raw gaps: the model itself, whatever the host's load.
    for name in ("win", "worker"):
        shape = raw["windows"][name]
        for k, (lo, hi) in RAW_WINDOWS.items():
            if shape is None or not lo <= shape[k] <= hi:
                failures.append(f"claiming windows, raw {name} {k}: {shape and shape[k]}, stock Windows {lo}-{hi}")
        shape = raw["linux"][name]
        if shape is None or not RAW_LINUX_MEDIAN[0] <= shape["median"] <= RAW_LINUX_MEDIAN[1]:
            failures.append(f"claiming linux, raw {name} median: {shape and shape['median']}, stock Linux 4.06")
    slow = max((raw["linux"][k] or {"slow": 10 ** 9})["slow"] for k in ("win", "worker"))
    loaded = slow > QUIET_SLOW
    sums = load_failures if loaded else failures
    for k, (lo, hi) in WINDOWS.items():
        if not lo <= win[k] <= hi:
            sums.append(f"claiming windows, window {k}: {win[k]} over 3 launches, stock Windows {lo}-{hi}")
        if k != "unclamped" and not lo <= wwin[k] <= hi:
            sums.append(f"claiming windows, worker {k}: {wwin[k]} over 3 launches, stock Windows {lo}-{hi}")
    # Linux keeps its own timers: few 5s. Not "many 3s": a stock realm's own midpoints leave 3 under 15 in
    # 3.5% of realms (LEAKS 255/342). Per realm, stock Linux reads 5 at 21-89 (p0.05-p99.95), a realm
    # carrying the Windows lateness 92-155.
    for name, b in (("window", lin), ("worker", wlin)):
        if b["5"] > 95:
            sums.append(f"claiming linux, {name}: {b} is not stock Linux's shape (5: 21-89 per realm)")
    for failure in failures:
        print(f"FAIL: {failure}")
    for failure in load_failures:
        print(f"FAIL (loaded host: {slow} of 1200 raw Linux gaps over 4.3 ms, a quiet host has at most {QUIET_SLOW}; "
              f"the raw check judges the model): {failure}")
    if failures:
        return 1
    if load_failures:
        return 2
    print("PASS: claiming Windows, timers fire as late as Windows'; claiming Linux, on time")
    return 0


if __name__ == "__main__":
    sys.exit(main())
