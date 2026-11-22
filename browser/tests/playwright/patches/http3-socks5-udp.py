"""
Verify HTTP/3 and WebTransport go through a SOCKS5 proxy's UDP ASSOCIATE relay.

A Firefox at the proxy's exit speaks QUIC to an origin that offers it. Behind a
proxy, Camoufox used to speak TCP only: no HTTP/3 where stock negotiates it, and
no WebTransport at all. zz-http3-socks5-udp.patch sends each QUIC connection
through a UDP ASSOCIATE relay (RFC 1928 §7) of its own on the SOCKS5 proxy the
request uses, when that proxy is given addresses (docs/proxy-dns.md).

The guard runs its own origin, HTTP/1.1 over TLS with an Alt-Svc header and
HTTP/3 on the same port, and its own SOCKS5 proxies. The origin is reached at
this host's address on its default route, which a direct page reaches too, so
the same pages are loaded direct for comparison.

What PASS means:
    * behind a SOCKS5 proxy given addresses, the page's requests reach the
      origin over HTTP/3 once Alt-Svc is known, every datagram of them through
      the relay, addressed by IP, and nothing reaches the origin's UDP port
      from anywhere else;
    * the QUIC datagrams the origin receives are the sizes it receives from the
      same browser with no proxy: the relay header is not taken out of them;
    * each QUIC connection has an association of its own, as each has a UDP
      socket of its own without a proxy;
    * a WebTransport session opens through the relay and echoes a datagram;
    * a proxy that refuses UDP ASSOCIATE, and one that drops the association
      once it is in use, leave the page loading over TCP, as a machine whose
      UDP is blocked does;
    * a SOCKS5 proxy given names (a context's own proxy), and an HTTP proxy,
      are sent no QUIC: their requests stay on TCP.

    python browser/tests/playwright/patches/http3-socks5-udp.py
"""

import asyncio
import json
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DatagramReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ProtocolNegotiated

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import Marionette, free_port, marionette_args, marionette_env, own_address, resolve_binary  # noqa: E402

# The browser resolves by DoH alone, which is what makes Juggler give the
# launch proxy addresses (the launcher's proxy_dns='doh'). No name is resolved
# here: the origin is an address.
DOH_ONLY = {"network.trr.mode": 3, "network.trr.uri": "https://doh.invalid/dns-query"}
# Firefox speaks no HTTP/3 to a certificate from an authority a user added.
PREFS = {**DOH_ONLY, "network.http.http3.disable_when_third_party_roots_found": False}

# Fetch until the origin answers over HTTP/3 (the first answer carries Alt-Svc,
# and the mapping is validated in the background), then report every answer.
FETCH = """async (url) => {
  const seen = [];
  for (let i = 0; i < 20; i++) {
    try {
      const body = await (await fetch(url + '?n=' + i + '_' + Math.random(), {cache: 'no-store'})).json();
      seen.push(body.via);
      if (body.via === 'h3' && seen.filter(v => v === 'h3').length >= 2) break;
    } catch (e) {
      seen.push('error: ' + e);
    }
    await new Promise(r => setTimeout(r, 500));
  }
  return seen;
}"""

WEBTRANSPORT = """async (url) => {
  try {
    const wt = new WebTransport(url);
    await wt.ready;
    const writer = wt.datagrams.writable.getWriter();
    await writer.write(new Uint8Array([7, 8, 9]));
    const {value} = await wt.datagrams.readable.getReader().read();
    wt.close();
    // An isolated world may read a page array's length, not its elements.
    return value.byteLength;
  } catch (e) {
    return 'error: ' + e;
  }
}"""


class Origin:
    """An HTTPS origin: HTTP/1.1 with Alt-Svc over TCP, HTTP/3 over UDP."""

    def __init__(self, host: str, cert: str, key: str) -> None:
        self.host, self.port = host, free_port()
        self.url = f"https://{host}:{self.port}/"
        self.h3_peers = []  # the UDP source of every HTTP/3 request
        self.tcp_requests = 0
        self.datagrams = {}  # UDP source -> sizes of the datagrams from it
        self._cert, self._key = cert, key

    async def start(self) -> None:
        origin = self

        class Recorded(QuicConnectionProtocol):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.h3 = None

            def quic_event_received(self, event) -> None:
                if isinstance(event, ProtocolNegotiated):
                    self.h3 = H3Connection(self._quic, enable_webtransport=True)
                if self.h3 is None:
                    return
                for ev in self.h3.handle_event(event):
                    if isinstance(ev, HeadersReceived):
                        headers = dict(ev.headers)
                        origin.h3_peers.append(self._quic._network_paths[0].addr[:2])
                        if headers.get(b":protocol") == b"webtransport":
                            self.h3.send_headers(ev.stream_id, [
                                (b":status", b"200"), (b"sec-webtransport-http3-draft", b"draft02")])
                        else:
                            body = b'{"via": "h3"}'
                            self.h3.send_headers(ev.stream_id, [
                                (b":status", b"200"), (b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())])
                            self.h3.send_data(ev.stream_id, body, end_stream=True)
                    elif isinstance(ev, DatagramReceived):
                        self.h3.send_datagram(ev.stream_id, ev.data)
                self.transmit()

        quic = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN, max_datagram_frame_size=65536)
        quic.load_cert_chain(self._cert, self._key)
        self._quic = await serve(self.host, self.port, configuration=quic, create_protocol=Recorded)
        received = self._quic.datagram_received

        def record(data, addr) -> None:
            origin.datagrams.setdefault(addr[:2], []).append(len(data))
            received(data, addr)

        self._quic.datagram_received = record
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self._cert, self._key)
        self._tcp = await asyncio.start_server(self._serve_tcp, self.host, self.port, ssl=tls)

    async def _serve_tcp(self, reader, writer) -> None:
        try:
            await reader.readline()
            while (await reader.readline()) not in (b"\r\n", b""):
                pass
            self.tcp_requests += 1
            body = b'{"via": "tcp"}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Alt-Svc: h3=\":%d\"; ma=3600\r\nConnection: close\r\n"
                         b"Content-Length: %d\r\n\r\n%s" % (self.port, len(body), body))
            await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    def close(self) -> None:
        self._quic.close()
        self._tcp.close()


async def read_addr(reader: asyncio.StreamReader, atyp: int) -> tuple:
    if atyp == 1:
        host = socket.inet_ntoa(await reader.readexactly(4))
    elif atyp == 3:
        host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
    else:
        host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
    return host, struct.unpack("!H", await reader.readexactly(2))[0]


class Relay(asyncio.DatagramProtocol):
    """One association: the socket the browser sends to, and the one it exits from."""

    def __init__(self, proxy: "Socks5Proxy", control) -> None:
        self.proxy, self.control = proxy, control
        self.client = None
        self.transport = None
        self.exit = None
        self.sent = 0

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.client = addr
        if len(data) < 10 or data[:3] != b"\0\0\0":
            return
        self.proxy.atyps.add(data[3])
        if data[3] != 1:
            return
        dst = (socket.inet_ntoa(data[4:8]), struct.unpack("!H", data[8:10])[0])
        self.sent += 1
        if self.proxy.hang_up_after and self.sent > self.proxy.hang_up_after:
            self.control.close()
            self.close()
            return
        self.exit.sendto(data[10:], dst)

    def from_exit(self, data: bytes, addr: tuple) -> None:
        if self.client and not self.transport.is_closing():
            header = b"\0\0\0\x01" + socket.inet_aton(addr[0]) + struct.pack("!H", addr[1])
            self.transport.sendto(header + data, self.client)

    def close(self) -> None:
        self.transport.close()
        self.exit.close()


class Exit(asyncio.DatagramProtocol):
    def __init__(self, relay: Relay) -> None:
        self.relay = relay

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.relay.from_exit(data, addr)


class Socks5Proxy:
    """A SOCKS5 proxy on loopback. CONNECT is piped to where it names;
    `udp=False` refuses UDP ASSOCIATE, and `hang_up_after` drops an association
    (control connection and relay) after that many datagrams."""

    def __init__(self, host: str, udp: bool = True, hang_up_after: int = 0) -> None:
        self.host, self.udp, self.hang_up_after = host, udp, hang_up_after
        self.connects = []  # (atyp, host, port) of every CONNECT
        self.refused = 0
        self.atyps = set()  # address type of every relayed datagram
        self.relays = []

    async def start(self) -> str:
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        return f"socks5://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def serve(self, reader, writer) -> None:
        try:
            await self.handle(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def handle(self, reader, writer) -> None:
        _, count = await reader.readexactly(2)
        await reader.readexactly(count)
        writer.write(b"\x05\x00")
        _, cmd, _, atyp = await reader.readexactly(4)
        host, port = await read_addr(reader, atyp)
        if cmd == 1:
            self.connects.append((atyp, host, port))
            up_reader, up_writer = await asyncio.open_connection(host, port)
            writer.write(b"\x05\x00\x00\x01" + bytes(6))

            async def pipe(src, dst) -> None:
                try:
                    while data := await src.read(65536):
                        dst.write(data)
                        await dst.drain()
                except (ConnectionError, OSError):
                    pass
                finally:
                    dst.close()

            await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
            return
        if cmd != 3 or not self.udp:
            self.refused += 1
            writer.write(b"\x05\x07\x00\x01" + bytes(6))
            return
        loop = asyncio.get_running_loop()
        _, relay = await loop.create_datagram_endpoint(lambda: Relay(self, writer), local_addr=("127.0.0.1", 0))
        relay.exit, _ = await loop.create_datagram_endpoint(lambda: Exit(relay), local_addr=(self.host, 0))
        self.relays.append(relay)
        port = relay.transport.get_extra_info("sockname")[1]
        writer.write(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", port))
        await writer.drain()
        # The association lives as long as its control connection.
        await reader.read()
        relay.close()

    def exits(self) -> set:
        return {relay.exit.get_extra_info("sockname")[:2] for relay in self.relays}

    def close(self) -> None:
        for relay in self.relays:
            relay.close()
        self.server.close()


async def serve_http_proxy(connects: list, reader, writer) -> None:
    """An HTTP proxy: CONNECT is piped to where it names."""
    try:
        line = (await reader.readline()).decode()
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        host, port = line.split()[1].rsplit(":", 1)
        connects.append(line.strip())
        up_reader, up_writer = await asyncio.open_connection(host, int(port))
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")

        async def pipe(src, dst) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
    except (ConnectionError, OSError, ValueError, IndexError):
        pass
    finally:
        writer.close()


def initial_sizes(sizes: list) -> list:
    """The client's first flight: what a server sees before it has answered."""
    return sizes[:2]


async def main() -> int:
    from camoufox.async_api import AsyncCamoufox

    host = own_address()
    tmp = tempfile.TemporaryDirectory()
    # Firefox keeps an alternative service only when its certificate verified,
    # which an ignored certificate error does not count as: the origin's
    # certificate is issued by an authority each browser is told to trust.
    ca, ca_key, cert, key = (f"{tmp.name}/{name}.pem" for name in ("ca", "ca-key", "cert", "key"))
    for command in (
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key, "-out", ca, "-days", "2",
         "-subj", "/CN=http3-socks5-udp guard", "-addext", "basicConstraints=critical,CA:TRUE"],
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "2",
         "-subj", "/CN=origin", "-CA", ca, "-CAkey", ca_key, "-addext", f"subjectAltName=IP:{host}",
         "-addext", "basicConstraints=critical,CA:FALSE"],
    ):
        subprocess.run(["openssl", *command], check=True, capture_output=True)
    ca_der = "".join(Path(ca).read_text().splitlines()[1:-1])
    trust = ("Cc['@mozilla.org/security/x509certdb;1'].getService(Ci.nsIX509CertDB)"
             f".addCertFromBase64('{ca_der}', 'C,,'); return true;")

    async def origin() -> Origin:
        o = Origin(host, cert, key)
        await o.start()
        return o

    async def launch(proxy=None):
        port = free_port()
        browser = await AsyncCamoufox(
            headless=True, executable_path=str(resolve_binary()), geoip=False, i_know_what_im_doing=True,
            args=marionette_args(), env=marionette_env(),
            firefox_user_prefs={**PREFS, "marionette.port": port}, proxy={"server": proxy} if proxy else None,
        ).__aenter__()

        def trust_authority() -> None:
            marionette = Marionette(port)
            try:
                marionette.js(trust)
            finally:
                marionette.close()

        # Marionette's session needs a window to attach to.
        window = await browser.new_page()
        await asyncio.to_thread(trust_authority)
        await window.close()
        return browser

    async def load(browser, target: Origin, script: str = FETCH, url: str = None, **context):
        ctx = await browser.new_context(**context)
        page = await ctx.new_page()
        await page.goto(target.url)
        result = await page.evaluate(script, url or target.url)
        await ctx.close()
        return result

    checks = []

    # No proxy: what the origin receives from this browser on its own.
    direct_origin = await origin()
    direct = await launch()
    direct_seen = await load(direct, direct_origin)
    await direct.close()
    direct_sources = list(direct_origin.datagrams)
    checks.append(("with no proxy the origin is reached over HTTP/3 (the guard's reference)",
                   "h3" in direct_seen and len(direct_sources) >= 1, direct_seen))

    # A SOCKS5 launch proxy, given addresses.
    relayed_origin, second_origin, wt_origin = await origin(), await origin(), await origin()
    relaying = Socks5Proxy(host)
    browser = await launch(await relaying.start())
    relayed_seen = await load(browser, relayed_origin)
    one_origin_relays = len(relaying.relays)
    await load(browser, second_origin)
    echoed = await load(browser, wt_origin, WEBTRANSPORT, wt_origin.url + "wt")

    # A context's own SOCKS5 proxy is given names, so it carries no QUIC.
    named_origin = await origin()
    naming = Socks5Proxy(host)
    named_seen = await load(browser, named_origin, proxy={"server": await naming.start()})
    await browser.close()

    exits = relaying.exits()
    checks += [
        ("behind a SOCKS5 proxy given addresses the origin is reached over HTTP/3",
         relayed_seen.count("h3") >= 2, relayed_seen),
        ("every QUIC datagram the origin received came from a relay",
         bool(relayed_origin.datagrams) and set(relayed_origin.datagrams) <= exits
         and set(relayed_origin.h3_peers) <= exits, (sorted(relayed_origin.datagrams), sorted(exits))),
        ("datagrams are addressed to the relay by IP", relaying.atyps == {1}, relaying.atyps),
        ("CONNECT is by IP too", {c[0] for c in relaying.connects} == {1}, relaying.connects[:4]),
        ("the client's first flight is the size it is with no proxy",
         initial_sizes(next(iter(relayed_origin.datagrams.values()), []))
         == initial_sizes(direct_origin.datagrams[direct_sources[0]]),
         (list(relayed_origin.datagrams.values())[:1], direct_origin.datagrams[direct_sources[0]][:4])),
        ("one QUIC connection has one association", one_origin_relays == len(relayed_origin.datagrams) >= 1,
         (one_origin_relays, len(relayed_origin.datagrams))),
        ("a second origin's connection has an association of its own",
         bool(second_origin.datagrams) and not set(second_origin.datagrams) & set(relayed_origin.datagrams),
         (sorted(second_origin.datagrams), sorted(relayed_origin.datagrams))),
        ("a WebTransport session opens through the relay and echoes a datagram",
         echoed == 3 and set(wt_origin.datagrams) <= exits, echoed),
        ("a SOCKS5 proxy given names is sent no QUIC",
         named_seen and set(named_seen) == {"tcp"} and not naming.relays and not named_origin.datagrams
         and {c[0] for c in naming.connects} == {3}, (named_seen, naming.connects[:2])),
    ]

    # Proxies that carry no UDP: the page reads a machine whose UDP is blocked.
    for label, kwargs in (("refuses UDP ASSOCIATE", {"udp": False}),
                          ("drops the association in use", {"hang_up_after": 3})):
        blocked_origin = await origin()
        proxy = Socks5Proxy(host, **kwargs)
        browser = await launch(await proxy.start())
        seen = await load(browser, blocked_origin)
        await browser.close()
        checks.append((f"a proxy that {label} leaves the page on TCP",
                       seen and set(seen) == {"tcp"} and not blocked_origin.h3_peers
                       and (proxy.refused > 0 if not proxy.udp else len(proxy.relays) > 0),
                       (seen[:6], proxy.refused, len(proxy.relays))))
        proxy.close()
        blocked_origin.close()

    # An HTTP proxy is sent no QUIC.
    http_origin = await origin()
    http_connects = []
    http_proxy = await asyncio.start_server(lambda r, w: serve_http_proxy(http_connects, r, w), "127.0.0.1", 0)
    browser = await launch(f"http://127.0.0.1:{http_proxy.sockets[0].getsockname()[1]}")
    http_seen = await load(browser, http_origin)
    await browser.close()
    checks.append(("an HTTP proxy is sent no QUIC",
                   http_seen and set(http_seen) == {"tcp"} and not http_origin.datagrams and bool(http_connects),
                   (http_seen[:6], sorted(http_origin.datagrams))))

    failed = 0
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  {json.dumps(detail, default=str)}"))
        failed += not ok
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    tmp.cleanup()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
