"""
Verify a browser that resolves names by DoH through its SOCKS5 proxy asks
this machine's resolver nothing, its DoH server's name included.

With proxy_dns='doh' every lookup is a DoH request through the launch proxy,
and the exit resolves the DoH request's own host (docs/proxy-dns.md). DNS
prefetch stays on, and Firefox prefetched the host of every DoH request unless
an HTTP proxy carried it. TRR never answers for its own server's name, so that
prefetch asked this machine's resolver: a resolver beside the user saw which
DoH server the browser uses, at every launch. uBlock Origin turns prefetch off
once it has started, so the guard launches without it, as the browser is
until then.

The guard serves the uBlock Origin guard's site and DoH resolver, the resolver
at a name only the proxy resolves, behind a SOCKS5 proxy on this machine, and
loads the site's page. It checks that:

    * the page loads, so its names were resolved by DoH through the proxy;
    * the proxy is given the resolver's name alone;
    * no name is resolved by this machine (nsHostResolver's log).

    python browser/tests/playwright/patches/doh-proxy-resolves-nothing-here.py
"""

import asyncio
import importlib.util
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from helpers import Marionette, free_port, marionette_args, marionette_env, resolve_binary  # noqa: E402

# The site, resolver, proxy and authority of the uBlock Origin guard.
_spec = importlib.util.spec_from_file_location("ublock_connections_socks5", HERE / "ublock-connections-socks5.py")
site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site)

NATIVE = re.compile(r"Calling getaddrinfo for host \[([^\]]+)\]")


async def main() -> int:
    from camoufox import DefaultAddons
    from camoufox.async_api import AsyncCamoufox

    if shutil.which("docker") is None:
        print("  FAIL docker is needed to give this host a public address")
        return 1
    tmp = tempfile.TemporaryDirectory()
    cert, key, trust = site.authority(tmp.name)
    log = tempfile.TemporaryDirectory()
    address = site.record.PublicAddress()
    network = site.Network(cert, key, site.NEAR)
    proxy = site.record.DistantProxy(local=True)
    try:
        await network.start()
        proxy.names = {f"doh.{site.ZONE}": site.ADDRESS}
        port = free_port()
        browser = await AsyncCamoufox(
            headless=True, executable_path=str(resolve_binary()), geoip=False, i_know_what_im_doing=True,
            args=marionette_args(), exclude_addons=[DefaultAddons.UBO],
            env={**marionette_env(), "MOZ_LOG": "append,nsHostResolver:5", "MOZ_LOG_FILE": f"{log.name}/resolver"},
            proxy={"server": await proxy.start()},
            firefox_user_prefs={
                # The launcher's proxy_dns='doh' is in force, at this
                # resolver's name. It answers over HTTP/2 and reads no
                # header, so the query has to be the request's body.
                "network.trr.uri": network.doh_by_name, "network.trr.useGET": False,
                "network.http.http3.disable_when_third_party_roots_found": False, "marionette.port": port},
        ).__aenter__()
        try:
            # Marionette's session needs a window to attach to.
            window = await browser.new_page()
            marionette = Marionette(port)
            try:
                await asyncio.to_thread(marionette.js, trust)
            finally:
                marionette.close()
            await window.close()
            page = await (await browser.new_context()).new_page()
            response = await page.goto(network.url)
        finally:
            await browser.close()
    finally:
        network.close()
        proxy.close()
        address.close()
    resolved = sorted({name for path in Path(log.name).iterdir()
                       for name in NATIVE.findall(path.read_text(errors="replace"))})
    given = sorted({host for atyp, host, _ in proxy.connects if atyp == 3})
    checks = [
        ("the page loads through the proxy", bool(response and response.ok), response and response.status),
        ("the proxy is given the resolver's name alone", given == [f"doh.{site.ZONE}"], given),
        ("no name is resolved by this machine", not resolved, resolved),
    ]
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}: {detail}")
    passed = sum(ok for _, ok, _ in checks)
    print(f"{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
