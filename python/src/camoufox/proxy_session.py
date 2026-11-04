"""
A managed proxy without a Camoufox Pro lease: what `managed=` opens on a standard Camoufox build
(docs/pro.md, "Managed proxies").

A Pro build gets its managed proxy as a section of its lease. A standard build has no lease, so it asks
the API for a proxy session instead: the same exit and the same credentials, paid from the account's
wallet, renewed while the browser runs and released when it closes. Any account with a key can open
one, pay-as-you-go included.
"""

import atexit
import logging
import random
import threading
from typing import Any, Callable, Dict, List, Optional, Union

from . import pro
from .exceptions import ProError

log = logging.getLogger("camoufox.pro")

# The API states the cadence it wants; this is the lease's own default when it does not.
HEARTBEAT_S = 60.0
# Local destinations never go through the managed proxy, which refuses them.
BYPASS = "localhost,127.0.0.1,::1,*.local"


def log_notices(notices: Any) -> None:
    """Say what the API had to say about a mint or a proxy session: a replaced static IP, and the like."""
    for notice in notices or ():
        if isinstance(notice, dict) and notice.get("message"):
            log.warning("camoufox: %s (%s)", notice["message"], notice.get("code") or "notice")


class ProxySession:
    """
    One browser's managed proxy on a standard build. Opened on creation, renewed in a daemon thread,
    released by `release()`, which is idempotent and safe from any thread. It stands in for a lease
    where the launch path expects one (`pro.claim`, `pro.attach_lease`, `pro.launch_failed`).
    """

    def __init__(self, managed: Union[bool, Dict[str, Any]], key: Optional[str] = None) -> None:
        self._key = pro.resolve_key(key)
        self._base = pro.api_base()
        answer = pro.post(
            "/api/v1/proxy/sessions",
            {"managed": managed, "client": pro.client_name()},
            key=self._key,
            base=self._base,
        )
        self.session_id: str = str(answer["session_id"])
        self.egress: Dict[str, Any] = {
            name: answer.get(name)
            for name in (
                "server", "username", "password", "class", "country", "sticky", "exit_ip", "binding_id",
                "name", "expires_at",
            )
        }
        self.notices: List[Dict[str, Any]] = list(answer.get("notices") or [])
        log_notices(self.notices)
        self._heartbeat_s = float(answer.get("heartbeat_s") or HEARTBEAT_S)
        # Sessions held for the browser (a render node's assignment): ended with this one.
        self.on_end: List[Callable[[], None]] = []
        self.claimed = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._released = False
        _LIVE[self.username] = self
        self._thread = threading.Thread(target=self._beat, name="camoufox-proxy-heartbeat", daemon=True)
        self._thread.start()

    # What the launch path reads from a lease.
    @property
    def lease_id(self) -> str:
        return self.session_id

    @property
    def grants(self) -> Dict[str, Any]:
        return {"egress": self.egress}

    @property
    def username(self) -> str:
        return str(self.egress["username"])

    def proxy(self) -> Dict[str, str]:
        """The launch's `proxy` option for this session."""
        return {
            "server": str(self.egress["server"]),
            "username": self.username,
            "password": str(self.egress["password"]),
            "bypass": BYPASS,
        }

    def _beat(self) -> None:
        failures = 0
        wait = self._next_beat()
        while not self._stop.wait(wait):
            try:
                answer = pro.post(
                    f"/api/v1/proxy/sessions/{self.session_id}/heartbeat",
                    {},
                    key=self._key,
                    timeout=pro.HEARTBEAT_TIMEOUT_S,
                    base=self._base,
                )
            except ProError as error:
                if self._stop.is_set():
                    return
                if pro._transient(error) or (error.status or 0) >= 500:
                    backoff = pro.HEARTBEAT_BACKOFF_S
                    wait = error.retry_after or backoff[min(failures, len(backoff) - 1)]
                    failures += 1
                    continue
                log.error(
                    "camoufox: the managed proxy session was refused a renewal and will lapse: %s", error
                )
                return
            if isinstance(answer, dict):
                self.egress["expires_at"] = answer.get("expires_at", self.egress.get("expires_at"))
                log_notices(answer.get("notices"))
            failures, wait = 0, self._next_beat()

    def _next_beat(self) -> float:
        # Jittered by a twelfth, so a fleet started together does not beat together.
        return self._heartbeat_s * random.uniform(11 / 12, 13 / 12)  # nosec

    def release(self, reason: str = "clean_exit") -> bool:
        """End the session. Only the first call does anything; returns whether its release reached the API."""
        with self._lock:
            if self._released:
                return False
            self._released = True
        self._stop.set()
        _LIVE.pop(self.username, None)
        done = False
        for attempt in range(2):
            try:
                pro.post(
                    f"/api/v1/proxy/sessions/{self.session_id}/release",
                    {"reason": reason},
                    key=self._key,
                    timeout=pro.RELEASE_TIMEOUT_S,
                    base=self._base,
                )
                done = True
                break
            except ProError as error:
                if attempt or not pro._transient(error):
                    # It lapses by itself once its heartbeats stop.
                    log.warning("camoufox: releasing managed proxy session %s failed: %s", self.session_id, error)
                    break
        for end in self.on_end:
            try:
                end()
            except Exception as error:  # one failed hook must not keep the others' resources
                log.warning("camoufox: ending a session held with proxy session %s failed: %s", self.session_id, error)
        return done

    def close(self) -> None:
        self.release()


_LIVE: Dict[str, ProxySession] = {}


def claim(options: Dict[str, Any]) -> Optional[ProxySession]:
    """The proxy session these launch options were built with, taken by the browser about to use them."""
    proxy = options.get("proxy") or {}
    session = _LIVE.get(str(proxy.get("username"))) if isinstance(proxy, dict) else None
    if session is None:
        return None
    if session.claimed:
        raise ValueError(
            "These launch options' managed proxy session is already held by another browser. "
            "Each browser needs its own: call launch_options() once per browser."
        )
    session.claimed = True
    return session


def _release_all() -> None:
    for session in list(_LIVE.values()):
        session.release("driver_shutdown")


atexit.register(_release_all)
