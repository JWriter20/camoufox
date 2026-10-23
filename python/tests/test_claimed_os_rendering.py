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
CLAIMED_OS_CONFIG = ("fonts:metrics", "media:spoof_codecs", "media:hwCodecs")
CLAIMED_OS_PREFS = (
    "font.name-list.emoji",
    "gfx.text.subpixel-position.force-enabled",
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
        # H.264 only: fpgen's GPU buckets do not say which GPUs decode HEVC or VP9.
        assert config["media:hwCodecs"] == ["video/avc"]

    @pytest.mark.parametrize("gpu, codecs", [
        (("Google Inc. (NVIDIA)",
          "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or similar"), ["video/avc"]),
        # A software rasteriser decodes nothing in hardware, and the RFP mask's
        # decodingInfo() reports no video power-efficient either.
        (("Google Inc. (Microsoft)",
          "ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11 vs_5_0 ps_5_0), or similar"), []),
        (("Mozilla", "Mozilla"), []),
    ])
    def test_hardware_codecs_follow_the_claimed_gpu(self, launch, gpu, codecs):
        config, _ = launch(WIN_UA, os="windows", webgl_config=gpu)
        assert config["webGl:renderer"] == gpu[1]
        assert config["media:hwCodecs"] == codecs

    def test_macos_identity_keeps_stock_codecs(self, launch):
        # Nothing records what decodingInfo() reports on a Mac.
        config, _ = launch(MAC_UA)
        assert "media:spoof_codecs" not in config
        assert "media:hwCodecs" not in config

    def test_host_os_identity_is_left_alone(self, launch, monkeypatch):
        config, _ = launch(LIN_UA)
        assert "media:spoof_codecs" not in config
        monkeypatch.setattr(utils, "_host_os_key", lambda: "win")
        config, _ = launch(WIN_UA)
        assert "media:spoof_codecs" not in config
        assert "media:hwCodecs" not in config

    def test_caller_values_win(self, launch):
        config, _ = launch(WIN_UA, config={"media:spoof_codecs": False, "media:hwCodecs": []})
        assert config["media:spoof_codecs"] is False
        assert config["media:hwCodecs"] == []


class TestLookAndFeel:
    """Glyph positioning and system colours of the claimed OS."""

    def test_windows_identity_gets_windows_look_and_feel(self, launch):
        _, prefs = launch(WIN_UA)
        assert prefs["gfx.text.subpixel-position.force-enabled"] is True
        assert utils.WINDOWS_UI_COLORS["ui.highlight"] == "#0078d7"
        for pref, value in utils.WINDOWS_UI_COLORS.items():
            assert prefs[pref] == value

    def test_macos_identity_gets_neither(self, launch):
        # Both were measured against stock Windows only.
        _, prefs = launch(MAC_UA)
        assert "gfx.text.subpixel-position.force-enabled" not in prefs
        assert "ui.highlight" not in prefs

    def test_host_os_identity_is_left_alone(self, launch, monkeypatch):
        _, prefs = launch(LIN_UA)
        assert "gfx.text.subpixel-position.force-enabled" not in prefs
        assert "ui.highlight" not in prefs
        monkeypatch.setattr(utils, "_host_os_key", lambda: "win")
        _, prefs = launch(WIN_UA)
        assert "gfx.text.subpixel-position.force-enabled" not in prefs
        assert "ui.highlight" not in prefs

    def test_caller_values_win(self, launch):
        _, prefs = launch(WIN_UA, firefox_user_prefs={
            "ui.highlight": "#ff0000", "gfx.text.subpixel-position.force-enabled": False,
        })
        assert prefs["ui.highlight"] == "#ff0000"
        assert prefs["gfx.text.subpixel-position.force-enabled"] is False
