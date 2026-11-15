"""
Launching a Camoufox Pro profile: its identity bundle, and its browser state
restored before the launch and synced back when the browser closes. A
`warm_plan: none` profile's state is sealed with the account's content key,
which never leaves the account's machines. A warmed profile's state is sealed
with the warm pool's key, which never reaches this machine: the API restores it
and serves the directory, and takes it back the same way. Python twin of
typescript/src/pro-profile.ts; see docs/pro.md, "Profiles".
"""

import base64
import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from platformdirs import user_cache_dir, user_config_dir

from . import pro
from .addons import DefaultAddons
from .exceptions import ProError, StatePoolSealed
from .pro_state import (
    CHUNKING,
    HARD_CAP,
    POLICY_VERSION,
    WORKERS,
    AccountKeys,
    ArchiveError,
    StateIntegrityError,
    StateTooLarge,
    canonical,
    capture,
    chunk_id_bytes,
    chunk_id_text,
    chunk_lengths,
    decode_manifest,
    manifest_of,
    read_archive,
    restore,
    write_archive,
)

CONTENT_KEY_ENV = "CAMOUFOX_PRO_CONTENT_KEY"
# The most chunks one uploads call may list.
UPLOAD_BATCH = 2000
FF_PLACEHOLDER = "{FF}"
# The refusals after which a capture is kept as a conflict, never merged, and
# those after which it is kept to retry.
CONFLICT = frozenset({"lease_not_holder", "state_window_expired", "state_conflict"})
PENDING = frozenset({"lease_conflict", "state_too_large"})
ARCHIVE_TYPE = "application/x-cfp-dir+gzip"
# Each session directory and kept capture has a mark under the profile's
# marks/ folder: the version it was restored from, which decides whether it can
# still be committed.

log = logging.getLogger("camoufox.pro")

# ── the content key ──────────────────────────────────────────────────────────


def content_key_path(account_id: int) -> Path:
    return Path(user_config_dir("camoufox")) / "pro-content-keys" / f"{account_id}.key"


def _decode_key(text: str, where: str) -> bytes:
    text = text.strip()
    key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(key) != 32:
        raise ValueError(f"{where} is not a 32-byte base64url content key")
    return key


def content_key(account_id: int) -> bytes:
    """
    The account's content key: CAMOUFOX_PRO_CONTENT_KEY, else the key file,
    which is created the first time. It is never sent anywhere; without it the
    account's synced state cannot be read.
    """
    from_env = os.environ.get(CONTENT_KEY_ENV, "").strip()
    if from_env:
        return _decode_key(from_env, CONTENT_KEY_ENV)
    path = content_key_path(account_id)
    if not path.exists():
        fresh = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=")
        if pro.write_private(path, fresh, replace=False):
            log.warning(
                "camoufox-pro: created this account's content key at %s. Copy it to every machine that "
                "launches the account's profiles (or set %s); state synced with a lost key cannot be "
                "read again.",
                path,
                CONTENT_KEY_ENV,
            )
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise PermissionError(f"{path} can be read by other users, so it is not used. Run: chmod 600 {path}")
    return _decode_key(path.read_text(), str(path))


# ── presigned transfers ──────────────────────────────────────────────────────


def _transfer(method: str, url: str, what: str, *, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None) -> bytes:
    failure = ""
    for delay in (*pro.MINT_RETRY_S, None):
        try:
            response = requests.request(method, url, data=data, headers=headers, timeout=120)
            if response.ok:
                return response.content
            failure = f"HTTP {response.status_code}"
            if response.status_code < 500 and response.status_code != 429:
                raise ProError(f"{what} failed: {failure}", status=response.status_code)
        except requests.RequestException as error:
            failure = str(error)
        if delay is None:
            raise ProError(f"{what} failed: {failure}")
        time.sleep(delay)
    raise AssertionError("unreachable")


def _download(url: str, what: str) -> bytes:
    return _transfer("GET", url, what)


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# ── the identity ─────────────────────────────────────────────────────────────


def fetch_bundle(section: Dict[str, Any]) -> Dict[str, Any]:
    """Download the profile's identity bundle and check it is the one the lease names."""
    body = _download(section["bundle"]["url"], "downloading the identity bundle")
    if _sha256(body).hex() != section["bundle"]["sha256"]:
        raise StateIntegrityError("the identity bundle does not match the sha256 its lease names")
    return json.loads(body)


def identity_options(bundle: Dict[str, Any], ff_major: str) -> Dict[str, Any]:
    """The bundle as launch options, for a browser whose Firefox major is `ff_major`."""
    return {
        "config": {
            key: value.replace(FF_PLACEHOLDER, ff_major) if isinstance(value, str) else value
            for key, value in bundle["config"].items()
        },
        "fingerprint": bundle["browserforge_fingerprint"],
        "os": bundle["profile"]["os"],
        "ff_version": int(ff_major),
        "firefox_user_prefs": dict(bundle["prefs"]),
        "exclude_addons": list(DefaultAddons),
    }


# ── where a profile's directories live ───────────────────────────────────────


def _profile_home(profile_id: str) -> Path:
    return Path(user_cache_dir("camoufox")) / "pro" / "profiles" / profile_id


def _marker(directory: Path) -> Path:
    """Where a session or kept directory's mark lives: beside its kind's folder, so the folders hold only state."""
    return directory.parent.parent / "marks" / f"{directory.parent.name}-{directory.name}.json"


def _mark(directory: Path, base_version: int, served: bool) -> None:
    """Record the version a session directory was restored from."""
    _marker(directory).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pro.write_private(_marker(directory), json.dumps({"base_version": base_version, "served": served}).encode())


def _read_mark(directory: Path) -> Optional[Dict[str, Any]]:
    try:
        mark = json.loads(_marker(directory).read_text())
    except (OSError, ValueError):
        return None
    return mark if isinstance(mark, dict) and isinstance(mark.get("base_version"), int) else None


def _move(directory: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.rename(destination)
    if _marker(directory).exists():
        _marker(destination).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _marker(directory).rename(_marker(destination))


def _keep(directory: Path, profile_id: str, kind: str, why: str) -> None:
    """Move a capture that was not committed aside, where it is kept, and say so."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    kept = _profile_home(profile_id) / kind / stamp
    _move(directory, kept)
    log.warning(
        "camoufox-pro: profile %s's state was not synced (%s); this session's state is kept at %s",
        profile_id,
        why,
        kept,
    )


# ── restore ──────────────────────────────────────────────────────────────────

# What is already stored of a chunk: its sealed size and the sha256 of its sealed bytes.
Stored = Tuple[int, bytes]


def _restore_state(
    lease: pro.Lease, section: Dict[str, Any], keys: AccountKeys, target: Path, ff_version: str
) -> Dict[str, Stored]:
    known: Dict[str, Stored] = {}
    version = section["state"]["version"]
    if version == 0:
        target.mkdir(mode=0o700)
        return known
    route = f"/api/v1/profiles/{section['id']}/state?version={version}"
    state = lease.api("GET", route)
    manifest_ref = state["manifest"]
    urls: Dict[str, str] = {}
    while True:
        urls.update((chunk["chunk_id"], chunk["url"]) for chunk in state["chunks"])
        if not state.get("next"):
            break
        state = lease.api("GET", f"{route}&cursor={quote(state['next'])}")
    sealed = _download(manifest_ref["url"], "downloading the state manifest")
    if _sha256(sealed).hex() != manifest_ref["sha256"]:
        raise StateIntegrityError("the state manifest does not match the sha256 the API names")
    manifest = decode_manifest(keys.open_manifest(section["id"], version, sealed), section["id"], version)

    def fetch(chunk_id: str) -> bytes:
        if chunk_id not in urls:
            raise ValueError(f"the API listed no chunk {chunk_id}")
        blob = _download(urls[chunk_id], "downloading a state chunk")
        known[chunk_id] = (len(blob), _sha256(blob))
        return blob

    restore(manifest, keys, fetch, target, ff_version)
    return known


# ── capture and commit ───────────────────────────────────────────────────────


def _commit_state(
    lease: pro.Lease,
    profile_id: str,
    keys: AccountKeys,
    known: Dict[str, Stored],
    base_version: int,
    directory: Path,
    ff_version: str,
    release: bool = True,
    crashed: bool = False,
) -> int:
    """
    Chunk and seal a captured profile, upload what the store does not hold, and
    commit it as the next version, releasing the lease with the commit unless
    `release` is false. Returns the committed version.
    """
    snapshot = capture(directory)
    sealed_dir = directory.with_name(directory.name + ".sealed")
    lease.scratch.append(sealed_dir)
    sealed_dir.mkdir(mode=0o700)
    # Each chunk's plaintext location, so one the store lost can be sealed again.
    where: Dict[str, Tuple[str, int, int]] = {}
    sealed_size = {chunk_id: stored for chunk_id, (stored, _) in known.items()}
    claims: Dict[str, Dict[str, Any]] = {}
    layout: List[Tuple[Any, List[Dict[str, Any]]]] = []

    def seal_chunk(chunk_id: str, plaintext: bytes) -> Dict[str, Any]:
        blob = keys.seal_chunk(chunk_id_bytes(chunk_id), plaintext)
        path = sealed_dir / chunk_id
        path.write_bytes(blob)
        path.chmod(0o600)
        sealed_size[chunk_id] = len(blob)
        return {"chunk_id": chunk_id, "size": len(blob), "sha256": _sha256(blob).hex()}

    for captured in snapshot.files:
        data = (directory / captured.path).read_bytes()
        if len(data) != captured.size:
            raise RuntimeError(f"{captured.path} changed size after capture")
        refs = []
        offset = 0
        for length in chunk_lengths(data):
            plaintext = data[offset : offset + length]
            chunk_id = chunk_id_text(keys.chunk_id(plaintext))
            where[chunk_id] = (captured.path, offset, length)
            if chunk_id not in claims:
                if chunk_id in known:
                    stored, digest = known[chunk_id]
                    claims[chunk_id] = {"chunk_id": chunk_id, "size": stored, "sha256": digest.hex()}
                else:
                    claims[chunk_id] = seal_chunk(chunk_id, plaintext)
            refs.append({"id": chunk_id, "size": length})
            offset += length
        layout.append((captured, refs))
    total = sum(captured.size for captured in snapshot.files)
    route = f"/api/v1/profiles/{profile_id}/state"

    def put(missing: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        path = sealed_dir / missing["chunk_id"]
        if not path.exists():
            # Stored once, lost since: sealed again, so its sha256 changes.
            relpath, offset, length = where[missing["chunk_id"]]
            data = (directory / relpath).read_bytes()
            return seal_chunk(missing["chunk_id"], data[offset : offset + length])
        _transfer("PUT", missing["url"], "uploading a state chunk", data=path.read_bytes(), headers=missing["headers"])
        return None

    def upload(listed: List[Dict[str, Any]]) -> None:
        resealed: List[Dict[str, Any]] = []
        for start in range(0, len(listed), UPLOAD_BATCH):
            answer = lease.api(
                "POST",
                f"{route}/uploads",
                {
                    "lease_id": lease.lease_id,
                    "base_version": base_version,
                    "total_bytes": total,
                    "chunks": listed[start : start + UPLOAD_BATCH],
                },
            )
            with ThreadPoolExecutor(WORKERS) as pool:
                resealed += [claim for claim in pool.map(put, answer["missing"]) if claim is not None]
        if resealed:
            upload(resealed)

    upload(list(claims.values()))

    version = base_version + 1
    manifest = manifest_of(
        {
            "profile_id": profile_id,
            "version": version,
            "base_version": base_version,
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "crashed": crashed,
            "integrity": "suspect" if snapshot.suspect_files else "ok",
            "suspect_files": list(snapshot.suspect_files),
            "ff_version": ff_version,
            "driver_version": pro.client_name(),
            "policy_version": POLICY_VERSION,
            "chunking": dict(CHUNKING),
            "files": [
                {
                    "path": captured.path,
                    "size": captured.size,
                    "mode": captured.mode,
                    "mtime": captured.mtime,
                    "chunks": [{**ref, "stored": sealed_size[ref["id"]]} for ref in refs],
                }
                for captured, refs in layout
            ],
            "dirs": list(snapshot.dirs),
        }
    )
    body = keys.seal_manifest(profile_id, version, canonical(manifest))
    commit = {
        "lease_id": lease.lease_id,
        "base_version": base_version,
        "version": version,
        "release": release,
        "manifest": {
            "sha256": _sha256(body).hex(),
            "size": len(body),
            "chunk_count": manifest["chunk_count"],
            "total_bytes": manifest["total_bytes"],
            "chunks": [{"chunk_id": chunk_id, "size": sealed_size[chunk_id]} for chunk_id in claims],
        },
        "manifest_body": base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii"),
        "file_count": manifest["file_count"],
        "captured_at": manifest["captured_at"],
        "ff_version": ff_version,
        "driver_version": manifest["driver_version"],
        "crashed": manifest["crashed"],
        "integrity": manifest["integrity"],
    }
    try:
        lease.api("PUT", route, commit)
    except ProError as error:
        if error.code != "state_chunks_missing":
            raise
        upload([claims[chunk_id] for chunk_id in error.details.get("chunk_ids", [])])
        lease.api("PUT", route, commit)
    return version


# ── state the API serves ─────────────────────────────────────────────────────


def _restore_served(lease: pro.Lease, section: Dict[str, Any], target: Path, ff_version: str) -> int:
    """Download a warmed profile's state as the API restored it, into `target`. Returns its version."""
    response = lease.send(
        "GET",
        section["state"]["archive"],
        {"lease_id": str(lease.lease_id), "ff_version": ff_version},
        stream=True,
    )
    response.raw.decode_content = False
    try:
        read_archive(response.raw, target, HARD_CAP)
    except ArchiveError as error:
        raise StateIntegrityError(f"the served state is not a cfp-dir/1 archive: {error}") from None
    finally:
        response.close()
    return int(response.headers.get("x-cfp-state-version", section["state"]["version"]))


def _commit_served(
    lease: pro.Lease,
    section: Dict[str, Any],
    base_version: int,
    directory: Path,
    ff_version: str,
    release: bool = True,
    crashed: bool = False,
) -> int:
    """Capture a warmed profile and send it to the API, which commits it as the next version."""
    snapshot = capture(directory)
    packed = directory.with_name(directory.name + ".cfpdir.gz")
    lease.scratch.append(packed)
    with packed.open("wb") as out:
        write_archive(out, snapshot)
    params = {
        "lease_id": str(lease.lease_id),
        "base_version": str(base_version),
        "ff_version": ff_version,
        "release": "1" if release else "0",
        "crashed": "1" if crashed else "0",
    }
    for delay in (*pro.MINT_RETRY_S, None):
        try:
            with packed.open("rb") as body:
                answer = lease.send("PUT", section["state"]["archive"], params, data=body, content_type=ARCHIVE_TYPE).json()
            packed.unlink(missing_ok=True)
            return int(answer["version"])
        except ProError as error:
            if delay is None or not pro._transient(error):
                raise
            time.sleep(error.retry_after or delay)
    raise AssertionError("unreachable")


# ── a session that never closed ──────────────────────────────────────────────


def _recover(
    lease: pro.Lease,
    section: Dict[str, Any],
    keys: Optional[AccountKeys],
    base_version: int,
    target: Path,
    ff_version: str,
) -> Optional[int]:
    """
    Commit the newest capture an earlier session left behind (a crash, or a
    commit that failed and was kept to retry) before anything is restored, so
    a launch never starts from older state than this machine holds. It is used
    only when it was restored from the version the API still has as its head;
    anything else is kept as a conflict, never merged. On success the capture
    becomes this session's directory, at `target`, and its version is returned.
    """
    home = _profile_home(section["id"])
    candidates = []
    for kind in ("pending", "sessions"):
        folder = home / kind
        if not folder.is_dir():
            continue
        for directory in folder.iterdir():
            if not directory.is_dir() or directory == target or directory.name.startswith("."):
                continue
            if kind == "sessions" and str(directory) in {str(path) for live in pro._LIVE.values() for path in live.scratch}:
                continue
            mark = _read_mark(directory)
            if mark is not None:
                candidates.append((directory.stat().st_mtime, directory, mark))
    if not candidates:
        return None
    candidates.sort(key=lambda found: found[0], reverse=True)
    for _, stale, _ in candidates[1:]:
        _keep(stale, section["id"], "conflicts", "a newer capture of this profile was left behind as well")
    _, directory, mark = candidates[0]
    if mark["base_version"] != base_version:
        _keep(
            directory,
            section["id"],
            "conflicts",
            f"it was restored from v{mark['base_version']} and the profile is at v{base_version} now",
        )
        return None
    log.warning("camoufox-pro: profile %s: committing the state a session left behind at %s", section["key"], directory)
    try:
        if keys is None:
            version = _commit_served(lease, section, base_version, directory, ff_version, release=False, crashed=True)
        else:
            version = _commit_state(lease, section["id"], keys, {}, base_version, directory, ff_version, release=False, crashed=True)
    except ProError as error:
        if error.code in CONFLICT:
            _keep(directory, section["id"], "conflicts", str(error))
            return None
        raise
    _move(directory, target)
    return version


# ── a profile launch ─────────────────────────────────────────────────────────


def open_profile(lease: pro.Lease, ff_version: str) -> Tuple[Dict[str, Any], Path]:
    """
    Prepare a profile's launch under `lease`: its identity (as launch options),
    and a user-data directory holding its restored state. The lease then syncs
    the state back when the browser closes, and releases itself with that commit.
    """
    section = lease.grants["profile"]
    identity = identity_options(fetch_bundle(section), ff_version.split(".", 1)[0])
    home = _profile_home(section["id"])
    (home / "sessions").mkdir(mode=0o700, parents=True, exist_ok=True)
    user_data_dir = home / "sessions" / str(lease.lease_id)
    lease.scratch.extend([user_data_dir, _marker(user_data_dir)])
    served = section["state"].get("transport") == "server"
    if section["key_class"] != "account" and not served:
        log.warning(
            "camoufox-pro: profile %s keeps its identity, but its browser state is not synced: this "
            "deployment does not serve warmed profiles' state",
            section["key"],
        )
        user_data_dir.mkdir(mode=0o700)
        return identity, user_data_dir
    keys = None if served else AccountKeys.derive(content_key(lease.account_id), lease.account_id)
    base_version = section["state"]["version"]
    known: Dict[str, Stored] = {}
    recovered = _recover(lease, section, keys, base_version, user_data_dir, ff_version)
    if recovered is not None:
        base_version = recovered
    elif served:
        base_version = _restore_served(lease, section, user_data_dir, ff_version)
    else:
        assert keys is not None
        known = _restore_state(lease, section, keys, user_data_dir, ff_version)
    _mark(user_data_dir, base_version, served)
    lease.state_dirty = True

    def sync(lease: pro.Lease) -> None:
        # The commit releases the lease, so a renewal racing it must not mint a new one.
        lease.stop_renewing()
        try:
            if keys is None:
                _commit_served(lease, section, base_version, user_data_dir, ff_version)
            else:
                _commit_state(lease, section["id"], keys, known, base_version, user_data_dir, ff_version)
        except Exception as error:
            code = error.code if isinstance(error, ProError) else None
            kept = isinstance(error, StateTooLarge) or code in PENDING or code in CONFLICT
            if user_data_dir.exists():
                kind = "conflicts" if code in CONFLICT or isinstance(error, StatePoolSealed) else "pending"
                _keep(user_data_dir, section["id"], kind, str(error))
            lease.release()
            if kept:
                return
            raise
        lease.forget()

    def abort(lease: pro.Lease) -> None:
        # The process is exiting with the browser never closed: keep the
        # directory where the next launch of this profile finds and commits it.
        for path in (user_data_dir, _marker(user_data_dir)):
            if path in lease.scratch:
                lease.scratch.remove(path)

    lease.on_close = sync
    lease.on_abort = abort
    return identity, user_data_dir
