"""
Camoufox Pro: signing in, and the session lease a Pro build starts with.

A Pro build ships pro-build.json beside its executable. The browser verifies a
signed lease from the file named by CAMOU_LEASE_FILE and will not start without
one. This module is the client half: it mints the lease from the Camoufox Pro
API, writes the file, renews it while the browser runs, and releases it when
the browser closes. A build without pro-build.json is launched as before, with
no request made. See docs/pro.md.
"""

import atexit
import base64
import getpass
import hashlib
import json
import logging
import os
import random
import re
import socket
import subprocess  # nosec
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import requests
from platformdirs import user_cache_dir, user_config_dir

from .exceptions import (
    AccountSuspended,
    AllowanceExhausted,
    BuildNotAllowlisted,
    CapabilityMismatch,
    InvalidRequest,
    LeaseLimitReached,
    LeaseRefused,
    NotSignedIn,
    ProClockSkew,
    ProError,
    ProUnavailable,
    RateLimited,
    SubscriptionRequired,
)

API_ENV = "CAMOUFOX_PRO_API"
KEY_ENV = "CAMOUFOX_PRO_KEY"
LEASE_FILE_ENV = "CAMOU_LEASE_FILE"
DEFAULT_API = "https://api.camoufox.com"
KEY_PREFIX = "cfp_live_"

# SPEC-LEASE § 10: mint retries, the heartbeat backoff table, and the skew
# thresholds (the last is the lease grace, past which a fresh token already
# looks expired to the browser).
MINT_RETRY_S = (1, 2, 4)
HEARTBEAT_BACKOFF_S = (5, 10, 20, 40, 60)
HEARTBEAT_TIMEOUT_S = 10
RELEASE_TIMEOUT_S = 5
UNREACHABLE_WARN_S = 180
SKEW_WARN_S = 60
SKEW_REFUSE_S = 180
STALE_LEASE_S = 24 * 3600

REFUSAL = re.compile(r"camoufox-pro: lease refused \(([^)\n]*)\)")
NOT_SIGNED_IN = "Camoufox Pro needs a key: run `camoufox login`, or set CAMOUFOX_PRO_KEY."

HOST_OS = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
TARGET_OS = {"win": "windows", "mac": "macos", "lin": "linux"}
# The sections a lease answer carries, each null when the lease does not grant it.
SECTIONS = ("profile", "egress", "gpu", "captcha")

log = logging.getLogger("camoufox.pro")

_BY_CODE = {
    "invalid_request": InvalidRequest,
    "invalid_api_key": NotSignedIn,
    "subscription_required": SubscriptionRequired,
    "allowance_exhausted": AllowanceExhausted,
    "account_suspended": AccountSuspended,
    "build_not_allowlisted": BuildNotAllowlisted,
    "lease_limit_reached": LeaseLimitReached,
    "capability_mismatch": CapabilityMismatch,
    "rate_limited": RateLimited,
}


def api_base() -> str:
    return (os.environ.get(API_ENV, "").strip() or DEFAULT_API).rstrip("/")


def client_name() -> str:
    return f"camoufox-python/{package_version('camoufox')}"


# ── files only this user may read ────────────────────────────────────────────


def _owner_only(path: Path) -> None:
    """Windows has no mode bits; strip inherited ACEs and grant the owner alone."""
    subprocess.run(  # nosec
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
        check=True,
        capture_output=True,
    )


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "nt":
        _owner_only(path)
    else:
        path.chmod(0o700)


def write_private(path: Path, data: bytes) -> None:
    """
    Replace `path` atomically with a file only this user can read: a temporary
    file beside it, fsynced, then renamed over it, so a reader never sees half a
    file (SPEC-LEASE § 8.3).
    """
    _private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f"{path.name}.tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            _owner_only(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── the key ──────────────────────────────────────────────────────────────────


def credential_path() -> Path:
    return Path(user_config_dir("camoufox")) / "pro-credentials.json"


def _stored_key() -> Optional[str]:
    path = credential_path()
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    if os.name != "nt" and mode & 0o077:
        raise PermissionError(
            f"{path} can be read by other users, so it is not used. Run: chmod 600 {path}"
        )
    return json.loads(path.read_text())["api_key"]


def resolve_key(explicit: Optional[str] = None) -> str:
    """The key to use: the `pro_key` argument, else CAMOUFOX_PRO_KEY, else `camoufox login`'s."""
    key = explicit or os.environ.get(KEY_ENV, "").strip() or _stored_key()
    if not key:
        raise NotSignedIn(NOT_SIGNED_IN)
    if not key.startswith(KEY_PREFIX):
        raise NotSignedIn(f"A Camoufox Pro key starts with {KEY_PREFIX}. {NOT_SIGNED_IN}")
    return key


def masked(key: str) -> str:
    return f"{KEY_PREFIX}...{key[-4:]}"


def store_key(key: str) -> Path:
    path = credential_path()
    write_private(path, json.dumps({"api_key": key}).encode())
    return path


def forget_key() -> Optional[str]:
    """Delete the stored key. Returns its masked form, or None when there was none."""
    path = credential_path()
    try:
        key = json.loads(path.read_text())["api_key"]
    except FileNotFoundError:
        return None
    path.unlink()
    return masked(key)


# ── the API ──────────────────────────────────────────────────────────────────


def _error(status: int, body: Dict[str, Any], retry_after: Optional[str]) -> ProError:
    code = body.get("error")
    message = str(body.get("message") or f"HTTP {status}")
    if code == "invalid_api_key":
        message = f"{message} {NOT_SIGNED_IN}"
    if code == "lease_limit_reached":
        holders = body.get("details", {}).get("holders", [])
        held = ", ".join(
            f"{holder.get('host_label') or holder.get('host_fingerprint', '')[:12]} until {holder.get('expires_at')}"
            for holder in holders
        )
        message = f"{message} Held by: {held}." if held else message
    kind = _BY_CODE.get(code) or (
        RateLimited if status == 429 else ProUnavailable if status >= 500 else ProError
    )
    wait = body.get("retry_after") or retry_after
    return kind(
        message,
        code=code,
        status=status,
        resolution_url=body.get("resolution_url"),
        retry_after=int(wait) if wait is not None else None,
        details=body.get("details"),
    )


def post(route: str, payload: Dict[str, Any], *, key: Optional[str] = None, timeout: float = 30) -> Dict[str, Any]:
    """POST to the Camoufox Pro API. Every failure is a ProError; ProUnavailable is the retryable kind."""
    headers = {"User-Agent": client_name()}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        response = requests.post(f"{api_base()}{route}", json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as error:
        raise ProUnavailable(f"The Camoufox Pro API at {api_base()} could not be reached: {error}") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400:
        raise _error(response.status_code, body if isinstance(body, dict) else {}, response.headers.get("Retry-After"))
    return body


def _transient(error: ProError) -> bool:
    return isinstance(error, (ProUnavailable, RateLimited))


# ── sign-in: RFC 8628's device flow ──────────────────────────────────────────


def login(echo: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """
    Sign in on this machine: show a code to approve in a browser, then create a
    Camoufox Pro key for this machine and store it. Returns the token response's
    account and user, and where the key was stored.
    """
    start = post("/api/v1/device/start", {"client": client_name()})
    echo(f"Open {start['verification_uri_complete']}")
    echo(f"and confirm the code {start['user_code']}.")
    interval = float(start["interval"])
    deadline = time.monotonic() + float(start["expires_in"])
    while True:
        sleep(interval)
        if time.monotonic() > deadline:
            raise NotSignedIn("The sign-in code expired before it was approved. Run `camoufox login` again.")
        try:
            granted = post("/api/v1/device/token", {"device_code": start["device_code"]})
            break
        except ProError as error:
            if error.code == "authorization_pending" or isinstance(error, ProUnavailable):
                continue
            if error.code == "slow_down":
                interval += 5
                continue
            if error.code in ("expired_token", "access_denied", "invalid_grant"):
                raise NotSignedIn(f"Sign-in did not complete: {error.message} Run `camoufox login` again.") from None
            raise
    # The device flow grants a management token, which cannot run a browser; a
    # runtime key is what a lease is minted with, so one is created for this machine.
    created = post(
        "/api/v1/keys",
        {"name": f"camoufox on {socket.gethostname()}"[:64]},
        key=granted["access_token"],
    )
    return {"account": granted["account"], "user": granted["user"], "path": store_key(created["api_key"])}


# ── the lease ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProBuild:
    """What a Pro build's pro-build.json declares."""

    build_hash: str
    version: str
    target: str


def read_build(path: str) -> Optional[ProBuild]:
    """The build's pro-build.json, or None when the build is not a Pro build."""
    try:
        with open(path, "rb") as handle:
            declared = json.loads(handle.read())
    except FileNotFoundError:
        return None
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(declared.get("build_hash"))):
        raise ValueError(f"{path} does not declare a build_hash of the form sha256:<64 hex>.")
    return ProBuild(declared["build_hash"], str(declared.get("version")), str(declared.get("target")))


def lease_dir() -> Path:
    """SPEC-LEASE § 8.1."""
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "camoufox-pro" / "leases"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "camoufox-pro" if runtime else Path.home() / ".cache" / "camoufox-pro" / "leases"


def host_fingerprint_of(host_id: str, os_name: str, username: str) -> str:
    """SPEC-LEASE § 10.6. Telemetry naming this install; the API never refuses on it."""
    return hashlib.sha256(f"cfp-host-v1\0{host_id}\0{os_name}\0{username}".encode()).hexdigest()


def host_fingerprint() -> str:
    path = Path(user_cache_dir("camoufox")) / "pro" / "host.id"
    try:
        host_id = path.read_text().strip()
    except FileNotFoundError:
        host_id = os.urandom(16).hex()
        write_private(path, host_id.encode())
    return host_fingerprint_of(host_id, HOST_OS, getpass.getuser())


def token_fields(token: str) -> Dict[str, Any]:
    """The binding fields, read back from the token (SPEC-LEASE § 5.2) so the file always matches it."""
    body = token[len("cfl1_"):]
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    profile = raw[26:42]
    return {
        "lease_id": str(uuid.UUID(bytes=raw[2:18])),
        "account_id": int.from_bytes(raw[18:26], "big"),
        "profile_id": None if profile == bytes(16) else str(uuid.UUID(bytes=profile)),
        "build_hash": raw[42:74].hex(),
        "issued_at": int.from_bytes(raw[74:82], "big"),
        "expires_at": int.from_bytes(raw[82:90], "big"),
    }


def lease_file_bytes(token: str, target_os: str, fidelity: str, written_at: int) -> bytes:
    """SPEC-LEASE § 8.2. Byte for byte what the TypeScript launcher writes for the same inputs."""
    body = {
        "v": 1,
        **token_fields(token),
        "token": token,
        "os": target_os,
        "fidelity": fidelity,
        "paths": {"identity_bundle": None, "font_metrics": None, "scene_cache_dir": None, "gpu_bundle": None},
        "written_at": written_at,
    }
    return json.dumps(body, separators=(",", ":")).encode()


_skew_warned = False


def _check_skew(server_time: str) -> float:
    global _skew_warned
    skew = datetime.fromisoformat(server_time.replace("Z", "+00:00")).timestamp() - time.time()
    if abs(skew) > SKEW_WARN_S and not _skew_warned:
        _skew_warned = True
        log.warning(
            "camoufox-pro: this machine's clock is %+.0f s off the API's; lease expiry is "
            "evaluated on this machine's clock; fix NTP",
            skew,
        )
    return skew


def clean_stale_leases() -> None:
    """Remove lease files a process that died without releasing left behind (SPEC-LEASE § 8.1)."""
    directory = lease_dir()
    if not directory.is_dir():
        return
    cutoff = time.time() - STALE_LEASE_S
    for path in directory.iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            continue


class Lease:
    """
    One browser's lease: minted on creation, renewed in a daemon thread, and
    released by `release()`, which is idempotent and safe from any thread.
    """

    def __init__(self, build: ProBuild, target_os: str, key: str) -> None:
        self.build = build
        self.target_os = target_os
        self._key = key
        self._host = host_fingerprint()
        self.path: Optional[Path] = None
        self.lease_id: Optional[str] = None
        self.grants: Dict[str, Any] = {}
        self.claimed = False
        self._seq = 0
        self._heartbeat_s = 60.0
        self._fidelity = "native"
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._release_lock = threading.Lock()
        self._released = False
        self._mint()
        _LIVE[str(self.path)] = self
        self._thread = threading.Thread(target=self._beat, name="camoufox-pro-heartbeat", daemon=True)
        self._thread.start()

    def _mint(self) -> None:
        body = {
            "build_hash": self.build.build_hash,
            "os": self.target_os,
            "client": client_name(),
            "host": {
                "fingerprint": self._host,
                "label": socket.gethostname()[:64],
                "capability": {"host": {"os": HOST_OS}},
            },
            # One key across the retries of this mint, so a retry after a lost
            # response gets the same lease back rather than a second one.
            "idempotency_key": uuid.uuid4().hex,
        }
        for delay in (*MINT_RETRY_S, None):
            try:
                lease = post("/api/v1/leases", body, key=self._key)
                break
            except ProError as error:
                if delay is None or not _transient(error):
                    raise
                time.sleep(error.retry_after or delay)
        self.lease_id = lease["lease_id"]
        self.grants = {name: lease.get(name) for name in SECTIONS}
        self._seq = 0
        self._heartbeat_s = float(lease["limits"]["heartbeat_s"])
        self._fidelity = lease["host"]["fidelity"]
        if self.path is None:
            self.path = lease_dir() / f"{token_fields(lease['token'])['lease_id']}.json"
        skew = _check_skew(lease["server_time"])
        if abs(skew) > SKEW_REFUSE_S:
            self._post_release("error")
            raise ProClockSkew(skew)
        self._write(lease["token"])

    def _write(self, token: str) -> None:
        with self._write_lock:
            if self._released:
                return
            write_private(self.path, lease_file_bytes(token, self.target_os, self._fidelity, int(time.time())))

    def _next_beat(self) -> float:
        # Jittered by a twelfth, so a fleet started together does not beat together.
        return self._heartbeat_s * random.uniform(11 / 12, 13 / 12)  # nosec

    def _beat(self) -> None:
        """SPEC-LEASE § 10.2 and § 10.3."""
        wait = self._next_beat()
        failing_since: Optional[float] = None
        failures = 0
        while not self._stop.wait(wait):
            try:
                answer = post(
                    f"/api/v1/leases/{self.lease_id}/heartbeat",
                    {"seq": self._seq + 1, "host": {"fingerprint": self._host}},
                    key=self._key,
                    timeout=HEARTBEAT_TIMEOUT_S,
                )
            except ProError as error:
                # A 402 cannot happen on a heartbeat (allowances are checked at
                # mint), so it is treated as the server failing.
                if _transient(error) or error.status == 402:
                    if failing_since is None:
                        failing_since = time.monotonic()
                    elif time.monotonic() - failing_since >= UNREACHABLE_WARN_S:
                        log.warning(
                            "camoufox-pro: the API has been unreachable for %.0f s; the browser keeps "
                            "running until its lease expires",
                            time.monotonic() - failing_since,
                        )
                        failing_since = float("inf")  # warned; not again this outage
                    wait = error.retry_after or HEARTBEAT_BACKOFF_S[min(failures, len(HEARTBEAT_BACKOFF_S) - 1)]
                    failures += 1
                    continue
                if error.code == "lease_not_found" and not self._stop.is_set():
                    # Swept after an outage, or released elsewhere: one new lease into the same file.
                    try:
                        self._mint()
                    except ProError as again:
                        log.error("camoufox-pro: lease %s is gone and a new one was refused: %s", self.lease_id, again)
                        return
                    failing_since, failures, wait = None, 0, self._next_beat()
                    continue
                log.error("camoufox-pro: heartbeat refused, the lease will not be renewed: %s", error)
                return
            self._seq += 1
            _check_skew(answer["server_time"])
            self._write(answer["token"])
            failing_since, failures, wait = None, 0, self._next_beat()

    def _post_release(self, reason: str) -> bool:
        for attempt in range(2):
            try:
                post(f"/api/v1/leases/{self.lease_id}/release", {"reason": reason}, key=self._key, timeout=RELEASE_TIMEOUT_S)
                return True
            except ProError as error:
                if attempt or not _transient(error):
                    log.warning("camoufox-pro: releasing lease %s failed: %s", self.lease_id, error)
                    return False
        return False

    def release(self, reason: str = "clean_exit") -> bool:
        """
        Release the lease and delete its file. Only the first call does anything.
        Returns whether this call's release reached the API.
        """
        with self._release_lock:
            if self._released:
                return False
            self._stop.set()
            with self._write_lock:
                self._released = True
            _LIVE.pop(str(self.path), None)
            released = self._post_release(reason)
            if self.path is not None:
                self.path.unlink(missing_ok=True)
            return released


_LIVE: Dict[str, Lease] = {}


def _release_all() -> None:
    for lease in list(_LIVE.values()):
        lease.release("driver_shutdown")


atexit.register(_release_all)


def acquire(build: ProBuild, target_os: str, key: Optional[str] = None) -> Lease:
    """Mint a lease for a Pro build about to launch an identity of `target_os` ('win', 'mac', 'lin')."""
    resolved = resolve_key(key)
    clean_stale_leases()
    return Lease(build, TARGET_OS[target_os], resolved)


def claim(options: Dict[str, Any]) -> Optional[Lease]:
    """
    The lease launch_options() minted for these options, taken by the browser
    about to launch with them. None when they carry no lease this process holds:
    a stock build, or a lease file the caller manages.
    """
    lease = _LIVE.get(str(options.get("env", {}).get(LEASE_FILE_ENV)))
    if lease is None:
        return None
    if lease.claimed:
        raise ValueError(
            "These launch options' Camoufox Pro lease is already held by another browser. "
            "Each browser needs its own: call launch_options() once per browser."
        )
    lease.claimed = True
    return lease


def refusal(error: BaseException) -> Optional[str]:
    """The reason in a Pro browser's `lease refused` line, when a launch failed with one."""
    match = REFUSAL.search(str(error))
    return match.group(1) if match else None


def launch_failed(lease: Optional[Lease], error: BaseException) -> None:
    """
    For a launch that raised `error`: release its lease, and raise LeaseRefused
    when the browser exited because it refused the lease.
    """
    if lease:
        lease.release("error")
    reason = refusal(error)
    if reason is not None:
        raise LeaseRefused(reason) from error


def release_on_close(lease: Lease, target: Any, event: str) -> None:
    """Release `lease` when the browser or persistent context emits `event`."""

    def closed(*_: Any) -> None:
        # Off the caller's thread, so an async event loop is never blocked by the
        # request; not a daemon, so the interpreter finishes it before exiting.
        try:
            threading.Thread(target=lease.release, name="camoufox-pro-release").start()
        except RuntimeError:
            lease.release()

    target.on(event, closed)



# ── camoufox pro --activate ──────────────────────────────────────────────────

# What --activate shows of a granted section: never its credentials.
_SHOWN = {"egress": ("class", "country"), "gpu": ("mode", "renderer"), "captcha": ("remaining",)}


def _granted(name: str, section: Dict[str, Any]) -> str:
    detail = ", ".join(str(section[field]) for field in _SHOWN.get(name, ()) if section.get(field) is not None)
    return f"{name}: granted ({detail})" if detail else f"{name}: granted"


def activate(build_file: str, target_os: str, key: Optional[str] = None, echo: Callable[[str], None] = print) -> bool:
    """
    `camoufox pro --activate`: mint a lease for the Pro build whose pro-build.json
    is `build_file`, for an identity of `target_os` ('win', 'mac', 'lin'), report
    what the lease grants, and release it. Returns whether the API granted it.
    """
    try:
        build = read_build(build_file)
        if build is None:
            echo(f"[FAIL] no Camoufox Pro build: {build_file} does not exist")
            return False
        lease = acquire(build, target_os, key)
    except (ProError, PermissionError, ValueError) as error:
        echo(f"[FAIL] lease: {error}")
        return False
    try:
        echo(
            f"[ ok ] lease verified: {lease.lease_id} for {build.version}, "
            f"{lease.target_os} identity, {lease._fidelity} fidelity"
        )
        for name in SECTIONS:
            section = lease.grants.get(name)
            echo(f"[ ok ] {_granted(name, section)}" if section else f"[ -- ] {name}: not granted")
    finally:
        released = lease.release()
    echo("[ ok ] lease released" if released else "[ -- ] lease not released: the API frees it when it expires")
    return True
