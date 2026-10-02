"""What a Camoufox Pro lease grants beyond the lease itself -- a profile's identity
and synced state, managed egress, remote rendering and the captcha solver --
and how the launcher applies each.

Every test talks to a local fake of the Camoufox Pro API and of the object store
its presigned URLs point at; no test reaches the network. The crypto vectors
are the synthetic ones the API's own code generates.
"""

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

# Needs the pro extra (pip install "camoufox[pro]"), which CI installs.
from camoufox import pro, pro_profile, pro_state, utils
from camoufox.exceptions import GpuUnavailable, ProfileMismatch, StatePoolSealed

VECTORS = json.loads((Path(__file__).parent / "data" / "pro-profile-state-v1.json").read_text())
BUILD_HASH = "sha256:" + "ab" * 32
KEY = "cfp_live_" + "k" * 43
BUILD = pro.ProBuild(BUILD_HASH, "156.0.1-pro.1", "linux-x86_64")
PROFILE_ID = "prf_0191f3a2-7c1e-7b52-9a10-3f9e6d5a1c22"
ACCOUNT = 42


def stream(size, label):
    """The vectors' input generator: SHA-256 in counter mode."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(label.encode() + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:size])


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def token_for(lease_id):
    now = int(time.time())
    raw = (
        bytes([1, 0])
        + lease_id.bytes
        + ACCOUNT.to_bytes(8, "big")
        + uuid.UUID(PROFILE_ID[4:]).bytes
        + bytes.fromhex(BUILD_HASH.removeprefix("sha256:"))
        + now.to_bytes(8, "big")
        + (now + 1800).to_bytes(8, "big")
        + (1).to_bytes(2, "big")
        + os.urandom(64)
    )
    return "cfl1_" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeCloud:
    """The Camoufox Pro API and its object store, as far as a lease's sections reach."""

    def __init__(self):
        self.requests = []
        self.scripted = {}
        self.sections = {}
        self.heartbeat = {}
        self.heartbeat_s = 60
        self.key_class = "account"
        self.objects = {}
        self.head = 0
        self.versions = {}
        self.bundle = b"{}"
        self.base = ""
        self.changed = threading.Condition()

    def script(self, route, *answers):
        self.scripted.setdefault(route, []).extend(answers)

    def calls(self, method, route):
        return [r for r in self.requests if r["method"] == method and re.fullmatch(route, r["path"])]

    def wait_for(self, predicate, timeout=5):
        with self.changed:
            assert self.changed.wait_for(predicate, timeout), self.requests

    def set_bundle(self, document):
        self.bundle = json.dumps(document).encode()
        self.objects["/store/bundle"] = self.bundle

    def state_block(self, version):
        if version == 0:
            return {"profile_id": PROFILE_ID, "version": 0, "manifest": None, "chunk_count": 0,
                    "total_bytes": 0, "chunks": [], "next": None}  # fmt: skip
        stored = self.versions[version]
        manifest = self.objects[stored["manifest"]]
        return {
            "profile_id": PROFILE_ID,
            "version": version,
            "manifest": {"url": self.base + stored["manifest"], "sha256": sha256(manifest), "size": len(manifest)},
            "chunk_count": len(stored["chunks"]),
            "total_bytes": 0,
            "chunks": [
                {"chunk_id": cid, "size": len(self.objects[f"/store/chunks/{cid}"]), "url": f"{self.base}/store/chunks/{cid}"}
                for cid in stored["chunks"]
            ],
            "next": None,
        }

    def answer(self, method, path, body):
        for route, queue in self.scripted.items():
            if re.fullmatch(route, path) and queue:
                return queue.pop(0)
        if path == "/api/v1/leases":
            lease_id = uuid.uuid4()
            profile = None
            if body.get("profile"):
                profile = {
                    "id": PROFILE_ID,
                    "key": body["profile"],
                    "os": body["os"],
                    "warm_plan": "none" if self.key_class == "account" else "standard",
                    "key_class": self.key_class,
                    "egress_regime": "none",
                    "bundle": {"url": f"{self.base}/store/bundle", "sha256": sha256(self.bundle),
                               "size": len(self.bundle), "cache_key": f"bundles/{sha256(self.bundle)}"},
                    "bundle_version": 1,
                    "state": self.state_block(self.head),
                    "launch_count": 1,
                }  # fmt: skip
            return 201, {
                "v": 1,
                "lease_id": f"lse_{lease_id}",
                "account_id": ACCOUNT,
                "server_time": now_iso(),
                "expires_at": now_iso(),
                "token": token_for(lease_id),
                "limits": {"ttl_s": 1800, "heartbeat_s": self.heartbeat_s, "grace_s": 180},
                "host": {"fingerprint": body["host"]["fingerprint"], "fidelity": "native"},
                "notices": [],
                "profile": profile,
                **self.sections,
            }
        match = re.fullmatch(r"/api/v1/leases/lse_([0-9a-f-]+)/(heartbeat|release)", path)
        if match and match.group(2) == "heartbeat":
            return 200, {
                "lease_id": f"lse_{match.group(1)}",
                "server_time": now_iso(),
                "expires_at": now_iso(),
                "token": token_for(uuid.UUID(match.group(1))),
                "notices": [],
                **self.heartbeat,
            }
        if match:
            return 200, {"released": True}
        route = f"/api/v1/profiles/{PROFILE_ID}/state"
        if method == "GET" and path.startswith(route + "?"):
            return 200, self.state_block(int(parse_qs(urlparse(path).query)["version"][0]))
        conflict = (409, {"error": "state_conflict", "message": "moved", "details": {"current_version": self.head}})
        if method == "POST" and path == route + "/uploads":
            if body["base_version"] != self.head:
                return conflict
            missing = [
                {
                    "chunk_id": claim["chunk_id"],
                    "url": f"{self.base}/store/chunks/{claim['chunk_id']}",
                    "headers": {
                        "content-length": str(claim["size"]),
                        "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(claim["sha256"])).decode(),
                    },
                }
                for claim in body["chunks"]
                if f"/store/chunks/{claim['chunk_id']}" not in self.objects
            ]
            return 200, {"next_version": self.head + 1, "missing": missing, "present": []}
        if method == "PUT" and path == route:
            if body["base_version"] != self.head:
                return conflict
            lost = [c["chunk_id"] for c in body["manifest"]["chunks"] if f"/store/chunks/{c['chunk_id']}" not in self.objects]
            if lost:
                return 409, {"error": "state_chunks_missing", "message": "lost", "details": {"chunk_ids": lost}}
            text = body["manifest_body"]
            sealed = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
            if sha256(sealed) != body["manifest"]["sha256"] or len(sealed) != body["manifest"]["size"]:
                return 400, {"error": "invalid_request", "message": "manifest_body"}
            self.head = body["version"]
            key = f"/store/manifests/{self.head}"
            self.objects[key] = sealed
            self.versions[self.head] = {"manifest": key, "chunks": [c["chunk_id"] for c in body["manifest"]["chunks"]]}
            return 200, {"version": self.head, "committed_at": now_iso()}
        return 404, {"error": "not_found", "message": path}


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    fake = FakeCloud()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path.startswith("/store/"):
                status, payload = 200, b""
                if self.command == "PUT":
                    want = self.headers.get("x-amz-checksum-sha256")
                    if want != base64.b64encode(hashlib.sha256(raw).digest()).decode():
                        status = 400
                    else:
                        fake.objects[self.path] = raw
                elif self.path in fake.objects:
                    payload = fake.objects[self.path]
                else:
                    status = 404
                body = None
            else:
                body = json.loads(raw) if raw else None
                status, answer = fake.answer(self.command, self.path, body)
                payload = json.dumps(answer).encode()
            with fake.changed:
                fake.requests.append({"method": self.command, "path": self.path, "body": body})
                fake.changed.notify_all()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = _handle

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    fake.base = f"http://127.0.0.1:{server.server_address[1]}"
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        (tmp_path / name).mkdir()
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv(pro.API_ENV, fake.base)
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    monkeypatch.delenv(pro.LEASE_FILE_ENV, raising=False)
    monkeypatch.delenv(pro_profile.CONTENT_KEY_ENV, raising=False)
    monkeypatch.setattr(pro, "MINT_RETRY_S", (0, 0, 0))
    yield fake
    pro._release_all()
    server.shutdown()


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def profiles_home():
    return Path(os.environ["XDG_CACHE_HOME"]) / "camoufox" / "pro" / "profiles" / PROFILE_ID


# ── the vectors ──────────────────────────────────────────────────────────────

KEYS = pro_state.AccountKeys.derive(bytes.fromhex(VECTORS["keys"]["k_acct"]), VECTORS["keys"]["account_id"])


def test_vectors_derive_the_account_s_keys():
    assert KEYS.k_id.hex() == VECTORS["keys"]["k_id"]
    assert KEYS.k_chunk.hex() == VECTORS["keys"]["k_chunk"]
    assert KEYS.k_man.hex() == VECTORS["keys"]["k_man"]


def test_vectors_name_seal_and_open_a_chunk():
    v = VECTORS["chunk"]
    plaintext = stream(v["plaintext"]["size"], v["plaintext"]["label"])
    assert sha256(plaintext) == v["plaintext"]["sha256"]
    chunk_id = KEYS.chunk_id(plaintext)
    assert chunk_id.hex() == v["chunk_id_hex"]
    assert pro_state.chunk_id_text(chunk_id) == v["chunk_id_b64url"]
    aad = pro_state.chunk_aad(ACCOUNT, chunk_id)
    assert aad.hex() == v["aad_hex"]
    sealed = pro_state.seal(KEYS.k_chunk, bytes.fromhex(v["zstd_frame_hex"]), aad, bytes.fromhex(v["nonce_hex"]))
    assert sealed.hex() == v["sealed_hex"] and len(sealed) == v["sealed_size"]
    assert base64.b64encode(hashlib.sha256(sealed).digest()).decode() == v["x_amz_checksum_sha256"]
    assert KEYS.open_chunk(chunk_id, sealed, v["plaintext"]["size"]) == plaintext
    assert KEYS.open_chunk(chunk_id, KEYS.seal_chunk(chunk_id, plaintext), len(plaintext)) == plaintext


def test_vectors_refuse_a_chunk_under_another_account_or_id():
    v = VECTORS["chunk"]
    sealed, chunk_id = bytes.fromhex(v["sealed_hex"]), bytes.fromhex(v["chunk_id_hex"])
    other = pro_state.AccountKeys.derive(bytes.fromhex(VECTORS["keys"]["k_acct"]), ACCOUNT + 1)
    with pytest.raises(pro_state.StateIntegrityError):
        other.open_chunk(chunk_id, sealed, 5000)
    with pytest.raises(pro_state.StateIntegrityError):
        KEYS.open_chunk(bytes([chunk_id[0] ^ 1]) + chunk_id[1:], sealed, 5000)


def test_vectors_build_seal_and_open_the_manifest():
    v = VECTORS["manifest"]
    fields = {k: val for k, val in v["document"].items() if k not in ("format", "total_bytes", "file_count", "chunk_count", "sha256")}
    manifest = pro_state.manifest_of(fields)
    assert manifest == v["document"]
    assert pro_state.canonical(manifest).decode() == v["canonical_json"]
    aad = pro_state.manifest_aad(ACCOUNT, v["profile_id"], v["version"])
    assert aad.hex() == v["aad_hex"]
    sealed = pro_state.seal(KEYS.k_man, bytes.fromhex(v["zstd_frame_hex"]), aad, bytes.fromhex(v["nonce_hex"]))
    assert sealed.hex() == v["sealed_hex"]
    assert base64.urlsafe_b64encode(sealed).rstrip(b"=").decode() == v["manifest_body_b64url"]
    opened = KEYS.open_manifest(v["profile_id"], v["version"], sealed)
    assert pro_state.decode_manifest(opened, v["profile_id"], v["version"]) == v["document"]
    with pytest.raises(ValueError, match="expected"):
        pro_state.decode_manifest(opened, v["profile_id"], v["version"] + 1)
    with pytest.raises(pro_state.StateIntegrityError):
        KEYS.open_manifest(v["profile_id"], v["version"] + 1, sealed)


def test_vectors_chunk_with_fastcdc_g1():
    v = VECTORS["fastcdc_g1"]
    assert f"{pro_state.GEAR_SEED:016x}" == v["seed_hex"]
    assert [v["min"], v["avg"], v["max"]] == [pro_state.CHUNKING[k] for k in ("min", "avg", "max")]
    assert pro_state.chunk_lengths(stream(v["input"]["size"], v["input"]["label"])) == v["lengths"]


# ── capture ──────────────────────────────────────────────────────────────────


def realistic_profile(directory):
    """A closed Firefox profile with what a real one holds: SQLite with a pending WAL, prefs, caches, locks."""
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "cookies.sqlite", isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT)")
    db.execute("INSERT INTO moz_cookies VALUES ('session', 'abc123')")
    # Copy while the WAL still holds the rows, as a killed browser leaves it.
    (directory / "c.sqlite").write_bytes((directory / "cookies.sqlite").read_bytes())
    (directory / "c.sqlite-wal").write_bytes((directory / "cookies.sqlite-wal").read_bytes())
    db.close()
    (directory / "c.sqlite").replace(directory / "cookies.sqlite")
    (directory / "c.sqlite-wal").replace(directory / "cookies.sqlite-wal")
    (directory / "prefs.js").write_text(
        'user_pref("browser.startup.homepage", "https://example.com");\n'
        'user_pref("webgl.force-enabled", true);\n'
        'user_pref("browser.download.dir", "/home/someone/Downloads");\n'
    )
    ls = directory / "storage" / "default" / "https+++example.com" / "ls"
    ls.mkdir(parents=True)
    (ls / "data.sqlite").write_bytes(stream(3 << 20, "ls"))
    (directory / "storage" / "default" / "https+++example.com" / "idb" / "x.files").mkdir(parents=True)
    (directory / "cache2" / "entries").mkdir(parents=True)
    (directory / "cache2" / "entries" / "A").write_text("cache")
    (directory / "user.js").write_text('user_pref("a", 1);')
    (directory / ".parentlock").write_text("")
    (directory / "times.json").write_text('{"created":1}')
    (directory / "big-at-root.bin").write_bytes(stream(2 << 20, "big"))


def test_capture_keeps_what_the_policy_keeps_and_folds_the_wal_in(tmp_path):
    directory = tmp_path / "profile"
    realistic_profile(directory)
    snapshot = pro_state.capture(directory)
    assert [f.path for f in snapshot.files] == [
        "cookies.sqlite",
        "prefs.js",
        "storage/default/https+++example.com/ls/data.sqlite",
        "times.json",
    ]
    assert snapshot.dirs == ("storage/default/https+++example.com/idb/x.files",)
    assert snapshot.suspect_files == ()
    wal = directory / "cookies.sqlite-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    with sqlite3.connect(f"file:{directory / 'cookies.sqlite'}?mode=ro", uri=True) as db:
        assert db.execute("SELECT value FROM moz_cookies").fetchall() == [("abc123",)]
    prefs = (directory / "prefs.js").read_text()
    assert "browser.startup.homepage" in prefs
    assert "webgl.force-enabled" not in prefs and "browser.download.dir" not in prefs
    assert not (directory / "user.js").exists()


# ── restore -> capture -> restore ────────────────────────────────────────────

BUNDLE_DOC = {
    "format": "cfp-bundle/1",
    "bundle_version": 1,
    "profile": {"id": PROFILE_ID, "os": "windows"},
    "config": {
        "navigator.userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{FF}.0) Gecko/20100101 Firefox/{FF}.0",
        "screen.width": 1920,
    },
    "browserforge_fingerprint": {"screen": {"width": 1920}},
    "prefs": {"webgl.enable-webgl2": True},
}


def tree(directory):
    out = {}
    for path in sorted(directory.rglob("*")):
        relpath = path.relative_to(directory).as_posix()
        out[relpath + ("/" if path.is_dir() else "")] = "" if path.is_dir() else sha256(path.read_bytes())
    return out


def profile_lease(**request):
    return pro.acquire(BUILD, "win", KEY, {"profile": "linkedin-01", **request})


@pytest.fixture
def bundle(cloud):
    cloud.set_bundle(BUNDLE_DOC)
    return cloud


def test_state_starts_empty_syncs_on_close_and_restores(bundle, tmp_path):
    cloud = bundle
    first = profile_lease()
    identity, user_data_dir = pro_profile.open_profile(first, "152.0.4")
    assert identity["config"]["navigator.userAgent"] == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"
    )
    assert identity["ff_version"] == 152 and identity["os"] == "windows"
    assert list(user_data_dir.iterdir()) == [] and mode(user_data_dir) == 0o700

    key_file = pro_profile.content_key_path(ACCOUNT)
    assert mode(key_file) == 0o600
    assert len(base64.urlsafe_b64decode(key_file.read_text() + "=")) == 32

    realistic_profile(user_data_dir)
    first.close()
    assert cloud.head == 1
    (commit,) = cloud.calls("PUT", f"/api/v1/profiles/{PROFILE_ID}/state")
    assert {k: commit["body"][k] for k in ("lease_id", "base_version", "version", "release", "file_count", "ff_version", "integrity")} == {
        "lease_id": first.lease_id, "base_version": 0, "version": 1, "release": True,
        "file_count": 4, "ff_version": "152.0.4", "integrity": "ok",
    }  # fmt: skip
    # The commit released the lease; no second release, and nothing left behind.
    assert cloud.calls("POST", ".*/release") == []
    assert not user_data_dir.exists() and not first.path.exists()
    # The store never saw plaintext.
    assert not any(b"abc123" in blob for key, blob in cloud.objects.items() if key.startswith("/store/chunks/"))

    # What the restore must reproduce: the same profile, captured, minus what does not travel.
    expected_dir = tmp_path / "expected"
    realistic_profile(expected_dir)
    pro_state.capture(expected_dir)
    for name in ("cache2", "user.js", "big-at-root.bin", "cookies.sqlite-wal", "cookies.sqlite-shm"):
        target = expected_dir / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    expected = tree(expected_dir)

    second = profile_lease()
    _, reopened = pro_profile.open_profile(second, "152.0.4")
    assert tree(reopened) == expected
    ls = "storage/default/https+++example.com/ls/data.sqlite"
    assert mode(reopened / ls) == mode(expected_dir / ls)

    # Unchanged chunks are not uploaded twice.
    puts = len(cloud.calls("PUT", "/store/chunks/.*"))
    second.close()
    assert cloud.head == 2
    assert len(cloud.calls("PUT", "/store/chunks/.*")) == puts

    third = profile_lease()
    _, again = pro_profile.open_profile(third, "152.0.4")
    assert tree(again) == expected
    third.release()


def test_state_a_newer_firefox_wrote_is_never_restored(bundle):
    first = profile_lease()
    _, user_data_dir = pro_profile.open_profile(first, "153.0")
    (user_data_dir / "times.json").write_text("{}")
    first.close()
    with pytest.raises(pro_state.StateNewerThanBrowser):
        pro_profile.open_profile(profile_lease(), "152.0.4")


def test_a_conflicting_capture_is_kept_and_the_lease_released(bundle, caplog):
    cloud = bundle
    lease = profile_lease()
    _, user_data_dir = pro_profile.open_profile(lease, "152.0.4")
    (user_data_dir / "times.json").write_text("{}")
    cloud.head = 5  # another machine committed meanwhile
    with caplog.at_level(logging.WARNING, logger="camoufox.pro"):
        lease.close()
    assert len(cloud.calls("POST", ".*/release")) == 1
    (kept,) = (profiles_home() / "conflicts").iterdir()
    assert (kept / "times.json").read_text() == "{}"
    assert "moved" in caplog.text


def test_a_capture_is_kept_pending_while_another_lease_holds_the_profile(bundle):
    cloud = bundle
    lease = profile_lease()
    _, user_data_dir = pro_profile.open_profile(lease, "152.0.4")
    (user_data_dir / "times.json").write_text("{}")
    cloud.script(
        f"/api/v1/profiles/{PROFILE_ID}/state/uploads",
        (409, {"error": "lease_conflict", "message": "held", "retry_after": 30, "details": {"holder": "x"}}),
    )
    lease.close()
    assert len(list((profiles_home() / "pending").iterdir())) == 1
    assert cloud.head == 0


def test_chunks_the_store_lost_are_uploaded_then_committed(bundle):
    cloud = bundle
    lease = profile_lease()
    _, user_data_dir = pro_profile.open_profile(lease, "152.0.4")
    (user_data_dir / "times.json").write_text("{}")
    answer = cloud.answer
    dropped = []

    def losing(method, path, body):
        if method == "PUT" and path.endswith("/state") and not dropped:
            dropped.append(True)
            for key in [k for k in cloud.objects if k.startswith("/store/chunks/")]:
                del cloud.objects[key]
        return answer(method, path, body)

    cloud.answer = losing
    lease.close()
    assert cloud.head == 1
    assert len(cloud.calls("PUT", f"/api/v1/profiles/{PROFILE_ID}/state")) == 2


def test_a_pool_sealed_profile_launches_without_state_sync(bundle, caplog):
    cloud = bundle
    cloud.key_class = "pool"
    lease = profile_lease()
    with caplog.at_level(logging.WARNING, logger="camoufox.pro"):
        identity, user_data_dir = pro_profile.open_profile(lease, "152.0.4")
    assert identity["os"] == "windows"
    assert "not synced" in caplog.text
    lease.close()
    assert cloud.calls("GET", ".*/state.*") == []
    assert len(cloud.calls("POST", ".*/release")) == 1
    assert not user_data_dir.exists()


def test_a_bundle_the_lease_does_not_name_is_refused(bundle):
    cloud = bundle
    lease = profile_lease()
    cloud.objects["/store/bundle"] = b'{"tampered":1}'
    with pytest.raises(pro_state.StateIntegrityError, match="sha256"):
        pro_profile.open_profile(lease, "152.0.4")


def test_the_content_key_comes_from_the_environment(cloud, monkeypatch):
    monkeypatch.setenv(pro_profile.CONTENT_KEY_ENV, base64.urlsafe_b64encode(bytes([7]) * 32).rstrip(b"=").decode())
    assert pro_profile.content_key(ACCOUNT) == bytes([7]) * 32
    assert not pro_profile.content_key_path(ACCOUNT).exists()


# ── refusals ─────────────────────────────────────────────────────────────────


def test_a_profile_that_does_not_match_raises_profile_mismatch(bundle):
    cloud = bundle
    cloud.script(
        "/api/v1/leases",
        (409, {"error": "profile_mismatch", "message": "standard profile",
               "details": {"field": "warm_plan", "profile": "standard", "requested": "none"}}),
    )  # fmt: skip
    with pytest.raises(ProfileMismatch) as raised:
        profile_lease(warm_plan="none")
    assert raised.value.details["field"] == "warm_plan"
    assert cloud.calls("POST", "/api/v1/leases")[0]["body"]["warm_plan"] == "none"


def test_a_farm_that_cannot_serve_raises_gpu_unavailable_without_retrying(cloud):
    cloud.script(
        "/api/v1/leases",
        (503, {"error": "gpu_unavailable", "message": "gpu: false renders locally.", "retry_after": 30,
               "details": {"reason": "no_worker"}}),
    )  # fmt: skip
    with pytest.raises(GpuUnavailable) as raised:
        pro.acquire(BUILD, "win", KEY)
    assert raised.value.retry_after == 30
    assert len(cloud.calls("POST", "/api/v1/leases")) == 1


def test_a_pool_sealed_sync_raises_state_pool_sealed_keeping_the_capture(bundle):
    cloud = bundle
    lease = profile_lease()
    _, user_data_dir = pro_profile.open_profile(lease, "152.0.4")
    (user_data_dir / "times.json").write_text("{}")
    cloud.script(f"/api/v1/profiles/{PROFILE_ID}/state/uploads", (409, {"error": "state_pool_sealed", "message": "pool"}))
    with pytest.raises(StatePoolSealed):
        lease.close()
    with pytest.raises(StatePoolSealed):
        lease.close()  # every caller hears it
    assert len(cloud.calls("POST", ".*/release")) == 1
    assert len(list((profiles_home() / "conflicts").iterdir())) == 1


# ── remote rendering ─────────────────────────────────────────────────────────

RENDER = {
    "endpoint": "wss://render.example/firefox-webgl",
    "token": f"cfl1.{'a' * 32}.{'b' * 32}.{'c' * 43}",
    "assignment": "b" * 32,
    "profile": "d" * 64,
    "version": "156.0",
    "expires_at": 1759400000,
}
GPU = {
    "mode": "farm",
    "identity_id": "d" * 64,
    "renderer": "ANGLE (AMD, Radeon R9 200 Series Direct3D11 vs_5_0 ps_5_0)",
    "render": RENDER,
    "prefs": {"gfx.canvas.renderfarm.enabled": True, "privacy.resistFingerprinting": False},
}


def test_the_render_document_is_written_verbatim_and_rewritten_on_each_heartbeat(cloud):
    cloud.sections = {"gpu": GPU}
    cloud.heartbeat_s = 0.05
    cloud.heartbeat = {"gpu": {**GPU, "render": {**RENDER, "expires_at": RENDER["expires_at"] + 60}}}
    lease = pro.acquire(BUILD, "win", KEY)
    path = lease.render_path
    assert path.is_absolute() and mode(path) == 0o600
    assert json.loads(path.read_text()) == RENDER
    assert path.read_text() == json.dumps(RENDER, separators=(",", ":"))
    cloud.wait_for(lambda: cloud.calls("POST", ".*/heartbeat"))
    deadline = time.monotonic() + 5
    while json.loads(path.read_text())["expires_at"] != RENDER["expires_at"] + 60:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    # A heartbeat without a render section keeps the file as it is.
    cloud.heartbeat = {"gpu": None}
    beats = len(cloud.calls("POST", ".*/heartbeat"))
    cloud.wait_for(lambda: len(cloud.calls("POST", ".*/heartbeat")) > beats + 1)
    assert json.loads(path.read_text())["expires_at"] == RENDER["expires_at"] + 60
    lease.release()
    assert not path.exists()


def test_no_remote_rendering_is_asked_for_when_told(cloud):
    pro.acquire(BUILD, "win", KEY, {"gpu": False})
    assert cloud.calls("POST", "/api/v1/leases")[0]["body"]["gpu"] is False


# ── the captcha solver ───────────────────────────────────────────────────────


class Emitter:
    def __init__(self):
        self.handlers = {}
        self.closed = False

    def on(self, event, handler):
        self.handlers[event] = handler

    def close(self):
        self.closed = True


def test_the_captcha_solver_is_exposed_without_the_lease_s_credentials(cloud):
    captcha = {"endpoint": "https://solver.example/v1", "remaining": 840, "expires_at": "2026-10-02T21:15:07.410Z"}
    cloud.sections = {"captcha": captcha, "egress": {"server": "http://127.0.0.1:1", "username": "u", "password": "p"}}
    lease = pro.acquire(BUILD, "win", KEY)
    browser = pro.attach_lease(lease, Emitter(), "disconnected")
    assert browser.pro == pro.ProSession(lease_id=lease.lease_id, captcha=captcha)
    assert "'p'" not in repr(browser.pro)
    browser.close()
    assert browser.closed
    assert len(cloud.calls("POST", ".*/release")) == 1
    assert not lease.path.exists()


# ── launch_options ───────────────────────────────────────────────────────────


@pytest.fixture
def pro_exe(tmp_path):
    directory = tmp_path / "build"
    directory.mkdir()
    (directory / "properties.json").write_text(json.dumps([{"property": "screen.width", "type": "uint"}]))
    (directory / "pro-build.json").write_text(json.dumps({"build_hash": BUILD_HASH, "version": "156.0.1-pro.1", "target": "linux-x86_64"}))
    return directory / "camoufox-bin"


class Geo:
    def __init__(self, ip):
        self.ip = ip

    def as_config(self):
        return {"timezone": "America/New_York", "locale:language": "en", "locale:region": "US",
                "geolocation:latitude": 40.7, "geolocation:longitude": -74.0, "geolocation:accuracy": 100}  # fmt: skip


@pytest.fixture
def launch(cloud, pro_exe, monkeypatch):
    looked_up = []
    configs = []
    monkeypatch.setattr(utils, "resolve_verstr", lambda *a: "152.0.4-beta.31")
    monkeypatch.setattr(utils, "public_ip", lambda *a: looked_up.append(a[0] if a else None) or "203.0.113.9")
    monkeypatch.setattr(utils, "get_geolocation", lambda ip, **k: Geo(ip))
    monkeypatch.setattr(utils, "geoip_allowed", lambda: None)
    monkeypatch.setattr(utils, "get_env_vars", lambda config, *a, **k: configs.append(dict(config)) or {})

    def run(**kwargs):
        options = {"executable_path": pro_exe, "os": "windows", "headless": True, "i_know_what_im_doing": True, **kwargs}
        return utils.launch_options(**options)

    run.looked_up = looked_up
    run.configs = configs
    run.mint = lambda: cloud.calls("POST", "/api/v1/leases")[-1]["body"]
    return run


def test_managed_egress_routes_the_browser_located_at_its_exit(cloud, launch):
    cloud.sections = {"egress": {"server": "http://100.64.12.34:7190", "username": "0192f0c4", "password": "secret",
                                 "class": "residential", "country": "US", "sticky": True, "exit_ip": "98.97.12.34"}}  # fmt: skip
    opts = launch(egress={"class": "isp", "country": "US"})
    assert launch.mint()["egress"] == {"class": "isp", "country": "US"}
    assert opts["proxy"] == {
        "server": "http://100.64.12.34:7190",
        "username": "0192f0c4",
        "password": "secret",
        "bypass": "localhost,127.0.0.1,::1,*.local",
    }
    config = launch.configs[-1]
    assert config["timezone"] == "America/New_York" and config["webrtc:ipv4"] == "98.97.12.34"
    assert launch.looked_up == []


def test_the_exit_is_looked_up_through_the_proxy_when_unknown(cloud, launch):
    cloud.sections = {"egress": {"server": "http://100.64.12.34:7190", "username": "u", "password": "p", "exit_ip": None}}
    launch()
    assert not {"egress", "profile", "warm_plan"} & set(launch.mint())
    assert launch.looked_up == ["http://u:p@100.64.12.34:7190"]


def test_the_caller_s_own_proxy_is_never_replaced(cloud, launch):
    proxy = {"server": "http://my.proxy:8080"}
    opts = launch(proxy=proxy, geoip="198.51.100.1")
    assert launch.mint()["egress"] is False
    assert opts["proxy"] == proxy


def test_a_proxy_and_an_egress_request_together_are_refused(cloud, launch):
    proxy = {"server": "http://my.proxy:8080"}
    with pytest.raises(ValueError, match="proxy and egress conflict"):
        launch(proxy=proxy, egress={"provider": "evomi"})
    # egress=False beside a proxy says the same thing twice, which is fine.
    launch(proxy=proxy, egress=False)
    assert launch.mint()["egress"] is False


def test_a_partner_provider_is_asked_for_by_name(cloud, launch):
    launch(egress={"provider": "evomi", "country": "US"})
    assert launch.mint()["egress"] == {"provider": "evomi", "country": "US"}


def test_the_browser_is_pointed_at_the_render_document(cloud, launch):
    cloud.sections = {"gpu": GPU}
    opts = launch()
    assert json.loads(Path(opts["env"][pro.RENDER_FILE_ENV]).read_text()) == RENDER
    assert opts["firefox_user_prefs"]["gfx.canvas.renderfarm.enabled"] is True
    assert opts["firefox_user_prefs"]["privacy.resistFingerprinting"] is False


def test_none_of_it_is_set_without_a_gpu_section(cloud, launch):
    opts = launch(gpu=False)
    assert launch.mint()["gpu"] is False
    assert pro.RENDER_FILE_ENV not in opts["env"]
    assert "gfx.canvas.renderfarm.enabled" not in opts["firefox_user_prefs"]


def test_a_profile_launches_with_its_identity_in_its_own_directory(cloud, launch):
    launch()
    generated = launch.configs[-1]
    config = {
        key: value
        for key, value in generated.items()
        if key != "timezone" and not key.startswith(("locale:", "geolocation:"))
    }
    config["navigator.userAgent"] = generated["navigator.userAgent"].replace("152", "{FF}")
    cloud.set_bundle({**BUNDLE_DOC, "config": config})
    opts = launch(profile="linkedin-01", warm_plan="none", proxy={"server": "http://my.proxy:8080"})
    mint = launch.mint()
    assert (mint["profile"], mint["warm_plan"], mint["os"], mint["egress"]) == ("linkedin-01", "none", "windows", False)
    presented = launch.configs[-1]
    assert presented["navigator.userAgent"] == generated["navigator.userAgent"]
    assert presented["fonts"] == generated["fonts"]
    assert presented["timezone"] == "America/New_York"
    assert launch.looked_up == ["http://my.proxy:8080"]
    assert re.search(rf"profiles/{PROFILE_ID}/sessions/lse_", opts["user_data_dir"])
    assert opts["firefox_user_prefs"]["webgl.enable-webgl2"] is True


def test_a_profile_needs_one_os_and_no_identity_of_the_caller_s(cloud, launch):
    with pytest.raises(ValueError, match="needs `os`"):
        launch(profile="p", os=["windows", "linux"])
    with pytest.raises(ValueError, match="own identity"):
        launch(profile="p", config={"a": 1})
    assert cloud.requests == []


def test_a_warm_plan_needs_a_profile(cloud, launch):
    with pytest.raises(ValueError, match="pass profile"):
        launch(warm_plan="none")


def test_lease_options_need_a_pro_build(cloud, launch, pro_exe):
    (pro_exe.parent / "pro-build.json").unlink()
    with pytest.raises(ValueError, match="Camoufox Pro build"):
        launch(profile="p")
    with pytest.raises(ValueError, match="Camoufox Pro build"):
        launch(gpu=False)


def test_the_lease_is_released_when_the_rest_of_the_launch_fails(cloud, launch, monkeypatch):
    def bad(*a, **k):
        raise ValueError("bad config")

    monkeypatch.setattr(utils, "validate_config", bad)
    with pytest.raises(ValueError, match="bad config"):
        launch()
    assert [r["body"] for r in cloud.calls("POST", ".*/release")] == [{"reason": "error"}]
