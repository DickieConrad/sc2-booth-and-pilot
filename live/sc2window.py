"""Where the SC2 window is, so the resource-panel crop follows it (bot games run SC2 windowed, anywhere on screen).

Read-only: it only asks Windows for the window's client-area geometry (FindWindowW / GetClientRect /
ClientToScreen). It never moves, resizes or focuses the window and never sends input.

Fullscreen is unchanged: the fullscreen client area is the whole primary screen at (0, 0), so the crop is exactly
the fixed panel_box(screen). No window found (or minimised, tiny, or off the primary screen) -> the fixed region.
"""
import sys
import time
from collections.abc import Callable

Rect = tuple[int, int, int, int]   # client area: left, top, width, height (screen pixels)
Box = tuple[int, int, int, int]    # crop: x0, y0, x1, y1 (screen pixels, x1/y1 exclusive)
Frac = tuple[float, float, float, float]

TITLE = "StarCraft II"     # both the window class and the title of the game window
REFRESH = 3.0              # seconds between window lookups (a lookup is a few microseconds; the read loop is 0.5 s)
MIN_CLIENT = (640, 360)    # smaller client areas (a launcher, a minimised stub) are not the game


def panel_in_client(client: Rect, frac: Frac) -> Box:
    """The panel crop for a client area: the same fractions panel_box applies to a fullscreen client
    (x by client width, y by client height), offset by the client's screen position."""
    left, top, w, h = client
    x0, y0, x1, y1 = frac
    return left + int(w * x0), top + int(h * y0), left + int(w * x1), top + int(h * y1)


def choose_box(client: Rect | None, screen: tuple[int, int], frac: Frac) -> tuple[Box, str]:
    """(crop, source): source "window" when the crop follows the SC2 client area, "screen" for the fixed region.
    The crop must lie inside the primary screen (the capture output); otherwise the fixed region is used."""
    sw, sh = screen
    fixed = panel_in_client((0, 0, sw, sh), frac)
    if client is None or client[2] < MIN_CLIENT[0] or client[3] < MIN_CLIENT[1]:
        return fixed, "screen"
    box = panel_in_client(client, frac)
    if box[0] < 0 or box[1] < 0 or box[2] > sw or box[3] > sh or box[2] <= box[0] or box[3] <= box[1]:
        return fixed, "screen"
    return box, "window"


def find_client() -> Rect | None:
    """The SC2 game window's client area in physical screen pixels, or None (no window, minimised, not Windows).
    Queries run per-monitor DPI aware on this thread only, so a scaled display still gives real pixels."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.IsIconic.argtypes = user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    set_ctx = getattr(user32, "SetThreadDpiAwarenessContext", None)   # Windows 10 1607+
    old = None
    if set_ctx is not None:
        set_ctx.argtypes, set_ctx.restype = [ctypes.c_void_p], ctypes.c_void_p
        old = set_ctx(ctypes.c_void_p(-4))   # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    try:
        hwnd = user32.FindWindowW(TITLE, None)
        if not hwnd:   # class not matched: fall back to the title, but not an Explorer folder named "StarCraft II"
            hwnd = user32.FindWindowW(None, TITLE)
            cls = ctypes.create_unicode_buffer(64)
            if hwnd and user32.GetClassNameW(hwnd, cls, 64) and cls.value in ("CabinetWClass", "ExploreWClass"):
                hwnd = None
        if not hwnd or user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
            return None
        rc = wintypes.RECT()
        pt = wintypes.POINT(0, 0)
        if not user32.GetClientRect(hwnd, ctypes.byref(rc)) or not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        return int(pt.x), int(pt.y), int(rc.right - rc.left), int(rc.bottom - rc.top)
    finally:
        if set_ctx is not None and old:
            set_ctx(ctypes.c_void_p(old))


class WindowTracker:
    """The current panel crop, looked up again every REFRESH seconds (cached in between)."""

    def __init__(self, screen: tuple[int, int], frac: Frac, find: Callable[[], Rect | None] = find_client,
                 clock: Callable[[], float] = time.monotonic, refresh: float = REFRESH,
                 log: Callable[[str], None] | None = None) -> None:
        self.screen, self.frac, self.find, self.clock, self.refresh = screen, frac, find, clock, refresh
        self.log = log if log is not None else (lambda s: print(s, flush=True))
        self.client: Rect | None = None
        self.source = ""
        self.next = float("-inf")
        self._box: Box = choose_box(None, screen, frac)[0]

    def box(self) -> Box:
        now = self.clock()
        if now < self.next:
            return self._box
        self.next = now + self.refresh
        try:
            client = self.find()
        except Exception:
            client = None
        box, source = choose_box(client, self.screen, self.frac)
        if box != self._box or source != self.source:
            if source == "window" and client is not None:
                self.log(f"panel: SC2 window {client[2]}x{client[3]} at ({client[0]},{client[1]}) -> crop {box}")
            else:
                why = "no SC2 window" if client is None else f"SC2 window {client} unusable"
                self.log(f"panel: fixed region {box} ({why})")
        self.client, self._box, self.source = client, box, source
        return box
