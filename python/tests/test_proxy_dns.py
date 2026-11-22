"""
Who resolves names behind a proxy (docs/proxy-dns.md).

Behind a SOCKS5 proxy the browser resolves by DoH through the proxy unless the
caller picks `proxy_dns='exit'`; an HTTP proxy is always given names. The
browser's half is browser/tests/playwright/patches/http3-socks5-udp.py.
"""

import pytest

from camoufox import utils

SOCKS = {"server": "socks5://proxy.example:1080", "username": "u", "password": "p"}
DOH = {**utils.PROXY_DOH_PREFS, "network.trr.excluded-domains": "proxy.example"}


def _apply(proxy_dns, proxy, exit_looked_up=False, config=None, prefs=None):
    config = {} if config is None else config
    prefs = {} if prefs is None else prefs
    utils.apply_proxy_dns(proxy_dns, proxy, config, prefs, exit_looked_up)
    return config, prefs


@pytest.mark.parametrize("proxy_dns", [None, "doh"])
def test_socks5_proxy_resolves_by_doh(proxy_dns):
    assert utils.PROXY_DOH_PREFS["network.trr.mode"] == 3
    assert _apply(proxy_dns, SOCKS) == ({}, DOH)


def test_a_proxy_named_by_address_needs_no_exclusion():
    for server in ("socks5://127.0.0.1:1080", "socks5://[2001:db8::1]:1080"):
        assert _apply(None, {"server": server}) == ({}, utils.PROXY_DOH_PREFS)


def test_exit_mode_leaves_names_to_the_proxy():
    assert _apply("exit", SOCKS) == ({}, {})


@pytest.mark.parametrize("proxy", [None, {"server": "http://proxy.example:8080"}, {"server": "proxy.example:8080"},
                                   {"server": "socks4://proxy.example:1080"}])
def test_only_socks5_resolves_by_doh(proxy):
    assert _apply(None, proxy) == ({}, {})
    assert _apply("exit", proxy) == ({}, {})
    with pytest.raises(ValueError, match="socks5"):
        _apply("doh", proxy)


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="proxy_dns"):
        _apply("system", SOCKS)


def test_caller_prefs_are_kept():
    mine = {"network.trr.uri": "https://dns.example/dns-query", "network.trr.excluded-domains": "a.example"}
    _, prefs = _apply(None, SOCKS, prefs=dict(mine))
    assert prefs == {**mine, "network.trr.mode": 3}


@pytest.mark.parametrize("ipv6, expected", [(None, {"network:ipv4Only": True}), ("2001:db8::7", {})])
def test_exit_without_ipv6_is_an_ipv4_only_network(monkeypatch, ipv6, expected):
    asked = []
    monkeypatch.setattr(utils, "public_ipv6", lambda proxy: asked.append(proxy) or ipv6)
    assert _apply(None, SOCKS, exit_looked_up=True)[0] == expected
    assert asked == ["socks5://u:p@proxy.example:1080"]


def test_exit_ipv6_is_not_asked_when_already_known(monkeypatch):
    monkeypatch.setattr(utils, "public_ipv6", lambda proxy: pytest.fail("looked up"))
    assert _apply(None, SOCKS, exit_looked_up=False)[0] == {}
    for known in ({"webrtc:ipv6": "2001:db8::7"}, {"network:ipv4Only": False}):
        assert _apply(None, SOCKS, exit_looked_up=True, config=dict(known))[0] == known
    assert _apply("exit", SOCKS, exit_looked_up=True)[0] == {}
