"""
AudioContext.outputLatency follows the identity's output device, as stock's does.

Stock Firefox asks the graph's current driver: an AudioCallbackDriver whose cubeb
stream is open answers the device's stream latency, in whole frames at the
context's rate, and the first non-zero answer is cached for the graph's life;
anything else (no stream yet, or a graph that switched to its SystemClockDriver
because none of its tracks is running) answers 0, uncached. So a context reads
0 as it is made, the latency once it runs, 0 for good if the autoplay policy
holds it (its graph opens the stream, then drops it within milliseconds as no
track runs), and 0 if it was suspended before anything read it. Stock 159 on
win-i9, 3 launches each: with no user activation, a context read once 200 ms
after it was made reads 0, as do 34 of 36 read once 10-500 ms after (two
reads at 40 ms caught the window and got 0.022; cfparity av.contexts, read at
200 ms, is 0 at every rate); with autoplay allowed, 0 until the stream opens (60-200
ms), then positive; ran, then suspended unread: 0; read while running, then
suspended: keeps the value.

audio-context-spoofing.patch answers through the same query: the drawn latency,
as stock Windows reports it (win-i9: 0.022 s device, floor(latency * rate)
frames, at least 128, truncated to 100 ns), only where stock would answer, i.e.
while the host's own stream is open, and not before
AudioContext:outputLatencyDelayMs after the graph was made. It used to answer
once the delay passed whatever the driver, so held and suspended contexts read
the drawn latency where stock reads 0 (av.contexts/*/outputLatency, 9 keys).
The launcher draws the latency per identity and the delay per launch from
audio-output-latency.json.

Checks:
  * the launcher: a Windows identity on a non-Windows host gets a latency from
    the table, fresh identities get different ones, a caller's value is kept,
    and an identity of the host's own OS gets none;
  * the browser, for two latencies: contexts made before any user activation
    (22050, 96000 Hz) are suspended and read 0, as made and 250 ms later;
    after a click, contexts at 3000, 8000, 32768, 44100 and 48000 Hz read 0 as
    made, the latency in stock's frames and 100 ns ticks 250 ms after they
    run, baseLatency 0, and
    getOutputTimestamp().contextTime == max(0, currentTime - outputLatency);
    a context that ran and was suspended unread reads 0, and one read while
    running keeps that value once suspended. The host needs an audio output
    device (CI starts a PulseAudio null sink).

    python browser/tests/playwright/patches/audio-output-latency-drawn.py
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

# Running contexts; the blocked and suspended cases use rates of their own, so
# no context shares a graph (one per rate) with another case's.
RATES = [3000, 8000, 32768, 44100, 48000]
BLOCKED_RATES = [22050, 96000]
SUSPENDED_UNREAD, SUSPENDED_READ = 16000, 24000
PROBE = """async ({rates, blockedRates, unread, read, phase}) => {
  const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
  const running = async (c) => {
    for (let i = 0; i < 400 && c.state !== 'running'; i++) await sleep(5);
    return c.state;
  };
  const out = {};
  if (phase === 'blocked') {
    for (const rate of blockedRates) {
      const c = new AudioContext({ sampleRate: rate });
      const r = { atCreate: c.outputLatency };
      await sleep(250);
      r.later = c.outputLatency;
      r.state = c.state;
      await c.close();
      out[rate] = r;
    }
    return out;
  }
  for (const rate of rates) {
    const c = new AudioContext({ sampleRate: rate });
    const r = { atCreate: c.outputLatency, baseLatency: c.baseLatency };
    r.state = await running(c);
    await sleep(250);
    r.later = c.outputLatency;
    const ts = c.getOutputTimestamp();
    r.tsGap = c.currentTime - r.later - ts.contextTime;
    r.currentTime = c.currentTime;
    await c.close();
    out[rate] = r;
  }
  for (const [rate, readWhileRunning] of [[unread, false], [read, true]]) {
    const c = new AudioContext({ sampleRate: rate });
    const r = { state: await running(c) };
    await sleep(250);
    if (readWhileRunning) r.running = c.outputLatency;
    await c.suspend();
    await sleep(250);
    r.suspended = c.outputLatency;
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
    args = {"rates": RATES, "blockedRates": BLOCKED_RATES, "unread": SUSPENDED_UNREAD, "read": SUSPENDED_READ}
    with Camoufox(executable_path=str(resolve_binary()), headless=True, os="windows", config=config,
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content('<button id="go">go</button>')
        blocked = page.evaluate(PROBE, {**args, "phase": "blocked"})
        # A trusted click is the user activation autoplay waits for.
        page.click("#go")
        got = page.evaluate(PROBE, {**args, "phase": "allowed"})
    print(f"  latency {latency}: blocked {blocked}, allowed {got}")
    for rate in BLOCKED_RATES:
        r = blocked[str(rate)]
        if r["state"] != "suspended":
            errors.append(f"{rate} Hz: a context made before any user activation is {r['state']}, "
                          "not suspended by the autoplay policy")
        if r["atCreate"] != 0 or r["later"] != 0:
            errors.append(f"{rate} Hz: a context the autoplay policy holds reads outputLatency "
                          f"{r['atCreate']}, then {r['later']} 250 ms later; stock's graph has left "
                          "its output stream by then, so both are 0")
    for rate in RATES:
        r = got[str(rate)]
        want = cubeb_rounded(latency, rate)
        if r["state"] != "running":
            errors.append(f"{rate} Hz: the context is {r['state']} 2 s after a user click; the host "
                          "needs an audio output device (a PulseAudio null sink will do)")
            continue
        if r["atCreate"] != 0:
            errors.append(f"{rate} Hz: outputLatency is {r['atCreate']} as the context is made; stock's is 0 "
                          "until the output stream opens")
        if abs(r["later"] - want) > 1e-12:
            errors.append(f"{rate} Hz: outputLatency is {r['later']} 250 ms after the context runs, want "
                          f"{want} ({latency} s in stock's frames)")
        if r["baseLatency"] != 0:
            errors.append(f"{rate} Hz: baseLatency {r['baseLatency']}, stock's is 0")
        if r["currentTime"] > r["later"] and abs(r["tsGap"]) > 0.01:
            errors.append(f"{rate} Hz: getOutputTimestamp().contextTime is {r['tsGap']} s off "
                          "currentTime - outputLatency")
    unread, read = got[str(SUSPENDED_UNREAD)], got[str(SUSPENDED_READ)]
    if unread["suspended"] != 0:
        errors.append(f"a context that ran, was never read and was then suspended reads {unread['suspended']}; "
                      "stock's reads 0 once its graph is off the output stream")
    want = cubeb_rounded(latency, SUSPENDED_READ)
    if abs(read.get("running", 0) - want) > 1e-12 or abs(read["suspended"] - want) > 1e-12:
        errors.append(f"a context read while running ({read.get('running')}) then suspended reads "
                      f"{read['suspended']}; stock's keeps the first non-zero read, {want}")
    return errors


def main() -> int:
    errors = check_launcher()
    for latency in (0.01, 0.0373):
        errors += check_browser(latency)
    for e in errors:
        print(f"FAIL: {e}")
    if errors:
        return 1
    print("PASS: outputLatency is the drawn device latency in whole frames while the output stream is open, else 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
