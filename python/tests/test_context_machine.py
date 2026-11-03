"""One browser is one machine: a NewContext() identity takes the launch
identity's machine-level values and draws only a user's own (LEAKS 619).

A context drew its own GPU, screen, fonts and voices, while the WebGL limits,
window geometry, glyph rendering and network adapter -- which have no
per-context setter -- stayed the launch's: a page read a GPU name with
another GPU's limits.
"""

import pytest

from camoufox import fingerprints
from camoufox.fingerprints import CONTEXT_USER_KEYS, generate_context_fingerprint, machine_config

LAUNCH = {
    "navigator.userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0",
    "navigator.platform": "Win32",
    "navigator.oscpu": "Windows NT 10.0; Win64; x64",
    "navigator.appVersion": "5.0 (Windows)",
    "navigator.hardwareConcurrency": 12,
    "navigator.language": "en-US",
    "screen.width": 2560, "screen.height": 1440, "screen.availWidth": 2560, "screen.availHeight": 1392,
    "screen.colorDepth": 24,
    "window.outerWidth": 1600, "window.outerHeight": 1000, "window.screenX": 66, "window.screenY": 32,
    "webGl:vendor": "Google Inc. (NVIDIA)",
    "webGl:renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "webGl:parameters": {"3379": 16384, "33901": [1, 1024]},
    "webrtc:hostipv6": True,
    "fonts": ["Arial", "Segoe UI"],
    "voices": [{"name": "Microsoft David - English (United States)"}],
    "timezone": "America/New_York",
    "locale:language": "en", "locale:region": "US",
    "webrtc:ipv4": "203.0.113.7",
    "geolocation:latitude": 40.7,
}


def test_machine_values_come_from_the_launch():
    fp = generate_context_fingerprint(machine=LAUNCH)
    config, script = fp["config"], fp["init_script"]
    for key in ("navigator.userAgent", "navigator.platform", "screen.width", "window.screenX",
                "webGl:vendor", "webGl:renderer", "webGl:parameters", "webrtc:hostipv6", "fonts", "voices"):
        assert config[key] == LAUNCH[key], key
    assert '"ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0, D3D11)"' in script
    # The machine's screen is the launch's own (LEAKS 635): no per-context setter.
    assert "setScreenDimensions" not in script and "setScreenColorDepth" not in script
    assert fp["context_options"]["user_agent"] == LAUNCH["navigator.userAgent"]


def test_a_context_draws_only_a_users_own_values():
    fp = generate_context_fingerprint(machine=LAUNCH, timezone="Europe/Paris", locale="fr-FR",
                                      webrtc_ip="198.51.100.4")
    config = fp["config"]
    assert config["timezone"] == "Europe/Paris" and config["navigator.language"] == "fr-FR"
    assert fp["context_options"]["timezone_id"] == "Europe/Paris"
    assert 'w.setWebRTCIPv4("198.51.100.4")' in fp["init_script"]
    assert not any(k.startswith(CONTEXT_USER_KEYS) for k in machine_config(LAUNCH))


def test_another_machine_is_refused():
    with pytest.raises(ValueError, match="claims windows, not linux"):
        generate_context_fingerprint(machine=LAUNCH, os="linux")
    with pytest.raises(ValueError, match="preset is a whole machine"):
        generate_context_fingerprint(machine=LAUNCH, preset={"navigator": {"platform": "Win32"}})
    assert generate_context_fingerprint(machine=LAUNCH, os="windows")["config"]["navigator.platform"] == "Win32"


def test_render_node_contexts_claim_no_webgl():
    config = generate_context_fingerprint(machine=LAUNCH, webgl=False)["config"]
    assert not any(k.startswith(("webGl:", "webGl2:")) for k in config)


def test_no_launch_identity_draws_as_before():
    fp = generate_context_fingerprint(os="linux")
    assert "Linux" in fp["config"]["navigator.userAgent"]
    assert fingerprints.machine_config({}) == {}


def test_the_machine_screen_keeps_its_available_origin():
    """A context reads the machine's available rect, origin included, from the
    launch config; the per-context setter carried a size only, so a context
    read availLeft/availTop 0 while its windows were placed in the drawn work
    area (LEAKS 635)."""
    launch = {**LAUNCH, "screen.availLeft": 66, "screen.availTop": 32,
              "screen.availWidth": 2494, "screen.availHeight": 1408}
    fp = generate_context_fingerprint(machine=launch)
    assert "setScreenDimensions" not in fp["init_script"]
    assert {k: fp["config"][k] for k in ("screen.availLeft", "screen.availTop")} == {
        "screen.availLeft": 66, "screen.availTop": 32}


def test_a_drawn_identity_still_sets_its_own_screen():
    fp = generate_context_fingerprint(os="windows")
    assert "w.setScreenDimensions(" in fp["init_script"]
