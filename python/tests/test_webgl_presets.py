"""
Launch-level guards for the WebGL identity a fingerprint preset asks for.

Mirrors typescript/tests/launcher-invariants.test.ts
("launchOptions WebGL for a preset GPU").

Run with:
    cd python && python -m pytest tests/test_webgl_presets.py -v

Roughly 10% of the bundled presets name a GPU the WebGL catalogue has no
recorded parameters for, which used to abort the launch with a bare
ValueError. The substitution below is deliberate: leaving the preset's
vendor/renderer in place and merging another GPU's extensions and limits
around them would report a name that contradicts the capabilities, which is
itself detectable.
"""

from contextlib import contextmanager
from unittest import mock

import orjson
import pytest

from camoufox import utils
from camoufox._warnings import LeakWarning
from camoufox.webgl import sample_webgl

WINDOWS_NAV = {
    "userAgent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
        "Gecko/20100101 Firefox/133.0"
    ),
    "platform": "Win32",
    "hardwareConcurrency": 12,
}

# A vendor/renderer pair the bundled WebGL catalogue has parameters for.
KNOWN = {
    "unmaskedVendor": "Google Inc. (Intel)",
    "unmaskedRenderer": (
        "ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0 ps_5_0), or similar"
    ),
}
# A real preset GPU the catalogue has no parameters for.
UNKNOWN = {
    "unmaskedVendor": "Google Inc. (Intel)",
    "unmaskedRenderer": (
        "ANGLE (Intel, Intel(R) Arc(TM) A750 Graphics Direct3D11 vs_5_0 ps_5_0), "
        "or similar"
    ),
}


@contextmanager
def host():
    """Run launch_options() without a browser on disk."""
    with mock.patch.object(utils, "get_screen_cons", lambda headless: None), (
        mock.patch.object(utils, "installed_verstr", lambda: "152.0.4-beta.28")
    ), mock.patch.object(utils, "launch_path", lambda **kwargs: "/nonexistent/camoufox"):
        yield


def config_of(options):
    """Reassemble the chunked CAMOU_CONFIG_<n> env vars into a dict."""
    env = options["env"]
    chunks = sorted(
        (int(k.rsplit("_", 1)[1]), v)
        for k, v in env.items()
        if k.startswith("CAMOU_CONFIG_")
    )
    return orjson.loads("".join(chunk for _, chunk in chunks))


def launch(**kwargs):
    kwargs.setdefault("os", "windows")
    kwargs.setdefault("i_know_what_im_doing", True)
    kwargs.setdefault("headless", True)
    with host():
        return config_of(utils.launch_options(**kwargs))


def with_preset(webgl, **extra):
    return launch(
        fingerprint_preset={"navigator": WINDOWS_NAV, "webgl": webgl}, **extra
    )


class TestPresetWebGL:
    def test_keeps_the_presets_gpu_when_the_catalogue_has_parameters(self):
        config = with_preset(KNOWN)
        assert config["webGl:vendor"] == KNOWN["unmaskedVendor"]
        assert config["webGl:renderer"] == KNOWN["unmaskedRenderer"]
        assert config["webGl:parameters"]

    def test_substitutes_a_sampled_gpu_instead_of_throwing(self):
        config = with_preset(UNKNOWN)
        assert config["webGl:renderer"] != UNKNOWN["unmaskedRenderer"]
        assert config["webGl:vendor"]
        assert config["webGl:renderer"]

    def test_substituted_vendor_renderer_and_parameters_stay_coherent(self):
        config = with_preset(UNKNOWN)
        match = sample_webgl("win", config["webGl:vendor"], config["webGl:renderer"])
        assert config["webGl:supportedExtensions"] == match["webGl:supportedExtensions"]
        assert config["webGl:parameters"] == match["webGl:parameters"]

    def test_warns_that_the_presets_gpu_was_swapped_out(self):
        with pytest.warns(LeakWarning, match="WebGL catalogue has no"):
            with_preset(UNKNOWN, i_know_what_im_doing=False)

    def test_still_raises_for_a_caller_supplied_pair_the_catalogue_lacks(self):
        # The fallback is for presets we ship. If the caller names a specific
        # pair, silently handing them a different GPU would be worse than
        # failing.
        with pytest.raises(ValueError, match="No WebGL data found"):
            launch(
                webgl_config=(
                    UNKNOWN["unmaskedVendor"],
                    UNKNOWN["unmaskedRenderer"],
                )
            )
