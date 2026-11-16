"""A launch pointed at an older Camoufox Pro release launches the newest one.

The API is test_pro's local fake. A release is a zip signed with a key the
launched build carries, as a published one is; its download is the one thing
stubbed, because the launcher fetches a release over https only.
"""

import base64
import hashlib
import io
import json
import os
import zipfile

import pytest
from nacl.signing import SigningKey

from camoufox import pro, pro_update
from camoufox.exceptions import UpdateRefused
from test_pro import KEY, api, build, launch, pro_build  # noqa: F401 - fixtures

NEWER = "156.0.1-pro.2"
NEWER_HASH = "sha256:" + "cd" * 32
CATALOGUE = r"/api/v1/artifacts\?target=linux-x86_64"
RESOLVE = r"/api/v1/artifacts/[^?]+"


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def archive_of(files, links=()):
    """A release archive: `files` as {name: bytes}, each executable; `links` as names of symbolic links."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as release:
        for name, body in files.items():
            entry = zipfile.ZipInfo(name)
            entry.external_attr = 0o100755 << 16
            release.writestr(entry, body)
        for name in links:
            entry = zipfile.ZipInfo(name)
            entry.external_attr = 0o120777 << 16
            release.writestr(entry, "/etc/passwd")
    return buffer.getvalue()


def build_files(version=NEWER):
    declared = {"build_hash": NEWER_HASH, "version": version, "target": "linux-x86_64"}
    return {
        "camoufox-bin": b"#!/bin/sh\n",
        "pro-build.json": json.dumps(declared).encode(),
        "properties.json": json.dumps([{"property": "screen.width", "type": "uint"}]).encode(),
    }


class Channel:
    """The release channel of the fake API, and the signing key the build a launch points at trusts."""

    def __init__(self, api, executable, monkeypatch):
        self.api = api
        self.signer = SigningKey.generate()
        self.downloads = []
        self.served = {}
        spki = pro_update.ED25519_SPKI + self.signer.verify_key.encode()
        marker = f"camoufox-pro-lease-trust-v1 keys=-,{spki.hex()},- build={'ab' * 32}".encode()
        (executable.parent / "libxul.so").write_bytes(b"\x7fELF" + bytes(64) + marker + bytes(64))
        monkeypatch.setattr(pro_update.requests, "get", self.get)

    def get(self, url, **kwargs):
        self.downloads.append(url)
        body = self.served[url]

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                return [body[at : at + size] for at in range(0, len(body), size)]

        return Response()

    def publish(self, archive, version=NEWER, signer=None, kid=1, **changed):
        """Publish `archive` as `version`; `changed` replaces fields of the answer after it is signed."""
        url = f"https://releases.test/{version}.zip"
        artifact = {
            "version": version,
            "target": "linux-x86_64",
            "filename": "camoufox-pro.zip",
            "sha256": hashlib.sha256(archive).hexdigest(),
            "size": len(archive),
            "build_hash": NEWER_HASH,
        }
        message = pro_update.DOMAIN + "\n".join(str(artifact[name]) for name in pro_update.SIGNED_FIELDS).encode()
        signature = (signer or self.signer).sign(message).signature
        self.api.latest = version
        self.api.artifact = {**artifact, "url": url, "signature": {"kid": kid, "ed25519": b64(signature)}, **changed}
        self.served[self.api.artifact["url"]] = archive


@pytest.fixture
def channel(api, pro_build, monkeypatch):  # noqa: F811
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    return Channel(api, pro_build, monkeypatch)


def installed():
    return sorted(entry.name for entry in pro_update.store().iterdir()) if pro_update.store().exists() else []


def test_a_launch_of_an_older_release_launches_the_newest(api, pro_build, channel, monkeypatch):  # noqa: F811
    channel.publish(archive_of(build_files()))

    opts = launch(pro_build, monkeypatch)

    newest = pro_update.store() / NEWER / "camoufox-bin"
    assert opts["executable_path"] == str(newest)
    assert os.access(newest, os.X_OK)
    # The lease is for the build that launches, not the one the launch pointed at.
    (mint,) = api.calls("/api/v1/leases")
    assert mint["body"]["build_hash"] == NEWER_HASH
    assert {call["auth"] for call in api.calls(CATALOGUE) + api.calls(RESOLVE)} == {f"Bearer {KEY}"}
    assert json.loads((pro_build.parent / "pro-build.json").read_text())["version"] == "156.0.1-pro.1"

    # The next launch finds it installed: one question, nothing resolved or downloaded again.
    assert launch(pro_build, monkeypatch)["executable_path"] == str(newest)
    assert len(api.calls(CATALOGUE)) == 2
    assert len(api.calls(RESOLVE)) == len(channel.downloads) == 1
    assert installed() == [NEWER]


@pytest.mark.parametrize("latest", [None, "156.0.1-pro.1", "156.0.1-pro.0", "156.0.1-pro.next"])
def test_a_launch_of_the_newest_release_launches_it(api, pro_build, channel, monkeypatch, latest):  # noqa: F811
    api.latest = latest
    assert launch(pro_build, monkeypatch)["executable_path"] == str(pro_build)
    assert len(api.calls(CATALOGUE)) == 1 and api.calls(RESOLVE) == []
    assert installed() == []


def test_a_build_that_is_not_a_numbered_release_asks_nothing(api, pro_build, channel, monkeypatch):  # noqa: F811
    declared = {"build_hash": NEWER_HASH, "version": "156.0.1-pro.dev", "target": "linux-x86_64"}
    (pro_build.parent / "pro-build.json").write_text(json.dumps(declared))
    channel.publish(archive_of(build_files()))
    assert launch(pro_build, monkeypatch)["executable_path"] == str(pro_build)
    assert api.calls(CATALOGUE) == []


REFUSED = {
    "a key the build does not trust": lambda c: c.publish(archive_of(build_files()), signer=SigningKey.generate()),
    "a key id the build has no key for": lambda c: c.publish(archive_of(build_files()), kid=0),
    "a digest that was not signed": lambda c: c.publish(archive_of(build_files()), sha256="00" * 32),
    "another release than was asked for": lambda c: (
        c.publish(archive_of(build_files()), version="156.0.1-pro.3"),
        setattr(c.api, "latest", NEWER),
    ),
    "a download that is not https": lambda c: c.publish(archive_of(build_files()), url="http://releases.test/x.zip"),
    "bytes that are not the signed ones": lambda c: (
        c.publish(archive_of(build_files())),
        c.served.update({c.api.artifact["url"]: archive_of({"camoufox-bin": b"other"})}),
    ),
    "an archive with a symbolic link": lambda c: c.publish(archive_of(build_files(), links=["libxul.so"])),
    "an archive that writes outside itself": lambda c: c.publish(archive_of({"../../outside": b"x"})),
    "an archive without the executable": lambda c: c.publish(archive_of({"pro-build.json": b"{}"})),
}


@pytest.mark.parametrize("case", REFUSED)
def test_a_release_that_does_not_check_out_is_refused(api, pro_build, channel, monkeypatch, case):  # noqa: F811
    REFUSED[case](channel)
    with pytest.raises(UpdateRefused):
        launch(pro_build, monkeypatch)
    assert api.calls("/api/v1/leases") == []
    if case != "an archive without the executable":
        assert installed() == []
    assert not (pro_update.store().parent.parent / "outside").exists()


def test_a_release_this_launcher_is_too_old_to_drive_is_left_for_a_newer_launcher(
    api, pro_build, channel, monkeypatch, caplog  # noqa: F811
):
    monkeypatch.setattr(pro_update, "package_version", lambda name: "0.5.7")
    channel.publish(archive_of(build_files()), min_driver_version="0.5.8")

    with caplog.at_level("WARNING", logger=pro.log.name):
        assert launch(pro_build, monkeypatch)["executable_path"] == str(pro_build)

    assert "pip install -U camoufox" in caplog.text
    assert channel.downloads == [] and installed() == []


def test_a_build_that_names_no_signing_key_installs_nothing(api, pro_build, channel, monkeypatch):  # noqa: F811
    channel.publish(archive_of(build_files()))
    (pro_build.parent / "libxul.so").write_bytes(b"camoufox-pro-lease-trust-v1 keys=-,-,- build=-")
    with pytest.raises(UpdateRefused):
        launch(pro_build, monkeypatch)
    (pro_build.parent / "libxul.so").unlink()
    with pytest.raises(UpdateRefused, match="names no signing key"):
        launch(pro_build, monkeypatch)
    assert channel.downloads == []


def test_the_release_before_the_newest_is_kept_and_older_ones_go(api, pro_build, channel, monkeypatch):  # noqa: F811
    for number in (2, 3, 4):
        version = f"156.0.1-pro.{number}"
        channel.publish(archive_of(build_files(version)), version=version)
        launch(pro_build, monkeypatch)
    assert installed() == ["156.0.1-pro.3", "156.0.1-pro.4"]
