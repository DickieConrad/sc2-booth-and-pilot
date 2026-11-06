"""timing.py helper-rule tests: judge() + the CONFIRM streak + Pusher.say's gaps, as the main loop runs them
(wall clock = game clock, like tools/rules_parity.py). Same cases as coach-core's helper.rs tests.
Run by ci.sh via tools/checks.py tests. Never touches the helper or out/."""

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import timing  # noqa: E402

PLAN = [
    {"sec": 0, "supply": 12, "action": "SCVs nonstop"},
    {"sec": 17, "supply": 14, "action": "Supply Depot"},
    {"sec": 40, "supply": 16, "action": "Barracks"},
    {"sec": 100, "supply": 20, "action": "Command Center (natural)"},
    {"sec": 180, "supply": 30, "action": "Factory"},
    {"sec": 300, "supply": 44, "action": "Starport"},
]


class Clock:
    now = 0.0

    @staticmethod
    def time():
        return Clock.now


class Helper:
    """The main loop's rule path for one game (no Rust, no I/O): tick(t, minerals, used, cap) -> said text or None."""

    def __init__(self):
        self.pusher = timing.Pusher.__new__(timing.Pusher)   # no __init__: it would create out/ dirs
        self.pusher.dry_run, self.pusher.last_say, self.pusher.recent = True, 0.0, {}
        self.hits = {}

    def tick(self, t, minerals, used=60, cap=70):
        Clock.now = t
        panel = {"minerals": minerals, "gas": 0, "used": used, "cap": cap}
        verdict = timing.judge(panel, t, PLAN)
        kind = verdict[0] if verdict else None
        self.hits = {kind: self.hits.get(kind, 0) + 1} if kind else {}
        if kind and self.hits[kind] >= timing.CONFIRM and self.pusher.say(
                verdict[1], kind, t, False, timing.repeat_gap(kind, minerals)):
            self.hits = {}
            return verdict[1]
        return None


@contextlib.contextmanager
def fake_clock():
    real = timing.time
    timing.time = Clock
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        timing.time = real


def test_repeat_gap():
    assert timing.repeat_gap("bank", 600) == timing.BANK_REPEAT_GAP == 25
    assert timing.repeat_gap("bank", 599) == timing.REPEAT_GAP == 45
    assert timing.repeat_gap("blocked", 2000) == timing.REPEAT_GAP


def test_bank_repeats_every_25_s_while_600_plus():
    with fake_clock():
        h = Helper()
        said = [(250 + i * 0.5, txt) for i in range(141) if (txt := h.tick(250 + i * 0.5, 800 + 5 * i))]
    assert [t for t, _ in said] == [250.5, 275.5, 300.5], said
    assert all(txt.startswith("Bank ") and txt.endswith(", spend") for _, txt in said), said   # same wording
    assert said[0][1] == "Bank 800, spend"


def test_bank_under_600_keeps_45_s():
    with fake_clock():
        h = Helper()
        said = [150 + i * 0.5 for i in range(121) if h.tick(150 + i * 0.5, 500, 30, 39)]
    assert said == [150.5, 195.5], said


def test_bank_gap_follows_the_current_pile():
    with fake_clock():
        h = Helper()
        assert not h.tick(150.0, 900, 30, 39) and h.tick(150.5, 900, 30, 39)
        assert not h.tick(170.0, 500, 30, 39) and not h.tick(170.5, 500, 30, 39)
        assert not h.tick(175.0, 650, 30, 39) and h.tick(175.5, 650, 30, 39)
        assert not h.tick(200.0, 500, 30, 39) and not h.tick(220.0, 500, 30, 39) and h.tick(220.5, 500, 30, 39)


def test_other_kinds_keep_45_s_and_the_say_gap():
    with fake_clock():
        h = Helper()
        said = [100 + i * 0.5 for i in range(121) if h.tick(100 + i * 0.5, 50, 23, 23)]
        assert said == [100.5, 145.5], said
        # a bank call right after another say still waits for the 15 s SAY_GAP
        assert not h.tick(150.0, 700, 20, 31) and not h.tick(155.0, 700, 20, 31) and h.tick(160.5, 700, 20, 31)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  test_timing: {len(tests)} tests ok")
