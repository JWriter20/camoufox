r"""
Page-observable differences from stock Firefox found in review, each checked as
an invariant stock Firefox 152 holds:

  canvas-system-font  ctx.font = 'caption' (and the other CSS2 system-font
                      keywords) reads back the keyword, not '10px sans-serif'
  webgl-state         pixelStorei / hint / {alpha:false} read back what the page
                      set; UNMASKED_RENDERER_WEBGL without the extension is null
                      with INVALID_ENUM
  webrtc-no-servers   with webrtc:ipv4 spoofed, a connection with no iceServers
                      gathers no srflx, and getStats() ids carry no "camou"
  gum-fake            getUserMedia({audio: true, fake: true}) resolves without a
                      prompt
  storage-partition   a cross-site iframe's document.hasStorageAccess() is false
  wheel-notches       page.mouse.wheel(0, 300) arrives as 3 events with
                      wheelDeltaY a multiple of 120
  query-cost          matchMedia('(color: 8)') and navigator.hardwareConcurrency
                      cost about what matchMedia('(min-width: 1px)') and
                      navigator.userAgent do (no sync IPC per read)
  timezone-cost       with a launch-level timezone, local Date getters cost
                      about what UTC getters do
  timezone-relaunch   a persistent profile relaunched with another timezone
                      reports the new one, in the page and in a worker

    python tests/patches/stock-parity-probes.py
"""

import http.server
import json
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

FRAME = b"""<!doctype html><script>
document.hasStorageAccess().then(v => parent.postMessage({hasStorageAccess: v}, '*'));
</script>"""
PAGE = b"""<!doctype html><body style="height:5000px"><script>
const wheel = [];
addEventListener('wheel', e => {
  wheel.push({dy: e.deltaY, mode: e.deltaMode, wd: e.wheelDeltaY});
  document.body.dataset.wheel = JSON.stringify(wheel);
});
(async () => {
  let out;
  try { out = await (%PROBES%)(location.port); } catch (e) { out = {error: String(e)}; }
  document.body.dataset.result = JSON.stringify(out);
})();
</script></body>"""


def serve():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = FRAME if self.path.startswith("/frame") else PAGE
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


PROBES = """async (port) => {
  const out = {};
  const c2d = document.createElement('canvas').getContext('2d');
  out.canvasFonts = {};
  for (const k of ['caption', 'icon', 'menu', 'message-box', 'small-caption', 'status-bar']) {
    c2d.font = '10px sans-serif';
    c2d.font = k;
    out.canvasFonts[k] = c2d.font;
  }

  const gl = document.createElement('canvas').getContext('webgl', {alpha: false, stencil: true, depth: false});
  if (gl) {
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    out.flipY = gl.getParameter(gl.UNPACK_FLIP_Y_WEBGL);
    out.alphaBits = gl.getParameter(gl.ALPHA_BITS);
    out.depthBits = gl.getParameter(gl.DEPTH_BITS);
    gl.getError();
    out.unmaskedNoExt = gl.getParameter(0x9246);
    out.unmaskedNoExtError = gl.getError();
  }
  const gl2 = document.createElement('canvas').getContext('webgl2');
  if (gl2) {
    gl2.hint(0x8B8B, gl2.NICEST);
    out.derivativeHint = gl2.getParameter(0x8B8B);
  }

  const pc = new RTCPeerConnection();
  const cands = [];
  pc.onicecandidate = e => { if (e.candidate) cands.push(e.candidate.candidate); };
  pc.createDataChannel('x');
  await pc.setLocalDescription();
  await new Promise(r => {
    const t = setTimeout(r, 6000);
    pc.addEventListener('icegatheringstatechange', () => { if (pc.iceGatheringState === 'complete') { clearTimeout(t); setTimeout(r, 300); } });
  });
  out.candidates = cands;
  out.statIds = [];
  (await pc.getStats()).forEach((v, k) => out.statIds.push(String(k)));
  pc.close();

  try {
    const r = await Promise.race([
      navigator.mediaDevices.getUserMedia({audio: true, fake: true}).then(s => { s.getTracks().forEach(t => t.stop()); return 'resolved'; }),
      new Promise(res => setTimeout(() => res('pending'), 3000)),
    ]);
    out.gumFake = r;
  } catch (e) {
    out.gumFake = 'rejected ' + e.name;
  }

  out.storageAccess = await new Promise(r => {
    addEventListener('message', e => r(e.data.hasStorageAccess), {once: true});
    const f = document.createElement('iframe');
    f.src = `http://127.0.0.1:${port}/frame`;
    document.body.appendChild(f);
    setTimeout(() => r('timeout'), 5000);
  });

  const time = (fn) => { const t = performance.now(); for (let i = 0; i < 20000; i++) fn(); return performance.now() - t; };
  out.costColor = time(() => matchMedia('(color: 8)').matches);
  out.costMinWidth = time(() => matchMedia('(min-width: 1px)').matches);
  out.costHwc = time(() => navigator.hardwareConcurrency);
  out.costUA = time(() => navigator.userAgent);
  // Fresh Date objects: a Date caches its local-time fields after one read.
  let n = 0;
  out.costLocalDate = time(() => new Date(1.6e12 + (n++) * 3.6e6).getHours());
  out.costUTCDate = time(() => new Date(1.6e12 + (n++) * 3.6e6).getUTCHours());
  out.timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return out;
}"""


PAGE = PAGE.replace(b"%PROBES%", PROBES.encode())


def run_probes(binary, port):
    from camoufox.sync_api import Camoufox

    config = {"webrtc:ipv4": "203.0.113.7", "timezone": "Asia/Tokyo"}
    with Camoufox(headless=True, executable_path=str(binary), config=config, i_know_what_im_doing=True) as b:
        page = b.new_page()
        page.goto(f"http://localhost:{port}/")
        page.wait_for_function("() => document.body.dataset.result", timeout=60000)
        out = json.loads(page.evaluate("() => document.body.dataset.result"))
        if "error" in out:
            raise RuntimeError(out["error"])
        page.mouse.move(200, 200)
        page.mouse.wheel(0, 300)
        page.wait_for_timeout(800)
        out["wheel"] = json.loads(page.evaluate("() => document.body.dataset.wheel || '[]'"))
        return out


def relaunch_timezones(binary, port):
    from camoufox.sync_api import Camoufox

    seen = []
    with tempfile.TemporaryDirectory(prefix="parity-profile-") as profile:
        for tz in ("Asia/Tokyo", "America/Chicago"):
            with Camoufox(headless=True, executable_path=str(binary), persistent_context=True,
                          user_data_dir=profile, config={"timezone": tz}, i_know_what_im_doing=True) as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(f"http://localhost:{port}/")
                # A worker reads the per-context store, where a stale value surfaced.
                seen.append(page.evaluate("""async () => {
                  const w = new Worker(URL.createObjectURL(new Blob(
                    ["postMessage(Intl.DateTimeFormat().resolvedOptions().timeZone)"],
                    {type: 'text/javascript'})));
                  const worker = await new Promise(r => { w.onmessage = e => r(e.data); setTimeout(() => r('timeout'), 5000); });
                  return [Intl.DateTimeFormat().resolvedOptions().timeZone, worker];
                }"""))
                # prefs.js is written asynchronously; give a persisted value time to land.
                page.wait_for_timeout(3000)
    return seen


def main() -> int:
    binary = resolve_binary()
    server, port = serve()
    try:
        out = run_probes(binary, port)
        relaunch = relaunch_timezones(binary, port)
    finally:
        server.shutdown()
    print(json.dumps({**out, "relaunch": relaunch}, indent=1, default=str))

    failures = []
    for k, v in out["canvasFonts"].items():
        if v != k:
            failures.append(f"canvas-system-font: ctx.font = '{k}' read back {v!r}")
    if "flipY" in out:
        if out["flipY"] is not True:
            failures.append(f"webgl-state: UNPACK_FLIP_Y_WEBGL after pixelStorei(true) = {out['flipY']}")
        if out["alphaBits"] != 0:
            failures.append(f"webgl-state: ALPHA_BITS on an {{alpha: false}} context = {out['alphaBits']}")
        if out["unmaskedNoExt"] is not None or out["unmaskedNoExtError"] != 0x0500:
            failures.append(f"webgl-state: UNMASKED_RENDERER_WEBGL without the extension = "
                            f"{out['unmaskedNoExt']!r}, error {out['unmaskedNoExtError']}")
    else:
        print("note: no WebGL context on this host; webgl-state not checked")
    if "derivativeHint" in out and out["derivativeHint"] != 0x1102:
        failures.append(f"webgl-state: FRAGMENT_SHADER_DERIVATIVE_HINT after hint(NICEST) = {out['derivativeHint']}")
    if not out["candidates"]:
        failures.append("webrtc-no-servers: no candidates at all -- check is vacuous")
    if any(" typ srflx " in c for c in out["candidates"]):
        failures.append(f"webrtc-no-servers: srflx gathered with no iceServers: {out['candidates']}")
    if any("camou" in i for i in out["statIds"]):
        failures.append(f"webrtc-no-servers: getStats id names camoufox: {out['statIds']}")
    if out["gumFake"] != "resolved":
        failures.append(f"gum-fake: getUserMedia({{fake: true}}) {out['gumFake']}")
    if out["storageAccess"] is not False:
        failures.append(f"storage-partition: cross-site iframe hasStorageAccess() = {out['storageAccess']}")
    wheel = out["wheel"]
    if len(wheel) != 3 or any(e["wd"] % 120 for e in wheel):
        failures.append(f"wheel-notches: wheel(0, 300) gave {wheel}")
    if out["costColor"] > 5 * out["costMinWidth"] + 15:
        failures.append(f"query-cost: (color) {out['costColor']:.0f} ms vs (min-width) {out['costMinWidth']:.0f} ms")
    if out["costHwc"] > 5 * out["costUA"] + 15:
        failures.append(f"query-cost: hardwareConcurrency {out['costHwc']:.0f} ms vs userAgent {out['costUA']:.0f} ms")
    if out["timeZone"] != "Asia/Tokyo":
        failures.append(f"timezone: launch-level zone not applied ({out['timeZone']}) -- timezone-cost is vacuous")
    if out["costLocalDate"] > 5 * out["costUTCDate"] + 15:
        failures.append(f"timezone-cost: getHours {out['costLocalDate']:.0f} ms vs getUTCHours {out['costUTCDate']:.0f} ms")
    if relaunch != [["Asia/Tokyo"] * 2, ["America/Chicago"] * 2]:
        failures.append(f"timezone-relaunch: persistent profile reported {relaunch}")

    print()
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: page-observable behaviour matches stock Firefox on every probe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
