"""`camoufox fetch pro` installs a Camoufox Pro release beside the public builds.

The API is test_pro's local fake, answering the download route in the server's
shape, and its public key is in a keys file of the shape the launcher ships.
A release's download is the one thing stubbed, because a release is fetched
over https only.
"""

import hashlib
import json

import pytest
import requests
from click.testing import CliRunner
from nacl.signing import SigningKey

from camoufox import __main__ as cli
from camoufox import browser_pin, multiversion, pkgman, pro, pro_update
from test_pro import KEY, api  # noqa: F401 - fixture
from test_pro_update import archive_of, b64

TARGET = "linux-x86_64"
LATEST = f"/api/v1/artifacts/latest/{TARGET}"
FIRST, SECOND = "159.0-pro.1", "159.0-pro.2"


class Gate:
    """The download gate of the fake API, and the storage its signed URLs point at."""

    def __init__(self, api, monkeypatch, keys):
        self.api = api
        self.signer = SigningKey.generate()
        self.keys = keys
        self.trust(self.signer)
        monkeypatch.setenv(pro_update.KEYS_ENV, str(keys))
        self.served = {}
        self.ranges = []
        # The download drops after this many bytes of a response, once.
        self.drop_after = None
        monkeypatch.setattr(pkgman.requests, "get", self.get)

    def trust(self, *signers):
        """Write the keys file as the API lists its signing keys: `signers` in slots 1, 2, ..."""
        listed = [
            {
                "kid": kid,
                "generation": 1,
                "algorithm": "ed25519",
                "public_key_b64url": b64(pro_update.ED25519_SPKI + signer.verify_key.encode()),
                "state": "active" if kid == 1 else "standby",
            }
            for kid, signer in enumerate(signers, 1)
        ]
        self.keys.write_text(json.dumps({"keys": listed}))

    def release(self, version):
        """The archive of a release. The key its build carries is not one that signs releases here."""
        spki = pro_update.ED25519_SPKI + SigningKey.generate().verify_key.encode()
        marker = f"camoufox-pro-lease-trust-v1 keys=-,{spki.hex()},- build={'ab' * 32}".encode()
        declared = {"build_hash": "sha256:" + "cd" * 32, "version": version, "target": TARGET}
        return archive_of(
            {
                "camoufox-bin": b"#!/bin/sh\n",
                "libxul.so": b"\x7fELF" + bytes(64) + marker + version.encode() * 4096,
                "pro-build.json": json.dumps(declared).encode(),
            }
        )

    def publish(self, release, archive=None, signer=None, kid=1, signed=None, **changed):
        """Publish `release`; `signed` replaces fields of the answer before it is signed, `changed` after."""
        archive = archive or self.release(release)
        artifact = {
            "version": release,
            "target": TARGET,
            "filename": "camoufox-pro.zip",
            "sha256": hashlib.sha256(archive).hexdigest(),
            "size": len(archive),
            "build_hash": "sha256:" + "cd" * 32,
            **(signed or {}),
        }
        message = pro_update.DOMAIN + "\n".join(str(artifact[name]) for name in pro_update.SIGNED_FIELDS).encode()
        signature = (signer or self.signer).sign(message).signature
        url = f"https://releases.test/{release}.zip?signature=secret"
        self.api.artifact = {**artifact, "url": url, "signature": {"kid": kid, "ed25519": b64(signature)}, **changed}
        self.served[self.api.artifact["url"]] = archive
        return archive

    def get(self, url, headers=None, **_kwargs):
        body = self.served[url]
        asked = (headers or {}).get("Range")
        self.ranges.append(asked)
        start = int(asked.removeprefix("bytes=").rstrip("-")) if asked else 0
        rest, drop = body[start:], self.drop_after
        self.drop_after = None

        class Response:
            status_code = 206 if asked else 200
            headers = {"content-length": str(len(rest))}

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                for at in range(0, len(rest), size):
                    if drop is not None and at >= drop:
                        raise requests.ConnectionError("connection reset")
                    yield rest[at : at + size]

        return Response()


@pytest.fixture
def gate(api, tmp_path, monkeypatch):  # noqa: F811
    """A machine with an empty camoufox cache, this checkout's unpinned launcher, and no Pro key."""
    cache = tmp_path / "XDG_CACHE_HOME" / "camoufox"
    cache.mkdir()
    monkeypatch.setattr(pkgman, "INSTALL_DIR", cache)
    monkeypatch.setattr(pkgman, "OS_NAME", "lin")
    for module in (multiversion, cli):
        monkeypatch.setattr(module, "INSTALL_DIR", cache)
        monkeypatch.setattr(module, "BROWSERS_DIR", cache / "browsers")
        monkeypatch.setattr(module, "CONFIG_FILE", cache / "config.json")
        monkeypatch.setattr(module, "REPO_CACHE_FILE", cache / "repo_cache.json")
    monkeypatch.setattr(multiversion, "OS_NAME", "lin")
    monkeypatch.setattr(multiversion, "COMPAT_FLAG", cache / ".0.5_FLAG")
    monkeypatch.setattr(browser_pin, "PIN_FILE", tmp_path / "browser-pin.json")
    for name in ("remove_incompatible_data", "ensure_fpgen_model", "download_mmdb"):
        monkeypatch.setattr(cli, name, lambda *a, **k: None)
    monkeypatch.setattr(pkgman, "ensure_browser_profile_dir", lambda *a, **k: None)
    monkeypatch.setattr(pro, "HOST_OS", "linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.delenv("CAMOUFOX_EXECUTABLE_PATH", raising=False)
    return Gate(api, monkeypatch, tmp_path / "signing-keys.json")


def fetch(*args):
    return CliRunner().invoke(cli.cli, ["fetch", "pro", *args])


def installed():
    return {iv.version.full_string: iv for iv in multiversion.list_installed()}


def partials():
    directory = pro_update.partials()
    return sorted(directory.iterdir()) if directory.exists() else []


def test_without_a_key_it_says_to_sign_in_and_asks_nothing(api, gate):  # noqa: F811
    result = fetch()

    assert result.exit_code == 1
    assert "camoufox login" in result.output
    assert api.requests == []


@pytest.mark.parametrize(
    "status, code, message",
    [
        (402, "subscription_required", "This account has no active Camoufox Pro subscription."),
        (403, "account_suspended", "This account is suspended."),
    ],
)
def test_an_account_that_may_not_download_is_told_where_to_fix_it(api, gate, monkeypatch, status, code, message):  # noqa: F811
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    api.script(LATEST, (status, {"error": code, "message": message, "resolution_url": "https://camoufox.test/billing"}))

    result = fetch()

    assert result.exit_code == 1
    assert message in result.output and "https://camoufox.test/billing" in result.output
    assert installed() == {}


def test_the_newest_release_is_installed_and_made_active_like_any_build(api, gate, monkeypatch):  # noqa: F811
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    gate.publish(FIRST)

    result = fetch()

    assert result.exit_code == 0, result.output
    (asked,) = api.calls(LATEST)
    assert asked["auth"] == f"Bearer {KEY}"
    # A signed URL is a credential for the release; it is not printed.
    assert "secret" not in result.output
    build = installed()[FIRST]
    assert build.repo_name == "pro" and build.is_active
    assert build.path.parent == multiversion.BROWSERS_DIR / "pro"
    executable = cli._active_executable()
    assert executable == str(build.path / "camoufox-bin")
    assert pro.read_build(str(pkgman.build_file(executable, "pro-build.json"))).version == FIRST
    assert partials() == []

    assert f"v{FIRST}" in CliRunner().invoke(cli.cli, ["list"]).output
    assert CliRunner().invoke(cli.cli, ["active"]).output.startswith(f"pro/stable/{FIRST}")
    pinned = CliRunner().invoke(cli.cli, ["set", f"pro/stable/{FIRST}"])
    assert f"Pinned: pro/stable/{FIRST} (installed)" in pinned.output
    assert CliRunner().invoke(cli.cli, ["set", f"pro/stable/{SECOND}"]).exit_code == 1
    assert CliRunner().invoke(cli.cli, ["remove", f"pro/{FIRST}", "-y"]).exit_code == 0
    assert installed() == {}


REFUSED_UNSEEN = {
    "a key the launcher does not trust": lambda gate: gate.publish(FIRST, signer=SigningKey.generate()),
    "a key id the launcher has no key for": lambda gate: gate.publish(FIRST, kid=0),
    "no signature": lambda gate: gate.publish(FIRST, signature=None),
    "a digest that was not signed": lambda gate: gate.publish(FIRST, sha256="00" * 32),
    "another key in the signer's slot": lambda gate: (gate.publish(FIRST), gate.trust(SigningKey.generate())),
}


@pytest.mark.parametrize("case", REFUSED_UNSEEN)
def test_a_release_the_launchers_keys_did_not_sign_is_refused_before_any_download(gate, monkeypatch, case):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    REFUSED_UNSEEN[case](gate)

    result = fetch()

    assert result.exit_code == 1
    assert "signature does not verify" in result.output or "not a key trusted here" in result.output
    assert gate.ranges == [] and installed() == {} and partials() == []


def test_a_standby_key_the_launcher_ships_signs_after_a_rotation(gate, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    standby = SigningKey.generate()
    gate.trust(gate.signer, standby)
    gate.publish(FIRST, signer=standby, kid=2)

    assert fetch().exit_code == 0
    assert installed()[FIRST].is_active


@pytest.fixture
def default_api(api, monkeypatch):  # noqa: F811
    """The fake API is the one the launcher asks by default, as api.camoufox.com is."""
    monkeypatch.setattr(pro, "DEFAULT_API", pro.api_base())
    monkeypatch.delenv(pro.API_ENV)


def test_the_default_api_is_checked_with_the_keys_the_launcher_ships(gate, default_api, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    monkeypatch.setattr(pro_update, "RELEASE_KEYS", gate.keys)
    monkeypatch.delenv(pro_update.KEYS_ENV)
    gate.publish(FIRST)

    assert fetch().exit_code == 0
    assert installed()[FIRST].is_active


def test_the_default_api_takes_no_other_keys_than_the_shipped_ones(api, gate, default_api, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    shipped = tmp_path / "shipped.json"
    shipped.write_text('{"keys": []}')
    monkeypatch.setattr(pro_update, "RELEASE_KEYS", shipped)
    gate.publish(FIRST)

    # CAMOUFOX_PRO_KEYS names the key that signed the release, and is not read for this API.
    result = fetch()

    assert result.exit_code == 1
    assert "ships no Camoufox Pro release key" in " ".join(result.output.split())
    assert api.requests == [] and installed() == {}


def test_another_api_is_asked_nothing_without_its_keys(api, gate, monkeypatch):  # noqa: F811
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    monkeypatch.delenv(pro_update.KEYS_ENV)
    gate.publish(FIRST)

    result = fetch()

    assert result.exit_code == 1
    assert pro_update.KEYS_ENV in result.output
    assert api.requests == [] and installed() == {}


def test_the_shipped_keys_file_is_the_signing_keys_listing():
    listing = json.loads(pro_update.RELEASE_KEYS.read_text())
    assert list(listing) == ["keys"]
    for entry in listing["keys"]:
        assert set(entry) == {"kid", "generation", "algorithm", "public_key_b64url", "state"}
        assert entry["kid"] in (0, 1, 2) and entry["algorithm"] == "ed25519"
    if listing["keys"]:
        assert sorted(pro_update.release_keys(pro.DEFAULT_API)) == sorted(entry["kid"] for entry in listing["keys"])


def test_a_release_this_launcher_is_too_old_to_drive_is_not_downloaded(gate, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    monkeypatch.setattr(pro_update, "package_version", lambda name: "0.5.7")
    gate.publish(FIRST, min_driver_version="0.6")

    refused = fetch()

    assert refused.exit_code == 1
    assert "needs camoufox 0.6 or newer, and this is 0.5.7" in " ".join(refused.output.split())
    assert "pip install -U camoufox" in refused.output
    assert gate.ranges == [] and installed() == {}

    gate.publish(FIRST, min_driver_version="0.5.7")
    assert fetch().exit_code == 0


@pytest.mark.parametrize(
    "answer",
    [
        {"signed": {"version": "../../elsewhere"}},
        {"signed": {"target": "windows-x86_64"}},
        {"url": "http://releases.test/159.0-pro.1.zip"},
    ],
    ids=["version", "target", "url"],
)
def test_a_signed_answer_that_is_not_the_release_asked_for_is_refused(gate, monkeypatch, answer):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    gate.publish(FIRST, **answer)

    result = fetch()

    assert result.exit_code == 1
    assert "not a release to install" in " ".join(result.output.split())
    assert gate.ranges == [] and installed() == {}


def test_a_download_that_is_not_the_release_is_not_installed(gate, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    gate.publish(FIRST)
    url = gate.api.artifact["url"]
    gate.served[url] = gate.served[url][:-1] + b"!"

    result = fetch()

    assert result.exit_code == 1
    assert "Checksum mismatch" in result.output
    assert installed() == {} and partials() == []


def test_an_interrupted_download_is_resumed(gate, monkeypatch):
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    archive = gate.publish(FIRST)
    gate.drop_after = 16384

    interrupted = fetch()

    assert interrupted.exit_code == 1
    (partial,) = partials()
    assert partial.stat().st_size == 16384 and installed() == {}

    resumed = fetch()

    assert resumed.exit_code == 0, resumed.output
    assert gate.ranges == [None, "bytes=16384-"]
    assert installed()[FIRST].sha256 == hashlib.sha256(archive).hexdigest()
    assert partials() == []


def test_a_named_release_is_installed_and_stays_the_choice(api, gate, monkeypatch):  # noqa: F811
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    gate.publish(FIRST)

    assert fetch(FIRST).exit_code == 0
    assert len(api.calls(f"/api/v1/artifacts/{FIRST}/{TARGET}")) == 1
    assert installed()[FIRST].is_active

    # A release the API answers with in place of the one named is not installed.
    gate.publish(SECOND)
    assert fetch(FIRST).exit_code == 1
    assert list(installed()) == [FIRST]

    # `camoufox fetch` installs the chosen build, which is now that Pro release.
    gate.publish(FIRST)
    again = CliRunner().invoke(cli.cli, ["fetch"])
    assert again.exit_code == 0, again.output
    assert len(api.calls(f"/api/v1/artifacts/{FIRST}/{TARGET}")) == 3


def test_fetch_follows_the_pro_channel_once_it_is_chosen(api, gate, monkeypatch):  # noqa: F811
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    gate.publish(FIRST)
    assert fetch().exit_code == 0
    gate.publish(SECOND)

    result = CliRunner().invoke(cli.cli, ["fetch"])

    assert result.exit_code == 0, result.output
    assert installed()[SECOND].is_active


def test_a_release_numbered_after_a_plus_is_installed_by_name(api, gate, monkeypatch):  # noqa: F811
    monkeypatch.setenv(pro.KEY_ENV, KEY)
    numbered = "152.0.4-beta.31+pro.3"
    gate.publish(numbered)

    assert fetch(numbered).exit_code == 0
    assert [call["path"] for call in api.requests] == [f"/api/v1/artifacts/152.0.4-beta.31%2Bpro.3/{TARGET}"]
    assert installed()[numbered].is_active
