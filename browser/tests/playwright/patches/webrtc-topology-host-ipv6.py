r"""
WebRTC host candidates follow the identity's interfaces (webrtc:hostipv6).

A dual-stack machine gathers two host addresses on its primary interface, IPv6
first: two UDP and two TCP host candidates, local preference 0x7F (IPv6) and
0x7E (IPv4), and an in-browser loopback connection succeeds. On a host without
IPv6 the build gathered one of each at 0x7F whatever the identity was. With
webrtc:hostipv6 the identity gets the second address
(webrtc-topology-host-ipv6.patch); without it the host's own shape stands.

The page comes from a local HTTP server, as cfparity's do, so nICEr looks up
the default IPv4 address and takes the IPv6 one on the same interface (a page
with no server address looks up both defaults, and a VPN's IPv6 route can
answer). A host whose primary interface has IPv6 of its own gathers it without
the flag and is reported as not judged (exit 2).

    python browser/tests/playwright/patches/webrtc-topology-host-ipv6.py
"""

import asyncio
import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

# Host candidates (local preference) and whether two connections in the page reach "connected".
PROBE = """async () => {
  const gather = async (pc) => {
    const out = [];
    const done = new Promise((r) => { pc.onicecandidate = (e) => { if (e.candidate) out.push(e.candidate); else r(); }; });
    await pc.setLocalDescription(await pc.createOffer());
    await Promise.race([done, new Promise((r) => setTimeout(r, 10000))]);
    return out;
  };
  const a = new RTCPeerConnection(), b = new RTCPeerConnection();
  a.createDataChannel('x');
  const hosts = (await gather(a)).filter((c) => c.type === 'host')
    .map((c) => `${c.protocol} ${((c.priority >> 8) & 0xffff).toString(16)}`).sort();
  b.onicecandidate = (e) => e.candidate && a.addIceCandidate(e.candidate);
  await b.setRemoteDescription(a.localDescription);
  await b.setLocalDescription(await b.createAnswer());
  await a.setRemoteDescription(b.localDescription);
  const state = await new Promise((r) => {
    const t = setTimeout(() => r(a.connectionState), 10000);
    a.onconnectionstatechange = () => { if (['connected', 'failed'].includes(a.connectionState)) { clearTimeout(t); r(a.connectionState); } };
  });
  a.close(); b.close();
  return { hosts, state };
}"""
EXPECTED = {
    True: ["tcp 7ec0", "tcp 7fc0", "udp 7e00", "udp 7f00"],
    False: ["tcp 7fc0", "udp 7f00"],
}


async def probe(binary, flag):
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(headless=True, executable_path=str(binary),
                             config={"webrtc:hostipv6": flag}, block_webrtc=False,
                             i_know_what_im_doing=True) as browser:
        page = await browser.new_page()
        await page.goto(f"http://127.0.0.1:{SERVER.server_address[1]}/")
        return await page.evaluate(PROBE)


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<body></body>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


SERVER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)


def main():
    binary = resolve_binary()
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()
    failures = []
    for flag, hosts in sorted(EXPECTED.items()):  # False first: is the host fit to judge?
        got = asyncio.run(probe(binary, flag))
        if not flag and got["hosts"] != hosts:
            print(f"NOT JUDGED: this host gathers {got['hosts']} without the flag (an IPv6 route of its own)")
            sys.exit(2)
        print(f"webrtc:hostipv6={flag}: {got}")
        if got["hosts"] != hosts:
            failures.append(f"hostipv6={flag}: host candidates {got['hosts']}, expected {hosts}")
        if got["state"] != "connected":
            failures.append(f"hostipv6={flag}: loopback connection {got['state']}, stock connected")
    for f in failures:
        print("FAIL", f)
    print("FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
