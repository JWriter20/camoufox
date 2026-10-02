"""
Launching a Camoufox Pro profile: its identity bundle, and its browser state
restored before the launch and synced back when the browser closes. The state
is sealed with the account's content key, which never leaves the account's
machines. Python twin of typescript/src/pro-profile.ts; see docs/pro.md,
"Profiles".
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
    POLICY_VERSION,
    WORKERS,
    AccountKeys,
    StateIntegrityError,
    StateTooLarge,
    canonical,
    capture,
    chunk_id_bytes,
    chunk_id_text,
    chunk_lengths,
    decode_manifest,
    manifest_of,
    restore,
)

CONTENT_KEY_ENV = "CAMOUFOX_PRO_CONTENT_KEY"
# The most chunks one uploads call may list.
UPLOAD_BATCH = 2000
FF_PLACEHOLDER = "{FF}"
# The refusals after which a capture is kept as a conflict, never merged, and
# those after which it is kept to retry.
CONFLICT = frozenset({"lease_not_holder", "state_window_expired", "state_conflict"})
PENDING = frozenset({"lease_conflict", "state_too_large"})

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


def _keep(directory: Path, profile_id: str, kind: str, why: str) -> None:
    """Move a capture that was not committed aside, where it is kept, and say so."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    kept = _profile_home(profile_id) / kind / stamp
    kept.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.rename(kept)
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
) -> None:
    """
    Chunk and seal a captured profile, upload what the store does not hold, and
    commit it as the next version, releasing the lease with the commit.
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
            "crashed": False,
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
        "release": True,
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
    lease.scratch.append(user_data_dir)
    if section["key_class"] != "account":
        log.warning(
            "camoufox-pro: profile %s keeps its identity, but its browser state is not synced: only a "
            "profile whose state is sealed with the account's own key is (this one is %r)",
            section["key"],
            section["key_class"],
        )
        user_data_dir.mkdir(mode=0o700)
        return identity, user_data_dir
    keys = AccountKeys.derive(content_key(lease.account_id), lease.account_id)
    known = _restore_state(lease, section, keys, user_data_dir, ff_version)
    base_version = section["state"]["version"]

    def sync(lease: pro.Lease) -> None:
        # The commit releases the lease, so a renewal racing it must not mint a new one.
        lease.stop_renewing()
        try:
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

    lease.on_close = sync
    return identity, user_data_dir
