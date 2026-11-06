"""watch.Watcher tests: synthetic /state sequences for every event, the hysteresis and the heartbeat.
Stdlib; run by ci.sh via tools/checks.py tests. Never touches the helper."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch  # noqa: E402


def st(t, used=40, cap=60, mins=100, gas=50, plan=None, in_game=True):
    return {
        "in_game": in_game,
        "clock": t,
        "plan": "A",
        "vs": "PZ",
        "plan_supply": used if plan is None else plan,
        "panel": {"minerals": mins, "gas": gas, "used": used, "cap": cap},
        "next": [{"action": "Depot"}, {"action": "Rax"}],
        "says": [{"t": "1:00", "text": "depot"}],
    }


def run(samples, heartbeat=10_000, mode="events"):
    """Feed samples after a game start; return the event kinds (excluding WATCH lines)."""
    w = watch.Watcher(mode, heartbeat)
    lines = []
    for s in samples:
        lines += w.feed(s)
    return lines


def kinds(lines, kind):
    return [ln for ln in lines if f"] {kind} " in ln]


def test_start_end():
    lines = run([st(0), st(2), st(4, in_game=False)])
    assert lines[0] == "WATCH start: plan A vs PZ" and lines[-1] == "WATCH end"
    assert len(lines) == 2


def test_start_line_names_the_player():
    bot = {**st(0), "player": "SixRax", "plan": "BOT", "vs": "Computer (Zerg)"}
    lines = run([bot, {**bot, "clock": 2}])
    assert lines == ["WATCH start: plan BOT vs Computer (Zerg), player SixRax"], lines


def test_supply_block_once():
    seq = [st(t, used=60, cap=60) for t in range(100, 130, 2)]
    lines = run([st(98)] + seq + [st(130, used=60, cap=68), st(132, used=68, cap=68)])
    blk = kinds(lines, "SUPPLY_BLOCK")
    assert len(blk) == 2, blk
    assert blk[0].startswith("[1:40] SUPPLY_BLOCK 60/60 | min 100 gas 50 supply 60/60 plan 60 | last call: 1:00 depot")


def test_no_block_at_200():
    assert not kinds(run([st(t, used=200, cap=200) for t in range(600, 640, 2)]), "SUPPLY_BLOCK")


def test_supply_drop_once():
    seq = [st(t, used=100, cap=150) for t in range(400, 420, 2)]
    seq += [st(420 + i * 2, used=100 - 3 * i, cap=150) for i in range(1, 11)]  # 97 .. 70
    lines = run(seq)
    d = kinds(lines, "SUPPLY_DROP")
    assert len(d) == 2, d  # 100->90 then a fresh 10 from 90 (the window restarts after firing)
    assert "SUPPLY_DROP 100->88 in 20s" in d[0]


def test_slow_decline_no_drop():
    seq = [st(400 + i * 4, used=100 - i, cap=150) for i in range(12)]  # -1 per 4 s = -5 per 20 s
    assert not kinds(run(seq), "SUPPLY_DROP")


def test_lost_buildings():
    seq = [st(t, used=50, cap=100) for t in range(500, 520, 2)]
    seq += [st(530, used=50, cap=92), st(540, used=50, cap=92)]
    lines = run(seq)
    lb = kinds(lines, "LOST_BUILDINGS")
    assert len(lb) == 1 and "cap 100->92 in 60s" in lb[0], lb
    assert not kinds(run([st(500, cap=100), st(570, cap=92)]), "LOST_BUILDINGS")  # outside the window


def test_bank_hysteresis():
    # 790/810 around the line is one pile (seen at 2:49 and 2:55 in a bot game); re-armed only below 600
    seq = [st(200, mins=850), st(202, mins=900), st(204, mins=790), st(206, mins=810), st(208, mins=820)]
    b = kinds(run(seq), "BANK")
    assert len(b) == 1 and "BANK 850 minerals" in b[0], b
    seq += [st(210, mins=550), st(212, mins=850)]
    assert len(kinds(run(seq), "BANK")) == 2


def test_gas_bank():
    seq = [st(200, mins=300, gas=700), st(202, mins=150, gas=700), st(204, mins=50, gas=750)]
    seq += [st(206, mins=50, gas=500), st(208, mins=50, gas=650)]   # 500 is still within REARM_GAP of 600
    g = kinds(run(seq), "GAS_BANK")
    assert len(g) == 1 and "GAS_BANK 700 gas, 150 minerals" in g[0], g
    seq += [st(210, mins=50, gas=350), st(212, mins=50, gas=650)]   # below 400: re-armed, fires again
    assert len(kinds(run(seq), "GAS_BANK")) == 2


def test_behind_plan_hysteresis():
    seq = [st(300, used=50, plan=57), st(302, used=50, plan=58), st(304, used=50, plan=60)]
    seq += [st(306, used=55, plan=60), st(308, used=56, plan=60), st(310, used=52, plan=60)]
    b = kinds(run(seq), "BEHIND_PLAN")
    assert len(b) == 2 and "50 vs plan 58 (-8)" in b[0], b


def test_idle_production():
    seq = [st(t, used=80 + (t // 2) % 3 - 1, cap=100) for t in range(300, 420, 2)]  # wobbles 79..81
    i = kinds(run(seq), "IDLE_PRODUCTION")
    assert len(i) == 1 and i[0].startswith("[6:30] IDLE_PRODUCTION"), i
    # growing supply resets the stretch, and a new flat stretch fires again
    seq += [st(420, used=90, cap=110)] + [st(t, used=90, cap=110) for t in range(422, 520, 2)]
    assert len(kinds(run(seq), "IDLE_PRODUCTION")) == 2


def test_idle_needs_free_supply_and_5min():
    assert not kinds(run([st(t, used=95, cap=100) for t in range(300, 420, 2)]), "IDLE_PRODUCTION")
    assert not kinds(run([st(t, used=30, cap=60) for t in range(100, 290, 2)]), "IDLE_PRODUCTION")


def test_heartbeat():
    lines = run([st(t, used=20 + t // 10, cap=200) for t in range(0, 181, 2)], heartbeat=60)
    hb = kinds(lines, "HEARTBEAT")
    assert [h.split("]")[0] for h in hb] == ["[1:00", "[2:00", "[3:00"], hb
    assert hb[0].endswith("| next: Depot, Rax | last call: 1:00 depot")


def test_event_resets_heartbeat():
    seq = [st(t, cap=200) for t in range(0, 50, 2)] + [st(50, mins=900, cap=200)]
    seq += [st(t, mins=900, cap=200) for t in range(52, 112, 2)]
    hb = kinds(run(seq, heartbeat=60), "HEARTBEAT")
    assert [h.split("]")[0] for h in hb] == ["[1:50"], hb


def test_snapshot_mode():
    lines = run([st(t) for t in range(0, 91, 2)], mode="snapshots")
    snaps = [ln for ln in lines if ln.startswith("[")]
    assert [s.split("]")[0] for s in snaps] == ["[0:00", "[0:30", "[1:00", "[1:30"], snaps
    assert snaps[0] == "[0:00] min 100 gas 50 supply 40/60 plan 40 | next: Depot, Rax | last call: 1:00 depot"


def test_new_game_resets():
    lines = run([st(200, mins=900), st(202, in_game=False), st(0, mins=900)])
    assert len(kinds(lines, "BANK")) == 2


def test_missing_panel():
    lines = run([{"in_game": True, "clock": 10}, {"in_game": True, "clock": 80}], heartbeat=60)
    assert kinds(lines, "HEARTBEAT")


def test_opening_wait_is_not_a_block():
    # 15/15 waiting on the first depot (seen at 0:26 in a bot test game) is normal, not a supply block
    lines = run([st(t, used=15, cap=15) for t in range(20, 40, 2)] + [st(70, used=23, cap=23)])
    assert len(kinds(lines, "SUPPLY_BLOCK")) == 1 and "[1:10]" in kinds(lines, "SUPPLY_BLOCK")[0]


def test_fight_under_same_cap_does_not_refire_block():
    # 47/47 at 3:55, Marines die (45/47) and are rebuilt (47/47) at 4:09: one block, not two
    seq = [st(t, used=47, cap=47) for t in range(235, 245, 2)] + [st(t, used=45, cap=47) for t in range(245, 249, 2)]
    seq += [st(t, used=47, cap=47) for t in range(249, 256, 2)]
    assert len(kinds(run(seq), "SUPPLY_BLOCK")) == 1
    # but clear for 15 s+ under the same cap, then blocked again: a new block
    later = seq + [st(t, used=40, cap=47) for t in range(256, 276, 2)] + [st(276, used=47, cap=47)]
    assert len(kinds(run(later), "SUPPLY_BLOCK")) == 2


def test_clock_rewind_is_a_new_game():
    # game 1 ends at 8:00 with 150/200; the 2 s polls miss the gap and the next poll is game 2 at 0:03 (in_game
    # never went false). Old samples must not read as a lost fight / lost buildings, and heartbeats must resume.
    g1 = [st(t, used=150, cap=200) for t in range(440, 481, 2)]
    g2 = [st(t, used=12, cap=15) for t in range(3, 130, 2)]
    lines = run(g1 + g2, heartbeat=60)
    assert lines.count("WATCH start: plan A vs PZ") == 2, lines[:3]
    assert not kinds(lines, "SUPPLY_DROP") and not kinds(lines, "LOST_BUILDINGS"), lines
    hb = [h.split("]")[0] for h in kinds(lines, "HEARTBEAT")]
    assert hb == ["[7:20", "[1:01", "[2:01"], hb   # game 2's heartbeats count from its own 0:00
    # a small jitter back (<= 5 s) is not a new game
    assert run([st(100), st(102), st(98), st(100)]).count("WATCH start: plan A vs PZ") == 1


def cp1252_stream():
    """A strict cp1252 text stream, like stdout on a Windows console."""
    import io
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)


def test_utf8_stdio_survives_non_cp1252_text():
    """The "≈" in a plan step raised UnicodeEncodeError on the cp1252 console; after utf8_stdio() it prints."""
    raw, out = cp1252_stream()
    try:
        print("next: Rax 2 ≈ 1:30", file=out)
        raise AssertionError("cp1252 should not encode ≈")
    except UnicodeEncodeError:
        pass
    raw2, out2 = cp1252_stream()
    _, err2 = cp1252_stream()
    real = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out2, err2
    try:
        watch.utf8_stdio()
        print("next: Rax 2 ≈ 1:30", flush=True)
    finally:
        sys.stdout, sys.stderr = real
    assert out2.encoding == "utf-8" and err2.encoding == "utf-8"
    assert raw2.getvalue().decode("utf-8") == "next: Rax 2 ≈ 1:30\n", raw2.getvalue()
    # a replaced stream without reconfigure (e.g. a test's StringIO) is left alone
    import io
    sys.stdout = io.StringIO()
    try:
        watch.utf8_stdio()
    finally:
        sys.stdout = real[0]


def test_main_prints_non_ascii_on_a_cp1252_console():
    """main() end to end on a cp1252 stdout: a /state whose player name has "≈" reaches the feed as UTF-8."""
    import json

    class Stop(Exception):
        pass

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    s = st(100)
    del s["says"]
    s["player"] = "Dickie ≈ Terran"
    s["next"] = [{"sec": 120, "t": "2:00", "action": "Factory ≈ 2:00"}]
    body = json.dumps(s).encode("utf-8")
    raw, out = cp1252_stream()
    real = sys.stdout, watch.urllib.request.urlopen, watch.time.sleep

    def stop(_):
        raise Stop

    sys.stdout = out
    watch.urllib.request.urlopen = lambda *a, **k: Resp(body)
    watch.time.sleep = stop
    try:
        watch.main([])
    except Stop:
        pass
    finally:
        sys.stdout, watch.urllib.request.urlopen, watch.time.sleep = real
    assert "player Dickie ≈ Terran" in raw.getvalue().decode("utf-8"), raw.getvalue()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  test_watch: {len(tests)} tests ok")
