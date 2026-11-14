"""
AudioContext.outputLatency follows the identity's output device, as stock's does.

Stock Firefox reads 0 for a context's outputLatency until its output stream has
opened (cubeb_stream_get_latency fails before then and a 0 is not cached), then
the device's stream latency, in whole frames at the context's rate, cached for
the graph's life. Stock 158 on win-i9 (cfparity av.contexts): 0 when read as the
context is made (identity.html), positive 200 ms later at every rate from 3000
to 768000 Hz, baseLatency 0. Camoufox answered AudioContext:outputLatency from
the moment the context was made, and the cross-OS gate pinned it to that first
read, 0, so every later read stayed 0 (av.contexts/*/outputLatency, 9 keys).

audio-context-spoofing.patch now answers through the graph: 0 until
AudioContext:outputLatencyDelayMs after the graph started, then the drawn
latency as stock Windows reports it (win-i9, 10 launches: 0.022 s device,
floor(latency * rate) frames, at least 128, truncated to 100 ns). The launcher draws the latency per
identity and the delay per launch from audio-output-latency.json.

Checks:
  * the launcher: a Windows identity on a non-Windows host gets a latency from
    the table, fresh identities get different ones, a caller's value is kept,
    and an identity of the host's own OS gets none;
  * the browser, for two latencies at 3000, 8000, 32768, 44100 and 48000 Hz:
    0 as the context is made, the latency in stock's frames and 100 ns ticks
    250 ms later, baseLatency 0, and
    getOutputTimestamp().contextTime == max(0, currentTime - outputLatency).

    python browser/tests/playwright/patches/audio-output-latency-drawn.py
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

RATES = [3000, 8000, 32768, 44100, 48000]
PROBE = """async (rates) => {
  const out = {};
  for (const rate of rates) {
    const c = new AudioContext({ sampleRate: rate });
    const r = { atCreate: c.outputLatency, baseLatency: c.baseLatency };
    await new Promise((res) => setTimeout(res, 250));
    r.later = c.outputLatency;
    const ts = c.getOutputTimestamp();
    r.tsGap = c.currentTime - r.later - ts.contextTime;
    r.currentTime = c.currentTime;
    await c.close();
    out[rate] = r;
  }
  return out;
}"""


def cubeb_rounded(seconds: float, rate: int) -> float:
    # Stock 158 on win-i9: floor(latency * rate) frames, at least 128, read
    # through a 100 ns TimeDuration (720/32768 -> 0.0219726, 128/3000 -> 0.0426666).
    frames = max(math.floor(seconds * rate + 1e-9), 128)
    return math.trunc(frames / rate * 1000.0 * 10000.0) / 1e7


def check_launcher() -> list:
    from camoufox.fingerprints import _load_audio_output_latency, set_output_latency_default

    errors = []
    table = {d["v"] for d in _load_audio_output_latency()["win"]["device_latency_s"]}
    drawn = set()
    delays = set()
    for salt in range(64):
        config = {"navigator.platform": "Win32"}
        set_output_latency_default(config, salt * 0x9E3779B1, "win", "lin")
        value = config.get("AudioContext:outputLatency")
        if value not in table:
            errors.append(f"a Windows identity drew outputLatency {value!r}, not one of the table's {sorted(table)}")
            break
        if not isinstance(config.get("AudioContext:outputLatencyDelayMs"), float):
            errors.append("a Windows identity drew no AudioContext:outputLatencyDelayMs")
            break
        drawn.add(value)
        delays.add(config["AudioContext:outputLatencyDelayMs"])
    if len(table) > 1 and len(drawn) < 2:
        errors.append(f"64 fresh Windows identities all drew {drawn}: the latency does not vary between machines")
    if len(delays) < 32:
        errors.append(f"64 launches drew only {len(delays)} distinct stream opening delays: it does not vary per launch")
    kept = {"AudioContext:outputLatency": 0.05}
    set_output_latency_default(kept, 1, "win", "lin")
    if kept != {"AudioContext:outputLatency": 0.05}:
        errors.append(f"a caller's outputLatency was changed: {kept}")
    native = {"navigator.platform": "Win32"}
    set_output_latency_default(native, 1, "win", "win")
    if "AudioContext:outputLatency" in native:
        errors.append("an identity of the host's own OS got a drawn latency; the host's device answers there")
    return errors


def check_browser(latency: float) -> list:
    from camoufox.sync_api import Camoufox

    errors = []
    config = {"AudioContext:outputLatency": latency, "AudioContext:outputLatencyDelayMs": 40.0}
    with Camoufox(executable_path=str(resolve_binary()), headless=True, os="windows", config=config,
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.goto("about:blank")
        got = page.evaluate(PROBE, RATES)
    print(f"  latency {latency}: {got}")
    for rate in RATES:
        r = got[str(rate)]
        want = cubeb_rounded(latency, rate)
        if r["atCreate"] != 0:
            errors.append(f"{rate} Hz: outputLatency is {r['atCreate']} as the context is made; stock's is 0 "
                          "until the output stream opens")
        if abs(r["later"] - want) > 1e-12:
            errors.append(f"{rate} Hz: outputLatency is {r['later']} 250 ms later, want {want} "
                          f"({latency} s in stock's frames)")
        if r["baseLatency"] != 0:
            errors.append(f"{rate} Hz: baseLatency {r['baseLatency']}, stock's is 0")
        if r["currentTime"] > r["later"] and abs(r["tsGap"]) > 0.01:
            errors.append(f"{rate} Hz: getOutputTimestamp().contextTime is {r['tsGap']} s off "
                          "currentTime - outputLatency")
    return errors


def main() -> int:
    errors = check_launcher()
    for latency in (0.01, 0.0373):
        errors += check_browser(latency)
    for e in errors:
        print(f"FAIL: {e}")
    if errors:
        return 1
    print("PASS: outputLatency is 0 until the stream opens, then the drawn device latency in whole frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
