r"""
A window the window manager shrinks as it maps it gets the identity's outer
size back during the startup hold.

CamoufoxWindow.sys.mjs sizes each new window to the identity's
window.outerWidth/outerHeight and, for five seconds, resizes it back whenever
the window manager changes it. Openbox maps a window taller than the screen
shorter than it asked for. GTK sends a size request only when it differs
from the last one it sent, and it records the window's own size as that
request only in a resize pass nobody asked a size in. The window manager's
shrink postpones one pass; when the hold's resizeBy() lands before the
postponed pass runs, that pass computes the size GTK already sent and sends
nothing. Nothing else runs a pass after that, so every later resizeBy() asked
GTK for that same size again and never reached the window manager: the
window kept the shorter height through the hold and after it.

This guard is such a window manager on a private Xvfb display. It maps the
browser window once it has asked for the identity's size, makes it SHORTER px
shorter, answers grows in the next CONSTRAIN_S seconds with SHORTER px less
(telling the browser the size it keeps, as ICCCM asks), and grants everything
after that. Whether the hold wins the race depends on startup timing: about
two launches in five from a fresh process on the unfixed build, and none of
the later launches in one process. So it runs LAUNCHES launches, each in its
own process; each window must read the identity's outer size once its hold
has run.

    python browser/tests/playwright/patches/startup-size-refused-resize.py
"""

import ctypes
import ctypes.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, launch_kwargs  # noqa: E402

OUTER = {"window.outerWidth": 1200, "window.outerHeight": 800}
SHORTER = 40
CONSTRAIN_S = 2
LAUNCHES = 6
# CamoufoxWindow.sys.mjs holds the startup size for 5 s from the window opening.
HOLD_S = 5
# The X window is the outer size less the frame Firefox draws itself.
WANT_MIN_HEIGHT = OUTER["window.outerHeight"] - 10

MAP_REQUEST, CONFIGURE_NOTIFY, CONFIGURE_REQUEST = 20, 22, 23
STRUCTURE_NOTIFY_MASK, SUBSTRUCTURE_REDIRECT_MASK = 1 << 17, 1 << 20
CW_X, CW_Y, CW_WIDTH, CW_HEIGHT = 1, 2, 4, 8


class XAnyEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong)]


class XMapRequestEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("parent", ctypes.c_ulong), ("window", ctypes.c_ulong)]


class XConfigureRequestEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("parent", ctypes.c_ulong), ("window", ctypes.c_ulong),
                ("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int),
                ("height", ctypes.c_int), ("border_width", ctypes.c_int), ("above", ctypes.c_ulong),
                ("detail", ctypes.c_int), ("value_mask", ctypes.c_ulong)]


class XConfigureEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("event", ctypes.c_ulong), ("window", ctypes.c_ulong),
                ("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int),
                ("height", ctypes.c_int), ("border_width", ctypes.c_int), ("above", ctypes.c_ulong),
                ("override_redirect", ctypes.c_int)]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xany", XAnyEvent), ("xmaprequest", XMapRequestEvent),
                ("xconfigurerequest", XConfigureRequestEvent), ("xconfigure", XConfigureEvent),
                ("pad", ctypes.c_long * 24)]


class XWindowChanges(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int),
                ("height", ctypes.c_int), ("border_width", ctypes.c_int),
                ("sibling", ctypes.c_ulong), ("stack_mode", ctypes.c_int)]


class XClassHint(ctypes.Structure):
    _fields_ = [("res_name", ctypes.c_char_p), ("res_class", ctypes.c_char_p)]


class XWindowAttributes(ctypes.Structure):
    # Only the leading geometry fields are read; the rest is padding.
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int),
                ("height", ctypes.c_int), ("border_width", ctypes.c_int), ("pad", ctypes.c_long * 32)]


class WindowManager(threading.Thread):
    """Maps each browser window SHORTER px short, constrains its grows for
    CONSTRAIN_S seconds, then grants everything. Its own connection, used only
    by this thread."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        self.lib.XOpenDisplay.restype = ctypes.c_void_p
        self.lib.XDefaultRootWindow.restype = ctypes.c_ulong
        dpy = self.lib.XOpenDisplay(os.environ["DISPLAY"].encode())
        if not dpy:
            raise SystemExit(f"FAIL: cannot open display {os.environ['DISPLAY']}")
        self.dpy = ctypes.c_void_p(dpy)
        self.root = self.lib.XDefaultRootWindow(self.dpy)
        self.lib.XSelectInput(self.dpy, ctypes.c_ulong(self.root), ctypes.c_long(SUBSTRUCTURE_REDIRECT_MASK))
        self.lib.XSync(self.dpy, False)
        self.stopping = threading.Event()
        self.waiting = {}  # browser window -> when to map it anyway
        self.mapped_at = {}  # browser window -> when it was mapped
        self.shrunk = 0

    def is_browser(self, window: int) -> bool:
        hint = XClassHint()
        return bool(self.lib.XGetClassHint(self.dpy, ctypes.c_ulong(window), ctypes.byref(hint))
                    and hint.res_name == b"Navigator")

    def geometry(self, window: int) -> list:
        attrs = XWindowAttributes()
        self.lib.XGetWindowAttributes(self.dpy, ctypes.c_ulong(window), ctypes.byref(attrs))
        return [attrs.x, attrs.y, attrs.width, attrs.height]

    def configure(self, window: int, mask: int, x=0, y=0, width=0, height=0) -> None:
        changes = XWindowChanges(x=x, y=y, width=width, height=height)
        self.lib.XConfigureWindow(self.dpy, ctypes.c_ulong(window), ctypes.c_uint(mask), ctypes.byref(changes))

    def tell_unchanged(self, window: int) -> None:
        """ICCCM 4.1.5: a request that changes nothing is answered with a
        synthetic ConfigureNotify, since the X server sends none."""
        x, y, width, height = self.geometry(window)
        event = XEvent()
        event.xconfigure = XConfigureEvent(type=CONFIGURE_NOTIFY, send_event=1, event=window, window=window,
                                           x=x, y=y, width=width, height=height)
        self.lib.XSendEvent(self.dpy, ctypes.c_ulong(window), False, ctypes.c_long(STRUCTURE_NOTIFY_MASK),
                            ctypes.byref(event))

    def stop(self) -> None:
        """Before the display goes: Xlib exits the process on a lost connection."""
        self.stopping.set()
        self.join()
        self.lib.XCloseDisplay(self.dpy)

    def map_shorter(self, window: int) -> None:
        self.lib.XMapWindow(self.dpy, ctypes.c_ulong(window))
        self.configure(window, CW_HEIGHT, height=self.geometry(window)[3] - SHORTER)
        self.lib.XSync(self.dpy, False)
        self.mapped_at[window] = time.monotonic()
        self.shrunk += 1

    def run(self) -> None:
        event = XEvent()
        while not self.stopping.is_set():
            # Map a browser window once it has asked for the identity's size,
            # as Openbox does with the size it was asked for before the map.
            for window, deadline in list(self.waiting.items()):
                if self.geometry(window)[3] >= WANT_MIN_HEIGHT or time.monotonic() > deadline:
                    del self.waiting[window]
                    self.map_shorter(window)
            if not self.lib.XPending(self.dpy):
                time.sleep(0.002)
                continue
            self.lib.XNextEvent(self.dpy, ctypes.byref(event))
            if event.type == MAP_REQUEST:
                window = event.xmaprequest.window
                if self.is_browser(window) and window not in self.mapped_at:
                    self.waiting[window] = time.monotonic() + 1
                else:
                    self.lib.XMapWindow(self.dpy, ctypes.c_ulong(window))
            elif event.type == CONFIGURE_REQUEST:
                req = event.xconfigurerequest
                before = self.geometry(req.window)
                height = req.height
                mapped = self.mapped_at.get(req.window)
                if mapped and time.monotonic() - mapped < CONSTRAIN_S and req.value_mask & CW_HEIGHT \
                        and req.height > before[3]:
                    height = req.height - SHORTER
                self.configure(req.window, req.value_mask & (CW_X | CW_Y | CW_WIDTH | CW_HEIGHT),
                               req.x, req.y, req.width, height)
                if self.geometry(req.window) == before:
                    self.tell_unchanged(req.window)
            self.lib.XSync(self.dpy, False)


def probe() -> tuple:
    from camoufox.sync_api import Camoufox

    with hidden_display(screen="1400x900"):
        wm = WindowManager()
        wm.start()
        try:
            with Camoufox(**launch_kwargs(headless=False, os="windows", config=OUTER,
                                          i_know_what_im_doing=True)) as browser:
                page = browser.new_page()
                time.sleep(HOLD_S + 1)
                reading = page.evaluate("[outerWidth, outerHeight, innerWidth, innerHeight]")
        finally:
            wm.stop()
        return wm.shrunk, reading


def once() -> int:
    shrunk, reading = probe()
    print(json.dumps({"windows shrunk": shrunk, "reading": reading}))
    return 0


def main() -> int:
    want = [OUTER["window.outerWidth"], OUTER["window.outerHeight"]]
    short = []
    for launch in range(LAUNCHES):
        run = subprocess.run([sys.executable, __file__, "--once"], capture_output=True, text=True, timeout=300)
        lines = [line for line in run.stdout.splitlines() if line.startswith("{")]
        if run.returncode or not lines:
            print(run.stdout + run.stderr)
            print(f"FAIL: launch {launch} did not finish")
            return 1
        result = json.loads(lines[-1])
        print(json.dumps({"launch": launch, **result}))
        if not result["windows shrunk"]:
            print("FAIL: the window manager never shrank the browser window, so the check proves nothing")
            return 1
        if result["reading"][:2] != want:
            short.append(result["reading"][:2])
    if short:
        print(f"FAIL: {len(short)} of {LAUNCHES} launches kept the window manager's size after the hold "
              f"({short}), not the identity's {want}")
        return 1
    print(f"PASS: {LAUNCHES} windows shrunk as they mapped all got the identity's outer size back")
    return 0


if __name__ == "__main__":
    raise SystemExit(once() if sys.argv[1:] == ["--once"] else main())
