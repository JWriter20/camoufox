"""Camoufox Pro's client half: sign-in, and the lease a Pro build starts with.

Every test talks to a local fake of the Camoufox Pro API whose answers follow
the server's own shapes (routes.py, leases.py, device.py, errors.py in the
control plane); no test reaches the network.
"""

import base64
import json
import os
import re
import stat
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from click.testing import CliRunner

from camoufox import __main__ as cli
from camoufox import pro, sync_api, utils
from camoufox.exceptions import (
    AccountSuspended,
    BuildNotAllowlisted,
    CapabilityMismatch,
    LeaseLimitReached,
    LeaseRefused,
    NotSignedIn,
    ProClockSkew,
    ProUnavailable,
    RateLimited,
    SubscriptionRequired,
)

GOLDEN = json.loads((Path(__file__).parent / "data" / "pro-lease-golden.json").read_text())
BUILD_HASH = "sha256:" + "ab" * 32
KEY = "cfp_live_" + "k" * 43


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def token_for(lease_id, build_hash, expires_in=1800):
    now = int(time.time())
    raw = (
        bytes([1, 0])
        + lease_id.bytes
        + (42).to_bytes(8, "big")
        + bytes(16)
        + bytes.fromhex(build_hash.removeprefix("sha256:"))
        + now.to_bytes(8, "big")
        + (now + expires_in).to_bytes(8, "big")
        + (1).to_bytes(2, "big")
        + os.urandom(64)  # a signature: the launcher never checks it, the browser does
    )
    return "cfl1_" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeApi:
    """The routes the launcher calls, answering as the control plane does unless a test scripts otherwise."""

    def __init__(self):
        self.requests = []
        self.scripted = {}
        self.clock_offset = timedelta(0)
        self.heartbeat_s = 60
        self.changed = threading.Condition()
        self.build_hash = BUILD_HASH
        self.grants = {}

    def script(self, route, *answers):
        """Answer the next calls to `route` (a regex) with these (status, body) pairs, in order."""
        self.scripted.setdefault(route, []).extend(answers)

    def calls(self, route):
        return [r for r in self.requests if re.fullmatch(route, r["path"])]

    def wait_for(self, predicate, timeout=5):
        with self.changed:
            assert self.changed.wait_for(predicate, timeout), self.requests

    def answer(self, path, body):
        for route, queue in self.scripted.items():
            if re.fullmatch(route, path) and queue:
                return queue.pop(0)
        now = iso(datetime.now(timezone.utc) + self.clock_offset)
        if path == "/api/v1/leases":
            lease_id = uuid.uuid4()
            return 201, {
                "v": 1,
                "lease_id": f"lse_{lease_id}",
                "server_time": now,
                "expires_at": now,
                "token": token_for(lease_id, body["build_hash"]),
                "limits": {"ttl_s": 1800, "heartbeat_s": self.heartbeat_s, "grace_s": 180, "degrade_drain_s": 900},
                "host": {"fingerprint": body["host"]["fingerprint"], "fidelity": "layout", "capability_hash": ""},
                "notices": [],
                **self.grants,
            }
        match = re.fullmatch(r"/api/v1/leases/lse_([0-9a-f-]+)/(heartbeat|release)", path)
        if match and match.group(2) == "heartbeat":
            return 200, {
                "lease_id": f"lse_{match.group(1)}",
                "server_time": now,
                "expires_at": now,
                "token": token_for(uuid.UUID(match.group(1)), self.build_hash),
                "token_kid": 0,
                "notices": [],
            }
        if match:
            return 200, {"released": True, "already_released": False, "held_s": 1}
        return 404, {"error": "not_found", "message": "No such route."}


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = FakeApi()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            status, answer = fake.answer(self.path, body)
            # Recorded before answering, so a caller that has its answer can already see its request.
            with fake.changed:
                fake.requests.append(
                    {"path": self.path, "body": body, "auth": self.headers.get("Authorization"), "status": status}
                )
                fake.changed.notify_all()
            payload = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv(pro.API_ENV, f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.delenv(pro.KEY_ENV, raising=False)
    monkeypatch.delenv(pro.LEASE_FILE_ENV, raising=False)
    monkeypatch.setattr(pro, "MINT_RETRY_S", (0, 0, 0))
    monkeypatch.setattr(pro, "HEARTBEAT_BACKOFF_S", (0.01,))
    yield fake
    pro._release_all()
    server.shutdown()


@pytest.fixture
def build(tmp_path):
    """A stock build directory: properties.json beside the executable."""
    directory = tmp_path / "build"
    directory.mkdir()
    (directory / "properties.json").write_text(json.dumps([{"property": "screen.width", "type": "uint"}]))
    return directory / "camoufox-bin"


@pytest.fixture
def pro_build(build):
    (build.parent / "pro-build.json").write_text(
        json.dumps({"build_hash": BUILD_HASH, "version": "156.0.1-pro.1", "target": "linux-x86_64"})
    )
    return build


def launch(build, monkeypatch, **kwargs):
    monkeypatch.setattr(utils, "get_env_vars", lambda *a, **k: {})
    monkeypatch.setattr(utils, "resolve_verstr", lambda *a: "156.0.1-beta.32")
    return utils.launch_options(executable_path=build, os="windows", headless=True, **kwargs)


def acquire(**kwargs):
    return pro.acquire(pro.ProBuild(BUILD_HASH, "156.0.1-pro.1", "linux-x86_64"), "win", KEY, **kwargs)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


# ── the file the browser reads ───────────────────────────────────────────────


@pytest.mark.parametrize("case", GOLDEN["lease_files"], ids=lambda c: c["os"])
def test_lease_file_is_byte_identical_to_the_shared_golden(case):
    written = pro.lease_file_bytes(case["token"], case["os"], case["fidelity"], case["written_at"])
    assert written.decode() == case["file"]
    body = json.loads(written)
    assert list(body) == [
        "v", "lease_id", "account_id", "profile_id", "build_hash", "issued_at", "expires_at",
        "token", "os", "fidelity", "paths", "written_at",
    ]
    assert body["v"] == 1 and body["account_id"] == 42 and len(body["build_hash"]) == 64


@pytest.mark.parametrize("host", GOLDEN["host_fingerprints"], ids=lambda h: h["os"])
def test_host_fingerprint_matches_the_shared_golden(host):
    assert pro.host_fingerprint_of(host["host_id"], host["os"], host["username"]) == host["fingerprint"]


def test_host_id_is_generated_once_and_private(api):
    first = pro.host_fingerprint()
    assert pro.host_fingerprint() == first
    host_id = Path(os.environ["XDG_CACHE_HOME"]) / "camoufox" / "pro" / "host.id"
    assert mode(host_id) == 0o600 and len(host_id.read_text()) == 32


# ── which builds get a lease ─────────────────────────────────────────────────


def test_a_stock_build_makes_no_request(api, build, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    opts = launch(build, monkeypatch)
    assert api.requests == []
    assert pro.LEASE_FILE_ENV not in opts["env"]


def test_a_pro_build_without_a_key_says_how_to_sign_in(api, pro_build, monkeypatch):
    with pytest.raises(NotSignedIn, match="camoufox login"):
        launch(pro_build, monkeypatch)
    assert api.requests == []


def test_a_pro_build_is_launched_with_a_minted_lease(api, pro_build, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    opts = launch(pro_build, monkeypatch)

    (mint,) = api.requests
    assert mint["path"] == "/api/v1/leases" and mint["auth"] == f"Bearer {KEY}"
    path = Path(opts["env"][pro.LEASE_FILE_ENV])
    body = json.loads(path.read_bytes())
    assert path.parent == Path(os.environ["XDG_RUNTIME_DIR"]) / "camoufox-pro"
    assert path.name == f"{body['lease_id']}.json"
    assert mode(path) == 0o600 and mode(path.parent) == 0o700
    assert body["os"] == "windows" and body["fidelity"] == "layout"
    assert body["build_hash"] == BUILD_HASH.removeprefix("sha256:")


def test_the_caller_s_own_lease_file_wins(api, pro_build, monkeypatch):
    monkeypatch.setenv(pro.LEASE_FILE_ENV, "/run/mine.json")
    opts = launch(pro_build, monkeypatch)
    assert opts["env"][pro.LEASE_FILE_ENV] == "/run/mine.json"
    assert api.requests == []


def test_mint_request_is_what_the_server_parses(api):
    acquire()
    body = api.requests[0]["body"]
    # cloud/src/cfp/api/leases.py parse_request, rule for rule.
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", body["build_hash"])
    assert re.fullmatch(r"[0-9a-f]{64}", body["host"]["fingerprint"])
    assert isinstance(body["host"]["capability"], dict)
    assert body["host"]["capability"]["host"]["os"] in ("windows", "macos", "linux")
    assert 0 < len(body["host"]["label"]) <= 64
    assert body["os"] == "windows"
    assert re.fullmatch(r"camoufox-python/\S+", body["client"]) and len(body["client"]) <= 128
    assert 0 < len(body["idempotency_key"]) <= 64
    assert "profile" not in body


# ── the key ──────────────────────────────────────────────────────────────────


def test_key_precedence_is_argument_then_environment_then_file(api, monkeypatch):
    stored = "cfp_live_" + "f" * 43
    pro.store_key(stored)
    assert pro.resolve_key() == stored
    monkeypatch.setenv(pro.KEY_ENV, "cfp_live_" + "e" * 43)
    assert pro.resolve_key() == "cfp_live_" + "e" * 43
    assert pro.resolve_key("cfp_live_" + "a" * 43) == "cfp_live_" + "a" * 43


def test_stored_key_is_private(api):
    path = pro.store_key(KEY)
    assert path == Path(os.environ["XDG_CONFIG_HOME"]) / "camoufox" / "pro-credentials.json"
    assert mode(path) == 0o600 and mode(path.parent) == 0o700


def test_a_key_other_users_can_read_is_refused_with_the_fix(api):
    path = pro.store_key(KEY)
    path.chmod(0o644)
    with pytest.raises(PermissionError, match=f"chmod 600 {re.escape(str(path))}"):
        pro.resolve_key()


def test_a_management_token_is_not_a_key(api):
    with pytest.raises(NotSignedIn, match="cfp_live_"):
        pro.resolve_key("cfp_mgmt_" + "m" * 43)


def test_logout_deletes_the_key(api):
    pro.store_key(KEY)
    assert pro.forget_key() == f"cfp_live_...{KEY[-4:]}"
    assert pro.forget_key() is None
    with pytest.raises(NotSignedIn):
        pro.resolve_key()


# ── sign-in ──────────────────────────────────────────────────────────────────


def start_answer(interval=5):
    return 200, {
        "device_code": "dc",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://camoufox.com/device",
        "verification_uri_complete": "https://camoufox.com/device?code=ABCD-EFGH",
        "expires_in": 900,
        "interval": interval,
    }


def test_device_flow_waits_slows_down_and_stores_a_runtime_key(api):
    api.script("/api/v1/device/start", start_answer())
    api.script(
        "/api/v1/device/token",
        (400, {"error": "authorization_pending", "message": "pending"}),
        (429, {"error": "slow_down", "message": "Polling too fast."}),
        (200, {
            "access_token": "cfp_mgmt_token",
            "token_type": "bearer",
            "account": {"id": 7, "name": "acme", "kind": "org"},
            "role": "owner",
            "user": {"id": 3, "github_login": "octo"},
        }),
    )
    api.script("/api/v1/keys", (201, {"id": 9, "api_key": KEY, "masked": "x", "name": "n"}))
    slept, printed = [], []

    signed_in = pro.login(echo=printed.append, sleep=slept.append)

    assert slept == [5, 5, 10]
    assert "https://camoufox.com/device?code=ABCD-EFGH" in printed[0] and "ABCD-EFGH" in printed[1]
    assert [r["body"] for r in api.calls("/api/v1/device/token")] == [{"device_code": "dc"}] * 3
    (key_request,) = api.calls("/api/v1/keys")
    assert key_request["auth"] == "Bearer cfp_mgmt_token"
    assert signed_in["account"]["name"] == "acme" and signed_in["user"]["github_login"] == "octo"
    assert pro.resolve_key() == KEY


@pytest.mark.parametrize("code", ["expired_token", "access_denied"])
def test_device_flow_that_is_not_approved_fails_clearly(api, code):
    api.script("/api/v1/device/start", start_answer())
    api.script("/api/v1/device/token", (400, {"error": code, "message": "No."}))
    with pytest.raises(NotSignedIn, match="camoufox login"):
        pro.login(echo=lambda _: None, sleep=lambda _: None)
    assert api.calls("/api/v1/keys") == []


# ── refusals ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, code, kind",
    [
        (401, "invalid_api_key", NotSignedIn),
        (402, "subscription_required", SubscriptionRequired),
        (403, "account_suspended", AccountSuspended),
        (403, "build_not_allowlisted", BuildNotAllowlisted),
        (409, "lease_limit_reached", LeaseLimitReached),
        (412, "capability_mismatch", CapabilityMismatch),
    ],
)
def test_a_refused_mint_raises_its_own_type_without_retrying(api, status, code, kind):
    api.script(
        "/api/v1/leases",
        (status, {"error": code, "message": "Refused.", "resolution_url": "https://camoufox.com/billing"}),
    )
    with pytest.raises(kind) as raised:
        acquire()
    assert raised.value.code == code and raised.value.resolution_url == "https://camoufox.com/billing"
    assert len(api.requests) == 1


def test_lease_limit_names_the_holders(api):
    api.script("/api/v1/leases", (409, {
        "error": "lease_limit_reached",
        "message": "All 1 concurrent browsers are in use.",
        "details": {"limit": 1, "active": 1, "holders": [{"host_label": "runner-3", "expires_at": "2026-09-29T12:00:00.000Z"}]},
    }))
    with pytest.raises(LeaseLimitReached, match="runner-3"):
        acquire()


def test_an_unreachable_api_is_retried_with_one_idempotency_key(api):
    api.script("/api/v1/leases", *[(503, {"error": "service_unavailable", "message": "down"})] * 4)
    with pytest.raises(ProUnavailable):
        acquire()
    keys = {r["body"]["idempotency_key"] for r in api.requests}
    assert len(api.requests) == 4 and len(keys) == 1


def test_rate_limiting_that_outlasts_the_retries_raises(api):
    api.script("/api/v1/leases", *[(429, {"error": "rate_limited", "message": "slow", "retry_after": 0})] * 4)
    with pytest.raises(RateLimited):
        acquire()
    assert len(api.requests) == 4


def test_a_transient_failure_then_success_mints(api):
    api.script("/api/v1/leases", (502, {}))
    lease = acquire()
    assert lease.path.exists() and len(api.requests) == 2


def test_a_clock_far_off_the_api_s_refuses_and_releases(api):
    api.clock_offset = timedelta(minutes=10)
    with pytest.raises(ProClockSkew) as raised:
        acquire()
    assert raised.value.skew > 500
    release = api.calls(r"/api/v1/leases/.*/release")
    assert [r["body"] for r in release] == [{"reason": "error"}]
    assert list(pro.lease_dir().glob("*.json")) == []


# ── renewal ──────────────────────────────────────────────────────────────────


def test_heartbeat_rewrites_the_file_with_each_token(api):
    api.heartbeat_s = 0.05
    lease = acquire()
    before = lease.path.read_bytes()
    api.wait_for(lambda: len(api.calls(r".*/heartbeat")) >= 2)
    after = lease.path.read_bytes()
    lease.release()
    beats = api.calls(r".*/heartbeat")
    assert [b["body"]["seq"] for b in beats[:2]] == [1, 2]
    assert beats[0]["body"]["host"]["fingerprint"] == api.requests[0]["body"]["host"]["fingerprint"]
    assert json.loads(after)["lease_id"] == json.loads(before)["lease_id"]
    assert json.loads(after)["token"] != json.loads(before)["token"]


def test_heartbeat_backs_off_and_recovers(api):
    api.heartbeat_s = 0.05
    api.script(r".*/heartbeat", (503, {"error": "service_unavailable", "message": "down"}), (402, {}))
    lease = acquire()
    api.wait_for(lambda: [r["status"] for r in api.calls(r".*/heartbeat")][:3] == [503, 402, 200])
    lease.release()


def test_a_swept_lease_is_minted_again_into_the_same_file(api):
    api.heartbeat_s = 0.05
    api.script(r".*/heartbeat", (404, {"error": "lease_not_found", "message": "gone"}))
    lease = acquire()
    path, first_id = lease.path, lease.lease_id
    api.wait_for(lambda: len(api.calls("/api/v1/leases")) == 2)
    api.wait_for(lambda: json.loads(path.read_bytes())["lease_id"] != first_id.removeprefix("lse_"))
    assert lease.path == path and lease.lease_id != first_id
    lease.release()


def test_a_revoked_key_stops_the_heartbeat(api):
    api.heartbeat_s = 0.05
    api.script(r".*/heartbeat", (401, {"error": "invalid_api_key", "message": "revoked"}))
    lease = acquire()
    lease._thread.join(5)
    assert not lease._thread.is_alive()
    assert len(api.calls(r".*/heartbeat")) == 1 and lease.path.exists()


# ── release ──────────────────────────────────────────────────────────────────


class Emitter:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def close(self):
        pass


def test_closing_the_browser_releases_and_deletes_the_file(api):
    lease = acquire()
    browser = Emitter()
    pro.attach_lease(lease, browser, "disconnected")
    browser.handlers["disconnected"](browser)
    api.wait_for(lambda: api.calls(r".*/release"))
    lease.release()  # joins the one in flight; releasing twice is one request
    assert [r["body"] for r in api.calls(r".*/release")] == [{"reason": "clean_exit"}]
    assert not lease.path.exists()


def test_each_browser_gets_its_own_lease(api):
    first, second = acquire(), acquire()
    assert first.path != second.path and first.lease_id != second.lease_id
    options = {"env": {pro.LEASE_FILE_ENV: str(first.path)}}
    assert pro.claim(options) is first
    with pytest.raises(ValueError, match="once per browser"):
        pro.claim(options)


def test_a_release_the_api_cannot_take_is_retried_once(api):
    api.script(r".*/release", (503, {}), (503, {}))
    lease = acquire()
    lease.release()
    assert len(api.calls(r".*/release")) == 2 and not lease.path.exists()


def test_stale_lease_files_are_cleaned_at_the_next_start(api):
    directory = pro.lease_dir()
    directory.mkdir(parents=True)
    stale, fresh = directory / "old.json", directory / "new.json"
    stale.write_text("{}")
    fresh.write_text("{}")
    day_ago = time.time() - 25 * 3600
    os.utime(stale, (day_ago, day_ago))
    acquire()
    assert not stale.exists() and fresh.exists()


# ── a browser that refuses its lease ─────────────────────────────────────────


def test_a_browser_that_exits_78_raises_lease_refused_and_releases(api):
    lease = acquire()

    def launch(**_):
        raise Exception(
            # Playwright's own wording for a browser that exits during launch.
            "BrowserType.launch: Failed to launch the browser process.\n"
            "Browser logs:\n\n"
            "<launched> pid=4242\n"
            "[pid=4242][err] camoufox-pro: lease refused (build_mismatch)\n"
            "[pid=4242] <process did exit: exitCode=78, signal=null>"
        )

    playwright = SimpleNamespace(firefox=SimpleNamespace(launch=launch))
    with pytest.raises(LeaseRefused) as raised:
        sync_api.NewBrowser(playwright, from_options={"env": {pro.LEASE_FILE_ENV: str(lease.path)}})
    assert raised.value.reason == "build_mismatch"
    assert [r["body"] for r in api.calls(r".*/release")] == [{"reason": "error"}]
    assert not lease.path.exists()


def test_any_other_launch_failure_is_raised_as_it_was(api):
    lease = acquire()

    def launch(**_):
        raise RuntimeError("no display")

    playwright = SimpleNamespace(firefox=SimpleNamespace(launch=launch))
    with pytest.raises(RuntimeError, match="no display"):
        sync_api.NewBrowser(playwright, from_options={"env": {pro.LEASE_FILE_ENV: str(lease.path)}})
    assert [r["body"] for r in api.calls(r".*/release")] == [{"reason": "error"}]


# ── camoufox pro --activate ──────────────────────────────────────────────────


def activate(build, *args):
    return CliRunner().invoke(cli.cli, ["pro", "--activate", "--executable-path", str(build), *args])


def test_activate_verifies_a_lease_and_releases_it(api, pro_build, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    result = activate(pro_build, "--os", "windows")

    assert result.exit_code == 0, result.output
    (mint,) = api.calls("/api/v1/leases")
    assert mint["body"]["os"] == "windows" and mint["body"]["build_hash"] == BUILD_HASH
    assert [r["body"] for r in api.calls(".*/release")] == [{"reason": "clean_exit"}]
    assert result.output.splitlines() == [
        f"[ ok ] lease verified: {api.requests[1]['path'].split('/')[4]} for 156.0.1-pro.1, windows identity, layout fidelity",
        "[ -- ] profile: not granted",
        "[ -- ] egress: not granted",
        "[ -- ] gpu: not granted",
        "[ -- ] captcha: not granted",
        "[ ok ] lease released",
    ]
    assert list((Path(os.environ["XDG_RUNTIME_DIR"]) / "camoufox-pro").iterdir()) == []


def test_activate_reports_only_the_sections_the_lease_carries_and_no_credentials(api, pro_build, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    api.grants = {
        "profile": None,
        "egress": {
            "server": "http://203.0.113.7:8080", "username": "user-1", "password": "secret-pass",
            "class": "residential", "country": "US", "exit_ip": None,
        },
        "gpu": None,
        "captcha": {"endpoint": "https://captcha.example/v1", "remaining": 250, "expires_at": "2026-10-02T12:00:00Z"},
    }
    result = activate(pro_build)

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[1:5] == [
        "[ -- ] profile: not granted",
        "[ ok ] egress: granted (residential, US)",
        "[ -- ] gpu: not granted",
        "[ ok ] captcha: granted (250)",
    ]
    assert "secret-pass" not in result.output and "user-1" not in result.output


def test_activate_without_a_pro_build_says_so_and_fails(api, build, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    result = activate(build)
    assert result.exit_code == 1
    assert result.output.startswith("[FAIL] no Camoufox Pro build:") and "pro-build.json" in result.output
    assert api.requests == []


def test_activate_without_a_key_fails_with_how_to_sign_in(api, pro_build):
    result = activate(pro_build)
    assert result.exit_code == 1
    assert "[FAIL] lease:" in result.output and "camoufox login" in result.output
    assert api.requests == []


def test_activate_reads_the_key_camoufox_login_stored(api, pro_build):
    pro.store_key(KEY)
    assert activate(pro_build).exit_code == 0
    assert api.calls("/api/v1/leases")[0]["auth"] == f"Bearer {KEY}"


def test_activate_reports_a_refused_lease_and_fails(api, pro_build, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    api.script(
        "/api/v1/leases",
        (402, {"error": "subscription_required", "message": "No active plan.", "resolution_url": "https://camoufox.com/pro"}),
    )
    result = activate(pro_build)
    assert result.exit_code == 1
    assert result.output.splitlines() == ["[FAIL] lease: No active plan. https://camoufox.com/pro"]
    assert api.calls(".*/release") == []


def test_activate_says_when_the_release_did_not_reach_the_api(api, pro_build, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    api.script(r"/api/v1/leases/.*/release", (500, {}), (500, {}))
    result = activate(pro_build)
    assert result.exit_code == 0
    assert result.output.splitlines()[-1] == "[ -- ] lease not released: the API frees it when it expires"
