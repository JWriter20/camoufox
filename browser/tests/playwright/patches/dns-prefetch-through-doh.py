"""
Verify a proxied browser that resolves by DoH makes the speculative lookups
an unproxied one makes, and makes them through the proxy.

Firefox resolves the name of a `<link rel=dns-prefetch>` and of a link in the
page before anything is asked of it, and the name's nameserver sees the
question. It stops for the life of the browser once a request has gone
through a proxy (nsProtocolProxyService::MaybeDisableDNSPrefetch), because
the lookup would be made by this machine. With proxy_dns='doh' it is made by
DoH, through the proxy, so the launcher lets it happen
(network.dns.prefetch_via_proxy). With proxy_dns='exit' there is no resolver
but this machine's, and it stays off.

The guard serves a page with a dns-prefetch link and an anchor, which it
hovers, from an address of TEST-NET-3 on a docker bridge of this host, with a
DoH resolver there and a SOCKS5 proxy. The browser's resolver log shows what
this machine's resolver was asked.

What PASS means:
    * with no proxy and DoH, the dns-prefetch link's name is asked (the
      guard's reference);
    * behind the proxy with proxy_dns='doh', the resolver is asked the same
      questions, name by name and type by type, no more and no fewer;
    * every connection to the resolver was made by the proxy, the proxy was
      given no name, and this machine's resolver was asked for none;
    * with proxy_dns='exit', this machine resolves nothing and the
      dns-prefetch link's name is asked of nobody.

    python browser/tests/playwright/patches/dns-prefetch-through-doh.py
"""

import asyncio
import importlib.util
import shutil
import socket
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from helpers import free_port, marionette_args, marionette_env, resolve_binary  # noqa: E402


def sibling(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The public address, the DoH resolver and its certificate; the SOCKS5 proxy.
record = sibling("http3-https-record-socks5")
relayed = record.relayed

LINKED, ANCHOR = f"linked.{record.ZONE}", f"anchor.{record.ZONE}"
PAGE = (f'<!doctype html><link rel="dns-prefetch" href="//{LINKED}">'
        f'<a href="http://{ANCHOR}/">a link</a>').encode()
# Firefox resolves a page's links in batches, off a timer.
SETTLE_SECONDS = 4


class Resolver(record.Resolver):
    """Answers every name with ADDRESS, and keeps what it was asked."""

    def __init__(self, cert: str, key: str) -> None:
        super().__init__(cert, key)
        self.asked = {}  # name -> the record types asked for it
        self.connections = 0

    def record(self, name: str, qtype: int) -> bytes:
        # An HTTPS record for another port than 443 is asked at _port._https.
        asked = ".".join(label for label in name.split(".") if not label.startswith("_"))
        self.asked.setdefault(asked, set()).add(qtype)
        return socket.inet_aton(record.ADDRESS) if qtype == 1 else b""

    async def serve(self, reader, writer) -> None:
        self.connections += 1
        await super().serve(reader, writer)


async def serve_page(reader, writer) -> None:
    try:
        await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError:  # a speculative connection, never used
        return writer.close()
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n"
                 b"Connection: close\r\n\r\n%s" % (len(PAGE), PAGE))
    await writer.drain()
    writer.close()


async def visit(certificate: tuple, mode: str) -> dict:
    """Load the page and hover its link with no proxy ('direct') or behind
    one, with proxy_dns `mode`: who was asked for what."""
    from camoufox import DefaultAddons
    from camoufox.async_api import AsyncCamoufox

    cert, key, trust = certificate
    resolver = Resolver(cert, key)
    doh = await resolver.start()
    doh_port = resolver.server.sockets[0].getsockname()[1]
    page_server = await asyncio.start_server(serve_page, record.ADDRESS, 0)
    proxy = relayed.Socks5Proxy(record.ADDRESS)
    port = free_port()
    log = tempfile.TemporaryDirectory()
    prefs = {"marionette.port": port}
    if mode != "exit":
        prefs.update({"network.trr.mode": 3, "network.trr.uri": doh, "network.trr.confirmationNS": "skip"})
    browser = await AsyncCamoufox(
        headless=True, executable_path=str(resolve_binary()), geoip=False, i_know_what_im_doing=True,
        args=marionette_args(), firefox_user_prefs=prefs,
        # uBlock Origin's "disable pre-fetching" setting, on by default, turns
        # every speculative lookup off, with or without a proxy.
        exclude_addons=[DefaultAddons.UBO],
        env={**marionette_env(), "MOZ_LOG": "append,nsHostResolver:5", "MOZ_LOG_FILE": f"{log.name}/resolver"},
        **({} if mode == "direct" else {"proxy": {"server": await proxy.start()}, "proxy_dns": mode}),
    ).__aenter__()
    try:
        await trust(browser, port)
        page = await browser.new_page()
        # The guard's proxy has no resolver, so in 'exit' it is given an address.
        host = record.ADDRESS if mode == "exit" else f"page.{record.ZONE}"
        await page.goto(f"http://{host}:{page_server.sockets[0].getsockname()[1]}/")
        await page.hover("a")
        await asyncio.sleep(SETTLE_SECONDS)
    finally:
        await browser.close()
        page_server.close()
        resolver.close()
        if mode != "direct":
            proxy.close()
    logged = "".join(path.read_text(errors="replace") for path in Path(log.name).iterdir())
    log.cleanup()
    return {
        "asked": {name: sorted(types) for name, types in sorted(resolver.asked.items())},
        "resolver connections": resolver.connections,
        "through the proxy": sum(to == doh_port for _, _, to in proxy.connects),
        "names given to the proxy": sorted(host for atyp, host, _ in proxy.connects if host.endswith(record.ZONE)),
        "asked of this machine": sorted(name for name in (LINKED, ANCHOR) if f"getaddrinfo for host [{name}]" in logged),
    }


async def main() -> int:
    if shutil.which("docker") is None:
        print("  FAIL docker is needed to give this host a public address")
        return 1
    tmp = tempfile.TemporaryDirectory()
    certificate = record.certificate(tmp.name)
    address = record.PublicAddress()
    try:
        direct = await visit(certificate, "direct")
        doh = await visit(certificate, "doh")
        through_exit = await visit(certificate, "exit")
    finally:
        address.close()

    checks = [
        ("no proxy: the dns-prefetch link's name is asked of the resolver (the guard's reference)",
         LINKED in direct["asked"], direct["asked"]),
        ("proxy_dns='doh': the resolver is asked what it is asked with no proxy",
         doh["asked"] == direct["asked"], doh["asked"]),
        ("proxy_dns='doh': every connection to the resolver was made by the proxy",
         0 < doh["resolver connections"] == doh["through the proxy"],
         (doh["resolver connections"], doh["through the proxy"])),
        ("proxy_dns='doh': the proxy is given no name, and this machine's resolver is asked for none",
         not doh["names given to the proxy"] and not doh["asked of this machine"],
         (doh["names given to the proxy"], doh["asked of this machine"])),
        # The hovered link's name is the proxy's to resolve: the speculative
        # connection to it goes through the proxy, as the navigation would.
        ("proxy_dns='exit': no name is asked of the resolver or this machine, and the proxy is not given the dns-prefetch link's",
         not through_exit["asked"] and LINKED not in through_exit["names given to the proxy"]
         and not through_exit["asked of this machine"],
         (through_exit["asked"], through_exit["names given to the proxy"], through_exit["asked of this machine"])),
    ]
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}: {detail}")
    passed = sum(ok for _, ok, _ in checks)
    print(f"{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
