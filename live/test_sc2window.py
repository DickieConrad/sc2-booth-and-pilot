"""Panel crop from the SC2 window's client area (stdlib; run by ci.sh via tools/checks.py tests).
Only the pure math and the cache: find_client (Windows API) is replaced by a fake."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sc2window  # noqa: E402
from sc2window import choose_box, panel_in_client  # noqa: E402

PANEL = (0.755, 0.008, 0.975, 0.042)    # timing.PANEL (timing itself needs pydantic; checked equal below)


def fixed_box(w, h):
    """Today's fixed region: timing.panel_box / panel_ocr.panel_box."""
    x0, y0, x1, y1 = PANEL
    return int(w * x0), int(h * y0), int(w * x1), int(h * y1)


def test_panel_constant_matches_timing():
    src = (Path(__file__).resolve().parent / "timing.py").read_text(encoding="utf-8")
    assert f"PANEL = {PANEL}" in src


def test_fullscreen_is_the_fixed_region():
    for w, h in ((2560, 1440), (1920, 1080), (3840, 2160), (1280, 720), (3440, 1440), (1680, 1050)):
        box, src = choose_box((0, 0, w, h), (w, h), PANEL)
        assert box == fixed_box(w, h), (w, h, box)
        assert src == "window"
    assert fixed_box(2560, 1440) == (1932, 11, 2496, 60)


def test_no_window_is_the_fixed_region():
    assert choose_box(None, (2560, 1440), PANEL) == (fixed_box(2560, 1440), "screen")


def test_windowed_follows_the_client_area():
    # a 1920x1080 client whose top-left sits at (300, 200) on a 2560x1440 screen (title bar above it)
    box, src = choose_box((300, 200, 1920, 1080), (2560, 1440), PANEL)
    assert src == "window"
    fx0, fy0, fx1, fy1 = fixed_box(1920, 1080)
    assert box == (300 + fx0, 200 + fy0, 300 + fx1, 200 + fy1) == (1749, 208, 2172, 245)
    # the crop sits inside the client area, below its top edge (not the title bar)
    assert box[1] >= 200 and box[3] <= 200 + 1080 and box[0] >= 300 and box[2] <= 300 + 1920


def test_crop_scales_with_the_client():
    a = panel_in_client((0, 0, 2560, 1440), PANEL)
    b = panel_in_client((0, 0, 1280, 720), PANEL)
    assert abs((a[2] - a[0]) / 2 - (b[2] - b[0])) <= 1 and abs((a[3] - a[1]) / 2 - (b[3] - b[1])) <= 1
    assert panel_in_client((10, 20, 1280, 720), PANEL) == (b[0] + 10, b[1] + 20, b[2] + 10, b[3] + 20)


def test_unusable_windows_fall_back():
    screen = (2560, 1440)
    fixed = fixed_box(*screen)
    assert choose_box((0, 0, 0, 0), screen, PANEL) == (fixed, "screen")             # minimised stub
    assert choose_box((100, 100, 400, 300), screen, PANEL) == (fixed, "screen")     # too small to be the game
    assert choose_box((2560, 0, 1920, 1080), screen, PANEL) == (fixed, "screen")    # on a second monitor (right)
    assert choose_box((-1920, 0, 1920, 1080), screen, PANEL) == (fixed, "screen")   # on a second monitor (left)
    assert choose_box((1500, 100, 1920, 1080), screen, PANEL) == (fixed, "screen")  # panel hangs off the right edge
    # partly off screen but the panel itself is visible: follow it
    assert choose_box((-200, 300, 1920, 1080), screen, PANEL)[1] == "window"


class Fake:
    def __init__(self, rects):
        self.rects, self.calls, self.t = list(rects), 0, 0.0

    def find(self):
        self.calls += 1
        r = self.rects.pop(0) if self.rects else None
        if isinstance(r, Exception):
            raise r
        return r

    def clock(self):
        return self.t


def test_tracker_caches_and_refreshes():
    f, logs = Fake([(300, 200, 1920, 1080), None, RuntimeError("user32 hiccup")]), []
    tr = sc2window.WindowTracker((2560, 1440), PANEL, find=f.find, clock=f.clock, refresh=3.0, log=logs.append)
    assert tr.box() == (1749, 208, 2172, 245) and tr.source == "window" and f.calls == 1
    f.t = 2.9
    assert tr.box() == (1749, 208, 2172, 245) and f.calls == 1          # cached between refreshes
    f.t = 3.0
    assert tr.box() == fixed_box(2560, 1440) and tr.source == "screen" and f.calls == 2   # window gone
    f.t = 6.5
    assert tr.box() == fixed_box(2560, 1440) and f.calls == 3           # a failing lookup is "no window"
    assert len(logs) == 2 and "SC2 window 1920x1080 at (300,200)" in logs[0] and "fixed region" in logs[1]


def test_tracker_starts_on_the_fixed_region():
    tr = sc2window.WindowTracker((1920, 1080), PANEL, find=lambda: None, clock=lambda: 0.0, log=lambda s: None)
    assert tr._box == fixed_box(1920, 1080)


def test_find_client_off_windows_is_none():
    if sys.platform != "win32":
        assert sc2window.find_client() is None


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
    print(f"{len(tests)} tests passed")
