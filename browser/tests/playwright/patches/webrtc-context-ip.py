"""
Verify each context's WebRTC IP is the one its ICE candidates carry.

NewContext(webrtc_ip=...) hands the IP to the browser through setWebRTCIPv4,
which stores it for that context. PeerConnectionImpl only ever read the
launch-level `webrtc:ipv4`/`webrtc:ipv6` keys, so a per-context IP was stored
and never used: a context created behind its own proxy reported no public
address, or the launch-level one.

The probe gathers against the guard's own STUN server on this host's address
(Firefox ignores a loopback STUN server). It maps every request to a TEST-NET
address, as a NAT would, so a real srflx forms without the internet and the
browser rewrites its address to the context's IP. A srflx comes only from a STUN
answer: Camoufox no longer makes one up when none came (LEAKS 616).

What PASS means:
    * two contexts in one browser each see their own IP in a srflx candidate;
    * the launch-level `webrtc:ipv4` still reaches a plain new_page();
    * every candidate arrives before end-of-candidates.

    python browser/tests/playwright/patches/webrtc-context-ip.py
"""

import asyncio
import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

LAUNCH_IP = "203.0.113.9"
CONTEXT_IPS = ("203.0.113.7", "203.0.113.8")

PROBE = """async (stun) => {
  const pc = new RTCPeerConnection({iceServers: [{urls: stun}]});
  const srflx = [];
  let ended = false;
  const done = new Promise(resolve => {
    setTimeout(resolve, 30000);
    pc.onicecandidate = e => {
      if (!e.candidate) {
        ended = true;
        // Long past any STUN round trip, so a straggler would show up here.
        return setTimeout(resolve, 1000);
      }
      if (ended) srflx.push('after end-of-candidates: ' + e.candidate.candidate);
      const m = e.candidate.candidate.match(/ (\\S+) \\d+ typ srflx/);
      if (m && !ended) srflx.push(m[1]);
    };
  });
  pc.createDataChannel('probe');
  await pc.setLocalDescription(await pc.createOffer());
  await done;
  pc.close();
  return srflx;
}"""


COOKIE = 0x2112A442
# What the guard's STUN server says it saw: a NAT's public side, never this host.
MAPPED = "198.51.100.77"


def host_address() -> str | None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # no datagram is sent; this picks the route's source
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


def answer_stun(sock: socket.socket) -> None:
    """Answers Binding requests with MAPPED and the request's own source port."""
    mapped = struct.unpack("!I", socket.inet_aton(MAPPED))[0]
    while True:
        try:
            data, src = sock.recvfrom(2048)
        except OSError:
            return
        if len(data) < 20 or struct.unpack("!HHI", data[:8])[0::2] != (1, COOKIE):
            continue
        attr = struct.pack("!HHBBHI", 0x0020, 8, 0, 1, src[1] ^ (COOKIE >> 16), mapped ^ COOKIE)
        sock.sendto(struct.pack("!HHI", 0x0101, len(attr), COOKIE) + data[8:20] + attr, src)


async def srflx_ips(page, stun: str) -> list:
    await page.goto("about:blank")
    return await page.evaluate(PROBE, stun)


async def main() -> int:
    from camoufox.async_api import AsyncCamoufox, AsyncNewContext

    lan = host_address()
    if lan is None:
        print("FAIL: this host has no non-loopback IPv4 address for the STUN server")
        return 1
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind((lan, 0))
    threading.Thread(target=answer_stun, args=(server,), daemon=True).start()
    stun = f"stun:{lan}:{server.getsockname()[1]}"

    checks = []
    async with AsyncCamoufox(headless=True, os="linux", config={"webrtc:ipv4": LAUNCH_IP},
                             i_know_what_im_doing=True,
                             executable_path=str(resolve_binary())) as browser:
        checks.append(("launch-level", LAUNCH_IP, await srflx_ips(await browser.new_page(), stun)))
        for ip in CONTEXT_IPS:
            context = await AsyncNewContext(browser, os="linux", webrtc_ip=ip)
            checks.append((f"context {ip}", ip, await srflx_ips(await context.new_page(), stun)))
    server.close()

    passed = True
    for label, expected, got in checks:
        if got == [expected]:
            print(f"  PASS {label}: srflx {got}")
        else:
            passed = False
            print(f"  FAIL {label}: srflx {got}, expected [{expected!r}]")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
