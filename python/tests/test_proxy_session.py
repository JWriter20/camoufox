"""
`managed=` on a standard Camoufox build (docs/pro.md, "Managed proxies"): no lease, so the launcher opens a
proxy session, renews it while the browser lives, and releases it when the browser closes. Against a fake
API on localhost; no browser.
"""

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from camoufox import Managed, ProxyNetwork, ProxyProvider, ProxyType, pro, proxies, proxy_session, utils
from camoufox.exceptions import PlanRequired

KEY = "cfp_live_" + "a" * 40
SESSION = {
    "session_id": "pxs_0192f0c4",
    "server": "http://100.64.12.34:7190",
    "username": "pxs_0192f0c4",
    "password": "secret",
    "class": "residential",
    "country": "US",
    "sticky": True,
    "exit_ip": "98.97.12.34",
    "binding_id": 7,
    "name": None,
    "expires_at": "2026-11-04T12:00:00Z",
    "notices": [],
}


class FakeApi:
    def __init__(self):
        self.requests = []
        self.session = dict(SESSION)
        self.refuse = None
        self.statics = []
        self.changed = threading.Condition()

    def answer(self, method, path, body):
        if self.refuse is not None:
            return self.refuse
        if method == "POST" and path == "/api/v1/proxy/sessions":
            return 201, self.session
        if re.fullmatch(r"/api/v1/proxy/sessions/[^/]+/heartbeat", path):
            return 200, {"expires_at": "2026-11-04T12:01:00Z"}
        if re.fullmatch(r"/api/v1/proxy/sessions/[^/]+/release", path):
            return 200, {"released": True}
        if method == "GET" and path == "/api/v1/proxy/statics":
            return 200, {"statics": self.statics}
        if method == "DELETE" and path.startswith("/api/v1/proxy/statics/"):
            return 200, {"released": True}
        return 404, {"error": "not_found", "message": path}

    def calls(self, method, route):
        return [r for r in self.requests if r["method"] == method and re.fullmatch(route, r["path"])]

    def wait_for(self, predicate, timeout=5):
        with self.changed:
            assert self.changed.wait_for(predicate, timeout), self.requests


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = FakeApi()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.loads(raw) if raw else None
            status, answer = fake.answer(self.command, self.path, body)
            with fake.changed:
                fake.requests.append(
                    {"method": self.command, "path": self.path, "body": body, "auth": self.headers.get("Authorization")}
                )
                fake.changed.notify_all()
            payload = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_DELETE = _handle

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv(pro.API_ENV, f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    monkeypatch.delenv(pro.LEASE_FILE_ENV, raising=False)
    yield fake
    proxy_session._release_all()
    server.shutdown()


class Geo:
    def __init__(self, ip):
        self.ip = ip

    def as_config(self):
        return {"timezone": "America/New_York", "locale:language": "en", "locale:region": "US",
                "geolocation:latitude": 40.7, "geolocation:longitude": -74.0, "geolocation:accuracy": 100}  # fmt: skip


@pytest.fixture
def launch(api, tmp_path, monkeypatch):
    # A standard build: its files, and no pro-build.json.
    directory = tmp_path / "build"
    directory.mkdir()
    (directory / "properties.json").write_text(json.dumps([{"property": "screen.width", "type": "uint"}]))
    looked_up, configs = [], []
    monkeypatch.setattr(utils, "resolve_verstr", lambda *a: "152.0.4-beta.31")
    monkeypatch.setattr(utils, "public_ip", lambda *a: looked_up.append(a[0] if a else None) or "203.0.113.9")
    monkeypatch.setattr(utils, "get_geolocation", lambda ip, **k: Geo(ip))
    monkeypatch.setattr(utils, "geoip_allowed", lambda: None)
    monkeypatch.setattr(utils, "get_env_vars", lambda config, *a, **k: configs.append(dict(config)) or {})

    def run(**kwargs):
        options = {"executable_path": directory / "camoufox-bin", "os": "windows", "headless": True,
                   "i_know_what_im_doing": True, **kwargs}  # fmt: skip
        return utils.launch_options(**options)

    run.looked_up = looked_up
    run.configs = configs
    return run


class Browser:
    """What attach_lease needs of a Playwright browser."""

    def __init__(self):
        self.handlers = {}
        self.closed = False

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def close(self):
        self.closed = True


# ── the session ──────────────────────────────────────────────────────────────


def test_managed_on_a_standard_build_opens_a_proxy_session(api, launch):
    managed = Managed(provider=ProxyProvider.EVOMI, type=ProxyType.ROTATING, country="US")
    opts = launch(managed=managed)
    (opened,) = api.calls("POST", "/api/v1/proxy/sessions")
    assert opened["body"]["managed"] == {"provider": "evomi", "type": "rotating", "country": "US"}
    assert opened["body"]["client"].startswith("camoufox-python/")
    assert opened["auth"] == f"Bearer {KEY}"
    assert opts["proxy"] == {
        "server": "http://100.64.12.34:7190",
        "username": "pxs_0192f0c4",
        "password": "secret",
        "bypass": "localhost,127.0.0.1,::1,*.local",
    }
    # Located at the exit the API names, without looking it up.
    assert launch.configs[-1]["webrtc:ipv4"] == "98.97.12.34"
    assert launch.looked_up == []


def test_the_session_is_released_when_the_browser_closes(api, launch):
    opts = launch(managed=True)
    session = pro.claim(opts)
    assert isinstance(session, proxy_session.ProxySession)
    browser = pro.attach_lease(session, Browser(), "disconnected")
    assert browser.pro.lease_id == "pxs_0192f0c4"
    browser.close()
    assert browser.closed
    (released,) = api.calls("POST", "/api/v1/proxy/sessions/pxs_0192f0c4/release")
    assert released["body"] == {"reason": "clean_exit"}
    # Only once, however it ends.
    session.release()
    assert len(api.calls("POST", "/api/v1/proxy/sessions/pxs_0192f0c4/release")) == 1


def test_one_set_of_options_is_one_browser(api, launch):
    opts = launch(managed=True)
    pro.claim(opts)
    with pytest.raises(ValueError, match="already held by another browser"):
        pro.claim(opts)


def test_a_failed_launch_releases_the_session(api, launch):
    opts = launch(managed=True)
    pro.launch_failed(pro.claim(opts), RuntimeError("the browser did not start"))
    (released,) = api.calls("POST", "/api/v1/proxy/sessions/pxs_0192f0c4/release")
    assert released["body"] == {"reason": "error"}


def test_the_session_is_renewed_while_the_browser_lives(api, launch):
    api.session = {**SESSION, "heartbeat_s": 0.05}
    session = pro.claim(launch(managed=True))
    api.wait_for(lambda: len(api.calls("POST", "/api/v1/proxy/sessions/pxs_0192f0c4/heartbeat")) >= 2)
    session.release()
    assert session.egress["expires_at"] == "2026-11-04T12:01:00Z"


def test_the_api_s_notices_are_said(api, launch, caplog):
    api.session = {**SESSION, "notices": [{"code": "static_ip_replaced", "message": "Its static IP was released."}]}
    with caplog.at_level(logging.WARNING, logger="camoufox.pro"):
        launch(managed=Managed(provider=ProxyProvider.EVOMI, type=ProxyType.STATIC, name="shop-ip"))
    assert "camoufox: Its static IP was released. (static_ip_replaced)" in caplog.text
    assert api.calls("POST", "/api/v1/proxy/sessions")[0]["body"]["managed"] == {
        "provider": "evomi", "type": "static", "name": "shop-ip"
    }  # fmt: skip


def test_a_refusal_is_the_api_s_error(api, launch):
    api.refuse = (402, {"error": "plan_required", "message": "CamouLink needs a Camoufox Pro plan.",
                        "details": {"feature": "camoulink"}})  # fmt: skip
    with pytest.raises(PlanRequired, match="CamouLink needs a Camoufox Pro plan"):
        launch(managed=Managed(provider=ProxyProvider.CAMOULINK))


def test_no_key_says_how_to_sign_in(api, launch, monkeypatch):
    monkeypatch.delenv(pro.KEY_ENV)
    with pytest.raises(pro.NotSignedIn, match="camoufox login"):
        launch(managed=True)
    assert api.requests == []


# ── when there is no session ─────────────────────────────────────────────────


def test_no_managed_means_no_proxy_and_no_call(api, launch):
    opts = launch()
    assert "proxy" not in opts or not opts["proxy"]
    launch(managed=False)
    assert api.requests == []


def test_your_own_proxy_is_never_replaced(api, launch):
    proxy = {"server": "http://my.proxy:8080"}
    assert launch(proxy=proxy, geoip="198.51.100.1")["proxy"] == proxy
    with pytest.raises(ValueError, match="proxy and managed conflict"):
        launch(proxy=proxy, managed=True)
    assert api.requests == []


def test_profiles_still_need_a_pro_build(api, launch):
    with pytest.raises(ValueError, match="profile, warm_plan and farm_gpu need a Camoufox Pro build"):
        launch(profile="shop-1")


# ── named static IPs ─────────────────────────────────────────────────────────


def test_the_statics_the_account_rents(api):
    api.statics = [{"name": "shop-ip", "provider": "evomi", "network": "isp", "country": "US",
                    "exit_ip": "198.51.100.7", "paid_through": "2026-12-04T00:00:00Z"}]  # fmt: skip
    (static,) = proxies.list_statics()
    assert (static.name, static.provider, static.network, static.exit_ip) == (
        "shop-ip", ProxyProvider.EVOMI, ProxyNetwork.ISP, "198.51.100.7"
    )  # fmt: skip
    proxies.release_static("shop ip/2")
    (deleted,) = api.calls("DELETE", "/api/v1/proxy/statics/.*")
    assert deleted["path"] == "/api/v1/proxy/statics/shop%20ip%2F2"
    assert deleted["auth"] == f"Bearer {KEY}"
