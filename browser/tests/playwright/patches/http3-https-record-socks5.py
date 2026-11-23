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
    * an origin that answers nothing over QUIC is still reached over TCP, by
      the connections it sees with no proxy.

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
        self.delay = 0  # how long an answer takes
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.load_cert_chain(cert, key)

    async def start(self) -> str:
        self.server = await asyncio.start_server(self.serve, ADDRESS, 0, ssl=self._tls)
        return f"https://{ADDRESS}:{self.server.sockets[0].getsockname()[1]}/dns-query"

    def answer(self, query: bytes) -> bytes:
        at, labels = 12, []
        while query[at]:
            labels.append(query[at + 1:at + 1 + query[at]].decode().lower())
            at += query[at] + 1
        (qtype,) = struct.unpack("!H", query[at + 1:at + 3])
        # An HTTPS record for another port than 443 is asked at _port._https.
        port = self.ports.get(".".join(label for label in labels if not label.startswith("_")))
        record = b""
        if port and qtype == 1:
            record = socket.inet_aton(ADDRESS)
        elif port and qtype == 65:
            record = struct.pack("!H", 1) + b"\0" + struct.pack("!HH", 1, 3) + b"\x02h3" + struct.pack("!HHH", 3, 2, port)
        if record:
            record = b"\xc0\x0c" + struct.pack("!HHIH", qtype, 1, 60, len(record)) + record
        return query[:2] + struct.pack("!HHHHH", 0x8180, 1, 1 if record else 0, 0, 0) + query[12:at + 5] + record

    async def serve(self, reader, writer) -> None:
        try:
            while line := await reader.readline():
                length = 0
                while (header := await reader.readline()) not in (b"\r\n", b""):
                    if header.lower().startswith(b"content-length:"):
                        length = int(header.split(b":")[1])
                query = await reader.readexactly(length)
                if not query:
                    encoded = line.split()[1].split(b"dns=")[1].split(b"&")[0]
                    query = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
                answer = self.answer(query)
                await asyncio.sleep(self.delay)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/dns-message\r\n"
                             b"Content-Length: %d\r\n\r\n%s" % (len(answer), answer))
                await writer.drain()
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

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
    long it takes to grant a UDP ASSOCIATE."""

    def __init__(self, associate: float = 0) -> None:
        super().__init__(ADDRESS)
        self.associate = associate
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
        await asyncio.sleep(self.associate)
        _, relay = await loop.create_datagram_endpoint(lambda: DistantRelay(self, writer), local_addr=("127.0.0.1", 0))
        relay.exit, _ = await loop.create_datagram_endpoint(lambda: relayed.Exit(relay), local_addr=(ADDRESS, 0))
        self.relays.append(relay)
        await reply(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1")
                    + struct.pack("!H", relay.transport.get_extra_info("sockname")[1]))
        await reader.read()
        relay.close()


async def main() -> int:
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

    async def origin(resolver: Resolver, deaf: bool) -> Origin:
        while True:
            o = Origin(f"n{next(serial)}.{ZONE}", cert, key, deaf)
            try:
                await o.start()
            except OSError:  # the port is free for TCP and taken for UDP
                continue
            resolver.ports[o.name] = o.port
            return o

    async def launch(resolver: Resolver, proxy: DistantProxy = None, optimistic: bool = False):
        port = free_port()
        browser = await AsyncCamoufox(
            headless=True, executable_path=str(resolve_binary()), geoip=False, i_know_what_im_doing=True,
            args=marionette_args(), env=marionette_env(), proxy={"server": await proxy.start()} if proxy else None,
            proxy_optimistic_data=optimistic or None,
            firefox_user_prefs={
                "network.trr.mode": 3, "network.trr.uri": await resolver.start(),
                "network.trr.confirmationNS": "skip",
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

    async def visit(deaf: bool, proxy: DistantProxy = None, optimistic: bool = False) -> list:
        """A navigation to one name and two rounds of fetches from two more:
        what each of the three origins saw."""
        resolver = Resolver(cert, key)
        origins = [await origin(resolver, deaf) for _ in range(3)]
        browser = await launch(resolver, proxy, optimistic)
        page = await (await browser.new_context()).new_page()
        try:
            await page.goto(origins[0].url)
            await page.evaluate(FETCH, [o.url for o in origins[1:]])
            await asyncio.sleep(2)
        finally:
            await browser.close()
            for closing in (*origins, resolver, *([proxy] if proxy else [])):
                closing.close()
        return [o.seen() for o in origins]

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
    finally:
        address.close()

    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}: {detail}")
    passed = sum(ok for _, ok, _ in checks)
    print(f"{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
