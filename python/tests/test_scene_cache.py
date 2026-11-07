"""
The scene cache on this machine: the download, its integrity, its updates, and
what reaches the browser. A real HTTP server stands in for the render gateway;
no browser runs here, so none of this shows the browser getting a hit.
"""

import hashlib
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_pro import token_for

from camoufox import pro, scene_cache

VERSION = "156.0"
OS = "windows"
TOKEN = "cfl1.token"


def body(fill: bytes = b"a", size: int = 2_000_000) -> bytes:
    return scene_cache.SQLITE_MAGIC + fill * (size - len(scene_cache.SQLITE_MAGIC))


def manifest_for(data: bytes) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    return {
        "schema": 1,
        "build": VERSION,
        "os": OS,
        "sha256": sha,
        "bytes": len(data),
        "path": f"/v1/scene-cache/snapshot/{VERSION}/{OS}/{sha}",
    }


class Gateway:
    def __init__(self):
        self.data = body()
        self.manifest = manifest_for(self.data)
        self.published = True
        self.requests = []
        # Serve this many bytes and hang up, once.
        self.cut_after = None
        self.ignore_range = False
        self.corrupt = False
        self.allow_token = TOKEN
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                gateway.requests.append((self.path, self.headers.get("Range"), self.headers.get("Authorization")))
                if self.headers.get("Authorization") != f"Bearer {gateway.allow_token}":
                    return self.answer(401, b"{}")
                if self.path.startswith("/v1/scene-cache/manifest"):
                    if not gateway.published:
                        return self.answer(404, b"{}")
                    return self.answer(200, json.dumps(gateway.manifest).encode())
                if self.path != gateway.manifest["path"]:
                    return self.answer(404, b"{}")
                data = gateway.data
                if gateway.corrupt:
                    data = data[:-1] + b"!"
                start = 0
                value = self.headers.get("Range")
                if value and not gateway.ignore_range:
                    start = int(value.removeprefix("bytes=").removesuffix("-"))
                    if start >= len(data):
                        return self.answer(416, b"")
                part = data[start:]
                self.send_response(206 if start else 200)
                if start:
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                self.send_header("Content-Length", str(len(part)))
                self.end_headers()
                if gateway.cut_after is not None:
                    cut, gateway.cut_after = gateway.cut_after, None
                    self.wfile.write(part[:cut])
                    self.wfile.flush()
                    self.close_connection = True
                    return None
                self.wfile.write(part)
                return None

            def answer(self, status, data):
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.render = {
            "endpoint": f"ws://127.0.0.1:{self.server.server_address[1]}/firefox-webgl",
            "token": TOKEN,
            "version": VERSION,
        }

    def publish(self, data):
        self.data = data
        self.manifest = manifest_for(data)

    def snapshot_requests(self):
        return [entry for entry in self.requests if "/snapshot/" in entry[0]]


@pytest.fixture
def gateway():
    gw = Gateway()
    yield gw
    gw.server.shutdown()


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(scene_cache, "user_cache_dir", lambda name: str(tmp_path / "cache"))
    monkeypatch.setattr(scene_cache, "RETRY_S", 0.01)
    monkeypatch.delenv(scene_cache.UPDATE_ENV, raising=False)
    path = scene_cache.directory(VERSION, "ab" * 32, OS)
    path.mkdir(parents=True)
    return path


def run(folder, gateway, stop=None):
    scene_cache.update(folder, OS, lambda: gateway.render, stop)


def installed(folder, data):
    path = scene_cache.verified_file(folder)
    assert path is not None
    assert path.name == f"{hashlib.sha256(data).hexdigest()}.sqlite"
    assert path.read_bytes() == data
    return path


def backdate(folder):
    state = json.loads((folder / scene_cache.STATE).read_text())
    state["checked_at"] = 0
    (folder / scene_cache.STATE).write_text(json.dumps(state))


def test_the_update_interval_works_unset_and_survives_a_bad_override(monkeypatch):
    monkeypatch.delenv(scene_cache.UPDATE_ENV, raising=False)
    assert scene_cache.update_seconds() == scene_cache.DEFAULT_UPDATE_S == 24 * 3600
    monkeypatch.setenv(scene_cache.UPDATE_ENV, "60")
    assert scene_cache.update_seconds() == 60
    for bad in ("soon", "-1"):
        monkeypatch.setenv(scene_cache.UPDATE_ENV, bad)
        assert scene_cache.update_seconds() == scene_cache.DEFAULT_UPDATE_S


def test_a_download_lands_whole_with_the_render_token(folder, gateway):
    run(folder, gateway)
    installed(folder, gateway.data)
    assert not list(folder.glob("*.part"))
    assert not (folder / scene_cache.LOCK).exists()
    manifest_request = gateway.requests[0]
    assert manifest_request[0] == f"/v1/scene-cache/manifest?build={VERSION}&os={OS}"
    assert manifest_request[2] == f"Bearer {TOKEN}"


def test_a_dropped_connection_resumes_where_it_stopped(folder, gateway, monkeypatch):
    monkeypatch.setattr(scene_cache, "CHUNK", 64 * 1024)
    gateway.cut_after = 500_000
    run(folder, gateway)
    installed(folder, gateway.data)
    ranges = [entry[1] for entry in gateway.snapshot_requests()]
    assert ranges[0] is None
    # Whole chunks reach the disk; the one the connection died inside is fetched again.
    assert 0 < int(ranges[1].removeprefix("bytes=").removesuffix("-")) <= 500_000


def test_a_part_file_from_an_earlier_launch_is_resumed(folder, gateway):
    part = folder / f"{gateway.manifest['sha256']}.sqlite.part"
    part.write_bytes(gateway.data[:1234])
    run(folder, gateway)
    installed(folder, gateway.data)
    assert gateway.snapshot_requests()[0][1] == "bytes=1234-"


def test_a_server_that_ignores_the_range_restarts_the_file(folder, gateway):
    part = folder / f"{gateway.manifest['sha256']}.sqlite.part"
    part.write_bytes(gateway.data[:1234])
    gateway.ignore_range = True
    run(folder, gateway)
    installed(folder, gateway.data)


def test_a_part_longer_than_the_file_starts_over(folder, gateway):
    part = folder / f"{gateway.manifest['sha256']}.sqlite.part"
    part.write_bytes(gateway.data + b"extra")
    run(folder, gateway)
    installed(folder, gateway.data)


def test_a_bad_hash_installs_nothing_and_leaves_nothing_to_resume(folder, gateway):
    gateway.corrupt = True
    run(folder, gateway)
    assert scene_cache.verified_file(folder) is None
    assert not list(folder.glob("*.sqlite"))
    assert not list(folder.glob("*.part"))
    # The next launch starts clean and succeeds.
    gateway.corrupt = False
    run(folder, gateway)
    installed(folder, gateway.data)


def test_a_file_that_is_not_sqlite_is_refused_even_with_the_right_hash(folder, gateway):
    gateway.publish(b"not a database" * 100)
    run(folder, gateway)
    assert scene_cache.verified_file(folder) is None
    assert not list(folder.glob("*.sqlite"))


def test_the_old_file_serves_until_the_new_one_is_whole_then_is_collected(folder, gateway):
    run(folder, gateway)
    old = installed(folder, gateway.data)
    new = body(b"b")
    gateway.publish(new)
    backdate(folder)
    gateway.corrupt = True
    run(folder, gateway)
    assert scene_cache.verified_file(folder) == old
    gateway.corrupt = False
    run(folder, gateway)
    installed(folder, new)
    assert not old.exists()
    assert len(list(folder.glob("*.sqlite"))) == 1


def test_nothing_is_asked_again_inside_the_interval(folder, gateway):
    run(folder, gateway)
    asked = len(gateway.requests)
    run(folder, gateway)
    assert len(gateway.requests) == asked
    backdate(folder)
    run(folder, gateway)
    assert len(gateway.requests) == asked + 1
    assert len(gateway.snapshot_requests()) == 1  # The same hash is not fetched again.


def test_nothing_published_is_remembered_and_keeps_what_is_here(folder, gateway):
    gateway.published = False
    run(folder, gateway)
    assert scene_cache.verified_file(folder) is None
    asked = len(gateway.requests)
    run(folder, gateway)
    assert len(gateway.requests) == asked

    gateway.published = True
    backdate(folder)
    run(folder, gateway)
    kept = installed(folder, gateway.data)
    gateway.published = False
    backdate(folder)
    run(folder, gateway)
    assert scene_cache.verified_file(folder) == kept


def test_a_lost_file_is_fetched_again_inside_the_interval(folder, gateway):
    run(folder, gateway)
    installed(folder, gateway.data).unlink()
    run(folder, gateway)
    installed(folder, gateway.data)


def test_a_file_of_the_wrong_size_or_a_symlink_is_not_handed_to_the_browser(folder, gateway, tmp_path):
    run(folder, gateway)
    path = installed(folder, gateway.data)
    path.write_bytes(b"short")
    assert scene_cache.verified_file(folder) is None
    path.unlink()
    target = tmp_path / "elsewhere"
    target.write_bytes(gateway.data)
    path.symlink_to(target)
    assert scene_cache.verified_file(folder) is None


@pytest.mark.parametrize(
    "change",
    [
        {"sha256": "nothex"},
        {"bytes": scene_cache.MAX_BYTES + 1},
        {"bytes": 0},
        {"bytes": True},
        {"build": "155.0"},
        {"os": "macos"},
        {"schema": 2},
        {"path": "https://evil.example/file"},
        {"path": "//evil.example/file"},
    ],
)
def test_a_manifest_that_does_not_check_out_is_ignored(folder, gateway, change):
    gateway.manifest = {**gateway.manifest, **change}
    run(folder, gateway)
    assert scene_cache.verified_file(folder) is None
    assert gateway.snapshot_requests() == []


def test_too_little_disk_downloads_nothing(folder, gateway, monkeypatch):
    usage = type("Usage", (), {"free": len(gateway.data)})
    monkeypatch.setattr(scene_cache.shutil, "disk_usage", lambda path: usage)
    run(folder, gateway)
    assert gateway.snapshot_requests() == []
    assert not (folder / scene_cache.LOCK).exists()


def test_a_live_lock_leaves_the_download_to_its_holder_and_a_stale_one_is_taken(folder, gateway):
    lock = folder / scene_cache.LOCK
    lock.touch()
    run(folder, gateway)
    assert gateway.requests == []
    old = time.time() - scene_cache.LOCK_STALE_S - 1
    os.utime(lock, (old, old))
    run(folder, gateway)
    installed(folder, gateway.data)


def test_a_lease_that_ends_stops_the_download_and_leaves_it_resumable(folder, gateway, monkeypatch):
    stop = threading.Event()
    original = scene_cache._Lock.touch

    def stop_after_first_chunk(self):
        original(self)
        stop.set()

    monkeypatch.setattr(scene_cache._Lock, "touch", stop_after_first_chunk)
    monkeypatch.setattr(scene_cache, "CHUNK", 100_000)
    run(folder, gateway, stop)
    assert scene_cache.verified_file(folder) is None
    part = folder / f"{gateway.manifest['sha256']}.sqlite.part"
    assert 0 < part.stat().st_size < len(gateway.data)
    assert not (folder / scene_cache.LOCK).exists()


def test_an_unreachable_or_refusing_gateway_never_raises(folder, gateway):
    gateway.allow_token = "another"
    run(folder, gateway)
    assert scene_cache.verified_file(folder) is None
    scene_cache.update(folder, OS, lambda: {"endpoint": "ws://127.0.0.1:1/x", "token": "t", "version": VERSION})
    scene_cache.update(folder, OS, lambda: {"endpoint": "nonsense", "token": "t", "version": VERSION})
    scene_cache.update(folder, OS, dict)


def test_a_server_that_sends_more_than_it_advertised_is_refused(folder, gateway):
    gateway.manifest = {**gateway.manifest, "bytes": 1000}
    run(folder, gateway)
    assert scene_cache.verified_file(folder) is None
    assert not list(folder.glob("*.sqlite"))
    assert not list(folder.glob("*.part"))


def test_a_renewed_token_is_used_for_the_next_connection(folder, gateway):
    current = {"token": TOKEN}

    def render():
        return {**gateway.render, **current}

    # The manifest goes out with the valid token; the snapshot's first try with an expired one.
    original = gateway.server.RequestHandlerClass.do_GET

    def rotate(handler):
        if "/snapshot/" in handler.path and not gateway.snapshot_requests():
            gateway.allow_token = "renewed"
            current["token"] = "renewed"
        original(handler)

    gateway.server.RequestHandlerClass.do_GET = rotate
    scene_cache.update(folder, OS, render)
    installed(folder, gateway.data)


def test_clearing_keeps_only_this_build_and_os(folder):
    base = scene_cache.root()
    other_build = base / "155.0-ffffffffffffffff" / OS
    other_os = folder.parent / "macos"
    for path in (other_build, other_os):
        path.mkdir(parents=True)
        (path / "x.sqlite").write_bytes(b"x")
    (folder / "keep.sqlite").write_bytes(b"x")
    scene_cache.clear_others(folder)
    assert not other_build.parent.exists()
    assert not other_os.exists()
    assert (folder / "keep.sqlite").exists()


def test_the_directory_is_per_build_and_os_and_a_build_cannot_escape_it(folder):
    assert scene_cache.directory(VERSION, "ab" * 32, OS) != scene_cache.directory(VERSION, "cd" * 32, OS)
    assert scene_cache.directory(VERSION, "ab" * 32, OS) != scene_cache.directory("157.0", "ab" * 32, OS)
    assert scene_cache.directory(VERSION, "ab" * 32, OS) != scene_cache.directory(VERSION, "ab" * 32, "macos")
    assert scene_cache.root() in scene_cache.directory("../../..", "/", OS).parents
    assert ".." not in scene_cache.directory("/../..", "/", OS).parent.name.split("/")


class FakeLease:
    def __init__(self, directory, render):
        self.scene_cache_dir = str(directory)
        self.target_os = OS
        self.on_end = []
        self.grants = {"gpu": {"render": render}}


def wait_for(condition):
    deadline = time.time() + 10
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("never happened")


def test_attach_clears_other_builds_returns_nothing_at_first_then_the_file(folder, gateway):
    stale = scene_cache.root() / "155.0-ffffffffffffffff" / OS
    stale.mkdir(parents=True)
    lease = FakeLease(folder, gateway.render)
    assert scene_cache.attach(lease) is None
    assert not stale.exists()
    wait_for(lambda: scene_cache.verified_file(folder) is not None)
    wait_for(lambda: not (folder / scene_cache.LOCK).exists())
    for end in lease.on_end:
        end()
    assert scene_cache.attach(FakeLease(folder, gateway.render)) == scene_cache.verified_file(folder)


def test_attach_never_fails_a_launch_when_the_directory_cannot_be_made(tmp_path, gateway):
    blocker = tmp_path / "file"
    blocker.write_bytes(b"x")
    assert scene_cache.attach(FakeLease(blocker / "sub", gateway.render)) is None


def test_a_lease_file_names_the_directory_only_when_given_one():
    token = token_for(uuid.uuid4(), "ab" * 32)
    assert json.loads(pro.lease_file_bytes(token, OS, "native", 1))["paths"]["scene_cache_dir"] is None
    written = pro.lease_file_bytes(token, OS, "native", 1, "/home/üser/cache")
    assert json.loads(written)["paths"]["scene_cache_dir"] == "/home/üser/cache"
    # Unescaped, as JSON.stringify writes it, so the two launchers stay byte-identical.
    assert "/home/üser/cache".encode() in written
