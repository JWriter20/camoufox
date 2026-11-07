"""
Camoufox Pro: the scene cache a leased browser keeps on this machine.

One SQLite file per browser build and OS, fetched from the render gateway with
the lease's render token, updated about once a day, and deleted when the browser
build or OS changes. A miss is still drawn on the farm, so nothing here can fail
a launch: every problem is logged, and the browser runs without the file.

The browser reads the pref once per process, so a new file takes effect at the
next launch, and the first launch has none. TypeScript twin: scene_cache.ts.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import requests
from platformdirs import user_cache_dir

# The only way the browser learns the file. It takes one regular file, not a directory.
PREF = "gfx.canvas.renderfarm.cache.path"
UPDATE_ENV = "CAMOUFOX_PRO_SCENE_CACHE_UPDATE_S"
DEFAULT_UPDATE_S = 24 * 3600
# What the browser will open; a larger file could only ever miss.
MAX_BYTES = 20 * 1024**3
# Room to spare once the file is down, so a download does not fill the disk.
FREE_SPACE_FACTOR = 1.1
CONNECT_TIMEOUT_S = 15
READ_TIMEOUT_S = 60
ATTEMPTS = 5
RETRY_S = 5.0
# A live download touches its lock as it goes, so an older lock is a process that died.
LOCK_STALE_S = 600
CHUNK = 1024 * 1024
STATE = "state.json"
LOCK = "download.lock"
SQLITE_MAGIC = b"SQLite format 3\0"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")

log = logging.getLogger("camoufox.pro")


def root() -> Path:
    return Path(user_cache_dir("camoufox")) / "pro" / "scene-cache"


def directory(version: str, build_hash: str, target_os: str) -> Path:
    """Where this browser build keeps the file for this OS."""
    return root() / re.sub(r"[^0-9A-Za-z._-]", "_", f"{version}-{build_hash[:16]}") / target_os


def update_seconds() -> float:
    """How often to ask for a new file. Works unset; a bad override falls back rather than fail a launch."""
    raw = os.environ.get(UPDATE_ENV)
    if not raw:
        return float(DEFAULT_UPDATE_S)
    try:
        seconds = float(raw)
    except ValueError:
        seconds = -1.0
    if seconds < 0:
        log.warning("camoufox-pro: ignoring %s=%r; it must be seconds", UPDATE_ENV, raw)
        return float(DEFAULT_UPDATE_S)
    return seconds


def clear_others(keep: Path) -> None:
    """A file for another browser build or OS can only miss: delete it. One still open stays until next time."""
    base = root()
    if not base.is_dir():
        return
    for build_dir in base.iterdir():
        if build_dir != keep.parent:
            shutil.rmtree(build_dir, ignore_errors=True)
            continue
        for os_dir in build_dir.iterdir():
            if os_dir != keep:
                shutil.rmtree(os_dir, ignore_errors=True)


def _read_state(folder: Path) -> Dict[str, Any]:
    try:
        state = json.loads((folder / STATE).read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(folder: Path, state: Dict[str, Any]) -> None:
    staged = folder / f"{STATE}.new"
    with staged.open("w") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staged, folder / STATE)


def verified_file(folder: Path) -> Optional[Path]:
    """
    The installed file, if it is the one the state names. Hashed when it was
    installed, not at every launch: 20 GiB of hashing would hold the launch up.
    """
    state = _read_state(folder)
    sha = state.get("sha256")
    if not isinstance(sha, str) or not _HEX64.match(sha):
        return None
    path = folder / f"{sha}.sqlite"
    try:
        stat = path.lstat()
    except OSError:
        return None
    # The browser refuses a symlink, and would miss on everything.
    if not path.is_file() or path.is_symlink() or stat.st_size != state.get("bytes"):
        return None
    return path


class _Lock:
    """One download per partition across this machine's launches."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def acquire(self) -> bool:
        for _ in range(2):
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                self.held = True
                return True
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime < LOCK_STALE_S:
                        return False
                    self.path.unlink()
                except FileNotFoundError:
                    continue
        return False

    def touch(self) -> None:
        os.utime(self.path)

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


class _Stopped(Exception):
    pass


class _Updater:
    def __init__(
        self,
        folder: Path,
        target_os: str,
        render: Callable[[], Dict[str, Any]],
        stop: threading.Event,
    ) -> None:
        self.folder = folder
        self.target_os = target_os
        self.render = render
        self.stop = stop
        self.lock = _Lock(folder / LOCK)

    def _base(self) -> str:
        endpoint = urlsplit(self.render()["endpoint"])
        scheme = {"wss": "https", "ws": "http"}.get(endpoint.scheme)
        if scheme is None or not endpoint.netloc:
            raise ValueError(f"cannot derive the gateway from {endpoint.geturl()!r}")
        return f"{scheme}://{endpoint.netloc}"

    def _headers(self) -> Dict[str, str]:
        # The current token: renewal replaces it while a long download runs.
        return {"Authorization": f"Bearer {self.render()['token']}"}

    def _manifest(self) -> Optional[Dict[str, Any]]:
        version = self.render()["version"]
        response = requests.get(
            f"{self._base()}/v1/scene-cache/manifest",
            params={"build": version, "os": self.target_os},
            headers=self._headers(),
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            allow_redirects=False,
        )
        with response:
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise OSError(f"the manifest answered {response.status_code}")
            manifest = response.json()
        return _checked(manifest, version, self.target_os)

    def _fetch(self, part: Path, manifest: Dict[str, Any]) -> bool:
        """One connection's worth of the file. False when the server no longer has it."""
        size = manifest["bytes"]
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        if have == size:
            return True
        headers = self._headers()
        if have:
            headers["Range"] = f"bytes={have}-"
        response = requests.get(
            f"{self._base()}{manifest['path']}",
            headers=headers,
            stream=True,
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            allow_redirects=False,
        )
        with response:
            if response.status_code == 404:
                return False
            if response.status_code == 416:
                part.unlink(missing_ok=True)
                raise OSError("the server refused the resume offset; starting over")
            if response.status_code not in (200, 206):
                raise OSError(f"the snapshot answered {response.status_code}")
            resumed = response.status_code == 206
            if resumed and not response.headers.get("Content-Range", "").startswith(f"bytes {have}-"):
                part.unlink(missing_ok=True)
                raise OSError("the server resumed from another offset; starting over")
            written = have if resumed else 0
            with part.open("ab" if resumed else "wb") as handle:
                for chunk in response.iter_content(CHUNK):
                    if self.stop.is_set():
                        raise _Stopped
                    written += len(chunk)
                    if written > size:
                        break
                    handle.write(chunk)
                    self.lock.touch()
                handle.flush()
                os.fsync(handle.fileno())
            if written > size:
                part.unlink(missing_ok=True)
                raise OSError(f"the server sent more than the {size} bytes it advertised")
        return True

    def _download(self, part: Path, manifest: Dict[str, Any]) -> bool:
        """The whole file in `part`, resuming across dropped connections. False when the server no longer has it."""
        for attempt in range(ATTEMPTS):
            try:
                if not self._fetch(part, manifest):
                    return False
                if part.stat().st_size == manifest["bytes"]:
                    return True
                log.info("camoufox-pro: scene cache download ended early; resuming")
            except (requests.RequestException, OSError) as error:
                if attempt == ATTEMPTS - 1:
                    raise
                log.info("camoufox-pro: scene cache download interrupted (%s); resuming", error)
            if self.stop.wait(RETRY_S):
                raise _Stopped
        return False

    def _install(self, part: Path, manifest: Dict[str, Any]) -> Path:
        sha = manifest["sha256"]
        digest = hashlib.sha256()
        with part.open("rb") as handle:
            first = handle.read(len(SQLITE_MAGIC))
            digest.update(first)
            while chunk := handle.read(CHUNK):
                if self.stop.is_set():
                    raise _Stopped
                digest.update(chunk)
                self.lock.touch()
        if digest.hexdigest() != sha or first != SQLITE_MAGIC:
            # Not resumable: whichever bytes are bad are already in the part file.
            part.unlink(missing_ok=True)
            raise OSError(f"the downloaded scene cache does not match {sha}")
        target = self.folder / f"{sha}.sqlite"
        os.replace(part, target)
        self._record(sha, manifest["bytes"])
        return target

    def _record(self, sha: Optional[str], size: Optional[int]) -> None:
        _write_state(self.folder, {"sha256": sha, "bytes": size, "checked_at": int(time.time())})

    def _collect(self, keep: Optional[Path], part: Optional[Path]) -> None:
        for path in self.folder.iterdir():
            if path.suffix == ".sqlite" and path != keep or path.name.endswith(".part") and path != part:
                try:
                    path.unlink()
                except OSError as error:
                    # A browser still running from the old file holds it on Windows.
                    log.debug("camoufox-pro: keeping %s for now: %s", path, error)

    def run(self) -> None:
        current = verified_file(self.folder)
        state = _read_state(self.folder)
        recent = time.time() - float(state.get("checked_at") or 0) < update_seconds()
        # Recently asked and nothing published is an answer too; a state naming a lost file is not.
        if recent and (current is not None or state.get("sha256") is None and "checked_at" in state):
            return
        if not self.lock.acquire():
            return
        try:
            manifest = self._manifest()
            if manifest is None:
                self._record(state.get("sha256") if current else None, state.get("bytes") if current else None)
                self._collect(current, None)
                return
            if current is not None and current.name == f"{manifest['sha256']}.sqlite":
                self._record(manifest["sha256"], manifest["bytes"])
                return
            part = self.folder / f"{manifest['sha256']}.sqlite.part"
            have = part.stat().st_size if part.exists() else 0
            free = shutil.disk_usage(self.folder).free
            needed = (manifest["bytes"] - have) * FREE_SPACE_FACTOR
            if free < needed:
                log.warning(
                    "camoufox-pro: %d bytes free is too little for the %d-byte scene cache; drawing on the farm",
                    free,
                    manifest["bytes"],
                )
                return
            self._collect(current, part)
            if self._download(part, manifest):
                installed = self._install(part, manifest)
                self._collect(installed, None)
                log.info("camoufox-pro: scene cache %s is in place for the next launch", manifest["sha256"])
        finally:
            self.lock.release()


def _checked(manifest: Any, version: str, target_os: str) -> Dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ValueError("the manifest is not an object")
    sha, size, path = manifest.get("sha256"), manifest.get("bytes"), manifest.get("path")
    if manifest.get("schema") != 1 or manifest.get("build") != version or manifest.get("os") != target_os:
        raise ValueError("the manifest is for something else")
    if not isinstance(sha, str) or not _HEX64.match(sha):
        raise ValueError("the manifest has no SHA-256")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BYTES:
        raise ValueError(f"the manifest advertises {size!r} bytes")
    # Always a path on the gateway that holds the token, so it can never send the token elsewhere.
    if path != f"/v1/scene-cache/snapshot/{version}/{target_os}/{sha}":
        raise ValueError(f"the manifest points at {path!r}")
    return manifest


def update(
    folder: Path,
    target_os: str,
    render: Callable[[], Dict[str, Any]],
    stop: Optional[threading.Event] = None,
) -> None:
    """Bring `folder` up to date if it is due. Never raises: a failure is logged and tried at the next launch."""
    try:
        _Updater(folder, target_os, render, stop or threading.Event()).run()
    except _Stopped:
        pass
    except (requests.RequestException, OSError, ValueError, KeyError, TypeError) as error:
        log.warning("camoufox-pro: scene cache not updated: %s", error)


def attach(lease: Any) -> Optional[Path]:
    """
    Get the cache ready for a launch with this lease: the file to hand the
    browser, if there is one, and a background update ended with the lease.
    """
    folder = Path(lease.scene_cache_dir)
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        clear_others(folder)
        current = verified_file(folder)
    except OSError as error:
        log.warning("camoufox-pro: no scene cache for this launch: %s", error)
        return None
    stop = threading.Event()
    lease.on_end.append(stop.set)
    threading.Thread(
        target=update,
        args=(folder, lease.target_os, lambda: lease.grants["gpu"]["render"], stop),
        name="camoufox-pro-scene-cache",
        daemon=True,
    ).start()
    return current
