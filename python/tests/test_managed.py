"""
The `managed` launch option (docs/pro.md, "Managed proxies"): what the launcher sends, and what it
refuses before minting. No network.
"""

import pytest

from camoufox import Managed, ProxyNetwork, ProxyProvider, ProxyType
from camoufox.managed import managed_request


def request(managed, profile=None, proxy=None):
    return managed_request(managed, profile=profile, proxy=proxy)


# ── the shapes docs/pro.md shows ─────────────────────────────────────────────


def test_the_documented_one_off_browser():
    managed = Managed(provider=ProxyProvider.EVOMI, type=ProxyType.ROTATING, network=ProxyNetwork.RESIDENTIAL, country="US")
    assert request(managed) == {"provider": "evomi", "type": "rotating", "network": "residential", "country": "US"}


def test_the_documented_camoulink_profile():
    assert request(Managed(provider=ProxyProvider.CAMOULINK), profile="shop-1") == {"provider": "camoulink"}


def test_the_documented_static_isp_profile():
    managed = Managed(provider=ProxyProvider.EVOMI, type=ProxyType.STATIC, network=ProxyNetwork.ISP)
    assert request(managed, profile="shop-2") == {"provider": "evomi", "type": "static", "network": "isp"}


def test_true_false_and_absent():
    assert request(True) is True
    assert request(False) is False
    assert request(None) is None
    assert request(None, profile="shop-1") is None


# ── your own proxy ───────────────────────────────────────────────────────────


def test_your_own_proxy_asks_for_no_managed_one():
    proxy = {"server": "http://my.proxy:8080"}
    assert request(None, proxy=proxy) is False
    assert request(False, proxy=proxy) is False
    for managed in (True, Managed()):
        with pytest.raises(ValueError, match="proxy and managed conflict"):
            request(managed, proxy=proxy)


# ── typing ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field, value, enum",
    [("provider", "evomi", "ProxyProvider"), ("type", "static", "ProxyType"), ("network", "isp", "ProxyNetwork")],
)
def test_a_bare_string_is_refused_naming_the_enum(field, value, enum):
    with pytest.raises(TypeError, match=enum):
        Managed(**{field: value})


@pytest.mark.parametrize("country", ["us", "USA", "U", 1])
def test_country_is_an_upper_case_iso_code(country):
    with pytest.raises(ValueError, match="ISO 3166"):
        Managed(country=country)


def test_managed_is_a_managed_or_a_bool():
    with pytest.raises(TypeError, match="Managed, True or False"):
        request({"provider": "evomi"})


# ── what a profile may use ───────────────────────────────────────────────────


def test_static_needs_a_profile():
    with pytest.raises(ValueError, match="belongs to a profile"):
        request(Managed(type=ProxyType.STATIC))


def test_a_profile_cannot_rotate():
    with pytest.raises(ValueError, match="cannot be ProxyType.ROTATING"):
        request(Managed(type=ProxyType.ROTATING), profile="shop-1")


@pytest.mark.parametrize("network", [ProxyNetwork.RESIDENTIAL, ProxyNetwork.DATACENTER])
def test_a_profile_on_a_partner_exits_through_mobile_or_isp_only(network):
    with pytest.raises(ValueError, match=network.name):
        request(Managed(provider=ProxyProvider.EVOMI, network=network), profile="shop-1")


@pytest.mark.parametrize("network", [None, ProxyNetwork.MOBILE, ProxyNetwork.ISP])
def test_a_profile_on_a_partner_may_use_mobile_or_isp(network):
    managed = Managed(provider=ProxyProvider.RAYOBYTE, network=network)
    assert request(managed, profile="shop-1")["provider"] == "rayobyte"


def test_a_profile_on_camoulink_takes_any_network():
    assert request(Managed(network=ProxyNetwork.RESIDENTIAL), profile="shop-1") == {"network": "residential"}


# ── targeting beyond country (SPEC-EGRESS § 9) ───────────────────────────────


def test_targeting_is_sent_as_given():
    managed = Managed(
        provider=ProxyProvider.EVOMI, country="US", region="New York", city="new_york", isp="comcast", asn="AS7018"
    )
    assert request(managed) == {
        "provider": "evomi",
        "country": "US",
        "region": "New York",
        "city": "new_york",
        "isp": "comcast",
        "asn": "AS7018",
    }
    assert request(Managed(asn=14593), profile="shop-1") == {"asn": 14593}


@pytest.mark.parametrize("field", ["region", "city", "zip"])
def test_a_place_needs_a_country(field):
    with pytest.raises(ValueError, match="needs a country"):
        Managed(**{field: "10001"})


@pytest.mark.parametrize("asn", ["AS", "ASN7018", True, "70-18"])
def test_an_asn_is_a_number(asn):
    with pytest.raises(ValueError, match="asn"):
        Managed(asn=asn)


def test_a_zip_is_short_and_alphanumeric():
    with pytest.raises(ValueError, match="zip"):
        Managed(country="US", zip="1")


# ── the catalogue ────────────────────────────────────────────────────────────


def test_the_catalogue_is_one_shape_for_every_provider(monkeypatch):
    from camoufox import ProxyDimension, pro, proxies

    product = {
        "provider": "rayobyte",
        "provider_name": "Rayobyte",
        "network": "residential",
        "type": "rotating",
        "unit": "gb",
        "retail_cents": 350,
        "sell_cents": 333,
        "available": True,
        "profiles": False,
        "targeting": {"country": {"mode": "open", "known": 3}, "asn": {"mode": "open", "known": 41}, "new": {}},
        "static_ips_available": None,
        "targets": "/api/v1/pro/proxies/rayobyte/residential/rotating",
    }
    pages = {
        None: {"options": [{"value": "701", "name": "UUNET", "country": "US", "listed": False}], "next": "701"},
        "701": {"options": [{"value": "7018", "name": "ATT", "country": "US", "listed": False}], "next": None},
    }
    routes = []

    def call(method, route, payload=None, **_):
        routes.append(route)
        if route == "/api/v1/pro/proxies":
            return {"products": [product]}
        after = route.partition("after=")[2] or None
        return pages[after]

    monkeypatch.setattr(pro, "call", call)
    [listed] = proxies.list_proxies()
    assert (listed.provider, listed.network, listed.type) == (
        ProxyProvider.RAYOBYTE,
        ProxyNetwork.RESIDENTIAL,
        ProxyType.ROTATING,
    )
    assert listed.targeting[ProxyDimension.ASN] == proxies.Targeting("open", 41)
    assert ProxyDimension.COUNTRY in listed.targeting and len(listed.targeting) == 2, "unknown dimensions are skipped"
    options = proxies.proxy_targets(
        ProxyProvider.RAYOBYTE, ProxyNetwork.RESIDENTIAL, ProxyType.ROTATING, ProxyDimension.ASN, country="US"
    )
    assert [option.value for option in options] == ["701", "7018"]
    assert routes[1] == "/api/v1/pro/proxies/rayobyte/residential/rotating/asn?limit=1000&country=US"
    assert routes[2].endswith("&after=701")


def test_a_target_the_provider_lacks_is_its_own_error():
    from camoufox.exceptions import EgressUnavailable, TargetUnavailable
    from camoufox.pro import _error

    error = _error(409, {"error": "target_unavailable", "message": "No evomi exit", "details": {"listing": "/x"}}, None)
    assert isinstance(error, TargetUnavailable) and error.details["listing"] == "/x"
    assert isinstance(_error(409, {"error": "egress_unavailable", "message": "none"}, None), EgressUnavailable)
