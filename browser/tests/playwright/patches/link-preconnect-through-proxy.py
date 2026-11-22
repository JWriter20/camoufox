"""
Verify <link rel=preconnect> opens its connection behind a proxy, through it.

A page's preconnect link makes Firefox open a connection to the link's origin
before anything is requested from it, and that origin sees the connection
arrive. Firefox makes no speculative connection at all once a proxy is in
play, so a proxied Camoufox was the one visitor whose preconnect links did
nothing. zz-proxy-speculative-connect.patch opens the connection through the
proxy the request would use (ci/tribal-rules.yml,
speculative-connects-go-through-the-proxy).

Firefox opens no speculative connection to a private address (RFC 1918), and
proxies nothing to loopback. So the direct reference links to a target on
loopback, and the proxied cases link to an address of no machine (TEST-NET-3)
or a name of none, which the guard's proxies take for the target: a
connection the target hears there came through the proxy.

What PASS means:
    * with no proxy, the target hears the preconnect (the guard's reference);
    * behind a SOCKS5 launch proxy, which is given addresses, the target
      hears it and the proxy carried a CONNECT to it by address;
    * behind a context's own SOCKS5 proxy, which is given names, the proxy
      carried a CONNECT to the target by name, and this machine's resolver
      was never asked for that name.

An HTTP proxy is not covered: Firefox's speculative connection carries no
request, so behind one it would reach the proxy and no further.

    python browser/tests/playwright/patches/link-preconnect-through-proxy.py
"""

import http.server
import os
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import own_address, resolve_binary  # noqa: E402

# A speculative connection is opened after the load that asked for it.
WAIT_SECONDS = 10
# An address and a name of no machine, which a proxy here takes for the target.
ELSEWHERE = "203.0.113.7"
ELSEWHERE_NAME = "preconnect-target.test"


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = self.server.body
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class Listener(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Serves `body` and counts every TCP connection, used or not."""

    daemon_threads = True

    def __init__(self, host: str) -> None:
        super().__init__((host, 0), Page)
        self.body = b"<!doctype html><title>target</title>"
        self.connections = 0
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def get_request(self):
        accepted = super().get_request()
        self.connections += 1
        return accepted

    def heard(self) -> int:
        """The connection count once there is one, or after the wait."""
        deadline = time.monotonic() + WAIT_SECONDS
        while not self.connections and time.monotonic() < deadline:
            time.sleep(0.1)
        return self.connections


class Socks5Proxy(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """A SOCKS5 proxy (CONNECT only) that takes `host` for every destination and records each request."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str) -> None:
        self.host = host
        self.connects = []  # (address type, host, port)
        super().__init__(("127.0.0.1", 0), self.Handler)
        self.url = f"socks5://127.0.0.1:{self.server_address[1]}"
        threading.Thread(target=self.serve_forever, daemon=True).start()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            read = self.rfile.read
            _version, methods = read(2)
            read(methods)
            self.wfile.write(b"\x05\x00")
            _version, command, _reserved, atyp = read(4)
            if atyp == 1:
                host = socket.inet_ntoa(read(4))
            elif atyp == 3:
                host = read(read(1)[0]).decode()
            else:
                host = socket.inet_ntop(socket.AF_INET6, read(16))
            (port,) = struct.unpack(">H", read(2))
            self.server.connects.append((atyp, host, port))
            if command != 1:
                self.wfile.write(b"\x05\x07\x00\x01" + bytes(6))
                return
            upstream = socket.create_connection((self.server.host, port))
            self.wfile.write(b"\x05\x00\x00\x01" + bytes(6))

            def pipe(source, sink) -> None:
                try:
                    while data := source.recv(65536):
                        sink.sendall(data)
                except OSError:
                    pass
                finally:
                    for end in (source, sink):
                        try:
                            end.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            threading.Thread(target=pipe, args=(upstream, self.connection), daemon=True).start()
            pipe(self.connection, upstream)


def linking_to(target: str) -> bytes:
    return f'<!doctype html><title>page</title><link rel="preconnect" href="http://{target}">'.encode()


def main() -> int:
    from camoufox.sync_api import Camoufox

    host = own_address()
    origin = Listener(host)
    resolver_log = tempfile.TemporaryDirectory()
    env = {**os.environ, "MOZ_LOG": "append,nsHostResolver:5", "MOZ_LOG_FILE": str(Path(resolver_log.name) / "resolver")}
    launch = dict(headless=True, os="linux", executable_path=str(resolve_binary()), i_know_what_im_doing=True,
                  geoip=False, env=env)
    checks = []

    # No proxy: what the target hears from this browser on its own.
    target = Listener("127.0.0.1")
    origin.body = linking_to(f"127.0.0.1:{target.port}")
    with Camoufox(**launch) as browser:
        browser.new_page().goto(f"http://{host}:{origin.port}/")
        direct = target.heard()
    checks.append(("with no proxy the target hears the preconnect (the guard's reference)", direct >= 1, direct))

    # A SOCKS5 launch proxy, given addresses.
    target = Listener(host)
    origin.body = linking_to(f"{ELSEWHERE}:{target.port}")
    proxy = Socks5Proxy(host)
    with Camoufox(**launch, proxy={"server": proxy.url}) as browser:
        browser.new_page().goto(f"http://{host}:{origin.port}/")
        heard = target.heard()
    carried = [c for c in proxy.connects if c[2] == target.port]
    checks.append(("behind a SOCKS5 launch proxy the proxy carried the preconnect, by address",
                   heard >= 1 and bool(carried) and set(carried) == {(1, ELSEWHERE, target.port)}, (heard, carried)))

    # A context's own SOCKS5 proxy, given names.
    target = Listener(host)
    origin.body = linking_to(f"{ELSEWHERE_NAME}:{target.port}")
    proxy = Socks5Proxy(host)
    with Camoufox(**launch) as browser:
        context = browser.new_context(proxy={"server": proxy.url})
        context.new_page().goto(f"http://preconnect-page.test:{origin.port}/")
        heard = target.heard()
    carried = [c for c in proxy.connects if c[2] == target.port]
    resolved = "".join(path.read_text(errors="replace") for path in Path(resolver_log.name).iterdir())
    resolver_log.cleanup()
    checks += [
        ("behind a context's SOCKS5 proxy the proxy carried the preconnect, by name",
         heard >= 1 and bool(carried) and set(carried) == {(3, ELSEWHERE_NAME, target.port)}, (heard, carried)),
        ("this machine's resolver was not asked for the target's name", ELSEWHERE_NAME not in resolved, ""),
    ]

    passed = True
    for label, ok, got in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}  {got}")
        passed &= ok
    print(f"{sum(1 for _l, ok, _g in checks if ok)}/{len(checks)} checks passed")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
