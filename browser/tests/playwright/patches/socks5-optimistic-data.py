"""
Verify proxy_optimistic_data sends the TLS ClientHello behind the SOCKS5 CONNECT
request, and nothing else changes.

Behind a SOCKS5 proxy Firefox waits for the proxy's reply to CONNECT before it
writes the ClientHello, so the origin sees the exit finish the TCP handshake
and the ClientHello arrive one round trip to the browser later. With
`proxy_optimistic_data` the ClientHello follows the request at once, and a
proxy that keeps it can send it the moment it has connected
(zz-socks5-optimistic-data.patch; ci/tribal-rules.yml,
optimistic-data-is-opt-in-and-tls-only).

The guard's proxy does not answer a CONNECT until it has a whole ClientHello
from the browser, or until HOLD_SECONDS have passed with none. What it has, it
sends to the origin, and it answers the browser only once the origin has
received it. So the order of events is the measurement, not a clock: a
ClientHello the proxy holds before it has replied was sent ahead of the reply,
and one that comes only after the wait was not.

What PASS means, for a launch proxy given addresses and one given names:
    * with the option, every TLS connection's whole ClientHello (more than one
      Ethernet segment of it) reaches the proxy, and the origin, before the
      proxy has replied, after the username and password exchange, and the page
      loads; a speculative connection's does too, and the request that follows
      it is answered;
    * without the option, no connection sends a byte before the reply;
    * the ClientHello is the same size and lists the same extensions either way;
    * a plain HTTP connection sends nothing before the reply, with or without;
    * a CONNECT the proxy refuses fails the navigation with the same error,
      with or without, and is asked no more often with;
    * a proxy that discards what it received before its reply leaves the TLS
      handshake unanswered: the navigation fails with a timeout, not before
      the handshake's own has run out, and does not hang.

    python browser/tests/playwright/patches/socks5-optimistic-data.py
"""

import asyncio
import json
import re
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import own_address, resolve_binary  # noqa: E402

# How long the proxy waits for bytes ahead of its reply before it decides there
# are none. Only the cases that expect none wait it out.
HOLD_SECONDS = 3
# network.http.tls-handshake-timeout, shortened for the discarding proxy.
TLS_TIMEOUT_SECONDS = 5
# A proxy given addresses: the browser resolves by DoH alone, and no name is
# resolved here, the origin being an address.
DOH_ONLY = {"network.trr.mode": 3, "network.trr.uri": "https://doh.invalid/dns-query"}
# An address and a name of no machine, which the proxy takes for the origin.
# Firefox opens no speculative connection to a private address, which this
# host's own may be.
ELSEWHERE = "203.0.113.7"
NAME = "optimistic-origin.test"
USER, PASSWORD = "guard", "secret"


def client_hello(data: bytes):
    """The length of the ClientHello at the start of `data` and its extension
    types, or None while it is not all there."""
    message, offset = b"", 0
    while offset + 5 <= len(data) and data[offset] == 22:
        (length,) = struct.unpack("!H", data[offset + 3:offset + 5])
        message += data[offset + 5:offset + 5 + length]
        offset += 5 + length
        if len(message) >= 4 and len(message) >= 4 + int.from_bytes(message[1:4], "big"):
            break
    else:
        return None
    if offset > len(data) or message[0] != 1:
        return None
    at = 4 + 2 + 32
    at += 1 + message[at]
    at += 2 + int.from_bytes(message[at:at + 2], "big")
    at += 1 + message[at]
    end = at + 2 + int.from_bytes(message[at:at + 2], "big")
    at += 2
    extensions = []
    while at < end:
        extensions.append(int.from_bytes(message[at:at + 2], "big"))
        at += 4 + int.from_bytes(message[at + 2:at + 4], "big")
    return offset, extensions


async def pipe(src, dst) -> None:
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        dst.close()


class Origin:
    """An origin on TLS or plain HTTP. `arrived` is set, per source port, when
    a connection's ClientHello is whole at the origin."""

    def __init__(self, host: str, tls: ssl.SSLContext = None, body: bytes = b"ok") -> None:
        self.host, self.tls, self.body = host, tls, body
        self.arrived = {}
        self.requests = 0

    async def start(self) -> "Origin":
        self._inner = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self.tls)
        self._front = await asyncio.start_server(self._accept, self.host, 0)
        self.port = self._front.sockets[0].getsockname()[1]
        return self

    def hello_from(self, port: int) -> asyncio.Event:
        return self.arrived.setdefault(port, asyncio.Event())

    async def _accept(self, reader, writer) -> None:
        try:
            first = b""
            if self.tls:
                while client_hello(first) is None:
                    chunk = await reader.read(65536)
                    if not chunk:
                        return
                    first += chunk
                self.hello_from(writer.get_extra_info("peername")[1]).set()
            in_reader, in_writer = await asyncio.open_connection(*self._inner.sockets[0].getsockname())
            in_writer.write(first)
            await asyncio.gather(pipe(reader, in_writer), pipe(in_reader, writer))
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _serve(self, reader, writer) -> None:
        try:
            await reader.readline()
            while (await reader.readline()) not in (b"\r\n", b""):
                pass
            self.requests += 1
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nAccess-Control-Allow-Origin: *\r\n"
                         b"Connection: close\r\nContent-Length: %d\r\n\r\n%s" % (len(self.body), self.body))
            await writer.drain()
        except (ConnectionError, ssl.SSLError, OSError):
            pass
        finally:
            writer.close()


class Socks5Proxy:
    """A SOCKS5 proxy on loopback that asks for a username and password, takes
    `host` for every destination, and holds its reply to CONNECT until the
    browser's ClientHello is whole or HOLD_SECONDS pass. `refuse` and `discard`
    are destination ports: a CONNECT to the first is refused, and what came
    ahead of the reply to the second is thrown away."""

    def __init__(self, host: str, origins: list, refuse: int = 0, discard: int = 0) -> None:
        self.host, self.refuse, self.discard = host, refuse, discard
        self.origins = {origin.port: origin for origin in origins}
        self.connects = []  # one record per CONNECT, in order

    async def start(self) -> str:
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        return f"socks5://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    def to(self, port: int) -> list:
        return [c for c in self.connects if c["port"] == port]

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
        writer.write(b"\x05\x02")
        _, length = await reader.readexactly(2)
        user = await reader.readexactly(length)
        password = await reader.readexactly((await reader.readexactly(1))[0])
        if (user, password) != (USER.encode(), PASSWORD.encode()):
            writer.write(b"\x01\x01")
            return
        writer.write(b"\x01\x00")
        _, _cmd, _, atyp = await reader.readexactly(4)
        if atyp == 1:
            host = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
        else:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        (port,) = struct.unpack("!H", await reader.readexactly(2))
        record = {"atyp": atyp, "host": host, "port": port, "early": 0, "hello": None, "at_origin_first": False}
        self.connects.append(record)

        async def first_flight() -> bytes:
            data = b""
            while client_hello(data) is None:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                data += chunk
            return data

        # Everything the browser sends before this proxy has replied.
        reading = asyncio.ensure_future(first_flight())
        done, _ = await asyncio.wait([reading], timeout=HOLD_SECONDS)
        early = reading.result() if done else b""
        record["early"] = len(early)
        record["hello"] = client_hello(early)

        origin = self.origins.get(port)
        if port == self.refuse:
            reading.cancel()
            writer.write(b"\x05\x05\x00\x01" + bytes(6))
            return
        up_reader, up_writer = await asyncio.open_connection(self.host, port)
        if early and port != self.discard:
            up_writer.write(early)
            if origin.tls:
                await origin.hello_from(up_writer.get_extra_info("sockname")[1]).wait()
                record["at_origin_first"] = True
        writer.write(b"\x05\x00\x00\x01" + bytes(6))
        if not done and origin.tls:
            late = await reading
            record["hello"] = client_hello(late)
            up_writer.write(late)
        elif not done:
            reading.cancel()
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))


def error_code(error: Exception) -> str:
    found = re.search(r"NS_ERROR_\w+", str(error))
    return found.group(0) if found else str(error)[:80]


async def run(mode: str, optimistic: bool, host: str, tls: ssl.SSLContext) -> dict:
    """One launch behind a proxy given addresses ('doh') or names ('exit')."""
    from camoufox import DefaultAddons
    from camoufox.async_api import AsyncCamoufox

    name = ELSEWHERE if mode == "doh" else NAME
    page_origin = await Origin(host, tls).start()
    linked = await Origin(host, tls).start()
    refused = await Origin(host, tls).start()
    discarded = await Origin(host, tls).start()
    plain = await Origin(host, body=f'<!doctype html><link rel="preconnect" href="https://{name}:{linked.port}">'
                         .encode()).start()
    proxy = Socks5Proxy(host, [page_origin, linked, refused, discarded, plain],
                        refuse=refused.port, discard=discarded.port)
    seen = {}
    async with AsyncCamoufox(
        headless=True, executable_path=str(resolve_binary()), geoip=False, i_know_what_im_doing=True,
        proxy={"server": await proxy.start(), "username": USER, "password": PASSWORD},
        proxy_dns=mode, proxy_optimistic_data=optimistic,
        # uBlock Origin turns speculative connections off once it has started
        # (privacy.network.networkPredictionEnabled), which the wait outlasts.
        exclude_addons=[DefaultAddons.UBO],
        firefox_user_prefs={**(DOH_ONLY if mode == "doh" else {}),
                            "network.http.tls-handshake-timeout": TLS_TIMEOUT_SECONDS},
    ) as browser:
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        # A page whose only use of the linked origin is a preconnect: the
        # connection the proxy carries to it has no request behind it.
        await page.goto(f"http://{name}:{plain.port}/")
        deadline = time.monotonic() + 3 * HOLD_SECONDS
        while not (proxy.to(linked.port) and proxy.to(linked.port)[0]["hello"]) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        seen["speculative"] = proxy.to(linked.port)[:1]
        seen["fetched"] = await page.evaluate(
            "url => fetch(url).then(r => r.text(), e => 'error: ' + e)", f"https://{name}:{linked.port}/")

        await page.goto(f"https://{name}:{page_origin.port}/")
        seen["page"] = await page.inner_text("body")

        try:
            await page.goto(f"https://{name}:{refused.port}/")
            seen["refused"] = "loaded"
        except Exception as error:  # noqa: BLE001 - the error is the result
            seen["refused"] = error_code(error)

        if optimistic:
            started = time.monotonic()
            try:
                await page.goto(f"https://{name}:{discarded.port}/")
                seen["discarded"] = "loaded"
            except Exception as error:  # noqa: BLE001
                seen["discarded"] = error_code(error)
            seen["discarded_seconds"] = round(time.monotonic() - started)

    seen.update(atyps={c["atyp"] for c in proxy.connects if c["port"] in proxy.origins}, tls=proxy.to(page_origin.port) + proxy.to(linked.port),
                plain=proxy.to(plain.port), refusals=proxy.to(refused.port), discards=proxy.to(discarded.port))
    return seen


def certificate(directory: str) -> ssl.SSLContext:
    cert, key = f"{directory}/cert.pem", f"{directory}/key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                    "-days", "2", "-subj", "/CN=origin", "-addext", f"subjectAltName=IP:{ELSEWHERE},DNS:{NAME}"],
                   check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    return tls


async def main() -> int:
    host = own_address()
    with tempfile.TemporaryDirectory() as directory:
        tls = certificate(directory)
    checks = []
    for mode, atyp in (("doh", 1), ("exit", 3)):
        on = await run(mode, True, host, tls)
        off = await run(mode, False, host, tls)
        given = "addresses" if mode == "doh" else "names"
        hellos = [c["hello"] for c in on["tls"]]
        checks += [
            (f"[{given}] the proxy is given {given}", on["atyps"] == off["atyps"] == {atyp},
             (on["atyps"], off["atyps"])),
            (f"[{given}] with the option every ClientHello is whole at the proxy, and at the origin, before the reply",
             bool(on["tls"]) and all(c["hello"] and c["early"] == c["hello"][0] and c["at_origin_first"]
                                     for c in on["tls"]),
             [(c["early"], c["at_origin_first"]) for c in on["tls"]]),
            (f"[{given}] that ClientHello is more than one Ethernet segment", all(h and h[0] > 1460 for h in hellos),
             [h and h[0] for h in hellos]),
            (f"[{given}] with the option the page loads", on["page"] == "ok", on["page"]),
            (f"[{given}] a speculative connection's ClientHello is ahead of the reply too, and the request is answered",
             bool(on["speculative"]) and bool(on["speculative"][0]["hello"]) and on["fetched"] == "ok",
             (on["speculative"], on["fetched"])),
            (f"[{given}] without the option no connection sends a byte before the reply, and the page loads",
             bool(off["tls"]) and not any(c["early"] for c in off["tls"] + off["plain"] + off["refusals"])
             and off["page"] == "ok" and off["fetched"] == "ok", [c["early"] for c in off["tls"]]),
            (f"[{given}] a plain HTTP connection sends nothing before the reply, with the option",
             bool(on["plain"]) and not any(c["early"] for c in on["plain"]), [c["early"] for c in on["plain"]]),
            (f"[{given}] a refused CONNECT is the same error with and without, and is asked no more often",
             on["refused"] == off["refused"] != "loaded" and 0 < len(on["refusals"]) <= len(off["refusals"]),
             (on["refused"], off["refused"], len(on["refusals"]), len(off["refusals"]))),
            (f"[{given}] a proxy that discards the early bytes leaves the handshake to time out",
             on["discarded"] == "NS_ERROR_NET_TIMEOUT" and on["discarded_seconds"] >= TLS_TIMEOUT_SECONDS,
             (on["discarded"], on["discarded_seconds"], len(on["discards"]))),
        ]
        # Compared within a mode: a connection by name carries the name, one by address does not.
        both = (hellos[:1], [c["hello"] for c in off["tls"]][:1])
        checks.append((f"[{given}] the ClientHello is the same size, with the same extensions, either way",
                       bool(both[0]) and both[0] == both[1] and both[0][0] is not None,
                       [h and (h[0], len(h[1])) for pair in both for h in pair]))

    failed = 0
    for label, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}  {json.dumps(detail, default=str)}")
        failed += not ok
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
