"""resolve_verstr() must read the version of a macOS app bundle it was pointed at.

On macOS the executable is Camoufox.app/Contents/MacOS/camoufox and Firefox
writes application.ini to Contents/Resources. Looking only beside the
executable made `Camoufox(executable_path=...)` raise CamoufoxNotInstalled on
every Mac without a fetched release (found smoke-testing 156.0.1-beta.32 on
macos-15 and macos-15-intel runners).
"""

import pytest

from camoufox import utils
from camoufox.exceptions import CamoufoxNotInstalled

INI = "[App]\nName=Camoufox\nVersion=156.0.1-beta.32\n"


def test_reads_application_ini_beside_the_executable(tmp_path):
    (tmp_path / "application.ini").write_text(INI)
    assert utils.resolve_verstr(tmp_path / "camoufox-bin") == "156.0.1-beta.32"


def test_reads_application_ini_from_a_macos_bundle(tmp_path):
    contents = tmp_path / "Camoufox.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    (contents / "Resources" / "application.ini").write_text(INI)
    exe = contents / "MacOS" / "camoufox"
    assert utils.resolve_verstr(exe) == "156.0.1-beta.32"


def test_falls_back_to_the_installed_release(tmp_path, monkeypatch):
    def not_installed():
        raise CamoufoxNotInstalled("none")

    monkeypatch.setattr(utils, "installed_verstr", not_installed)
    with pytest.raises(CamoufoxNotInstalled):
        utils.resolve_verstr(tmp_path / "camoufox-bin")
