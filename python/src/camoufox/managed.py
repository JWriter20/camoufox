"""
Camoufox Pro managed proxies: the `managed` launch option (docs/pro.md, "Managed proxies").

The launcher checks the request before minting so a mistake fails here, with a message naming the
option, rather than as an API refusal. The API applies the same rules and every default.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Type, Union


class ProxyProvider(str, Enum):
    """Who runs the exit. CAMOULINK is Camoufox Pro's own Starlink network."""

    CAMOULINK = "camoulink"
    EVOMI = "evomi"
    RAYOBYTE = "rayobyte"


class ProxyType(str, Enum):
    """ROTATING: a fresh exit each launch, kept for that browser's life. STATIC: an exit that stays."""

    ROTATING = "rotating"
    STATIC = "static"


class ProxyNetwork(str, Enum):
    """What kind of address the exit is."""

    RESIDENTIAL = "residential"
    MOBILE = "mobile"
    ISP = "isp"
    DATACENTER = "datacenter"


class ProxyDimension(str, Enum):
    """What an exit can be targeted by. Which a product takes, and their values, are in the listing
    (`camoufox.proxies.list_proxies`, `camoufox.proxies.proxy_targets`)."""

    COUNTRY = "country"
    REGION = "region"
    CITY = "city"
    ZIP = "zip"
    ISP = "isp"
    ASN = "asn"


# A profile keeps one IP for life, so it exits only through networks that can hold one.
PROFILE_PARTNER_NETWORKS = (ProxyNetwork.MOBILE, ProxyNetwork.ISP)
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_ASN = re.compile(r"^(?:AS)?[0-9]{1,10}$", re.IGNORECASE)
_ZIP = re.compile(r"^[A-Za-z0-9]{2,10}$")
# A place name is ambiguous across countries.
_NEEDS_COUNTRY = ("region", "city", "zip")


@dataclass(frozen=True)
class Managed:
    """
    The managed proxy a launch asks for. A field left as None takes the API's default: provider
    CAMOULINK; type STATIC with a profile, ROTATING without; network RESIDENTIAL, or ISP for a static
    exit from a partner; any country.

    Targeting beyond country: `region` and `city` take a name or its listing value ("New York" and
    "new_york" are the same), `zip` a postal code, `isp` the listing's value, `asn` 14593 or "AS14593".
    A region, city or ZIP needs a country. A product that cannot be targeted by a field refuses it.
    """

    provider: Optional[ProxyProvider] = None
    type: Optional[ProxyType] = None
    network: Optional[ProxyNetwork] = None
    country: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    zip: Optional[str] = None
    isp: Optional[str] = None
    asn: Optional[Union[int, str]] = None

    def __post_init__(self) -> None:
        _member("provider", self.provider, ProxyProvider)
        _member("type", self.type, ProxyType)
        _member("network", self.network, ProxyNetwork)
        if self.country is not None and (not isinstance(self.country, str) or not _COUNTRY.match(self.country)):
            raise ValueError(f"managed country must be an upper-case ISO 3166 code such as 'US', not {self.country!r}")
        for name in ("region", "city", "isp"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"managed {name} must be a non-empty string, not {value!r}")
        if self.zip is not None and (not isinstance(self.zip, str) or not _ZIP.match(self.zip)):
            raise ValueError(f"managed zip must be 2-10 letters or digits, not {self.zip!r}")
        if self.asn is not None and (isinstance(self.asn, bool) or not _ASN.match(str(self.asn))):
            raise ValueError(f"managed asn must be a number such as 14593 or 'AS14593', not {self.asn!r}")
        place = next((name for name in _NEEDS_COUNTRY if getattr(self, name) is not None), None)
        if place is not None and self.country is None:
            raise ValueError(f"managed {place} needs a country: a place name is ambiguous without one")


def _member(name: str, value: Any, enum: Type[Enum]) -> None:
    if value is not None and not isinstance(value, enum):
        raise TypeError(f"managed {name} must be a {enum.__name__} member (e.g. {enum.__name__}.{next(iter(enum)).name}), not {value!r}")


def managed_request(
    managed: Optional[Union[Managed, bool]], *, profile: Optional[str], proxy: Optional[Dict[str, Any]]
) -> Optional[Union[bool, Dict[str, Union[int, str]]]]:
    """The mint request's `managed` field, or None to leave it out."""
    if managed is not None and not isinstance(managed, (bool, Managed)):
        raise TypeError(f"managed must be a Managed, True or False, not {managed!r}")
    # The caller's own proxy is never replaced by a managed one, and asking for both is a mistake
    # worth naming rather than a request to drop one of them quietly.
    if proxy is not None:
        if managed not in (None, False):
            raise ValueError(
                "proxy and managed conflict: with your own proxy there is no managed proxy. "
                "Pass proxy to use your own, or managed (and no proxy) for a managed one."
            )
        return False
    if not isinstance(managed, Managed):
        return managed
    kind = managed.type or (ProxyType.STATIC if profile is not None else ProxyType.ROTATING)
    if kind is ProxyType.STATIC and profile is None:
        raise ValueError("A STATIC managed proxy belongs to a profile: pass profile too, or use ProxyType.ROTATING.")
    if kind is ProxyType.ROTATING and profile is not None:
        raise ValueError("A profile keeps its IP for life, so its managed proxy cannot be ProxyType.ROTATING.")
    if profile is not None and managed.provider not in (None, ProxyProvider.CAMOULINK):
        network = managed.network or ProxyNetwork.ISP
        if network not in PROFILE_PARTNER_NETWORKS:
            raise ValueError(
                "A profile exits through ProxyProvider.CAMOULINK, or a partner's ProxyNetwork.MOBILE or "
                f"ProxyNetwork.ISP, so it matches its IP for life; not {network.name}."
            )
    fields = {
        "provider": managed.provider,
        "type": managed.type,
        "network": managed.network,
        "country": managed.country,
        "region": managed.region,
        "city": managed.city,
        "zip": managed.zip,
        "isp": managed.isp,
        "asn": managed.asn,
    }
    return {key: value.value if isinstance(value, Enum) else value for key, value in fields.items() if value is not None}
