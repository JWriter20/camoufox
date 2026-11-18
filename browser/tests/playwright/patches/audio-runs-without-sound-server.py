"""
An AudioContext a user gesture allows runs on a host with no sound server.

Firefox applies a context's pending resume only once its graph runs on an audio
callback driver whose cubeb stream is open. On a host where no cubeb backend
finds a sound server (a container, a server: no PulseAudio socket), the stream
never opens, the graph stays on its fallback clock and the context stays
`suspended` after the click that allows it, with currentTime 0 and outputLatency
0. A desktop identity has a sound card: there the context is `running` within
milliseconds. cubeb-null-backend.patch adds a backend that plays to nothing as
cubeb's last choice, so the stream opens and the context runs as on a machine
with a device, and audio-context-spoofing.patch's drawn latency answers.

The browser is launched with PULSE_SERVER naming a socket that does not exist,
so the check is the same whether or not the machine it runs on has a sound
server. Checks, as a Windows identity: a context made before any user
activation is suspended and reads outputLatency 0; after a click, contexts at
44100 and 48000 Hz are running, currentTime advances at the wall clock's rate,
outputLatency is the drawn latency in stock's frames and baseLatency is 0.

    python browser/tests/playwright/patches/audio-runs-without-sound-server.py
"""

import math
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

RATES = [44100, 48000]
LATENCY = 0.022
PROBE = """async ({rates, phase}) => {
  const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
  if (phase === 'blocked') {
    const c = new AudioContext({ sampleRate: 22050 });
    await sleep(250);
    const r = { state: c.state, outputLatency: c.outputLatency };
    await c.close();
    return r;
  }
  const out = {};
  for (const rate of rates) {
    const c = new AudioContext({ sampleRate: rate });
    for (let i = 0; i < 400 && c.state !== 'running'; i++) await sleep(5);
    const r = { state: c.state };
    const t0 = c.currentTime, w0 = performance.now();
    await sleep(500);
    r.advanced = c.currentTime - t0;
    r.elapsed = (performance.now() - w0) / 1000;
    r.outputLatency = c.outputLatency;
    r.baseLatency = c.baseLatency;
    await c.close();
    out[rate] = r;
  }
  return out;
}"""


def cubeb_rounded(seconds: float, rate: int) -> float:
    # As audio-output-latency-drawn.py: floor(latency * rate) frames, at least 128, in 100 ns ticks.
    frames = max(math.floor(seconds * rate + 1e-9), 128)
    return math.trunc(frames / rate * 1000.0 * 10000.0) / 1e7


def main() -> int:
    from camoufox.sync_api import Camoufox

    errors = []
    config = {"AudioContext:outputLatency": LATENCY, "AudioContext:outputLatencyDelayMs": 40.0}
    with tempfile.TemporaryDirectory() as empty:
        # No sound server: PULSE_SERVER set also stops libpulse starting one.
        env = {**os.environ, "PULSE_SERVER": f"unix:{empty}/no-pulse", "XDG_RUNTIME_DIR": empty}
        with Camoufox(executable_path=str(resolve_binary()), headless=True, os="windows", config=config, env=env,
                      i_know_what_im_doing=True) as browser:
            page = browser.new_page()
            page.set_content('<button id="go">go</button>')
            blocked = page.evaluate(PROBE, {"rates": RATES, "phase": "blocked"})
            # A trusted click is the user activation autoplay waits for.
            page.click("#go")
            got = page.evaluate(PROBE, {"rates": RATES, "phase": "allowed"})
    print(f"  blocked {blocked}, allowed {got}")
    if blocked["state"] != "suspended" or blocked["outputLatency"] != 0:
        errors.append(f"a context made before any user activation is {blocked['state']} with outputLatency "
                      f"{blocked['outputLatency']}; the autoplay policy holds it suspended, reading 0")
    for rate in RATES:
        r = got[str(rate)]
        if r["state"] != "running":
            errors.append(f"{rate} Hz: the context is {r['state']} 2 s after a user click on a host with no sound "
                          "server; on a machine with a sound card it is running")
            continue
        if not 0.8 * r["elapsed"] < r["advanced"] < 1.2 * r["elapsed"]:
            errors.append(f"{rate} Hz: currentTime advanced {r['advanced']:.3f} s in {r['elapsed']:.3f} s; a device "
                          "plays at the wall clock's rate")
        want = cubeb_rounded(LATENCY, rate)
        if abs(r["outputLatency"] - want) > 1e-12:
            errors.append(f"{rate} Hz: outputLatency is {r['outputLatency']} while the context runs, want {want}")
        if r["baseLatency"] != 0:
            errors.append(f"{rate} Hz: baseLatency {r['baseLatency']}, stock's is 0")
    for e in errors:
        print(f"FAIL: {e}")
    if errors:
        return 1
    print("PASS: with no sound server, a context a click allows runs, its clock advances and it reports the drawn latency")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
