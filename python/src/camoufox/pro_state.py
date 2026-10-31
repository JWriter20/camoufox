"""
A Camoufox Pro profile's browser state, sealed on this machine: which files of
a Firefox profile travel, how they are cut into chunks, sealed with the
account's own key, and listed in a manifest, and how a manifest is written back
out as a profile directory. Nothing here talks to the API; see pro_profile.py.
Python twin of typescript/src/pro-state.ts; both pass the same vectors
(docs/pro.md, "Profiles").

Needs the `pro` extra: pip install "camoufox[pro]".
"""

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

try:
    import nacl.bindings
    import nacl.exceptions
    import pyfastcdc
    import zstandard
except ImportError as error:  # pragma: no cover - the message is the point
    raise ImportError(
        'Syncing a Camoufox Pro profile needs the pro extra: pip install "camoufox[pro]"'
    ) from error

# ── chunking: FastCDC 2020, gear "g1" ────────────────────────────────────────

CHUNKING: Dict[str, Any] = {"algo": "fastcdc", "min": 262144, "avg": 1048576, "max": 4194304, "gear": "g1"}
# The first 8 bytes of SHA-256("cfp/fastcdc-gear/g1"), big-endian, top bit cleared.
GEAR_SEED = 0x47FB_985C_9B39_3779

_CHUNKER = pyfastcdc.FastCDC(
    CHUNKING["avg"], min_size=CHUNKING["min"], max_size=CHUNKING["max"], normalized_chunking=2, seed=GEAR_SEED
)


def chunk_lengths(data: bytes) -> List[int]:
    """The chunk lengths FastCDC g1 cuts `data` into."""
    return [chunk.length for chunk in _CHUNKER.cut_buf(data)]


# ── keys and sealing ─────────────────────────────────────────────────────────

NONCE_BYTES = 24
ZSTD_LEVEL = 3
PROFILE_ID = re.compile(r"prf_([0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})")


class StateIntegrityError(RuntimeError):
    """A sealed object that fails authentication, decompression or its id check."""


def hkdf_sha256(ikm: bytes, info: bytes) -> bytes:
    """RFC 5869 with an empty salt (32 zero bytes) and L = 32."""
    prk = hmac.digest(bytes(32), ikm, "sha256")
    return hmac.digest(prk, info + b"\x01", "sha256")


def uuid16(profile_id: str) -> bytes:
    """The 16 raw bytes of the UUID in a `prf_` id."""
    match = PROFILE_ID.fullmatch(profile_id)
    if match is None:
        raise ValueError(f"not a prf_ UUIDv7 profile id: {profile_id!r}")
    return uuid.UUID(match.group(1)).bytes


def chunk_aad(account_id: int, chunk_id: bytes) -> bytes:
    return b"cfp/chunk/1" + account_id.to_bytes(8, "big") + chunk_id


def manifest_aad(account_id: int, profile_id: str, version: int) -> bytes:
    return b"cfp/manifest/1" + account_id.to_bytes(8, "big") + uuid16(profile_id) + version.to_bytes(4, "big")


def seal(key: bytes, plaintext: bytes, aad: bytes, nonce: Optional[bytes] = None) -> bytes:
    """nonce || XChaCha20-Poly1305(key, nonce, plaintext, aad)."""
    nonce = nonce if nonce is not None else os.urandom(NONCE_BYTES)
    return nonce + nacl.bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, nonce, key)


def _open(key: bytes, blob: bytes, aad: bytes, what: str) -> bytes:
    try:
        return nacl.bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
            blob[NONCE_BYTES:], aad, blob[:NONCE_BYTES], key
        )
    except (nacl.exceptions.CryptoError, ValueError) as error:
        raise StateIntegrityError(f"{what} failed authentication") from error


def _zstd(data: bytes) -> bytes:
    return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(data)


@dataclass(frozen=True)
class AccountKeys:
    """An account's state keys, derived from its content key (K_acct); kept out of repr."""

    account_id: int
    k_id: bytes = field(repr=False)
    k_chunk: bytes = field(repr=False)
    k_man: bytes = field(repr=False)

    @classmethod
    def derive(cls, k_acct: bytes, account_id: int) -> "AccountKeys":
        if len(k_acct) != 32:
            raise ValueError(f"a content key is 32 bytes, got {len(k_acct)}")
        return cls(
            account_id,
            hkdf_sha256(k_acct, b"cfp/chunk-id/1"),
            hkdf_sha256(k_acct, b"cfp/chunk-enc/1"),
            hkdf_sha256(k_acct, b"cfp/manifest-enc/1"),
        )

    def chunk_id(self, plaintext: bytes) -> bytes:
        return hmac.digest(self.k_id, plaintext, "sha256")

    def seal_chunk(self, chunk_id: bytes, plaintext: bytes) -> bytes:
        return seal(self.k_chunk, _zstd(plaintext), chunk_aad(self.account_id, chunk_id))

    def open_chunk(self, chunk_id: bytes, blob: bytes, size: int) -> bytes:
        """Decrypt, decompress and recompute the id, so a chunk swapped in from elsewhere never restores."""
        compressed = _open(self.k_chunk, blob, chunk_aad(self.account_id, chunk_id), "chunk")
        try:
            plaintext = zstandard.ZstdDecompressor().decompress(compressed, max_output_size=size)
        except zstandard.ZstdError as error:
            raise StateIntegrityError(f"chunk does not decompress to {size} bytes: {error}") from error
        if len(plaintext) != size or not hmac.compare_digest(self.chunk_id(plaintext), chunk_id):
            raise StateIntegrityError("chunk plaintext does not match its id")
        return plaintext

    def seal_manifest(self, profile_id: str, version: int, document: bytes) -> bytes:
        return seal(self.k_man, _zstd(document), manifest_aad(self.account_id, profile_id, version))

    def open_manifest(self, profile_id: str, version: int, blob: bytes) -> bytes:
        compressed = _open(self.k_man, blob, manifest_aad(self.account_id, profile_id, version), "manifest")
        return zstandard.ZstdDecompressor().decompress(compressed)


# ── the manifest ─────────────────────────────────────────────────────────────

MANIFEST_FORMAT = "cfp-state-manifest/1"
_SAFE_INTEGER = (1 << 53) - 1


def canonical(document: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8: the same bytes as the TypeScript launcher's."""
    _check_integers(document)
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _check_integers(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _check_integers(item)
    elif isinstance(value, list):
        for item in value:
            _check_integers(item)
    elif isinstance(value, int) and not isinstance(value, bool) and abs(value) > _SAFE_INTEGER:
        raise ValueError(f"{value} is not a JSON-safe integer")


def self_hash(document: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical({**document, "sha256": ""})).hexdigest()


def manifest_of(fields: Dict[str, Any]) -> Dict[str, Any]:
    """The complete manifest for these fields: totals, counts and the self-hash filled in."""
    distinct = {ref["id"] for entry in fields["files"] for ref in entry["chunks"]}
    document = {
        "format": MANIFEST_FORMAT,
        **fields,
        "total_bytes": sum(entry["size"] for entry in fields["files"]),
        "file_count": len(fields["files"]),
        "chunk_count": len(distinct),
        "sha256": "",
    }
    document["sha256"] = self_hash(document)
    return document


def chunk_id_text(chunk_id: bytes) -> str:
    return base64.urlsafe_b64encode(chunk_id).rstrip(b"=").decode("ascii")


def chunk_id_bytes(text: str) -> bytes:
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(raw) != 32 or chunk_id_text(raw) != text:
        raise ValueError(f"malformed chunk id {text!r}")
    return raw


def _check_relpath(relpath: str) -> None:
    parts = relpath.split("/")
    if relpath.startswith("/") or any(part in ("", ".", "..") for part in parts) or "\\" in relpath or "\0" in relpath:
        raise ValueError(f"manifest path {relpath!r} is not a plain relative path")


def decode_manifest(data: bytes, profile_id: str, version: int) -> Dict[str, Any]:
    """Parse a decrypted manifest, refusing one whose self-hash, identity or counts do not hold."""
    document = json.loads(data)
    if document.get("format") != MANIFEST_FORMAT:
        raise ValueError(f"not a {MANIFEST_FORMAT} document: {document.get('format')!r}")
    if document["sha256"] != self_hash(document):
        raise ValueError("manifest self-hash does not match its contents")
    if (document["profile_id"], document["version"]) != (profile_id, version):
        raise ValueError(
            f"manifest is {document['profile_id']} v{document['version']}, expected {profile_id} v{version}"
        )
    fields = {
        key: value
        for key, value in document.items()
        if key not in ("format", "total_bytes", "file_count", "chunk_count", "sha256")
    }
    if manifest_of(fields) != document:
        raise ValueError("manifest totals or fields are inconsistent with its file list")
    for entry in document["files"]:
        _check_relpath(entry["path"])
        for ref in entry["chunks"]:
            chunk_id_bytes(ref["id"])
        if sum(ref["size"] for ref in entry["chunks"]) != entry["size"]:
            raise ValueError(f"{entry['path']}: chunk sizes do not add up to the file size")
    for directory in document["dirs"]:
        _check_relpath(directory)
    return document


# ── which files travel (state policy sp/2) ───────────────────────────────────

POLICY_VERSION = "sp/2"
HARD_CAP = 1 << 30
_DEFAULT_RULE_MAX = 1 << 20
# Files the launcher writes into every profile itself.
_DRIVER_OWNED = ("user.js", "motor.json")
_LOCK_FILES = ("lock", ".parentlock", "parent.lock")

_INCLUDED_FILES = frozenset({
    "cookies.sqlite", "places.sqlite", "favicons.sqlite",
    "storage.sqlite", "storage/ls-archive.sqlite",
    "sessionstore.jsonlz4",
    "key4.db", "cert9.db", "pkcs11.txt", "logins.json", "logins.db",
    "permissions.sqlite", "content-prefs.sqlite", "formhistory.sqlite", "webappsstore.sqlite",
    "protections.sqlite", "bounce-tracking-protection.sqlite", "notificationstore.json", "serviceworker.txt",
    "SiteSecurityServiceState.bin", "AlternateServices.bin",
    "prefs.js",
    "extensions.json", "extension-preferences.json", "extension-settings.json", "addons.json",
    "handlers.json", "containers.json", "search.json.mozlz4", "xulstore.json", "times.json",
    "signedInUser.json",
})  # fmt: skip
_INCLUDED_TREES = ("storage/default", "storage/permanent", "sessionstore-backups", "extensions", "extension-store")
_EXCLUDED_ROOT_DIRS = (
    "cache2", "startupCache", "thumbnails", "shader-cache", "jumpListCache", "OfflineCache", "gmp*",
    "safebrowsing", "remote-settings", "settings", "security_state",
    "datareporting", "crashes", "minidumps", "saved-telemetry-pings", "bookmarkbackups",
)  # fmt: skip
_EXCLUDED_NESTED_DIRS = ("storage/temporary", "extensions/staged")
_EXCLUDED_ROOT_FILES = (
    "suggest.sqlite", "domain_to_categories.sqlite",
    "Telemetry*.json", "ExperimentStoreData.json", "shield-preference-experiments.json",
    "activity-stream.*.json",
    "compatibility.ini", "addonStartup.json.lz4", "sessionCheckpoints.json", ".startup-incomplete",
    *_LOCK_FILES,
    *_DRIVER_OWNED,
)  # fmt: skip
_EXCLUDED_ANY_DEPTH = ("*.sqlite-wal", "*.sqlite-shm", "*.sqlite-journal", "*-corrupt", "*.tmp")
# The Nimbus store is root-only: IndexedDB files under storage/ can have the same shape of name.
_NIMBUS_STORE = re.compile(r"[0-9a-f]{8}\.sqlite")

# Prefs the launcher sets on every launch, and prefs naming a path on this host.
_MANAGED_PREF_PREFIXES = (
    "webgl.",
    "font.name-list.",
    "font.size.",
    "browser.cache.",
    "browser.sessionhistory.",
    "browser.tabs.remote.useCrossOriginOpenerPolicy",
    "dom.webgpu.enabled",
    "dom.w3c_touch_events.enabled",
    "gfx.bundled-fonts.activate",
    "javascript.options.use_ucrt_for_sin_cos_tan",
    "media.holo-",
    "media.peerconnection.enabled",
    "network.dns.disableIPv6",
    "network.http.http2.timeout",
    "network.http.keep-alive.timeout",
    "network.http.max-connections",
    "permissions.default.image",
    "ui.use_standins_for_native_colors",
)
_HOST_PATH_PREFS = frozenset({"browser.download.dir", "browser.download.lastDir", "print_printer"})
_USER_PREF = re.compile(rb'\s*user_pref\(\s*"([^"]*)"\s*,\s*(.*)\)\s*;\s*', re.S)
# prefs.js escapes a backslash as two, so `C:\` is written `C:\\`.
_HOST_PATH_VALUE = re.compile(rb'"(/|[A-Za-z]:\\\\)')


def _matches(name: str, patterns: Tuple[str, ...]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def _dir_excluded(relpath: str) -> bool:
    return ("/" not in relpath and _matches(relpath, _EXCLUDED_ROOT_DIRS)) or relpath in _EXCLUDED_NESTED_DIRS


def _under_excluded_dir(relpath: str) -> bool:
    parts = relpath.split("/")
    return any(_dir_excluded("/".join(parts[:depth])) for depth in range(1, len(parts)))


def _file_excluded(relpath: str) -> bool:
    name = relpath.rsplit("/", 1)[-1]
    if _under_excluded_dir(relpath) or _matches(name, _EXCLUDED_ANY_DEPTH):
        return True
    return "/" not in relpath and (_matches(name, _EXCLUDED_ROOT_FILES) or _NIMBUS_STORE.fullmatch(name) is not None)


def file_included(relpath: str, size: int) -> bool:
    """Whether a file of this path and size travels."""
    if _file_excluded(relpath):
        return False
    if relpath in _INCLUDED_FILES or any(relpath.startswith(tree + "/") for tree in _INCLUDED_TREES):
        return True
    return "/" not in relpath and size < _DEFAULT_RULE_MAX


def _dir_kept(relpath: str) -> bool:
    """A directory Firefox may expect even when empty: inside an included tree or on the way to one."""
    if _dir_excluded(relpath) or _under_excluded_dir(relpath):
        return False
    return any(
        relpath == tree or relpath.startswith(tree + "/") or tree.startswith(relpath + "/")
        for tree in _INCLUDED_TREES
    )


def filter_prefs(text: bytes) -> bytes:
    """Drop managed and host-bound user_pref lines; every other byte is kept."""

    def dropped(line: bytes) -> bool:
        match = _USER_PREF.fullmatch(line)
        if match is None:
            return False
        name = match.group(1).decode("latin-1")
        return (
            name.startswith(_MANAGED_PREF_PREFIXES)
            or name in _HOST_PATH_PREFS
            or _HOST_PATH_VALUE.match(match.group(2)) is not None
        )

    return b"\n".join(line for line in text.split(b"\n") if not dropped(line))


# ── capture ──────────────────────────────────────────────────────────────────


class StateTooLarge(RuntimeError):
    pass


@dataclass(frozen=True)
class CapturedFile:
    path: str
    size: int
    mode: int
    mtime: int


@dataclass(frozen=True)
class Snapshot:
    root: Path
    files: Tuple[CapturedFile, ...]
    dirs: Tuple[str, ...]
    suspect_files: Tuple[str, ...]


def _checkpoint(path: Path) -> bool:
    """
    Fold a database's pending write-ahead log into it and check it. False means
    the database must travel as it is, with its -wal and -shm.
    """
    family = [member for member in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")) if member.exists()]
    aside = [(member, member.with_name(member.name + ".cfp.tmp")) for member in family]
    for member, copy in aside:
        shutil.copy2(member, copy)
    folded = False
    try:
        with closing(sqlite3.connect(f"file:{quote(str(path))}?mode=rw", uri=True, timeout=1.0, isolation_level=None)) as db:
            busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy:
                raise RuntimeError(f"another connection holds {path}, so its WAL cannot be checkpointed")
            folded = True
            return db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    except sqlite3.DatabaseError:
        if not folded:
            for member, copy in aside:
                shutil.copy2(copy, member)
        return False
    finally:
        for _, copy in aside:
            copy.unlink(missing_ok=True)


def _sqlite_files(root: Path) -> List[str]:
    found = [root.glob("*.sqlite"), (root / "storage").rglob("*.sqlite")]
    relpaths = {
        path.relative_to(root).as_posix()
        for paths in found
        for path in paths
        if path.is_file() and not path.is_symlink()
    }
    return sorted(relpath for relpath in relpaths if not _file_excluded(relpath))


def capture(root: Path) -> Snapshot:
    """
    Capture a closed Firefox profile directory in place: fold pending SQLite
    write-ahead logs, filter prefs.js, drop the launcher's own files, and list
    what travels.
    """
    for name in _LOCK_FILES:
        (root / name).unlink(missing_ok=True)
    suspect = []
    for relpath in _sqlite_files(root):
        wal = root / (relpath + "-wal")
        # A browser that shut down cleanly leaves no WAL behind.
        if wal.exists() and wal.stat().st_size and not _checkpoint(root / relpath):
            suspect.append(relpath)
    forced = {f"{relpath}{suffix}" for relpath in suspect for suffix in ("-wal", "-shm")}
    prefs = root / "prefs.js"
    if prefs.is_file():
        before = prefs.read_bytes()
        after = filter_prefs(before)
        if after != before:
            prefs.write_bytes(after)
    for name in _DRIVER_OWNED:
        (root / name).unlink(missing_ok=True)

    files: List[CapturedFile] = []
    dirs: List[str] = []

    def walk(directory: Path, rel: str) -> bool:
        kept = False
        for entry in sorted(os.scandir(directory), key=lambda entry: entry.name):
            relpath = f"{rel}/{entry.name}" if rel else entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if _dir_excluded(relpath):
                    continue
                if walk(Path(entry.path), relpath):
                    kept = True
                elif _dir_kept(relpath):
                    dirs.append(relpath)
                    kept = True
            elif relpath in forced or file_included(relpath, info.st_size):
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"{relpath} is not a regular file; state holds regular files only")
                files.append(CapturedFile(relpath, info.st_size, stat.S_IMODE(info.st_mode), info.st_mtime_ns // 10**9))
                kept = True
        return kept

    walk(root, "")
    total = sum(captured.size for captured in files)
    if total > HARD_CAP:
        raise StateTooLarge(f"state_too_large: {total} bytes over the {HARD_CAP}-byte cap")
    return Snapshot(root, tuple(files), tuple(dirs), tuple(suspect))


# ── restore ──────────────────────────────────────────────────────────────────


class StateNewerThanBrowser(RuntimeError):
    """Firefox migrates profiles forward only; opening newer state in an older build corrupts it."""


def _major(ff_version: str) -> int:
    head = ff_version.split(".", 1)[0]
    if not head.isdigit():
        raise ValueError(f"not a Firefox version: {ff_version!r}")
    return int(head)


# How many chunks are fetched at once, and so held in memory at once.
WORKERS = 8


def restore(
    manifest: Dict[str, Any],
    keys: AccountKeys,
    fetch_chunk: Callable[[str], bytes],
    target: Path,
    browser_ff_version: str,
) -> None:
    """
    Write `manifest` out at `target`, which must not exist. `fetch_chunk`
    returns a chunk's sealed bytes. Nothing lands at `target` unless every chunk
    opens.
    """
    if _major(manifest["ff_version"]) > _major(browser_ff_version):
        raise StateNewerThanBrowser(
            f"state_newer_than_browser: {manifest['profile_id']} v{manifest['version']} was written by "
            f"Firefox {manifest['ff_version']} and cannot be opened by {browser_ff_version}"
        )
    if target.exists():
        raise FileExistsError(f"{target} exists; a restore never replaces a profile directory")
    staging = target.with_name(f".{target.name}.partial")
    staging.mkdir(mode=0o700)
    try:
        refs = [(entry, ref) for entry in manifest["files"] for ref in entry["chunks"]]

        def fetch(ref: Dict[str, Any]) -> bytes:
            return keys.open_chunk(chunk_id_bytes(ref["id"]), fetch_chunk(ref["id"]), ref["size"])

        with ThreadPoolExecutor(WORKERS) as pool:
            for start in range(0, len(refs), WORKERS):
                batch = refs[start : start + WORKERS]
                for (entry, _), plaintext in zip(batch, pool.map(fetch, [ref for _, ref in batch])):
                    path = staging / entry["path"]
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with path.open("ab") as out:
                        out.write(plaintext)
        for entry in manifest["files"]:
            # An empty file has no chunks.
            path = staging / entry["path"]
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.touch()
        for directory in manifest["dirs"]:
            (staging / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        prefs = staging / "prefs.js"
        if prefs.is_file():
            prefs.write_bytes(filter_prefs(prefs.read_bytes()))
        for entry in manifest["files"]:
            path = staging / entry["path"]
            os.chmod(path, entry["mode"])
            os.utime(path, (entry["mtime"], entry["mtime"]))
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# ── cfp-dir/1: state the API serves in the clear ─────────────────────────────
#
# A profile whose state is sealed under the warm pool's key never has that key
# here: the API restores it and sends the directory over TLS, and takes the
# directory back the same way. The format is the server's cfp.state.archive:
#
#   gzip(b"cfp-dir/1\n" { be32(len(header)) header_json [size bytes] } be32(0))
#
# with headers {"path", "type": "file"|"dir", "size", "mode", "mtime"}.

ARCHIVE_MAGIC = b"cfp-dir/1\n"
_ARCHIVE_HEADER_CAP = 64 << 10
_COPY = 1 << 20


class ArchiveError(ValueError):
    pass


def _archive_header(stream: Any, header: Dict[str, Any]) -> None:
    body = json.dumps(header, separators=(",", ":")).encode()
    stream.write(len(body).to_bytes(4, "big") + body)


def _copy_exact(source: Any, sink: Any, size: int) -> int:
    copied = 0
    while copied < size:
        block = source.read(min(_COPY, size - copied))
        if not block:
            break
        sink.write(block)
        copied += len(block)
    return copied


def write_archive(out: Any, snapshot: Snapshot) -> int:
    """Write what `snapshot` captured as cfp-dir/1 to the binary file `out`. Returns the raw bytes written."""
    import gzip

    total = 0
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6, mtime=0) as stream:
        stream.write(ARCHIVE_MAGIC)
        for directory in snapshot.dirs:
            _archive_header(stream, {"path": directory, "type": "dir", "size": 0, "mode": 0o700, "mtime": 0})
        for captured in snapshot.files:
            _archive_header(
                stream,
                {"path": captured.path, "type": "file", "size": captured.size, "mode": captured.mode, "mtime": captured.mtime},
            )
            with (snapshot.root / captured.path).open("rb") as source:
                if _copy_exact(source, stream, captured.size) != captured.size:
                    raise ArchiveError(f"{captured.path} changed size after capture")
            total += captured.size
        stream.write((0).to_bytes(4, "big"))
    return total


def _exact(stream: Any, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise ArchiveError("the archive ends early")
    return data


def read_archive(source: Any, target: Path, cap: int = HARD_CAP) -> List[str]:
    """
    Write a cfp-dir/1 stream out at `target`, which must not exist. Nothing
    lands there unless the whole stream reads cleanly, and no entry can write
    outside it. Returns the file paths.
    """
    import gzip
    import zlib

    if target.exists():
        raise FileExistsError(f"{target} exists; a restore never replaces a profile directory")
    staging = target.with_name(f".{target.name}.partial")
    staging.mkdir(mode=0o700)
    written: List[str] = []
    total = 0
    try:
        with gzip.GzipFile(fileobj=source, mode="rb") as stream:
            if _exact(stream, len(ARCHIVE_MAGIC)) != ARCHIVE_MAGIC:
                raise ArchiveError("not a cfp-dir/1 archive")
            while True:
                length = int.from_bytes(_exact(stream, 4), "big")
                if length == 0:
                    break
                if length > _ARCHIVE_HEADER_CAP:
                    raise ArchiveError("an entry header is too large")
                header = json.loads(_exact(stream, length))
                path = header.get("path") if isinstance(header, dict) else None
                if not isinstance(path, str) or not path:
                    raise ArchiveError("an entry has no path")
                _check_relpath(path)
                size, mode, mtime = header.get("size"), header.get("mode", 0o600), header.get("mtime", 0)
                if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (size, mode, mtime)):
                    raise ArchiveError(f"{path}: size, mode and mtime are non-negative integers")
                destination = staging / path
                if header.get("type") == "dir":
                    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if header.get("type") != "file":
                    raise ArchiveError(f"{path}: only files and directories are allowed")
                total += size
                if total > cap:
                    raise ArchiveError(f"the archive holds more than {cap} bytes")
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with destination.open("xb") as out:
                    if _copy_exact(stream, out, size) != size:
                        raise ArchiveError("the archive ends early")
                os.chmod(destination, (mode & 0o777) | 0o600)
                os.utime(destination, ns=(mtime * 10**9, mtime * 10**9))
                written.append(path)
            if stream.read(1):
                raise ArchiveError("bytes follow the end of the archive")
        staging.rename(target)
    except (EOFError, OSError, zlib.error, ValueError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(error, (ArchiveError, FileNotFoundError, PermissionError)):
            raise
        raise ArchiveError(f"the archive is malformed: {error}") from None
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return written
