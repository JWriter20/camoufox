"""
Camoufox Pro: installing a release, and launching the newest one.

`camoufox fetch pro` asks the Camoufox Pro API for a published release for this
machine, checks that it is the one the API signed, and installs it beside the
public builds.

A Pro release is numbered: `159.0-pro.12` is the twelfth. When a launch points
at a numbered Pro build and the Camoufox Pro API has published a release with a
higher number for the same target, the launcher downloads that release, checks
that it is the one the API signed, installs it in the camoufox cache and
launches it in place of the build it was pointed at. The build the launch
points at is never modified. See docs/pro.md.
"""

import base64
import hashlib
import mmap
import platform
import re
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

import requests
from platformdirs import user_cache_dir

from . import pro
from .exceptions import ProUnavailable, UnsupportedArchitecture, UpdateRefused
from .multiversion import InstalledVersion, list_installed, remove_version, set_active
from .pkgman import (
    ARCH_MAP,
    OS_MAP,
    AvailableVersion,
    CamoufoxFetcher,
    DownloadBuffer,
    RepoConfig,
    Version,
    launch_path,
    rprint,
    webdl,
)

RELEASE_NUMBER = re.compile(r"(?:^|[-+.])pro\.(\d+)$")
# A release's version as an install is named: `<Firefox version>-<build>`.
VERSION = re.compile(r"(\d+(?:\.\d+)*)-([0-9A-Za-z]+(?:[.+][0-9A-Za-z]+)*)")
SHA256 = re.compile(r"[0-9a-f]{64}")
# The folder of `browsers/` that Pro releases are installed in, and the name `camoufox list` shows.
REPO = "pro"
# What the API signs of a release, and the domain it signs under.
DOMAIN = b"cfp-artifact-v1\n"
SIGNED_FIELDS = ("version", "target", "filename", "sha256", "size", "build_hash")
# A Pro build carries the public keys it verifies its lease with. A release is signed with the same
# keys, so the installed build is what says which signatures to believe.
TRUSTED_KEYS = re.compile(rb"camoufox-pro-lease-trust-v1 keys=((?:[0-9a-f]+|-)(?:,(?:[0-9a-f]+|-))*) build=")
ED25519_SPKI = bytes.fromhex("302a300506032b6570032100")
LIBRARIES = ("libxul.so", "XUL", "xul.dll")
# The release that launches and the one before it stay installed.
KEPT = 2
ABANDONED_S = 24 * 3600
DOWNLOAD_TIMEOUT_S = 600


def release_number(version: str) -> Optional[int]:
    """The `<n>` of a version ending `pro.<n>`; None for a build that is not a numbered release."""
    match = RELEASE_NUMBER.search(version)
    return int(match.group(1)) if match else None


def store() -> Path:
    return Path(user_cache_dir("camoufox")) / "pro" / "browsers"


def partials() -> Path:
    """Where the download of a release is kept until it is whole."""
    return Path(user_cache_dir("camoufox")) / "pro" / "downloads"


def host_target() -> str:
    """The release target this machine runs: `linux-x86_64`, `macos-arm64`, ..."""
    machine = platform.machine().lower()
    if machine not in ARCH_MAP:
        raise UnsupportedArchitecture(f"Architecture {machine} is not supported")
    return f"{pro.HOST_OS}-{ARCH_MAP[machine]}"


def trusted_keys(executable: Path) -> Dict[int, bytes]:
    """The Ed25519 public keys the build beside `executable` trusts, by key id."""
    for name in LIBRARIES:
        library = executable.parent / name
        if not library.is_file():
            continue
        with open(library, "rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as image:
            found = TRUSTED_KEYS.search(image)
            named = found.group(1) if found else None
        if named:
            slots = [bytes.fromhex(slot.decode()) if slot != b"-" else b"" for slot in named.split(b",")]
            return {
                kid: slot[len(ED25519_SPKI) :] for kid, slot in enumerate(slots) if slot.startswith(ED25519_SPKI)
            }
    raise UpdateRefused(f"The Pro build at {executable.parent} names no signing key, so no release can be checked.")


def _nacl() -> Any:
    try:
        import nacl.exceptions
        import nacl.signing
    except ImportError as error:  # pragma: no cover - the message is the point
        raise ImportError('Installing a Camoufox Pro release needs the pro extra: pip install "camoufox[pro]"') from error
    return nacl


def _verify(artifact: Dict[str, Any], keys: Dict[int, bytes]) -> None:
    nacl = _nacl()
    signature = artifact.get("signature") or {}
    kid = signature.get("kid")
    key = keys.get(kid) if isinstance(kid, int) else None
    if key is None:
        raise UpdateRefused(f"The release is signed by key {kid!r}, which this build does not trust.")
    text = str(signature.get("ed25519", ""))
    try:
        message = DOMAIN + "\n".join(str(artifact[name]) for name in SIGNED_FIELDS).encode()
        nacl.signing.VerifyKey(key).verify(message, base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    except (nacl.exceptions.BadSignatureError, ValueError, KeyError):
        raise UpdateRefused("The release's signature does not verify.") from None


def _download(artifact: Dict[str, Any], archive: Path) -> None:
    url = str(artifact["url"])
    if not url.startswith("https://"):
        raise UpdateRefused("The release's download is not an https URL.")
    digest = hashlib.sha256()
    try:
        with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_S) as response, open(archive, "wb") as out:
            response.raise_for_status()
            for chunk in response.iter_content(1 << 20):
                digest.update(chunk)
                out.write(chunk)
    except requests.RequestException as error:
        raise ProUnavailable(f"The Camoufox Pro release could not be downloaded: {error}") from None
    if archive.stat().st_size != int(artifact["size"]) or digest.hexdigest() != artifact["sha256"]:
        raise UpdateRefused("The download is not the release the API signed.")


def _unpack(archive: Path, directory: Path) -> None:
    with zipfile.ZipFile(archive) as release:
        for entry in release.infolist():
            if not (directory / entry.filename).resolve().is_relative_to(directory.resolve()):
                raise UpdateRefused(f"The release archive writes outside its directory: {entry.filename}")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise UpdateRefused(f"The release archive holds a symbolic link: {entry.filename}")
        release.extractall(directory)  # nosec - every entry was checked above
        for entry in release.infolist():
            if (entry.external_attr >> 16) & 0o111:
                (directory / entry.filename).chmod((entry.external_attr >> 16) & 0o777)


def _install(artifact: Dict[str, Any], installed: Path) -> None:
    """Download, check and unpack the release into `installed`, which appears whole or not at all."""
    installed.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=".", dir=installed.parent))
    try:
        archive = partial.with_name(partial.name + ".zip")
        try:
            _download(artifact, archive)
            _unpack(archive, partial)
        finally:
            archive.unlink(missing_ok=True)
        try:
            partial.rename(installed)
        except OSError:
            # Another launch installed the same release first.
            if not installed.is_dir():
                raise
    finally:
        shutil.rmtree(partial, ignore_errors=True)


def _prune(browsers: Path) -> None:
    releases = sorted(
        (entry for entry in browsers.iterdir() if entry.is_dir() and release_number(entry.name) is not None),
        key=lambda entry: release_number(entry.name) or 0,
    )
    # An install that was killed half way leaves its directory behind.
    abandoned = [
        entry
        for entry in browsers.iterdir()
        if entry.name.startswith(".") and time.time() - entry.stat().st_mtime > ABANDONED_S
    ]
    for old in releases[:-KEPT] + abandoned:
        shutil.rmtree(old, ignore_errors=True)


def newest(build: pro.ProBuild, executable: str, key: Optional[str] = None) -> str:
    """
    The executable to launch for a launch pointed at `executable`, whose build is `build`: itself,
    or the same file of a newer release of its target, installed first if it is not yet.
    """
    mine = release_number(build.version)
    if mine is None:
        return executable
    key = pro.resolve_key(key)
    base = pro.api_base(build)
    latest = (pro.call("GET", f"/api/v1/artifacts?target={build.target}", key=key, base=base).get("latest") or {}).get(
        "version"
    )
    theirs = release_number(str(latest or ""))
    if theirs is None or theirs <= mine:
        return executable
    installed = store() / str(latest)
    if installed.parent != store():
        raise UpdateRefused(f"The API names a release that is not a version: {latest!r}")
    if not installed.is_dir():
        keys = trusted_keys(Path(executable))
        artifact = pro.call("GET", f"/api/v1/artifacts/{latest}/{build.target}", key=key, base=base)
        _verify(artifact, keys)
        if (artifact["version"], artifact["target"]) != (latest, build.target):
            raise UpdateRefused(
                f"Asked for {latest} for {build.target}; the signed answer is "
                f"{artifact['version']} for {artifact['target']}."
            )
        pro.log.info("Camoufox Pro %s is installed; downloading %s.", build.version, latest)
        _install(artifact, installed)
        _prune(store())
    newer = installed / Path(executable).name
    if not newer.is_file():
        raise UpdateRefused(f"Camoufox Pro {latest} has no {newer.name}.")
    return str(newer)


class ProFetcher(CamoufoxFetcher):
    """Installs the release the API resolved, as `camoufox fetch` installs a public build."""

    def __init__(self, artifact: Dict[str, Any], version: str, build: str) -> None:
        self.size = int(artifact["size"])
        super().__init__(
            repo_config=RepoConfig(repos=[REPO], name=REPO, pattern="", os_map=OS_MAP, arch_map=ARCH_MAP),
            selected_version=AvailableVersion(
                version=Version(build=build, version=version),
                url=artifact["url"],
                is_prerelease=False,
                asset_size=self.size,
                sha256=artifact["sha256"],
            ),
        )
        self.partial = partials() / f"{artifact['sha256']}.part"

    def download_file(self, file: DownloadBuffer, url: str) -> DownloadBuffer:  # type: ignore[override]
        # The URL is signed: it downloads the release for whoever holds it, so it is not printed.
        have = file.seek(0, 2)
        if have > self.size:
            file.truncate(0)
            have = 0
        if have < self.size:
            rprint(f"Downloading Camoufox Pro {self.verstr}" + (f", resuming at {have} bytes" if have else ""))
            webdl(url, buffer=file, offset=have)
        return file

    def install(self, replace: bool = False) -> None:
        self.partial.parent.mkdir(parents=True, exist_ok=True)
        for other in self.partial.parent.iterdir():
            if other != self.partial:
                other.unlink()
        try:
            super().install(replace)
        finally:
            # A download that stopped short is resumed by the next fetch; a whole one is done with.
            if self.partial.exists() and self.partial.stat().st_size >= self.size:
                self.partial.unlink()


def fetch(
    version: Optional[str] = None,
    build: Optional[pro.ProBuild] = None,
    executable: Optional[str] = None,
    echo: Callable[[str], None] = print,
) -> InstalledVersion:
    """
    Install the newest published release for this machine, or `version`, and make it the active build.

    `build` and `executable` are the Pro build already active on this machine, if one is: the
    release is checked against the keys it carries before anything is downloaded. Without one
    there is no key to check with until the release is unpacked, so it is checked against its own.
    """
    key = pro.resolve_key()
    # Needed to check the release, so its absence is found before the download and not after.
    _nacl()
    target = host_target()
    artifact = pro.call(
        "GET", f"/api/v1/artifacts/{quote(version or 'latest', safe='')}/{target}", key=key, base=pro.api_base(build)
    )
    keys = trusted_keys(Path(executable)) if build and executable else None
    if keys is not None:
        _verify(artifact, keys)
    named = VERSION.fullmatch(str(artifact.get("version")))
    if (
        named is None
        or artifact.get("target") != target
        or version not in (None, artifact["version"])
        or not SHA256.fullmatch(str(artifact.get("sha256")))
        or not str(artifact.get("url")).startswith("https://")
    ):
        raise UpdateRefused(
            f"Asked for {version or 'the newest release'} for {target}; the API answered with "
            f"{artifact.get('version')!r} for {artifact.get('target')!r}, which is not a release to install."
        )
    ProFetcher(artifact, *named.groups()).install()
    installed = next(iv for iv in list_installed() if (iv.repo_name, iv.sha256) == (REPO, artifact["sha256"]))
    if keys is None:
        try:
            _verify(artifact, trusted_keys(Path(launch_path(installed.path))))
        except UpdateRefused:
            remove_version(installed.path)
            raise
        echo(
            "No Camoufox Pro build is installed to check this release against, so its signature was "
            "checked with the key the release itself carries. Later releases are checked with that key "
            "before they are downloaded."
        )
    set_active(installed.relative_path)
    return installed
