"""Rendering answers that come from the host must follow the claimed OS instead.

A Linux build lays text out with its own font prefs, fallback table and
FreeType metrics. When the identity claims another OS, the launcher sets that
OS's values; when it claims the host's own, nothing changes. Expected values
were measured on stock Firefox 152.0.4 on Windows 10 against this build on a
Linux host claiming Windows (2026-09-24).
"""

import pytest

from camoufox import utils

from test_launch_environment import isolated_launch_dependencies  # noqa: F401

WIN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"
MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:156.0) Gecko/20100101 Firefox/156.0"
LIN_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:156.0) Gecko/20100101 Firefox/156.0"


@pytest.fixture
def launch(monkeypatch, isolated_launch_dependencies):  # noqa: F811
    """launch_options on a Linux host; returns (config, prefs)."""
    monkeypatch.setattr(utils, "_host_os_key", lambda: "lin")
    seen = {}

    def capture(config, *args, **kwargs):
        seen["config"] = dict(config)
        return {}

    monkeypatch.setattr(utils, "get_env_vars", capture)

    def run(ua, **kwargs):
        config = {"navigator.userAgent": ua, **kwargs.pop("config", {})}
        prefs = utils.launch_options(config=config, i_know_what_im_doing=True, **kwargs)["firefox_user_prefs"]
        return seen["config"], prefs

    return run


# Everything this module sets for another OS's identity, config keys and prefs.
CLAIMED_OS_CONFIG = ("fonts:metrics", "media:spoof_codecs")
CLAIMED_OS_PREFS = (
    "font.name-list.emoji",
    *utils.WINDOWS_UI_COLORS,
)


class TestLinuxHostOnly:
    """The C++ behind these exists for a Linux build only (gfxPlatformGtk,
    FreeType), so another host sets none of it, whatever the identity claims."""

    @pytest.mark.parametrize(
        "host, ua",
        [("mac", WIN_UA), ("mac", LIN_UA), ("win", MAC_UA), ("win", LIN_UA)],
    )
    def test_other_hosts_set_nothing(self, launch, monkeypatch, host, ua):
        monkeypatch.setattr(utils, "_host_os_key", lambda: host)
        config, prefs = launch(ua)
        assert [k for k in CLAIMED_OS_CONFIG if k in config] == []
        assert [k for k in CLAIMED_OS_PREFS if k in prefs] == []

    def test_linux_host_sets_it(self, launch):
        config, prefs = launch(WIN_UA)
        assert all(k in config for k in CLAIMED_OS_CONFIG)
        assert all(k in prefs for k in CLAIMED_OS_PREFS)


class TestFontPrefs:
    """font.* defaults of the claimed OS, from font-prefs.json."""

    def test_windows_identity_gets_windows_font_prefs(self, launch):
        _, prefs = launch(WIN_UA)
        assert prefs["font.name-list.emoji"].startswith("Segoe UI Emoji")
        assert prefs["font.name-list.serif.th"] == "Tahoma"
        # A name-list only Linux sets is emptied rather than left to Linux.
        assert prefs["font.name-list.cursive.ja"] == ""

    def test_macos_identity_gets_macos_font_prefs(self, launch):
        _, prefs = launch(MAC_UA)
        assert prefs["font.name-list.serif.th"] == "Thonburi"

    def test_async_fallback_off_on_a_linux_host(self, launch):
        for ua in (WIN_UA, MAC_UA, LIN_UA):
            _, prefs = launch(ua)
            assert prefs["gfx.font_rendering.fallback.async"] is False

    def test_linux_identity_on_linux_host_is_left_alone(self, launch):
        _, prefs = launch(LIN_UA)
        assert "font.name-list.emoji" not in prefs

    def test_windows_identity_on_windows_host_is_left_alone(self, launch, monkeypatch):
        monkeypatch.setattr(utils, "_host_os_key", lambda: "win")
        _, prefs = launch(WIN_UA)
        assert "font.name-list.emoji" not in prefs
        assert "gfx.font_rendering.fallback.async" not in prefs

    def test_caller_pref_wins(self, launch):
        _, prefs = launch(WIN_UA, firefox_user_prefs={"font.name-list.serif.th": "Garuda"})
        assert prefs["font.name-list.serif.th"] == "Garuda"

    def test_table_matches_stock_firefox(self):
        # Spot checks against all.js, so a regeneration cannot quietly agree
        # with itself.
        prefs = utils._stock_font_prefs("win", "lin")
        assert prefs["font.name-list.serif.x-western"] == "Times New Roman"
        assert prefs["font.name-list.sans-serif.ko"] == "Malgun Gothic, Gulim, Noto Sans CJK KR, Noto Sans KR"


class TestFontMetrics:
    """fonts:metrics "windows": DirectWrite's line boxes on a FreeType host."""

    def test_windows_identity_gets_windows_metrics(self, launch):
        config, _ = launch(WIN_UA)
        assert config["fonts:metrics"] == "windows"

    @pytest.mark.parametrize("ua", [MAC_UA, LIN_UA])
    def test_other_identities_keep_freetype_metrics(self, launch, ua):
        config, _ = launch(ua)
        assert "fonts:metrics" not in config

    def test_windows_host_is_left_alone(self, launch, monkeypatch):
        monkeypatch.setattr(utils, "_host_os_key", lambda: "win")
        config, _ = launch(WIN_UA)
        assert "fonts:metrics" not in config

    def test_caller_value_wins(self, launch):
        config, _ = launch(WIN_UA, config={"fonts:metrics": "freetype"})
        assert config["fonts:metrics"] == "freetype"


class TestCodecs:
    """The codec matrix and decodingInfo() follow the claimed OS (#558)."""

    def test_windows_identity_claims_windows_codecs(self, launch):
        config, _ = launch(WIN_UA)
        assert config["media:spoof_codecs"] is True

    def test_macos_identity_keeps_stock_codecs(self, launch):
        # Nothing records what decodingInfo() reports on a Mac.
        config, _ = launch(MAC_UA)
        assert "media:spoof_codecs" not in config

    def test_host_os_identity_is_left_alone(self, launch, monkeypatch):
        config, _ = launch(LIN_UA)
        assert "media:spoof_codecs" not in config
        monkeypatch.setattr(utils, "_host_os_key", lambda: "win")
        config, _ = launch(WIN_UA)
        assert "media:spoof_codecs" not in config

    def test_caller_values_win(self, launch):
        config, _ = launch(WIN_UA, config={"media:spoof_codecs": False})
        assert config["media:spoof_codecs"] is False


class TestLookAndFeel:
    """System colours of the claimed OS."""

    def test_windows_identity_gets_windows_look_and_feel(self, launch):
        _, prefs = launch(WIN_UA)
        assert utils.WINDOWS_UI_COLORS["ui.highlight"] == "#0078d4"
        # Windows 11's non-native menu hover, NS_RGBA(0, 0, 0, 30).
        assert utils.WINDOWS_UI_COLORS["ui.-moz_menuhover"] == "#0000001e"
        for pref, value in utils.WINDOWS_UI_COLORS.items():
            assert prefs[pref] == value

    def test_macos_identity_gets_none(self, launch):
        # They were measured against stock Windows only.
        _, prefs = launch(MAC_UA)
        assert "ui.highlight" not in prefs

    def test_host_os_identity_is_left_alone(self, launch, monkeypatch):
        _, prefs = launch(LIN_UA)
        assert "ui.highlight" not in prefs
        monkeypatch.setattr(utils, "_host_os_key", lambda: "win")
        _, prefs = launch(WIN_UA)
        assert "ui.highlight" not in prefs

    def test_caller_values_win(self, launch):
        _, prefs = launch(WIN_UA, firefox_user_prefs={"ui.highlight": "#ff0000"})
        assert prefs["ui.highlight"] == "#ff0000"


class TestLegacyFontFamilies:
    def test_canonical_families_expose_their_legacy_aliases(self, launch):
        config, _ = launch(WIN_UA, fonts=["Arial", "Calibri", "Franklin Gothic"])
        assert config["fonts"] == [
            "Arial", "Calibri", "Franklin Gothic", "Arial Black", "Calibri Light", "Franklin Gothic Medium",
        ]

    def test_segoe_ui_variable_exposes_its_bare_family(self, launch):
        # Windows 11 25H2 answers the bare `Segoe UI Variable` name; the font
        # enumerator lists only its three optical-size families.
        config, _ = launch(WIN_UA, fonts=["Segoe UI Variable Text"])
        assert config["fonts"] == ["Segoe UI Variable Text", "Segoe UI Variable"]

    def test_faces_named_by_their_own_family_are_exposed(self, launch):
        # A list in DirectWrite's namespace (a captured Windows enumeration) names Rockwell and
        # Sitka Text; once Windows' font-info loader has landed, stock also answers the nameID 1/16
        # names of their faces. Stock 159 on win-i9 found Rockwell Extra Bold and Sitka in every
        # frame, and a list without those names blocked them.
        config, _ = launch(WIN_UA, fonts=["Rockwell", "Sitka Text", "Sitka Small"])
        assert config["fonts"] == [
            "Rockwell", "Sitka Text", "Sitka Small", "Rockwell Condensed", "Rockwell Extra Bold", "Sitka",
        ]

    def test_light_faces_are_exposed_with_their_family(self, launch):
        # The Light and Semilight names are families of their own faces (fonts.conf no longer
        # rewrites them to the regular family), so the list has to hold them.
        config, _ = launch(WIN_UA, fonts=["Candara", "Corbel", "Leelawadee UI", "Nirmala UI", "Nirmala Text"])
        assert config["fonts"][5:] == [
            "Candara Light", "Corbel Light", "Leelawadee UI Semilight", "Nirmala Text Semilight",
            "Nirmala UI Semilight",
        ]

    def test_office_faces_of_os_families_need_an_office_family(self, launch):
        # Arial Narrow and Franklin Gothic Demi/Heavy are faces Office installs into families
        # Windows ships (win-i9: ARIALN.TTF is in Office's own font folder). A list shows that
        # Office is installed only by holding an Office family.
        config, _ = launch(WIN_UA, fonts=["Arial", "Franklin Gothic", "Agency FB"])
        assert config["fonts"] == [
            "Arial", "Franklin Gothic", "Agency FB", "Arial Black", "Franklin Gothic Medium",
            "Arial Narrow", "Franklin Gothic Demi", "Franklin Gothic Demi Cond", "Franklin Gothic Heavy",
            "Franklin Gothic Medium Cond",
        ]
        config, _ = launch(WIN_UA, fonts=["Arial", "Franklin Gothic"])
        assert config["fonts"] == ["Arial", "Franklin Gothic", "Arial Black", "Franklin Gothic Medium"]

    def test_a_drawn_list_is_expanded_too(self, launch, monkeypatch):
        # The launcher's own Windows 11 list names the six Sitka sizes and not the bare family,
        # which has to come with them as it does for a list the caller passes.
        monkeypatch.setattr(utils, "_generate_random_font_subset", lambda *args, **kwargs: ["Arial", "Sitka Text"])
        config, _ = launch(WIN_UA)
        assert config["fonts"] == ["Arial", "Sitka Text", "Arial Black", "Sitka"]

    def test_the_launcher_base_lists_do_not_hide_the_bare_sitka(self):
        # The data the draw reads: every Windows base with a Sitka size maps to the bare name.
        table = utils.LAUNCHER_CONSTANTS["windowsLegacyFontFamilies"]
        for size in ("Banner", "Display", "Heading", "Small", "Subheading", "Text"):
            assert table[f"Sitka {size}"] == ["Sitka"]

    def test_no_alias_without_its_canonical_family(self, launch):
        config, _ = launch(WIN_UA, fonts=["Times New Roman"])
        assert "Arial Black" not in config["fonts"]
        assert "Calibri Light" not in config["fonts"]
        assert "Franklin Gothic Medium" not in config["fonts"]
