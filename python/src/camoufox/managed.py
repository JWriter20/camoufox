"""
Managed proxies: the `managed` launch option (docs/pro.md, "Managed proxies"). Any Camoufox build can
use one with a Camoufox key, pay-as-you-go included; CamouLink needs a Camoufox Pro plan.

The launcher checks the request before minting so a mistake fails here, with a message naming the
option, rather than as an API refusal. The API applies the same rules and every default.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Type, Union


class ProxyProvider(str, Enum):
    """Who runs the exit. CAMOULINK is Camoufox Pro's own Starlink network, for Pro plans; the partners
    (EVOMI, RAYOBYTE, BYTEFUL) are open to every account."""

    CAMOULINK = "camoulink"
    EVOMI = "evomi"
    RAYOBYTE = "rayobyte"
    BYTEFUL = "byteful"


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
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# A place name is ambiguous across countries.
_NEEDS_COUNTRY = ("region", "city", "zip")
# CamouLink is residential Starlink: it is targeted by country, and by SpaceX's ASN only.
STARLINK_ASN = 14593
_CAMOULINK_UNTARGETED = ("region", "city", "zip", "isp")


@dataclass(frozen=True)
class Managed:
    """
    The managed proxy a launch asks for. A field left as None takes the API's default: provider
    CAMOULINK on a Pro build, EVOMI on a standard one (CamouLink needs a Pro plan); type STATIC with a
    profile or a name, ROTATING without; network RESIDENTIAL, or ISP for
    a static exit from a partner; any country.

    `name` keeps a STATIC exit without a profile: the account rents that IP under the name, and every
    launch naming it exits through the same IP until it is released
    (`camoufox.proxies.release_static`). A profile keeps its own, so a name and a profile conflict.

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
    name: Optional[str] = None

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
        if self.name is not None:
            if not isinstance(self.name, str) or not _NAME.match(self.name):
                raise ValueError(
                    f"managed name must be 1-64 letters, digits, '.', '_' or '-', not {self.name!r}"
                )
            if self.type is ProxyType.ROTATING:
                raise ValueError(
                    "A name keeps a STATIC exit, and ProxyType.ROTATING takes a fresh one each launch: "
                    "drop the name, or use ProxyType.STATIC."
                )
        # Only when named: an unset provider is CamouLink on a Pro lease but a partner (Evomi) on a
        # standard build's proxy session, and the API checks whichever it turns out to be.
        if self.provider is ProxyProvider.CAMOULINK:
            _check_camoulink(self)


def _check_camoulink(managed: "Managed") -> None:
    """The API's CamouLink rules (SPEC-EGRESS § 1.1, § 9.2), checked before any call. Whether the account
    may buy CamouLink (a Pro plan) is the API's to say: `PlanRequired`."""
    if managed.network not in (None, ProxyNetwork.RESIDENTIAL):
        raise ValueError(
            f"CamouLink exits are residential Starlink, so its network is ProxyNetwork.RESIDENTIAL, not "
            f"{managed.network.name}; name a partner provider for a {managed.network.name} exit."
        )
    targeted = next((name for name in _CAMOULINK_UNTARGETED if getattr(managed, name) is not None), None)
    if targeted is not None:
        raise ValueError(
            f"CamouLink exits cannot be targeted by {targeted}; they take country and asn ({STARLINK_ASN})."
        )
    if managed.asn is not None and int(str(managed.asn).upper().removeprefix("AS")) != STARLINK_ASN:
        raise ValueError(f"CamouLink exits are Starlink, AS{STARLINK_ASN}, not {managed.asn!r}.")


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
    if managed.name is not None and profile is not None:
        raise ValueError(
            "managed name and profile conflict: a profile keeps its own static IP. Drop the name, or launch "
            "without the profile."
        )
    kind = managed.type or (
        ProxyType.STATIC if profile is not None or managed.name is not None else ProxyType.ROTATING
    )
    if kind is ProxyType.STATIC and profile is None and managed.name is None:
        raise ValueError(
            "A STATIC managed proxy is kept by a profile or a name: pass profile, or Managed(name=...) to "
            "rent the IP under a name, or use ProxyType.ROTATING."
        )
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
        "name": managed.name,
    }
    return {key: value.value if isinstance(value, Enum) else value for key, value in fields.items() if value is not None}
