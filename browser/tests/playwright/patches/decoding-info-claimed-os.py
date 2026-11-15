"""
A Windows identity's navigator.mediaCapabilities.decodingInfo() answers as
stock Firefox 158 on Windows 11 does, agrees with canPlayType(), and gives an
encrypted query the same decoder.

With media:spoof_codecs (set for a Windows identity on a Linux host),
canPlayType() and isTypeSupported() answer as stock Firefox on Windows does.
decodingInfo() asked the host instead, so with no system decoder (as in a
container) it reported H.264 and HEVC unsupported while canPlayType() said
"probably". media-codec-spoofing.patch makes it answer as the recording does.

assets/decoding-info-windows.json is stock Firefox 158.0 on Windows 11
(provenance in the file): the cfparity media.capabilities probe, H.264, HEVC,
VP9 and AV1 at 720p30, 1080p60 and 2160p30 and Opus, every one
"supported,smooth,powerEfficient" = true,true,true. The page asks the same
queries (type "file", 8 Mb/s) and must get the same answers. Each type
decodingInfo() calls supported must not be "" to canPlayType(). A ClearKey
query for H.264 1080p must answer as the plain one does.

The host's decoders are switched off (FFmpeg, VA-API, hardware decoding) so
the check does not depend on what the runner has installed.

    python browser/tests/playwright/patches/decoding-info-claimed-os.py
"""

import functools
import http.server
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

ASSETS = Path(__file__).resolve().parent / "assets"
NO_HOST_DECODERS = {
    "media.ffmpeg.enabled": False,
    "media.ffmpeg.vaapi.enabled": False,
    "media.hardware-video-decoding.enabled": False,
    "media.eme.enabled": True,
}
# The probe's own queries (cfparity/web/probes/media.js, media.capabilities).
QUERY = """async () => {
  const el = document.createElement('video');
  const vids = [['video/mp4; codecs="avc1.640028"', 'h264'], ['video/webm; codecs="vp09.00.10.08"', 'vp9'],
    ['video/mp4; codecs="av01.0.08M.08"', 'av1'], ['video/mp4; codecs="hvc1.1.6.L120.B0"', 'hevc']];
  const ask = async (cfg) => {
    const r = await navigator.mediaCapabilities.decodingInfo(cfg);
    return [r.supported, r.smooth, r.powerEfficient].join(',');
  };
  const out = {decodingInfo: {}, canPlayType: {}};
  for (const [ct, n] of vids) {
    out.canPlayType[ct] = el.canPlayType(ct);
    for (const [w, h, fps] of [[1280, 720, 30], [1920, 1080, 60], [3840, 2160, 30]]) {
      out.decodingInfo[`decode:${n}:${h}p${fps}`] = await ask(
        {type: 'file', video: {contentType: ct, width: w, height: h, bitrate: 8e6, framerate: fps}});
    }
  }
  const opus = 'audio/webm; codecs="opus"';
  out.canPlayType[opus] = el.canPlayType(opus);
  out.decodingInfo['decode:audio:opus'] = await ask(
    {type: 'file', audio: {contentType: opus, channels: '2', bitrate: 128000, samplerate: 48000}});
  const h264 = {contentType: vids[0][0], width: 1920, height: 1080, bitrate: 8e6, framerate: 60};
  out.plain = await ask({type: 'media-source', video: h264});
  out.clearkey = await ask({type: 'media-source', video: h264, keySystemConfiguration: {keySystem: 'org.w3.clearkey'}});
  return out;
}"""


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def main() -> int:
    from camoufox.sync_api import Camoufox

    recorded = json.loads((ASSETS / "decoding-info-windows.json").read_text(encoding="utf-8"))["decodingInfo"]
    binary = resolve_binary()
    site = tempfile.mkdtemp()
    (Path(site) / "i.html").write_text("<!doctype html><title>decodingInfo</title>", encoding="utf-8")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=site))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 127.0.0.1 is a secure context, which EME requires.
        with Camoufox(os="windows", headless=True, executable_path=str(binary),
                      firefox_user_prefs=NO_HOST_DECODERS, i_know_what_im_doing=True) as browser:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{srv.server_address[1]}/i.html")
            got = page.evaluate(QUERY)
    finally:
        srv.shutdown()

    failures = []
    for key, want in recorded.items():
        have = got["decodingInfo"].get(key)
        print(f"  {'ok  ' if have == want else 'FAIL'} {key}: {have} (Windows 11 {want})")
        if have != want:
            failures.append(f"{key}: {have}, stock Windows {want}")
    for ct, answer in got["canPlayType"].items():
        if answer == "":
            failures.append(f"canPlayType({ct}) is '' while decodingInfo() calls it supported")
    print(f"  ClearKey H.264 1080p: {got['clearkey']} (plain {got['plain']})")
    if got["clearkey"] != got["plain"]:
        failures.append(f"ClearKey {got['clearkey']} differs from the plain answer {got['plain']}")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("PASS: decodingInfo() answers as stock Firefox 158 on Windows 11, encrypted or not.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
