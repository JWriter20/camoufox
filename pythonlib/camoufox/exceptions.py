from typing import Any, Dict, Optional


class UnsupportedVersion(Exception):
    """
    Raised when the Camoufox executable is outdated.
    """

    ...


class MissingRelease(Exception):
    """
    Raised when a required GitHub release asset is missing.
    """

    ...


class CorruptedDownload(Exception):
    """
    Raised when a downloaded asset does not match its expected sha256 digest.
    """

    ...


class UnsupportedArchitecture(Exception):
    """
    Raised when the architecture is not supported.
    """

    ...


class UnsupportedOS(Exception):
    """
    Raised when the OS is not supported.
    """

    ...


class InvalidPropertyType(Exception):
    """
    Raised when the property type is invalid.
    """

    ...


class InvalidAddonPath(FileNotFoundError):
    """
    Raised when the addon path is invalid.
    """

    ...


class LocaleError(Exception):
    """
    Raised when the locale is invalid.
    """

    ...


class InvalidIP(Exception):
    """
    Raised when an IP address is invalid.
    """

    ...


class InvalidProxy(Exception):
    """
    Raised when a proxy is invalid.
    """

    ...


class UnknownIPLocation(LocaleError):
    """
    Raised when the location of an IP is unknown.
    """

    ...


class InvalidLocale(LocaleError):
    """
    Raised when the locale input is invalid.
    """

    @classmethod
    def invalid_input(cls, locale: str) -> 'InvalidLocale':
        return cls(
            f"Invalid locale: '{locale}'. Must be either a region, language, "
            "language-region, or language-script-region."
        )


class UnknownTerritory(InvalidLocale):
    """
    Raised when the territory is unknown.
    """

    ...


class UnknownLanguage(InvalidLocale):
    """
    Raised when the language is unknown.
    """

    ...


class NotInstalledGeoIPExtra(ImportError):
    """
    Raised when the maxminddb module is not installed.
    """

    ...


class NonFirefoxFingerprint(Exception):
    """
    Raised when a passed fingerprint is not a Firefox fingerprint.
    """

    ...


class InvalidOS(ValueError):
    """
    Raised when the target OS is invalid.
    """

    ...


class VirtualDisplayError(Exception):
    """
    Raised when there is an error with the virtual display.
    """

    ...


class CannotFindXvfb(VirtualDisplayError):
    """
    Raised when Xvfb cannot be found.
    """

    ...
    pass


class CannotExecuteXvfb(VirtualDisplayError):
    """
    Raised when Xvfb cannot be executed.
    """

    ...


class VirtualDisplayNotSupported(VirtualDisplayError):
    """
    Raised when the user tried to use a virtual display on a non-Linux OS.
    """

    ...


class CamoufoxNotInstalled(FileNotFoundError):
    """
    Raised when camoufox is not installed.
    """

    ...


class ProfileDirectoryError(RuntimeError):
    """Raised when Camoufox's required runtime directory cannot be prepared."""

    ...


class FpgenModelError(RuntimeError):
    """Raised when fpgen's pinned model cannot be installed where fpgen reads it."""

    ...


class HumanizeEngineUnavailable(ValueError):
    """
    Raised at launch when `humanize` names an engine the browser build does not
    ship: one its humanize-engines.json does not list for that channel.
    """

    def __init__(self, channel: str, engine: str, available: list, reason: str = '') -> None:
        self.channel = channel
        self.engine = engine
        self.available = available
        super().__init__(
            f'humanize {channel}: {engine} is not available in this build'
            + (f' ({reason})' if reason else '')
            + f'; available: {available}'
        )


class ProError(Exception):
    """
    Raised when Camoufox Pro cannot start a session. `code` is the API's stable
    error code, `message` its explanation, and `resolution_url` the page that
    fixes it, when there is one.
    """

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status: Optional[int] = None,
        resolution_url: Optional[str] = None,
        retry_after: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(f"{message} {resolution_url}" if resolution_url else message)
        self.message = message
        self.code = code
        self.status = status
        self.resolution_url = resolution_url
        self.retry_after = retry_after
        self.details = details or {}


class NotSignedIn(ProError):
    """
    Raised when there is no Camoufox Pro key, or the API does not accept it.
    """


class InvalidRequest(ProError):
    """
    Raised when the API rejects the lease request itself.
    """


class SubscriptionRequired(ProError):
    """
    Raised when the account has no active Camoufox Pro subscription.
    """


class AllowanceExhausted(ProError):
    """
    Raised when a metered allowance is used up and usage-based billing is off.
    """


class AccountSuspended(ProError):
    """
    Raised when the account is suspended.
    """


class BuildNotAllowlisted(ProError):
    """
    Raised when the browser is not a published Camoufox Pro release, or has been
    revoked.
    """


class LeaseLimitReached(ProError):
    """
    Raised when every concurrent browser the plan includes is already running.
    """


class CapabilityMismatch(ProError):
    """
    Raised when this machine cannot present the requested identity.
    """


class RateLimited(ProError):
    """
    Raised when the API is still refusing for rate after the retries.
    """


class ProUnavailable(ProError):
    """
    Raised when the Camoufox Pro API cannot be reached, or fails, after the
    retries.
    """


class ProfileMismatch(ProError):
    """
    Raised when the profile exists with another OS, warm plan or egress regime
    than the launch asked for.
    """


class StatePoolSealed(ProError):
    """
    Raised when a profile's state is sealed for the warm pool, so this machine
    cannot sync it.
    """


class GpuUnavailable(ProError):
    """
    Raised when no remote GPU can serve a Windows identity now. Retry after
    `retry_after`, or launch with `gpu=False` to render on this machine.
    """


class ProClockSkew(ProError):
    """
    Raised when this machine's clock is so far from the API's that a fresh lease
    would look expired to the browser.
    """

    def __init__(self, skew: float) -> None:
        super().__init__(
            f"This machine's clock is {skew:+.0f} s off the Camoufox Pro API's. Lease expiry "
            "is evaluated on this machine's clock; fix NTP.",
            code="clock_skew",
        )
        self.skew = skew


class LeaseRefused(ProError):
    """
    Raised when a Camoufox Pro browser refuses the lease it was started with.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"The browser refused its Camoufox Pro lease ({reason}).", code="lease_refused")
        self.reason = reason
