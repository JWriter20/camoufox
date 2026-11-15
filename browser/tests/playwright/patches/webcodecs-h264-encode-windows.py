"""
A Windows identity encodes H.264 with WebCodecs, and says so only because it can.

Stock Firefox 158 on Windows encodes avc1 through Media Foundation, so
VideoEncoder.isConfigSupported({codec: 'avc1.42E01E', ...}) is true on every
Windows machine (cfparity media.codecs on win-i9). Linux Firefox encodes H.264
only through the system FFmpeg (libavcodec with libx264, FFmpegRuntimeLinker):
on a host without it, stock Linux 158 and Camoufox both answer false and fail
to encode (measured in the cross-OS gate's Linux-half image, which had no
libavcodec), so a Windows identity there answered what no Windows machine does.
The fix is the host's: the gate image installs libavcodec60, and the launcher
warns (no_h264_encoder) when a Windows identity runs on a Linux host without
it. Support is never faked: isConfigSupported() must agree with a real encode.

Checks, for a Windows identity: libavcodec is on this host (else FAIL: the
host cannot give a Windows identity its H.264 encoder); avc1.42E01E,
avc1.640028, vp8, vp09 and av01 are each reported supported and encode five
1280x720 frames into five chunks, the avc1 ones with an avcC description.

    python browser/tests/playwright/patches/webcodecs-h264-encode-windows.py
"""

import ctypes.util
import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

CODECS = ["avc1.42E01E", "avc1.640028", "vp8", "vp09.00.10.08", "av01.0.05M.08"]
PROBE = """async (codecs) => {
  const out = {};
  for (const codec of codecs) {
    const cfg = { codec, width: 1280, height: 720, bitrate: 2e6 };
    const r = { supported: (await VideoEncoder.isConfigSupported(cfg)).supported, chunks: 0, desc: null, err: null };
    try {
      const enc = new VideoEncoder({
        output: (c, m) => { r.chunks++; if (m && m.decoderConfig && m.decoderConfig.description) r.desc = m.decoderConfig.description.byteLength; },
        error: (e) => { r.err = String(e); },
      });
      enc.configure(cfg);
      const cv = new OffscreenCanvas(1280, 720);
      const g = cv.getContext('2d');
      for (let i = 0; i < 5; i++) {
        g.fillStyle = `rgb(${i * 40},100,200)`;
        g.fillRect(0, 0, 1280, 720);
        const f = new VideoFrame(cv, { timestamp: i * 33333 });
        enc.encode(f, { keyFrame: i === 0 });
        f.close();
      }
      await Promise.race([enc.flush(), new Promise((_, rej) => setTimeout(() => rej(new Error('flush timeout')), 8000))]);
      enc.close();
    } catch (e) { r.err = r.err || String(e); }
    out[codec] = r;
  }
  return out;
}"""


class Page(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<!doctype html><title>webcodecs</title>")


def main() -> int:
    from camoufox.sync_api import Camoufox

    errors = []
    if not ctypes.util.find_library("avcodec"):
        errors.append("this host has no system libavcodec: a Windows identity here cannot encode H.264 "
                      "(install libavcodec60 or the release's libavcodecNN)")
    # WebCodecs is [SecureContext]: a top-level about:blank (null principal,
    # no precursor) is not one in stock Firefox either, so VideoEncoder is not
    # defined there. A page from http://127.0.0.1 is potentially trustworthy.
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with Camoufox(executable_path=str(resolve_binary()), headless=True, os="windows",
                      i_know_what_im_doing=True) as browser:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
            if not page.evaluate("isSecureContext && typeof VideoEncoder === 'function'"):
                print("FAIL: the guard page is not a secure context with VideoEncoder")
                return 1
            got = page.evaluate(PROBE, CODECS)
    finally:
        server.shutdown()
    for codec, r in got.items():
        print(f"  {codec}: {r}")
        works = r["chunks"] == 5 and not r["err"]
        if r["supported"] != works:
            errors.append(f"{codec}: isConfigSupported says {r['supported']} but a real encode "
                          f"{'works' if works else 'fails'} ({r['chunks']} chunks, {r['err']})")
        elif not works:
            errors.append(f"{codec}: unsupported, stock Firefox on Windows encodes it")
        if codec.startswith("avc1") and works and not r["desc"]:
            errors.append(f"{codec}: no avcC description, stock gives one")
    for e in errors:
        print(f"FAIL: {e}")
    if errors:
        return 1
    print("PASS: a Windows identity reports and really performs H.264, VP8, VP9 and AV1 encoding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
