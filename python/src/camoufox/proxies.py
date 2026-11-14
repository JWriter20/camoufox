"""
The managed proxy catalogue: what each provider sells and what it can be targeted at, in the same
shape whatever the provider (docs/pro.md, "Managed proxies"). Public: no key is needed. And the static
IPs an account rents under a name (`list_statics`, `release_static`), which need its key.

    from camoufox.proxies import list_proxies, proxy_targets
    for product in list_proxies():
        print(product.provider, product.network, product.type, product.sell_cents, product.targeting)
    cities = proxy_targets(ProxyProvider.EVOMI, ProxyNetwork.RESIDENTIAL, ProxyType.ROTATING,
                           ProxyDimension.CITY, country="US")

A value from `proxy_targets` is what `Managed(...)` takes: `Managed(provider=..., country="US",
city=cities[0].value)`.

    for static in list_statics():
        print(static.name, static.provider, static.network, static.exit_ip, static.paid_through)
    release_static("shop-ip")    # stop renting it
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

from . import pro
from .managed import ProxyDimension, ProxyNetwork, ProxyProvider, ProxyType

PAGE = 1000


@dataclass(frozen=True)
class Targeting:
    """How a product is targeted on one dimension. `mode` is "listed" (the provider names its values)
    or "open" (any well-formed value is tried; the known ones are those Camoufox Pro has exited on)."""

    mode: str
    known: int


@dataclass(frozen=True)
class ProxyProduct:
    provider: ProxyProvider
    provider_name: str
    network: ProxyNetwork
    type: ProxyType
    # "gb" for bandwidth, "ip_month" for a static IP's rent.
    unit: str
    sell_cents: int
    retail_cents: Optional[int]
    # Whether an exit for it can be had right now.
    available: bool
    # Whether a profile may use it (a profile keeps its IP: mobile, static ISP or CamouLink).
    profiles: bool
    targeting: Dict[ProxyDimension, Targeting]
    # A static product's IPs in stock; None for bandwidth products.
    static_ips_available: Optional[int]


@dataclass(frozen=True)
class ProxyOption:
    """One value a product can be targeted at."""

    value: str
    name: Optional[str]
    country: Optional[str]
    region: Optional[str]
    # Named by the provider's own listing, or seen by Camoufox Pro's probes (`observed_at`), or both.
    listed: bool
    observed_at: Optional[str]
    # For a static product, how many IPs in stock carry this value.
    available: Optional[int]


@dataclass(frozen=True)
class NamedStatic:
    """A static IP the account rents under a name (`Managed(type=ProxyType.STATIC, name=...)`)."""

    name: str
    provider: Optional[ProxyProvider]
    type: Optional[ProxyType]
    network: Optional[ProxyNetwork]
    country: Optional[str]
    exit_ip: Optional[str]
    # When the rent paid so far runs out; it renews from the wallet until the IP is released.
    paid_through: Optional[str]
    # A month's rent, your plan's discount applied; None when the product has no monthly rent.
    monthly_cents: Optional[int]


def list_proxies() -> List[ProxyProduct]:
    """Every managed proxy product on sale, with its price and targeting."""
    body = pro.call("GET", "/api/v1/pro/proxies")
    return [_product(entry) for entry in body.get("products", [])]


def proxy_targets(
    provider: ProxyProvider,
    network: ProxyNetwork,
    type: ProxyType,
    dimension: ProxyDimension,
    *,
    country: Optional[str] = None,
    query: Optional[str] = None,
) -> List[ProxyOption]:
    """Every value of one dimension for one product, every page of it. An unsupported dimension is an
    empty list."""
    options: List[ProxyOption] = []
    after: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"limit": PAGE}
        if country is not None:
            params["country"] = country
        if query:
            params["q"] = query
        if after is not None:
            params["after"] = after
        route = (
            f"/api/v1/pro/proxies/{ProxyProvider(provider).value}/{ProxyNetwork(network).value}"
            f"/{ProxyType(type).value}/{ProxyDimension(dimension).value}?{urlencode(params)}"
        )
        body = pro.call("GET", route)
        options.extend(_option(entry) for entry in body.get("options", []))
        after = body.get("next")
        if not after:
            return options


def _product(entry: Dict[str, Any]) -> ProxyProduct:
    return ProxyProduct(
        provider=ProxyProvider(entry["provider"]),
        provider_name=str(entry.get("provider_name") or entry["provider"]),
        network=ProxyNetwork(entry["network"]),
        type=ProxyType(entry["type"]),
        unit=str(entry["unit"]),
        sell_cents=int(entry["sell_cents"]),
        retail_cents=None if entry.get("retail_cents") is None else int(entry["retail_cents"]),
        available=bool(entry.get("available")),
        profiles=bool(entry.get("profiles")),
        targeting={
            ProxyDimension(name): Targeting(str(value["mode"]), int(value.get("known") or 0))
            for name, value in (entry.get("targeting") or {}).items()
            if name in ProxyDimension._value2member_map_
        },
        static_ips_available=entry.get("static_ips_available"),
    )


def _option(entry: Dict[str, Any]) -> ProxyOption:
    return ProxyOption(
        value=str(entry["value"]),
        name=entry.get("name"),
        country=entry.get("country"),
        region=entry.get("region"),
        listed=bool(entry.get("listed")),
        observed_at=entry.get("observed_at"),
        available=entry.get("available"),
    )


def list_statics(*, key: Optional[str] = None) -> List[NamedStatic]:
    """The static IPs this account rents under a name. Needs the account's key (`camoufox login`)."""
    body = pro.call("GET", "/api/v1/proxy/statics", key=pro.resolve_key(key))
    return [_static(entry) for entry in body.get("statics", [])]


def release_static(name: str, *, key: Optional[str] = None) -> None:
    """Stop renting the static IP held under `name`. The next launch naming it rents a new IP."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"release_static takes the static IP's name, not {name!r}")
    pro.call("DELETE", f"/api/v1/proxy/statics/{quote(name, safe='')}", key=pro.resolve_key(key))


def _member(enum: Any, value: Any) -> Any:
    try:
        return enum(value)
    except ValueError:
        return None


def _static(entry: Dict[str, Any]) -> NamedStatic:
    return NamedStatic(
        name=str(entry["name"]),
        provider=_member(ProxyProvider, entry.get("provider")),
        type=_member(ProxyType, entry.get("type")),
        network=_member(ProxyNetwork, entry.get("network")),
        country=entry.get("country"),
        exit_ip=entry.get("exit_ip"),
        paid_through=entry.get("paid_through"),
        monthly_cents=entry.get("monthly_cents"),
    )
