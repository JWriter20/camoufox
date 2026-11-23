"""
Verify a site sees the same connections from a browser with uBlock Origin
behind a SOCKS5 proxy as from the same browser with none, and that both see
what stock Firefox with uBlock Origin shows.

Stock Firefox on the release channel connects with Happy Eyeballs v3
(network.http.happy_eyeballs_enabled, a 100% Nimbus rollout that camoufox.cfg
restores): a request's addresses and HTTPS record are asked for together, the
connection the record names is tried first, and another is started beside it
every time one has gone 50 ms without connecting
(network.http.happy_eyeballs_connection_attempt_delay). A site near enough
sees QUIC alone. Firefox does not use Happy Eyeballs behind a proxy, so a
proxied browser raced on the older timers, which open TCP connections stock no
longer opens. Camoufox keeps it on behind a SOCKS5 proxy given addresses, and
runs it on the exit's clock: an attempt sends nothing until the proxy has been
asked for it, and its answer crosses the proxy, so the delay would run out on
an attempt a Firefox at the proxy's exit is still waiting for. A lookup's
answer crosses the proxy too. uBlock Origin resolves a request's name before
letting it go, so the connection finds the addresses in the cache and waits
25 ms for the HTTPS record alone (the resolution delay); on the browser's
clock that ran out before the answer had crossed the proxy, and every request
went over TCP to the URL's port with a GREASE ECH extension. A Firefox at the
exit has its resolver beside it, so the wait is for as long as the resolver's
answers have been taking, and no round trip to the proxy is assumed: a proxy
whose endpoint is on the browser's machine answers the SOCKS greeting at once
however far its exit is.

uBlock Origin's default "Disable pre-fetching" sets
network.http.speculative-parallel-limit to 0, so no connection is opened ahead
of a request. With Happy Eyeballs a <link rel=preconnect> then asks nothing
of the resolver either; without it the name's addresses and HTTPS record were
asked for a connection that was never going to be opened.

The guard runs a site of several names on one address, a DoH resolver and a
SOCKS5 proxy. The site's URLs name a port that answers HTTP/1.1 over TLS, and
its HTTPS records (alpn h3,h2) name another, which answers HTTP/3 too: a
request that did not wait for its record is one over TCP to the URL's port.
The page is one of the site's names; its stylesheet, image, video, font,
EventSource and fetch are at six more, and it preconnects to another. The
proxy's endpoint is on the browser's machine and answers the greeting at
once; its exit is 50 ms away each way, for every byte after that. The
resolver answers over one HTTP/2 connection, 20 ms after it is asked (120 ms
through the proxy), and is addressed by name, as the launcher's
proxy_dns='doh' addresses its own: the proxy is given that one name and
addresses for the rest. The page is opened in the launch's own context, whose
lookups share a cache with uBlock Origin's. The site is a set distance from
whoever connects to it, the browser or the proxy: once nearer than the delay
and once further, where Firefox opens TCP beside QUIC with no proxy too.

What PASS means, at each distance, with uBlock Origin loaded and its
preferences in place before the page is opened:
    * with no proxy the site sees the connections measured for that distance
      from stock Firefox 160.0.1 with uBlock Origin 1.75.4 (REFERENCE), and
      the resolver is asked nothing about the preconnected name;
    * behind the proxy with proxy_optimistic_data the site sees what it sees
      with no proxy: the TCP connections (port, server name, ALPN offered,
      ECH extension), the QUIC connections, the protocol that carried each
      request, the page's first among them, the names asked of the resolver
      and their types, and no QUIC connection of one datagram;
    * no name is resolved by this machine, the resolver's included.

    python browser/tests/playwright/patches/ublock-connections-socks5.py
"""

import asyncio
import importlib.util
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ProtocolNegotiated

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from helpers import Marionette, free_port, marionette_args, marionette_env, resolve_binary  # noqa: E402

# The resolver, the proxy and the address of the HTTPS record guard.
_spec = importlib.util.spec_from_file_location("http3_https_record_socks5", HERE / "http3-https-record-socks5.py")
record = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(record)

ADDRESS = record.ADDRESS
ZONE = "ublock-guard.test"
KINDS = ("css", "img", "font", "video", "es", "fetch")
# Round trips between the site and whoever connects to it. NEAR: every QUIC
# connection is up well inside the 50 ms Firefox gives an attempt. FAR: a TCP
# connection is started beside each, and QUIC still connects first.
NEAR, FAR = 0.004, 0.1
# What the site sees of stock Firefox 160.0.1 with uBlock Origin 1.75.4 and no
# proxy (the same in every run measured): the names it has a TCP connection
# for, and the requests each protocol carried. The stylesheet is asked for
# twice, and the page's name for the page and its icon. Which QUIC connection
# carries the EventSource, the fetch and the font differs from run to run, in
# stock too, so it is not compared.
REFERENCE = {
    NEAR: {"tcp names": [], "over tcp": [],
           "over h3": ["css", "css", "es", "fetch", "font", "img", "page", "page", "video"]},
    FAR: {"tcp names": sorted(("page", *KINDS)), "over tcp": [],
          "over h3": ["css", "css", "es", "fetch", "font", "img", "page", "page", "video"]},
}
# How long the resolver takes to answer. A page's requests are asked about
# together, so with a resolver that answers at once the first of them are on
# their way before the stylesheet is asked for, and nothing is held back.
RESOLVER = 0.02
PAGE = """<!doctype html><html><head><title>page</title>
<script>
new EventSource('https://es.{zone}:{port}/es');
fetch('https://fetch.{zone}:{port}/fetch', {{mode: 'no-cors', cache: 'no-store'}}).catch(() => 0);
new FontFace('guard', 'url(https://font.{zone}:{port}/font)').load().catch(() => 0);
</script>
<link rel="preconnect" href="https://preconnect.{zone}:{port}">
<link rel="stylesheet" href="https://css.{zone}:{port}/css">
</head><body>
<img src="https://img.{zone}:{port}/img">
<video src="https://video.{zone}:{port}/video" preload="auto"></video>
</body></html>"""
BODIES = {
    "/css": (b"text/css", b"body { margin: 0 }"),
    "/img": (b"image/gif", b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"),
    "/es": (b"text/event-stream", b"retry: 600000\ndata: 1\n\n"),
}
SPECULATIVE_LIMIT = "return Services.prefs.getIntPref('network.http.speculative-parallel-limit');"


def client_hello(data: bytes) -> dict:
    """The server name, the ALPN protocols and the ECH extension of a TLS
    ClientHello record."""
    at = 5 + 4 + 2 + 32
    at += 1 + data[at]
    at += 2 + struct.unpack("!H", data[at:at + 2])[0]
    at += 1 + data[at]
    end = at + 2 + struct.unpack("!H", data[at:at + 2])[0]
    at += 2
    hello = {"name": None, "alpn": (), "ech": False}
    while at < end:
        kind, size = struct.unpack("!HH", data[at:at + 4])
        value = data[at + 4:at + 4 + size]
        if kind == 0:
            hello["name"] = value[5:].decode()
        elif kind == 16:
            protocols, i = [], 2
            while i < len(value):
                protocols.append(value[i + 1:i + 1 + value[i]].decode())
                i += 1 + value[i]
            hello["alpn"] = tuple(protocols)
        elif kind == 0xFE0D:
            hello["ech"] = True
        at += 4 + size
    return hello


class Site:
    """Several names on one address, the page's among them. Their URLs name
    one port, which answers HTTP/1.1 over TLS; their HTTPS records name
    another, which answers HTTP/3 as well. `distance` is the round trip
    between the site and whoever connects. Records every TCP connection's
    port and ClientHello, every QUIC connection's datagrams, and which
    connection carried each request."""

    def __init__(self, cert: str, key: str, distance: float) -> None:
        self.port = free_port()  # the one the HTTPS records name
        self.way = distance / 2
        self.tcp = []  # {"port", "name", "alpn", "ech", "requests"}
        self.datagrams = {}  # UDP source -> how many datagrams came from it
        self.h3_requests = {}  # UDP source -> the names requested
        self._cert, self._key = cert, key
        self._inner = {}  # the port a TCP connection reaches the TLS server from -> its record

    async def start(self) -> None:
        site, loop = self, asyncio.get_running_loop()

        class Recorded(QuicConnectionProtocol):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.h3 = None

            def quic_event_received(self, event) -> None:
                if isinstance(event, ProtocolNegotiated):
                    self.h3 = H3Connection(self._quic)
                if self.h3 is None:
                    return
                for ev in self.h3.handle_event(event):
                    if isinstance(ev, HeadersReceived):
                        headers = dict(ev.headers)
                        peer = self._quic._network_paths[0].addr[:2]
                        site.h3_requests.setdefault(peer, []).append(headers[b":authority"].decode().split(".")[0])
                        kind, body = site.body(headers[b":path"].decode())
                        self.h3.send_headers(ev.stream_id, [
                            (b":status", b"200"), (b"content-type", kind),
                            (b"content-length", str(len(body)).encode())])
                        self.h3.send_data(ev.stream_id, body, end_stream=True)
                self.transmit()

        quic = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN)
        quic.load_cert_chain(self._cert, self._key)
        self._quic = await serve(ADDRESS, self.port, configuration=quic, create_protocol=Recorded)
        received, transport = self._quic.datagram_received, self._quic._transport

        class Far:
            """The server's socket, `way` from its peers."""

            def sendto(self, data, addr=None) -> None:
                loop.call_later(site.way, lambda: transport.is_closing() or transport.sendto(data, addr))

            def __getattr__(self, name):
                return getattr(transport, name)

        def arrived(data, addr) -> None:
            site.datagrams[addr[:2]] = site.datagrams.get(addr[:2], 0) + 1
            loop.call_later(site.way, received, data, addr)

        self._quic._transport = Far()
        self._quic.datagram_received = arrived
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self._cert, self._key)
        self._tls = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=tls)
        self._tcp = await asyncio.start_server(self._accept, ADDRESS, self.port)
        self._url = await asyncio.start_server(self._accept, ADDRESS, 0)
        self.url_port = self._url.sockets[0].getsockname()[1]
        self._page = PAGE.format(zone=ZONE, port=self.url_port).encode()

    def body(self, path: str) -> tuple:
        if path == "/":
            return b"text/html", self._page
        return BODIES.get(path.split("?")[0], (b"application/octet-stream", b"ok"))

    async def _accept(self, reader, writer) -> None:
        """A TCP connection: its ClientHello is recorded, and its bytes reach
        the TLS server `way` late, the first ones a round trip later still,
        as behind a handshake over that distance."""
        loop = asyncio.get_running_loop()
        to_url = writer.get_extra_info("sockname")[1] == self.url_port
        connection = {"port": "the URL's" if to_url else "the record's", "requests": []}
        self.tcp.append(connection)
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self._tls.sockets[0].getsockname()[1])
        self._inner[up_writer.get_extra_info("sockname")[1]] = connection

        async def pipe(src, dst, first: float, hello: bool) -> None:
            queue, seen = asyncio.Queue(), b""

            async def write() -> None:
                while True:
                    due, data = await queue.get()
                    await asyncio.sleep(max(0, due - loop.time()))
                    if not data:
                        return dst.close()
                    try:
                        dst.write(data)
                        await dst.drain()
                    except (ConnectionError, OSError):
                        return

            writing = asyncio.ensure_future(write())
            due = loop.time() + first
            try:
                while data := await src.read(65536):
                    if hello:
                        seen += data
                        if len(seen) >= 5 and len(seen) >= 5 + struct.unpack("!H", seen[3:5])[0]:
                            connection.update(client_hello(seen))
                            hello = False
                    due = max(due, loop.time() + self.way)
                    queue.put_nowait((due, data))
            except (ConnectionError, OSError):
                pass
            queue.put_nowait((max(due, loop.time() + self.way), b""))
            await writing

        await asyncio.gather(pipe(reader, up_writer, 3 * self.way, True), pipe(up_reader, writer, self.way, False))

    async def _serve(self, reader, writer) -> None:
        connection = self._inner[writer.get_extra_info("peername")[1]]
        try:
            while line := await reader.readline():
                host = ""
                while (header := await reader.readline()) not in (b"\r\n", b""):
                    if header.lower().startswith(b"host:"):
                        host = header.split(b":")[1].strip().decode()
                connection["requests"].append(host.split(".")[0])
                kind, body = self.body(line.split()[1].decode())
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n%s"
                             % (kind, len(body), body))
                await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    def close(self) -> None:
        for server in (self._quic, self._tcp, self._url, self._tls):
            server.close()

    def seen(self) -> dict:
        """What the site can tell of the browser's connections to it."""
        return {
            "tcp": sorted((c["port"], str(c.get("name", "")).split(".")[0], c.get("alpn"), c.get("ech"),
                           tuple(c["requests"])) for c in self.tcp),
            "tcp names": sorted(str(c.get("name", "")).split(".")[0] for c in self.tcp),
            "over tcp": sorted(name for c in self.tcp for name in c["requests"]),
            "over h3": sorted(name for names in self.h3_requests.values() for name in names),
            "quic": len(self.datagrams),
            "quic of one datagram": sum(count == 1 for count in self.datagrams.values()),
        }


class Resolver(record.Resolver):
    """The HTTPS record guard's resolver for ZONE, recording what it is
    asked."""

    def __init__(self, cert: str, key: str) -> None:
        super().__init__(cert, key)
        self.asked = set()
        self.delay = RESOLVER
        self.http2 = True
        self.alpn = (b"h3", b"h2")

    def answer(self, query: bytes) -> bytes:
        at, labels = 12, []
        while query[at]:
            labels.append(query[at + 1:at + 1 + query[at]].decode().lower())
            at += query[at] + 1
        (qtype,) = struct.unpack("!H", query[at + 1:at + 3])
        name = ".".join(labels)
        self.asked.add((name.replace("." + ZONE, ""), qtype))
        return super().answer(query)


class Network:
    """A site at `distance` and its resolver."""

    def __init__(self, cert: str, key: str, distance: float) -> None:
        self._cert, self._key = cert, key
        self.resolver = Resolver(cert, key)
        self.site = Site(cert, key, distance)

    async def start(self) -> None:
        while True:
            try:
                await self.site.start()
                break
            except OSError:  # the port is free for TCP and taken for UDP
                self.site = Site(self._cert, self._key, self.site.way * 2)
        self.url = f"https://page.{ZONE}:{self.site.url_port}/"
        self.resolver.ports = {f"{name}.{ZONE}": self.site.port for name in ("page", *KINDS)}
        self.doh_by_address = await self.resolver.start()
        doh_port = self.doh_by_address.split(":")[2].split("/")[0]
        self.doh_by_name = f"https://doh.{ZONE}:{doh_port}/dns-query"

    def close(self) -> None:
        for closing in (self.site, self.resolver):
            closing.close()

    def seen(self) -> dict:
        asked = {(name.replace(f"_{self.site.url_port}.", "_url."), qtype) for name, qtype in self.resolver.asked}
        return {**self.site.seen(), "asked": sorted(asked)}


def authority(directory: str) -> tuple:
    """A certificate for ZONE and ADDRESS, its key, and the script that makes
    the browser trust the authority that signed it."""
    ca, ca_key, cert, key = (f"{directory}/{name}.pem" for name in ("ca", "ca-key", "cert", "key"))
    for command in (
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key, "-out", ca, "-days", "2",
         "-subj", "/CN=ublock-connections-socks5 guard", "-addext", "basicConstraints=critical,CA:TRUE"],
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "2",
         "-subj", "/CN=site", "-CA", ca, "-CAkey", ca_key,
         "-addext", f"subjectAltName=IP:{ADDRESS},DNS:*.{ZONE}", "-addext", "basicConstraints=critical,CA:FALSE"],
    ):
        subprocess.run(["openssl", *command], check=True, capture_output=True)
    ca_der = "".join(Path(ca).read_text().splitlines()[1:-1])
    return cert, key, ("Cc['@mozilla.org/security/x509certdb;1'].getService(Ci.nsIX509CertDB)"
                       f".addCertFromBase64('{ca_der}', 'C,,'); return true;")


def prepare(port: int, trust: str) -> int:
    """Trusts the guard's authority and waits for uBlock Origin to have set
    its preferences. Returns network.http.speculative-parallel-limit."""
    marionette = Marionette(port)
    try:
        marionette.js(trust)
        deadline = time.time() + 30
        while (limit := marionette.js(SPECULATIVE_LIMIT)) and time.time() < deadline:
            time.sleep(0.1)
        return limit
    finally:
        marionette.close()


def ublock(binary: Path) -> str:
    from camoufox.addons import DefaultAddons, get_addon_path

    return get_addon_path(DefaultAddons.UBO.name, binary)


async def visit(binary: Path, cert: str, key: str, trust: str, distance: float, proxied: bool) -> dict:
    """What a site `distance` away sees of the page's load."""
    from camoufox.async_api import AsyncCamoufox

    network = Network(cert, key, distance)
    await network.start()
    proxy = record.DistantProxy(local=True) if proxied else None
    if proxy:
        proxy.names = {f"doh.{ZONE}": ADDRESS}
    log = tempfile.TemporaryDirectory()
    profile = tempfile.TemporaryDirectory()
    port = free_port()
    # The page is opened in the launch's own context, where the lookup uBlock
    # Origin makes for each request leaves the addresses in the cache the
    # connection reads: the HTTPS record is then the one answer still to come.
    context = await AsyncCamoufox(
        persistent_context=True, user_data_dir=profile.name,
        headless=True, executable_path=str(binary), geoip=False, i_know_what_im_doing=True,
        args=marionette_args(),
        env={**marionette_env(), "MOZ_LOG": "append,nsHostResolver:5", "MOZ_LOG_FILE": f"{log.name}/resolver"},
        proxy={"server": await proxy.start()} if proxy else None, proxy_optimistic_data=proxied or None,
        firefox_user_prefs={
            # Behind the proxy the launcher's proxy_dns='doh' is in force, at
            # this resolver's name. With no proxy there is nobody to resolve
            # that name, so the resolver is an address.
            **({"network.trr.uri": network.doh_by_name} if proxied
               else {"network.trr.mode": 3, "network.trr.uri": network.doh_by_address}),
            # The resolver answers over HTTP/2 and reads no header, so the
            # query has to be the request's body.
            "network.trr.useGET": False,
            "network.http.http3.disable_when_third_party_roots_found": False, "marionette.port": port},
    ).__aenter__()
    try:
        # Marionette's session needs a window to attach to.
        window = await context.new_page()
        limit = await asyncio.to_thread(prepare, port, trust)
        await window.close()
        page = await context.new_page()
        await page.goto(network.url)
        await asyncio.sleep(4)
    finally:
        await context.close()
        network.close()
        if proxy:
            proxy.close()
    resolved = "".join(path.read_text(errors="replace") for path in Path(log.name).iterdir())
    log.cleanup()
    profile.cleanup()
    seen = network.seen()
    seen["speculative limit"] = limit
    seen["resolved here"] = resolved.count("Calling getaddrinfo")
    if proxy:
        seen["names given to the proxy"] = sorted({host for atyp, host, _ in proxy.connects if atyp == 3})
    return seen


def same(seen: dict, direct: dict) -> bool:
    return all(seen[what] == direct[what] for what in (
        "tcp", "over tcp", "over h3", "quic", "quic of one datagram", "asked"))


async def main() -> int:
    if shutil.which("docker") is None:
        print("  FAIL docker is needed to give this host a public address")
        return 1
    binary = resolve_binary()
    if not Path(ublock(binary), "manifest.json").is_file():
        print(f"  FAIL the build at {binary} bundles no uBlock Origin")
        return 1
    tmp = tempfile.TemporaryDirectory()
    cert, key, trust = authority(tmp.name)

    checks = []
    address = record.PublicAddress()
    try:
        for distance in (NEAR, FAR):
            where = f"a site {distance * 1000:.0f} ms away"
            direct = await visit(binary, cert, key, trust, distance, False)
            checks.append((f"{where}, no proxy: uBlock Origin's preferences are set, the site sees the connections "
                           "stock Firefox makes at this distance and no name is resolved here",
                           direct["speculative limit"] == 0
                           and all(direct[what] == expected for what, expected in REFERENCE[distance].items())
                           and not direct["resolved here"], direct))
            checks.append((f"{where}, no proxy: the resolver is asked nothing about the preconnected name",
                           not any(name.startswith("preconnect") for name, _ in direct["asked"]), direct["asked"]))
            seen = await visit(binary, cert, key, trust, distance, True)
            checks.append((f"{where}, behind the proxy: the site sees what it sees with no proxy",
                           seen["speculative limit"] == 0 and same(seen, direct), seen))
            checks.append((f"{where}, behind the proxy: the proxy is given the resolver's name alone, "
                           "and no name is resolved here",
                           seen["names given to the proxy"] == [f"doh.{ZONE}"] and not seen["resolved here"],
                           (seen["names given to the proxy"], seen["resolved here"])))
    finally:
        address.close()

    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}: {detail}")
    passed = sum(ok for _, ok, _ in checks)
    print(f"{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
