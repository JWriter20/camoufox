"""
Camoufox Pro: WebGL drawn on a render node.

A Pro build can send every WebGL call to a renderfarm render node instead of
drawing locally: a Windows machine that runs the call on stock Firefox's own
ANGLE (Direct3D 11) on its real GPU and sends back the results. The page then
sees what Windows Firefox on that GPU shows it, parameters and pixels alike,
so a Windows identity needs no WebGL masking and no canvas noise.

The node is reached through a renderfarm coordinator. One file names it:

    {"coordinator": "http://192.168.1.20:9471", "token": "<client token>", "gpu": "<optional>"}

found, in order, at the path launch_options(render_node=...) passes, the path in
CAMOUFOX_RENDER_NODE, or ~/.config/camoufox/render-node.json. `gpu` picks the
node's GPU by the WebGL renderer Windows Firefox reports for it, for example
"ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or similar";
without it the coordinator picks any free GPU.

Each browser holds its own assignment on the coordinator, renewed while the
browser runs and released when its Pro lease ends. WebGPU and text go to the
node too when its renderer serves those routes (the assignment's angle_routes).
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Set, Union
from urllib.parse import urlsplit

from .pro import write_private

log = logging.getLogger("camoufox")

CONFIG_ENV = "CAMOUFOX_RENDER_NODE"
# Read by the build's relay client (additions/pro/angle-relay): the session to open.
RELAY_ENV = "ANGLE_RELAY_CONFIG"
# Read by the build's DirectWrite (holo_dwrite): the socket every process sends
# its text calls through, when the node's renderer serves the text route.
TEXT_ENV = "HOLO_DWRITE_NODE_SOCK"
# sockaddr_un.sun_path holds 108 bytes on Linux, its terminating NUL included.
SOCKET_PATH_MAX = 107
DEFAULT_PATH = Path.home() / ".config" / "camoufox" / "render-node.json"
ROUTE = "/angle-relay"
# The coordinator's assignment lease is one minute; renewing at a third of it
# survives one lost renewal.
RENEW_S = 20.0
# How long a launch waits for a GPU when every one is busy.
BUSY_WAIT_S = 30.0
TIMEOUT_S = 10.0

RenderNodeOption = Union[None, bool, str, Path, Dict[str, Any]]


class RenderNodeError(RuntimeError):
    """The render node could not be reached or gave no session."""


def resolve(option: RenderNodeOption) -> Optional[Dict[str, Any]]:
    """
    The render-node configuration a launch uses, or None for local rendering.
    `option` False turns it off; a dict or path names it; None looks in
    CAMOUFOX_RENDER_NODE and then the default file.
    """
    if option is False:
        return None
    if option is True:
        option = None
    if isinstance(option, dict):
        return _validate(dict(option), "render_node")
    if option is None:
        named = os.environ.get(CONFIG_ENV, "").strip()
        if named:
            option = named
        elif DEFAULT_PATH.is_file():
            option = DEFAULT_PATH
        else:
            return None
    path = Path(option).expanduser()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RenderNodeError(f"Cannot read the render node configuration {path}: {error}") from error
    if not isinstance(data, dict):
        raise RenderNodeError(f"{path} must hold a JSON object.")
    return _validate(data, str(path))


def _validate(data: Dict[str, Any], where: str) -> Dict[str, Any]:
    unknown = set(data) - {"coordinator", "token", "gpu"}
    if unknown:
        raise RenderNodeError(f"{where}: unknown keys {sorted(unknown)}")
    coordinator = str(data.get("coordinator") or "").rstrip("/")
    parts = urlsplit(coordinator)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path:
        raise RenderNodeError(f"{where}: coordinator must be an origin such as http://host:9471")
    if parts.scheme == "https":
        # The relay client speaks plain ws:// to a private-network coordinator only.
        raise RenderNodeError(f"{where}: an https coordinator is not supported yet; use the LAN http origin")
    if not isinstance(data.get("token"), str) or not data["token"]:
        raise RenderNodeError(f"{where}: token is required")
    gpu = data.get("gpu")
    if gpu is not None and (not isinstance(gpu, str) or not gpu):
        raise RenderNodeError(f"{where}: gpu must be a renderer string")
    return {"coordinator": coordinator, "token": data["token"], "gpu": gpu}


_SESSIONS: Dict[str, "Session"] = {}


def track(session: "Session") -> None:
    _SESSIONS[str(session.path)] = session


def claim(options: Dict[str, Any]) -> Optional["Session"]:
    session = _SESSIONS.get(str(options.get("env", {}).get(RELAY_ENV)))
    if session is None:
        return None
    if session.claimed:
        raise ValueError(
            "These launch options' render node is already held by another browser. "
            "Each browser needs its own: call launch_options() once per browser."
        )
    session.claimed = True
    return session


class Session:
    """One browser's assignment on the coordinator, and the file its relay client reads."""

    grants: Dict[str, Any] = {}

    def __init__(self, config: Dict[str, Any], version: str, directory: Path) -> None:
        self.claimed = False
        self.config = config
        self.version = version
        self.path: Optional[Path] = None
        self.text_socket: Optional[Path] = None
        self.assignment: Dict[str, Any] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._released = False
        self.assignment = self._acquire()
        self.path = directory / f"render-node-{self.assignment['assignment_id'][:16]}.json"
        coordinator = config["coordinator"]
        session = {
            "endpoint": "ws" + coordinator[len("http"):] + ROUTE,
            "token": config["token"],
            "assignment": self.assignment["assignment_id"],
            "profile": self.assignment["profile_digest"],
            "browser": "firefox",
            "version": version,
        }
        write_private(self.path, json.dumps(session, separators=(",", ":")).encode())
        if "text" in self.routes:
            # The browser creates it; it only has to be private and short enough to bind.
            self.text_socket = directory / f"render-node-{self.assignment['assignment_id'][:16]}.sock"
            if len(os.fsencode(self.text_socket)) > SOCKET_PATH_MAX:
                self.release()
                raise RenderNodeError(
                    f"{self.text_socket}: longer than a Unix socket path can be; set XDG_RUNTIME_DIR to a shorter directory"
                )
        self._renewer = threading.Thread(target=self._renew_loop, name="camoufox-render-node", daemon=True)
        self._renewer.start()
        log.info(
            "camoufox-pro: WebGL renders on %s (%s)",
            self.assignment.get("renderer_id"),
            self.assignment.get("hello", {}).get("gpu_summary", "unknown GPU"),
        )

    @property
    def routes(self) -> Set[str]:
        """The node's routes beyond WebGL: "webgpu" and "text" where its renderer serves them."""
        return set(self.assignment.get("angle_routes") or ())

    @property
    def gpu_summary(self) -> str:
        return str(self.assignment.get("hello", {}).get("gpu_summary") or "")

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        request = urllib.request.Request(
            self.config["coordinator"] + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self.config["token"], "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # nosec: operator-configured origin
            raw = response.read()
        return json.loads(raw) if raw else None

    def _acquire(self) -> Dict[str, Any]:
        request = {
            "browser_id": "camoufox-" + uuid.uuid4().hex,
            "browser": "firefox",
            "version": self.version,
            "route": "angle",
        }
        if self.config.get("gpu"):
            request["gpu"] = self.config["gpu"]
        deadline = time.monotonic() + BUSY_WAIT_S
        while True:
            try:
                answer = self._request("POST", "/assignments", request)
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace").strip()
                busy = error.code == 503 and error.headers.get("Retry-After")
                if busy and time.monotonic() < deadline:
                    time.sleep(min(float(error.headers["Retry-After"]), 5.0))
                    continue
                if error.code == 503 and detail == "no compatible renderer":
                    raise RenderNodeError(
                        f"No render node serves Firefox {self.version}"
                        + (f" on {self.config['gpu']!r}" if self.config.get("gpu") else "")
                        + ": the farm needs an ANGLE pack for this exact release."
                    ) from error
                raise RenderNodeError(f"The coordinator refused a render node: HTTP {error.code} {detail}") from error
            except (OSError, ValueError) as error:
                raise RenderNodeError(f"Cannot reach the render node coordinator: {error}") from error
            if not isinstance(answer, dict) or answer.get("route") != "angle" or not answer.get("assignment_id"):
                raise RenderNodeError("The coordinator answered with something other than an ANGLE assignment.")
            return answer

    def _renew_loop(self) -> None:
        failures = 0
        while not self._stop.wait(RENEW_S):
            try:
                self._request("POST", f"/assignments/{self.assignment['assignment_id']}/renew")
                failures = 0
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    log.warning("camoufox-pro: the render node assignment expired; WebGL in this browser has stopped")
                    return
                failures += 1
            except (OSError, ValueError):
                failures += 1
            if failures == 2:
                log.warning("camoufox-pro: renewing the render node assignment is failing; retrying")

    @property
    def lease_id(self) -> str:
        return str(self.assignment["assignment_id"])

    def close(self) -> None:
        self.release()

    def release(self, *_: Any) -> None:
        """End the assignment and delete the session file and socket. Only the first call does anything."""
        with self._lock:
            if self._released:
                return
            self._released = True
        _SESSIONS.pop(str(self.path), None)
        self._stop.set()
        try:
            self._request("DELETE", f"/assignments/{self.assignment['assignment_id']}")
        except (OSError, ValueError) as error:
            # It lapses by itself within a minute.
            log.debug("camoufox-pro: releasing the render node assignment failed: %s", error)
        for path in (self.path, self.text_socket):
            if path is not None:
                path.unlink(missing_ok=True)
