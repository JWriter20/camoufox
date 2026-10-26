"""Camoufox Pro's render node: the coordinator assignment a launch holds and hands to the build.

The coordinator is a local fake answering as renderfarm's assignment API does
(coordinator/render/assignment_http.go); no test reaches the network.
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_pro import KEY, api, build, launch, mode, pro_build  # noqa: F401 (fixtures)

from camoufox import pro, render_node, utils
from camoufox.fingerprints import generate_context_fingerprint

TOKEN = "client-token"
GPU = "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or similar"


class FakeCoordinator:
    def __init__(self):
        self.requests = []
        self.scripted = []
        self.changed = threading.Condition()
        self.released = set()

    def calls(self, method, route):
        return [r for r in self.requests if r["method"] == method and re.fullmatch(route, r["path"])]

    def wait_for(self, predicate, timeout=5):
        with self.changed:
            assert self.changed.wait_for(predicate, timeout), self.requests

    def answer(self, method, path, body):
        if self.scripted and method == "POST" and path == "/assignments":
            return self.scripted.pop(0)
        if method == "POST" and path == "/assignments":
            return 201, {}, {
                "assignment_id": "a" * 32,
                "browser_id": body["browser_id"],
                "renderer_id": "win-i9-angle-156.0.1-10de25a0-firefox",
                "profile_digest": "d" * 64,
                "hello": {"gpu_summary": "NVIDIA GeForce RTX 3050 Ti Laptop GPU"},
                "webgpu_ready": False,
                "expires_at": "2026-10-26T17:14:09Z",
                "route": "angle",
            }
        match = re.fullmatch(r"/assignments/(\w+)(/renew)?", path)
        if match and match.group(1) not in self.released:
            if method == "DELETE":
                self.released.add(match.group(1))
                return 204, {}, None
            if method == "POST" and match.group(2):
                return 200, {}, {"assignment_id": match.group(1), "route": "angle"}
        return 404, {}, None


@pytest.fixture
def coordinator(monkeypatch, tmp_path):
    fake = FakeCoordinator()

    class Handler(BaseHTTPRequestHandler):
        def handle_one(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            status, headers, answer = fake.answer(method, self.path, body)
            with fake.changed:
                fake.requests.append(
                    {"method": method, "path": self.path, "body": body, "auth": self.headers.get("Authorization")}
                )
                fake.changed.notify_all()
            payload = b"" if answer is None else (answer if isinstance(answer, bytes) else json.dumps(answer).encode())
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            self.handle_one("POST")

        def do_DELETE(self):
            self.handle_one("DELETE")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    fake.origin = f"http://127.0.0.1:{server.server_address[1]}"
    fake.config = {"coordinator": fake.origin, "token": TOKEN, "gpu": GPU}
    monkeypatch.delenv(render_node.CONFIG_ENV, raising=False)
    monkeypatch.setattr(render_node, "DEFAULT_PATH", tmp_path / "render-node.json")
    yield fake
    server.shutdown()


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)


def node_launch(build, monkeypatch, **kwargs):
    kwargs.setdefault("os", "windows")
    config = {}
    monkeypatch.setattr(utils, "get_env_vars", lambda drawn, *a, **k: config.update(drawn) or {})
    monkeypatch.setattr(utils, "resolve_verstr", lambda *a: "156.0.1-beta.32")
    return utils.launch_options(executable_path=build, headless=True, **kwargs), config


def held_lease(opts):
    return pro._LIVE[opts["env"][pro.LEASE_FILE_ENV]]


def test_nothing_configured_draws_locally(coordinator):
    assert render_node.resolve(None) is None
    assert render_node.resolve(False) is None


def test_the_configuration_is_found_where_documented(coordinator, monkeypatch, tmp_path):
    render_node.DEFAULT_PATH.write_text(json.dumps(coordinator.config))
    assert render_node.resolve(None)["coordinator"] == coordinator.origin
    named = tmp_path / "other.json"
    named.write_text(json.dumps({**coordinator.config, "gpu": None}))
    monkeypatch.setenv(render_node.CONFIG_ENV, str(named))
    assert render_node.resolve(None)["gpu"] is None
    assert render_node.resolve(False) is None


@pytest.mark.parametrize(
    "config, error",
    [
        ({"coordinator": "https://farm.example", "token": "t"}, "https"),
        ({"coordinator": "http://farm.example/path", "token": "t"}, "origin"),
        ({"coordinator": "http://farm.example"}, "token"),
        ({"coordinator": "http://farm.example", "token": "t", "adapter": "x"}, "unknown"),
    ],
)
def test_a_bad_configuration_is_refused(config, error):
    with pytest.raises(render_node.RenderNodeError, match=error):
        render_node.resolve(config)


def test_a_pro_launch_holds_an_assignment_and_hands_the_build_its_session(
    api, coordinator, pro_build, signed_in, monkeypatch
):
    (pro_build.parent / "launch.json").write_text(
        json.dumps({"rules": [{"target": ["win"], "prefs": {"webgl.holo-point-sprites": True}}]})
    )
    opts, config = node_launch(pro_build, monkeypatch, os=None, render_node=coordinator.config)

    (acquire,) = coordinator.calls("POST", "/assignments")
    assert acquire["auth"] == f"Bearer {TOKEN}"
    assert {k: v for k, v in acquire["body"].items() if k != "browser_id"} == {
        "browser": "firefox", "version": "156.0.1", "route": "angle", "gpu": GPU,
    }
    session_file = Path(opts["env"][render_node.RELAY_ENV])
    assert mode(session_file) == 0o600
    assert json.loads(session_file.read_text()) == {
        "endpoint": coordinator.origin.replace("http", "ws", 1) + "/angle-relay",
        "token": TOKEN,
        "assignment": "a" * 32,
        "profile": "d" * 64,
        "browser": "firefox",
        "version": "156.0.1",
    }
    # The node renders, so nothing local is asked of the farm, masked or covered.
    assert api.calls("/api/v1/leases")[0]["body"]["gpu"] is False
    assert config["navigator.platform"] == "Win32"
    assert not [k for k in config if k.startswith(("webGl:", "webGl2:"))]
    prefs = opts["firefox_user_prefs"]
    assert prefs["webgl.holo-point-sprites"] is False
    assert prefs["webgl.force-enabled"] is True and prefs["webgl.enable-webgl2"] is True
    assert not utils.has_canvas_placeholder(prefs)


def test_ending_the_lease_releases_the_assignment(api, coordinator, pro_build, signed_in, monkeypatch):
    opts, _ = node_launch(pro_build, monkeypatch, render_node=coordinator.config)
    session_file = Path(opts["env"][render_node.RELAY_ENV])
    held_lease(opts).release()
    assert [r["path"] for r in coordinator.calls("DELETE", ".*")] == ["/assignments/" + "a" * 32]
    assert not session_file.exists()


def test_the_assignment_is_renewed_while_the_browser_runs(
    api, coordinator, pro_build, signed_in, monkeypatch
):
    monkeypatch.setattr(render_node, "RENEW_S", 0.02)
    opts, _ = node_launch(pro_build, monkeypatch, render_node=coordinator.config)
    coordinator.wait_for(lambda: len(coordinator.calls("POST", r"/assignments/\w+/renew")) >= 2)
    held_lease(opts).release()


def test_a_busy_farm_is_waited_for(api, coordinator, pro_build, signed_in, monkeypatch):
    coordinator.scripted.append((503, {"Retry-After": "0"}, b"capacity full"))
    opts, _ = node_launch(pro_build, monkeypatch, render_node=coordinator.config)
    assert len(coordinator.calls("POST", "/assignments")) == 2
    assert render_node.RELAY_ENV in opts["env"]


def test_a_release_no_node_serves_is_named_and_the_lease_released(
    api, coordinator, pro_build, signed_in, monkeypatch
):
    coordinator.scripted.append((503, {}, b"no compatible renderer\n"))
    with pytest.raises(render_node.RenderNodeError, match="Firefox 156.0.1 on .*ANGLE pack"):
        node_launch(pro_build, monkeypatch, render_node=coordinator.config)
    assert [r["body"] for r in api.calls(r".*/release")] == [{"reason": "error"}]


def test_the_default_file_is_used_by_a_pro_build_only(api, coordinator, build, pro_build, signed_in, monkeypatch):
    render_node.DEFAULT_PATH.write_text(json.dumps(coordinator.config))
    opts, _ = node_launch(pro_build, monkeypatch, os=None)
    assert render_node.RELAY_ENV in opts["env"]
    held_lease(opts).release()


def test_a_stock_build_ignores_the_default_file_and_refuses_a_named_node(
    coordinator, build, monkeypatch
):
    render_node.DEFAULT_PATH.write_text(json.dumps(coordinator.config))
    assert render_node.RELAY_ENV not in node_launch(build, monkeypatch)[0]["env"]
    with pytest.raises(ValueError, match="Camoufox Pro build"):
        node_launch(build, monkeypatch, render_node=coordinator.config)
    assert coordinator.requests == []


def test_another_os_draws_locally_unless_the_node_was_named(api, coordinator, pro_build, signed_in, monkeypatch):
    render_node.DEFAULT_PATH.write_text(json.dumps(coordinator.config))
    opts, _ = node_launch(pro_build, monkeypatch, os="macos")
    assert render_node.RELAY_ENV not in opts["env"] and coordinator.requests == []
    with pytest.raises(ValueError, match="Windows"):
        node_launch(pro_build, monkeypatch, os="macos", render_node=coordinator.config)


def test_a_named_gpu_conflicts_with_the_node_s_own(api, coordinator, pro_build, signed_in, monkeypatch):
    with pytest.raises(ValueError, match="render_node=False"):
        node_launch(pro_build, monkeypatch, render_node=coordinator.config,
                    webgl_config=("Google Inc. (NVIDIA)", GPU))
    assert coordinator.calls("DELETE", ".*")


def test_contexts_of_a_node_browser_are_windows_and_unmasked():
    browser = SimpleNamespace(_camoufox_render_node=True)
    assert utils.context_identity(browser, None, None) == ("windows", None)
    with pytest.raises(ValueError, match="render node"):
        utils.context_identity(browser, "linux", None)
    fp = generate_context_fingerprint(os="windows", ff_version="156", webgl=False)
    assert not [k for k in fp["config"] if k.startswith(("webGl:", "webGl2:"))]
