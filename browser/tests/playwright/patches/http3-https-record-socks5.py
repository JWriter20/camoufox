"""
Verify an origin sees the same connections behind a SOCKS5 proxy as with none,
when it announces HTTP/3 in an HTTPS record.

Firefox learns of HTTP/3 from the record before it has connected, starts QUIC,
and gives it 50 ms (network.dns.httpssvc.http3_fast_fallback_timeout) before it
also starts TCP; whichever is ready first carries the request. With no proxy
QUIC's first datagram leaves at once. Behind a SOCKS5 proxy it waits for the
UDP ASSOCIATE, so the timer ran out before QUIC had sent anything: the origin
got a TCP connection a Firefox at the proxy's exit never opens, the request
went over it when it won (it does with proxy_optimistic_data and a proxy
further than 50 ms away), and a QUIC connection closed before its relay was
ready sent one datagram, saying it closed, to an origin that had never heard
of it. A navigation's speculative connection was also keyed apart from the
request's and never used, so each navigation opened two connections.

The guard runs an origin (HTTP/1.1 over TLS and HTTP/3 on one port), a DoH
resolver that answers with an HTTPS record for it, and a SOCKS5 proxy that
takes 50 ms each way. The origin is at an address of TEST-NET-3 on a docker
bridge of this host: Firefox opens no speculative or backup connection to a
private address, with or without a proxy.

What PASS means:
    * for a navigation and for two names a page fetches from, the origin sees
      the same connections behind the proxy as from the same browser with
      none: how many over TCP and with which server names, how many over
      QUIC, and which of them carried requests. With proxy_optimistic_data
      and without;
    * a proxy that takes a second to grant the association changes none of
      that;
    * nor does a lookup that waits for a new connection to the resolver,
      after the page has learned that its answers come quickly: the clock
      that decides when TCP starts stands still while a query waits to be
      sent, as it does while it waits for its answer;
    * an origin that answers nothing over QUIC is still reached over TCP, by
      the connections it sees with no proxy;
    * behind the proxy, a WebSocket's HTTPS record is asked of the resolver
      as it is with no proxy: the proxy carries the DoH requests and is the
      network, so DNS prefetch stays on. With no addon, since uBlock Origin
      turns prefetch off.

    python browser/tests/playwright/patches/http3-https-record-socks5.py
"""

import asyncio
import base64
import importlib.util
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from helpers import Marionette, free_port, marionette_args, marionette_env, resolve_binary  # noqa: E402

# The origin, the relay and the proxy of the Alt-Svc guard, which this one
# puts behind names, an HTTPS record and a delay.
_spec = importlib.util.spec_from_file_location("http3_socks5_udp", HERE / "http3-socks5-udp.py")
relayed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relayed)

SUBNET, ADDRESS = "203.0.113.0/29", "203.0.113.1"
ZONE = "https-record.test"
# One way, between the browser and the proxy. The round trip is twice the
# time Firefox gives QUIC before it starts TCP.
DELAY = 0.05
SLOW_ASSOCIATE = 1.0
# How long the resolver takes to answer before its answers speed up, and how
# many names are then looked up at once.
SLOW_ANSWER = 0.6
BURST = 8
# How many rounds of three names a page looks up before the resolver hangs up.
WARM_ROUNDS = 30

FETCH = """async (urls) => {
  for (let round = 0; round < 2; round++) {
    await Promise.all(urls.map(u => fetch(u + '?' + Math.random(), {mode: 'no-cors', cache: 'no-store'}).catch(() => 0)));
    await new Promise(r => setTimeout(r, 1000));
  }
}"""


class PublicAddress:
    """ADDRESS on this host: the gateway of a docker network of its own."""

    def __init__(self) -> None:
        self.name = f"https-record-guard-{os.getpid()}"
        subprocess.run(["docker", "network", "create", "--subnet", SUBNET, self.name],
                       check=True, stdout=subprocess.DEVNULL)

    def close(self) -> None:
        subprocess.run(["docker", "network", "rm", self.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Origin(relayed.Origin):
    """The Alt-Svc guard's origin without the Alt-Svc: HTTP/3 is known from
    the HTTPS record alone. Records the server name of every TCP connection.
    A `deaf` one counts the datagrams it gets and answers none."""

    def __init__(self, name: str, cert: str, key: str, deaf: bool) -> None:
        super().__init__(ADDRESS, cert, key)
        self.name, self.deaf = name, deaf
        self.url = f"https://{name}:{self.port}/"
        self.server_names = []

    async def start(self) -> None:
        await super().start()
        if self.deaf:
            self._quic.datagram_received = lambda data, addr: self.datagrams.setdefault(addr[:2], []).append(len(data))
        self._tcp.close()
        await self._tcp.wait_closed()
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self._cert, self._key)
        tls.sni_callback = lambda _socket, name, _context: self.server_names.append(name)
        self._tcp = await asyncio.start_server(self._serve_tcp, self.host, self.port, ssl=tls)

    async def _serve_tcp(self, reader, writer) -> None:
        try:
            while await reader.readline():
                while (await reader.readline()) not in (b"\r\n", b""):
                    pass
                self.tcp_requests += 1
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    def seen(self) -> dict:
        """What a site can tell of the browser's connections to it."""
        with_requests = set(self.h3_peers)
        names = sorted("the origin's" if name == self.name else str(name) for name in self.server_names)
        if self.deaf:
            # How often QUIC is tried again with an origin that never answers
            # depends on when the next request comes, with no proxy too.
            return {"tcp": names, "tcp carried requests": self.tcp_requests > 0,
                    "quic tried": bool(self.datagrams)}
        return {
            "tcp": names,
            "tcp carried requests": self.tcp_requests > 0,
            "quic": len(self.datagrams),
            "quic with requests": len(with_requests),
            "quic of one datagram": sum(len(sizes) == 1 for sizes in self.datagrams.values()),
        }


class Resolver:
    """DNS over HTTPS for ZONE: an A record, and an HTTPS record naming
    HTTP/3 on the origin's port."""

    def __init__(self, cert: str, key: str) -> None:
        self.ports = {}  # name -> the port its HTTPS record names
        self.delay = 0  # how long an answer takes, unless `wait` is replaced
        self.http2 = False  # answers over HTTP/2 (serve_http2)
        self.alpn = (b"h3",)  # what the HTTPS record offers
        self.asked = set()  # (name, qtype) of every question
        self.connections = set()  # the browser's open connections
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.load_cert_chain(cert, key)

    async def start(self) -> str:
        if self.http2:
            self._tls.set_alpn_protocols(["h2"])
        self.server = await asyncio.start_server(self.serve, ADDRESS, 0, ssl=self._tls)
        return f"https://{ADDRESS}:{self.server.sockets[0].getsockname()[1]}/dns-query"

    async def wait(self, name: str, qtype: int) -> None:
        """What the answer to a question waits for."""
        await asyncio.sleep(self.delay)

    async def answer(self, query: bytes) -> bytes:
        at, labels = 12, []
        while query[at]:
            labels.append(query[at + 1:at + 1 + query[at]].decode().lower())
            at += query[at] + 1
        (qtype,) = struct.unpack("!H", query[at + 1:at + 3])
        self.asked.add((".".join(labels), qtype))
        # An HTTPS record for another port than 443 is asked at _port._https.
        port = self.ports.get(".".join(labels)) or self.ports.get(
            ".".join(label for label in labels if not label.startswith("_")))
        record = b""
        if port and qtype == 1:
            record = socket.inet_aton(ADDRESS)
        elif port and qtype == 65:
            alpn = b"".join(bytes((len(protocol),)) + protocol for protocol in self.alpn)
            record = (struct.pack("!H", 1) + b"\0" + struct.pack("!HH", 1, len(alpn)) + alpn
                      + struct.pack("!HHH", 3, 2, port))
        await self.wait(".".join(labels), qtype)
        if record:
            record = b"\xc0\x0c" + struct.pack("!HHIH", qtype, 1, 60, len(record)) + record
        return query[:2] + struct.pack("!HHHHH", 0x8180, 1, 1 if record else 0, 0, 0) + query[12:at + 5] + record

    def hang_up(self) -> None:
        """Closes the browser's connections, as a resolver closes idle ones."""
        for writer in self.connections:
            writer.close()

    async def serve(self, reader, writer) -> None:
        self.connections.add(writer)
        try:
            if writer.get_extra_info("ssl_object").selected_alpn_protocol() == "h2":
                await self.serve_http2(reader, writer)
            while line := await reader.readline():
                length = 0
                while (header := await reader.readline()) not in (b"\r\n", b""):
                    if header.lower().startswith(b"content-length:"):
                        length = int(header.split(b":")[1])
                query = await reader.readexactly(length)
                if not query:
                    encoded = line.split()[1].split(b"dns=")[1].split(b"&")[0]
                    query = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
                answer = await self.answer(query)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/dns-message\r\n"
                             b"Content-Length: %d\r\n\r\n%s" % (len(answer), answer))
                await writer.drain()
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            pass
        finally:
            self.connections.discard(writer)
            writer.close()

    async def serve_http2(self, reader, writer) -> None:
        """HTTP/2 as a DoH resolver speaks it: every query is a stream of one
        connection, answered when it is ready. Queries are POSTed
        (network.trr.useGET false), so no header has to be decoded."""

        def frame(kind: int, flags: int, stream: int, payload: bytes = b"") -> None:
            writer.write(len(payload).to_bytes(3, "big") + bytes((kind, flags)) + struct.pack("!I", stream) + payload)

        async def reply(stream: int, query: bytes) -> None:
            answer = await self.answer(query)
            if writer.is_closing():
                return
            kind = b"application/dns-message"
            length = str(len(answer)).encode()
            # :status 200, then content-type and content-length as literals
            # with the static table's names.
            frame(1, 4, stream, b"\x88\x0f\x10" + bytes((len(kind),)) + kind
                  + b"\x0f\x0d" + bytes((len(length),)) + length)
            frame(0, 1, stream, answer)

        await reader.readexactly(24)
        frame(4, 0, 0)
        bodies, replies = {}, set()
        while True:
            header = await reader.readexactly(9)
            kind, flags, stream = header[3], header[4], struct.unpack("!I", header[5:])[0]
            payload = await reader.readexactly(int.from_bytes(header[:3], "big"))
            if kind == 4 and not flags & 1:
                frame(4, 1, 0)
            elif kind == 6 and not flags & 1:
                frame(6, 1, 0, payload)
            elif kind == 0:
                if payload:
                    # Returns the connection's window, or the browser stops sending.
                    frame(8, 0, 0, struct.pack("!I", len(payload)))
                bodies[stream] = bodies.get(stream, b"") + payload
                if flags & 1:
                    replies.add(task := asyncio.ensure_future(reply(stream, bodies.pop(stream))))
                    task.add_done_callback(replies.discard)

    def close(self) -> None:
        self.server.close()


class DistantRelay(relayed.Relay):
    def datagram_received(self, data: bytes, addr: tuple) -> None:
        asyncio.get_running_loop().call_later(
            DELAY, lambda: self.transport.is_closing() or super(DistantRelay, self).datagram_received(data, addr))

    def from_exit(self, data: bytes, addr: tuple) -> None:
        asyncio.get_running_loop().call_later(DELAY, lambda: super(DistantRelay, self).from_exit(data, addr))


class DistantProxy(relayed.Socks5Proxy):
    """The Alt-Svc guard's proxy, DELAY away: every reply, every byte it
    pipes and every datagram takes that long each way. `associate` is how
    long it takes to grant a UDP ASSOCIATE, or a coroutine function the grant
    waits for. A `local` one has its endpoint on the browser's machine, which
    forwards to the exit: the greeting is answered at once, and everything
    after it is DELAY away."""

    def __init__(self, associate: float = 0, local: bool = False) -> None:
        super().__init__(ADDRESS)
        self.associate = associate
        self.local = local
        self.names = {}  # the names this proxy resolves, and their addresses

    async def handle(self, reader, writer) -> None:
        loop = asyncio.get_running_loop()

        async def reply(data: bytes) -> None:
            await asyncio.sleep(2 * DELAY)
            writer.write(data)
            await writer.drain()

        async def delayed(src, dst) -> None:
            queue = asyncio.Queue()

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
            try:
                while data := await src.read(65536):
                    queue.put_nowait((loop.time() + DELAY, data))
            except (ConnectionError, OSError):
                pass
            queue.put_nowait((loop.time() + DELAY, b""))
            await writing

        _, count = await reader.readexactly(2)
        await reader.readexactly(count)
        if self.local:
            writer.write(b"\x05\x00")
        else:
            await reply(b"\x05\x00")
        _, cmd, _, atyp = await reader.readexactly(4)
        host, port = await relayed.read_addr(reader, atyp)
        if cmd == 1:
            self.connects.append((atyp, host, port))
            await asyncio.sleep(DELAY)
            up_reader, up_writer = await asyncio.open_connection(self.names.get(host, host), port)
            # What the browser sent behind the request is not kept waiting
            # for the reply.
            up = asyncio.ensure_future(delayed(reader, up_writer))
            await asyncio.sleep(DELAY)
            writer.write(b"\x05\x00\x00\x01" + bytes(6))
            await asyncio.gather(up, delayed(up_reader, writer))
            return
        await (self.associate() if callable(self.associate) else asyncio.sleep(self.associate))
        _, relay = await loop.create_datagram_endpoint(lambda: DistantRelay(self, writer), local_addr=("127.0.0.1", 0))
        relay.exit, _ = await loop.create_datagram_endpoint(lambda: relayed.Exit(relay), local_addr=(ADDRESS, 0))
        self.relays.append(relay)
        await reply(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1")
                    + struct.pack("!H", relay.transport.get_extra_info("sockname")[1]))
        await reader.read()
        relay.close()


async def main() -> int:
    from camoufox import DefaultAddons
    from camoufox.async_api import AsyncCamoufox

    if shutil.which("docker") is None:
        print("  FAIL docker is needed to give this host a public address")
        return 1
    tmp = tempfile.TemporaryDirectory()
    ca, ca_key, cert, key = (f"{tmp.name}/{name}.pem" for name in ("ca", "ca-key", "cert", "key"))
    for command in (
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key, "-out", ca, "-days", "2",
         "-subj", "/CN=http3-https-record-socks5 guard", "-addext", "basicConstraints=critical,CA:TRUE"],
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "2",
         "-subj", "/CN=origin", "-CA", ca, "-CAkey", ca_key,
         "-addext", f"subjectAltName=IP:{ADDRESS},DNS:*.{ZONE}", "-addext", "basicConstraints=critical,CA:FALSE"],
    ):
        subprocess.run(["openssl", *command], check=True, capture_output=True)
    ca_der = "".join(Path(ca).read_text().splitlines()[1:-1])
    trust = ("Cc['@mozilla.org/security/x509certdb;1'].getService(Ci.nsIX509CertDB)"
             f".addCertFromBase64('{ca_der}', 'C,,'); return true;")
    serial = iter(range(1000))

    async def origin(resolver: Resolver, deaf: bool, name: str = None) -> Origin:
        """An origin at a new name, or at another port of `name`."""
        while True:
            o = Origin(name or f"n{next(serial)}.{ZONE}", cert, key, deaf)
            try:
                await o.start()
            except OSError:  # the port is free for TCP and taken for UDP
                continue
            resolver.ports[f"_{o.port}._https.{name}" if name else o.name] = o.port
            return o

    async def launch(resolver: Resolver, proxy: DistantProxy = None, optimistic: bool = False, addons: bool = True):
        port = free_port()
        browser = await AsyncCamoufox(
            headless=True, executable_path=str(resolve_binary()), geoip=False, i_know_what_im_doing=True,
            args=marionette_args(), env=marionette_env(), proxy={"server": await proxy.start()} if proxy else None,
            proxy_optimistic_data=optimistic or None, exclude_addons=None if addons else [DefaultAddons.UBO],
            firefox_user_prefs={
                "network.trr.mode": 3, "network.trr.uri": await resolver.start(),
                "network.trr.confirmationNS": "skip", "network.trr.useGET": False,
                "network.http.http3.disable_when_third_party_roots_found": False, "marionette.port": port},
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

    async def visit(deaf: bool, proxy: DistantProxy = None, optimistic: bool = False, hung_up: bool = False) -> list:
        """A navigation to one name and two rounds of fetches from two more:
        what each of the three origins saw. With `hung_up`, the page first
        fetches from WARM_ROUNDS * 3 other names, the resolver then hangs up,
        and the second origin is the first's name at another port: its
        address is known, and its HTTPS record waits for a new connection."""
        resolver = Resolver(cert, key)
        resolver.http2 = True
        first = await origin(resolver, deaf)
        origins = [first, await origin(resolver, deaf, first.name if hung_up else None), await origin(resolver, deaf)]
        browser = await launch(resolver, proxy, optimistic)
        page = await (await browser.new_context()).new_page()
        try:
            await page.goto(origins[0].url)
            if hung_up:
                warm = await origin(resolver, False)
                names = [f"w{i}.{ZONE}" for i in range(WARM_ROUNDS * 3)]
                resolver.ports.update(dict.fromkeys(names, warm.port))
                await page.evaluate("""async urls => { for (let i = 0; i < urls.length; i += 3)
                    await Promise.all(urls.slice(i, i + 3).map(u => fetch(u, {mode: 'no-cors'}).catch(() => 0))); }""",
                                    [f"https://{name}:{warm.port}/" for name in names])
                warm.close()
                resolver.hang_up()
                await asyncio.sleep(0.5)
            await page.evaluate(FETCH, [o.url for o in origins[1:]])
            await asyncio.sleep(2)
        finally:
            await browser.close()
            for closing in (*origins, resolver, *([proxy] if proxy else [])):
                closing.close()
        return [o.seen() for o in origins]

    async def socket_record(proxy: DistantProxy = None) -> bool:
        """Whether a WebSocket's HTTPS record is asked of the resolver."""
        resolver = Resolver(cert, key)
        page_origin, socket_origin = [await origin(resolver, False) for _ in range(2)]
        browser = await launch(resolver, proxy, addons=False)
        page = await (await browser.new_context()).new_page()
        try:
            await page.goto(page_origin.url)
            # The origin answers the upgrade with a plain 200, so the socket fails.
            await page.evaluate("url => new Promise(done => { new WebSocket(url).onerror = done; })",
                                f"wss://{socket_origin.name}:{socket_origin.port}/")
        finally:
            await browser.close()
            for closing in (page_origin, socket_origin, resolver, *([proxy] if proxy else [])):
                closing.close()
        return (f"_{socket_origin.port}._https.{socket_origin.name}", 65) in resolver.asked

    async def answers_speed_up() -> dict:
        """A fetch from an origin whose IPv6 address is still being asked for
        while its QUIC connection waits for the association. The resolver's
        answers, slow until then, become fast meanwhile, which shortens how
        long a lookup is taken to have stood still at the exit; the time the
        target's lookup was already counted as lost must not be given back.
        The proxy grants the association SLOW_ASSOCIATE after the fast answers
        are in: what it saw before."""
        resolver = Resolver(cert, key)
        resolver.http2 = True
        target, other = await origin(resolver, False), await origin(resolver, False)
        slow = [f"w{i}.{ZONE}" for i in range(4)]
        burst = [f"b{i}.{ZONE}" for i in range(BURST)]
        for name in slow + burst:
            resolver.ports[name] = other.port
        associating, burst_done, granted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        answered, held, associations = set(), [], []

        async def wait(name: str, qtype: int) -> None:
            if name == target.name and qtype == 28:
                held.append(name)
                await granted.wait()
            elif name in slow:
                await asyncio.sleep(SLOW_ANSWER)
            elif name in burst and qtype in (1, 65):
                answered.add((name, qtype))
                if len(answered) == 2 * BURST:
                    burst_done.set()

        async def associate() -> None:
            if not associating.is_set() and not held:  # the slow names' connections
                return
            associations.append(None)
            if len(associations) > 1:
                return await granted.wait()
            associating.set()
            await burst_done.wait()
            await asyncio.sleep(SLOW_ASSOCIATE)
            seen["tcp before the association"] = sum(port == target.port for _, _, port in proxy.connects)
            seen["its IPv6 address was still asked for"] = bool(held)
            granted.set()

        resolver.wait = wait
        proxy = DistantProxy(associate=associate)
        seen = {}
        browser = await launch(resolver, proxy, addons=False)
        page = await (await browser.new_context()).new_page()
        fetch = "names => names.map(name => fetch(`https://${name}/`, {mode: 'no-cors'}).catch(() => 0))"
        try:
            await page.evaluate(f"names => Promise.all(({fetch})(names))", slow)
            await page.evaluate("url => { window.fetched = fetch(url, {mode: 'no-cors'}).catch(e => String(e)); }",
                                target.url)
            await asyncio.wait_for(associating.wait(), 15)
            await page.evaluate(f"names => {{ ({fetch})(names); }}", burst)
            await asyncio.wait_for(granted.wait(), 15)
            await page.evaluate("() => window.fetched")
        finally:
            await browser.close()
            for closing in (target, other, resolver, proxy):
                closing.close()
        return seen

    checks = []
    address = PublicAddress()
    try:
        for deaf in (False, True):
            what = "an origin deaf to QUIC" if deaf else "an origin with HTTP/3"
            direct = await visit(deaf)
            checks.append((f"{what}, no proxy: the requests are carried over "
                           f"{'TCP' if deaf else 'HTTP/3, and there is no TCP connection'} (the guard's reference)",
                           all(o["tcp carried requests"] and o["quic tried"] if deaf
                               else o["quic with requests"] and not o["tcp"] for o in direct), direct))
            for optimistic in (False, True):
                how = "with proxy_optimistic_data" if optimistic else "behind the proxy"
                checks.append((f"{what}, {how}: each origin sees the connections it sees with no proxy",
                               (seen := await visit(deaf, DistantProxy(), optimistic)) == direct, seen))
            if not deaf:
                checks.append((f"{what}, a proxy slow to grant the association: the same connections",
                               (seen := await visit(deaf, DistantProxy(associate=SLOW_ASSOCIATE), True)) == direct, seen))
        direct = await visit(True, hung_up=True)
        checks.append(("an origin deaf to QUIC, after the resolver hangs up, no proxy: the requests are carried "
                       "over TCP (the guard's reference)",
                       all(o["tcp carried requests"] and o["quic tried"] for o in direct), direct))
        checks.append(("an origin deaf to QUIC, after the resolver hangs up on a page that learned its answers "
                       "are fast, behind the proxy: the same connections",
                       (seen := await visit(True, DistantProxy(), hung_up=True)) == direct, seen))
        checks.append(("answers that speed up while a QUIC connection waits for its association: no TCP "
                       "connection is started meanwhile",
                       (seen := await answers_speed_up()) == {"tcp before the association": 0,
                                                              "its IPv6 address was still asked for": True}, seen))
        direct = await socket_record()
        checks.append(("a WebSocket, no proxy: its HTTPS record is asked (the guard's reference)", direct, direct))
        checks.append(("a WebSocket, behind the proxy: its HTTPS record is asked, as with no proxy",
                       (asked := await socket_record(DistantProxy())) == direct, asked))
    finally:
        address.close()

    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}: {detail}")
    passed = sum(ok for _, ok, _ in checks)
    print(f"{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
