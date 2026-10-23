"""
A Windows identity's navigator.mediaCapabilities.decodingInfo() answers for
the claimed OS's decoders, consistently with canPlayType(), encrypted or not.

With media:spoof_codecs (set for a Windows identity on a Linux host),
canPlayType() and isTypeSupported() answer as stock Firefox on Windows does.
decodingInfo() asked the host instead, so with no system decoder (as in a
container) it reported H.264, HEVC and AAC unsupported while canPlayType()
said "probably". media-codec-spoofing.patch makes it report every supported
codec smooth, audio power-efficient, and video power-efficient where the
claimed GPU decodes it (media:hwCodecs, H.264 on a hardware GPU) or the frame
is small (<= 640x480, Gecko's own rule); an encrypted (ClearKey) query gets the
same answer as the plain one.

The host's decoders are switched off (FFmpeg, VA-API, hardware decoding) so the
check does not depend on what the runner has installed; the claimed GPU is a
GeForce GTX 980, a hardware GPU, so media:hwCodecs is ["video/avc"].

    python browser/tests/playwright/patches/decoding-info-claimed-os.py
"""

import functools
import http.server
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

GPU = ("Google Inc. (NVIDIA)",
       "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or similar")
NO_HOST_DECODERS = {
    "media.ffmpeg.enabled": False,
    "media.ffmpeg.vaapi.enabled": False,
    "media.hardware-video-decoding.enabled": False,
    "media.eme.enabled": True,
}
VIDEO = {
    "H.264": 'video/mp4; codecs="avc1.42E01E"',
    "HEVC": 'video/mp4; codecs="hev1.1.6.L93.B0"',
    "VP9": 'video/webm; codecs="vp9"',
    "AV1": 'video/mp4; codecs="av01.0.05M.08"',
}
AUDIO = {
    "AAC": 'audio/mp4; codecs="mp4a.40.2"',
    "Opus": 'audio/webm; codecs="opus"',
}
QUERY = """async ([video, audio]) => {
  const el = document.createElement('video');
  const ask = async (cfg) => {
    const r = await navigator.mediaCapabilities.decodingInfo(cfg);
    return [r.supported, r.smooth, r.powerEfficient];
  };
  const v = (type, width, height) => ({contentType: type, width, height, bitrate: 2000000, framerate: 30});
  const out = {};
  for (const [name, type] of Object.entries(video)) {
    out[name] = {
      canPlayType: el.canPlayType(type),
      big: await ask({type: 'media-source', video: v(type, 1920, 1080)}),
      small: await ask({type: 'media-source', video: v(type, 640, 480)}),
    };
  }
  for (const [name, type] of Object.entries(audio)) {
    out[name] = {
      canPlayType: el.canPlayType(type),
      big: await ask({type: 'media-source', audio: {contentType: type, channels: '2', bitrate: 128000, samplerate: 48000}}),
    };
  }
  const enc = await navigator.mediaCapabilities.decodingInfo({
    type: 'media-source', video: v(video['H.264'], 1920, 1080),
    keySystemConfiguration: {keySystem: 'org.w3.clearkey'},
  });
  out.clearkey = [enc.supported, enc.smooth, enc.powerEfficient];
  return out;
}"""


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def main() -> int:
    from camoufox.sync_api import Camoufox

    binary = resolve_binary()
    site = tempfile.mkdtemp()
    (Path(site) / "i.html").write_text("<!doctype html><title>decodingInfo</title>", encoding="utf-8")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=site))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 127.0.0.1 is a secure context, which EME requires.
        with Camoufox(os="windows", headless=True, executable_path=str(binary),
                      webgl_config=GPU, firefox_user_prefs=NO_HOST_DECODERS,
                      i_know_what_im_doing=True) as browser:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{srv.server_address[1]}/i.html")
            got = page.evaluate(QUERY, [VIDEO, AUDIO])
    finally:
        srv.shutdown()

    failures = []
    for name, r in got.items():
        if name == "clearkey":
            continue
        print(f"  {name}: canPlayType {r['canPlayType']!r}, decodingInfo {r['big']}"
              + (f", 640x480 {r['small']}" if "small" in r else ""))
        if r["canPlayType"] != "probably":
            failures.append(f"{name}: canPlayType {r['canPlayType']!r}, stock Windows says 'probably'")
        want = [True, True, name == "H.264" or name in AUDIO]
        if r["big"] != want:
            failures.append(f"{name}: decodingInfo {r['big']}, want {want}")
        if "small" in r and r["small"] != [True, True, True]:
            failures.append(f"{name} 640x480: decodingInfo {r['small']}, want [True, True, True]")
    print(f"  ClearKey H.264: {got['clearkey']}")
    if got["clearkey"] != got["H.264"]["big"]:
        failures.append(f"ClearKey H.264 {got['clearkey']} differs from the plain answer {got['H.264']['big']}")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("PASS: decodingInfo() answers for the claimed OS's decoders, encrypted or not.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
