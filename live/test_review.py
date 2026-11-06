"""Post-game review tests (history.review + GET /history/review/<id>). Stdlib; run by ci.sh via tools/checks.py tests.
Synthetic games in a temp SQLite DB built from db.SCHEMA; the real coach.db is never opened."""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import history  # noqa: E402

GAME = {"id": 1, "started_at": 1793400000.0, "result": "Victory", "length_sec": 900.0, "plan": "A",
        "coach_plan": "A", "enemies": "Foe (Terr) + Bar (Zerg)", "matchup": "TZ"}


def clean(length=900):
    """A tidy game: reads every 5 s, supply grows 1 per 8 s on plan, 10 free supply, small banks."""
    out = []
    for t in range(5, length + 1, 5):
        used = min(190, 12 + t // 8)
        out.append({"t": t, "minerals": 150, "gas": 100, "used": used, "cap": used + 10, "plan_supply": used})
    return out


def patch(samples, a, b, **vals):
    """Set fields on reads with a <= t <= b; a value may be a function of t."""
    for s in samples:
        if a <= s["t"] <= b:
            for k, v in vals.items():
                s[k] = v(s["t"]) if callable(v) else v
    return samples


def rev(samples, **game):
    return history.review({**GAME, **game}, samples)


def test_clean_game():
    r = rev(clean())
    for k in ("behind", "blocks", "bank", "gas", "fights", "buildings", "idle", "findings"):
        assert r[k] == [], (k, r[k])
    assert r["top_fix"]["key"] == "none" and r["top_fix"]["also"] is None
    assert [(c["t"], c["diff"]) for c in r["checkpoints"]] == [(180, 0), (360, 0), (480, 0)]
    assert r["length_text"] == "15:00" and r["series"][0] == [5, 12, 12, 22]


def test_checkpoints_short_game():
    r = rev(clean(300), length_sec=300.0)
    assert [c["t"] for c in r["checkpoints"]] == [180]   # no reads near 6:00 / 8:00
    s = patch(clean(), 0, 900, plan_supply=None)
    assert all(c["plan"] is None and c["diff"] is None for c in rev(s)["checkpoints"])


def test_behind():
    s = patch(clean(), 180, 390, plan_supply=lambda t: min(190, 12 + t // 8) + 6)   # 6 behind 3:00-6:30
    s = patch(s, 600, 610, plan_supply=lambda t: min(190, 12 + t // 8) + 5)         # 15 s: too short to count
    r = rev(s)
    assert r["behind"] == [{"start": 180, "end": 390, "min": 6, "max": 6}]
    assert r["checkpoints"][1]["diff"] == -6
    assert r["findings"][0]["text"] == "Behind plan by 6 supply 3:00-6:30"


def test_blocks():
    s = clean()
    patch(s, 25, 40, used=15, cap=15)                 # the opening 15/15 wait: not a block
    patch(s, 440, 450, used=70, cap=70)               # 7:20-7:30, with an OCR miss in the middle
    patch(s, 445, 445, used=None, cap=None)
    patch(s, 600, 600, used=86, cap=86)               # one read
    patch(s, 700, 720, used=200, cap=200)             # maxed, not blocked
    r = rev(s)
    assert r["blocks"] == [{"start": 440, "end": 450, "used": 70, "cap": 70},
                           {"start": 600, "end": 600, "used": 86, "cap": 86}]
    texts = [f["text"] for f in r["findings"]]
    assert "Blocked 70/70 at 7:20-7:30" in texts and "Blocked 86/86 at 10:00" in texts


def test_bank_and_gas():
    s = clean()
    patch(s, 210, 330, minerals=lambda t: 400 + (t - 200) * 7)   # 3:30-5:30, peaks 1310
    patch(s, 420, 900, gas=lambda t: 300 + t, minerals=300)      # gas climbs past 400 (minerals fine)...
    patch(s, 420, 480, minerals=100)                             # ...gas-heavy 7:00-8:00
    patch(s, 500, 520, minerals=100)                             # a 20 s gap: still the same window
    patch(s, 600, 640, minerals=50)                              # a 80 s gap: a new window
    r = rev(s)
    assert [(w["start"], w["end"], w["peak"]) for w in r["bank"]] == [(215, 330, 1310)]
    assert [(w["start"], w["end"], w["peak"]) for w in r["gas"]] == [(420, 520, 820), (600, 640, 940)]
    assert r["gas_peak"] == 1200 and r["bank_peak"] == 1310
    texts = [f["text"] for f in r["findings"]]
    assert "Bank over 500 minerals 3:35-5:30 (peak 1310)" in texts
    assert "Gas banked 720-940 with minerals under 200: 7:00-8:40, 10:00-10:40 (150 s)" in texts


def test_lost_fights():
    s = clean()
    patch(s, 600, 900, used=132, cap=150)
    patch(s, 670, 680, used=lambda t: 132 - (t - 665) * 2)   # 132 -> 102 in 15 s, ...
    patch(s, 685, 900, used=111)                             # ...back up to 111 (reinforcements)
    patch(s, 800, 900, used=lambda t: 111 - (t - 800) // 10)   # slow bleed of 1 per 10 s: not a fight
    r = rev(s)
    assert r["fights"] == [{"start": 665, "end": 680, "before": 132, "after": 102}]
    assert "Lost a fight 11:05-11:20 (132->102)" in [f["text"] for f in r["findings"]]


def test_lost_buildings():
    s = clean()
    patch(s, 500, 900, used=90, cap=149)
    patch(s, 840, 900, cap=134)        # 14:00 lose a CC
    patch(s, 880, 900, cap=118)        # 40 s later two depots: same event
    patch(s, 700, 700, cap=146)        # one bad read 3 below: under a depot, ignored
    r = rev(s)
    assert r["buildings"] == [{"start": 840, "end": 880, "before": 149, "after": 118}]
    assert "Lost buildings 14:00-14:40 (cap 149->118)" in [f["text"] for f in r["findings"]]


def test_one_misread_is_not_a_loss():
    # an early defeat where OCR misread the cap once (70 -> 7 -> 70) and used once (60 -> 6 -> 60): no lost
    # buildings, no lost fight, so not the 1000-point "opening didn't hold"
    s = patch(clean(330), 200, 330, used=60, cap=70)
    patch(s, 250, 250, cap=7)
    patch(s, 300, 300, used=6)
    patch(s, 280, 280, cap=170)   # and a one-read spike up: no drop back down from it
    r = rev(s, result="Defeat")
    assert r["buildings"] == [] and r["fights"] == [], (r["buildings"], r["fights"])
    assert r["top_fix"]["key"] != "early", r["top_fix"]
    # a real loss is seen on two reads or more and still counts
    s = patch(clean(330), 200, 330, used=60, cap=70)
    patch(s, 250, 330, cap=54, used=40)
    r = rev(s, result="Defeat")
    assert r["buildings"] == [{"start": 250, "end": 250, "before": 70, "after": 54}]
    assert r["fights"] and r["top_fix"]["key"] == "early"


def test_idle():
    s = clean(1000)
    patch(s, 200, 290, used=40, cap=60)                        # flat but before 5:00
    patch(s, 600, 720, used=lambda t: 120 + (t // 5) % 3 - 1, cap=135)   # 119-121 for 2:00 with 15 free
    patch(s, 725, 1000, used=lambda t: 130 + (t - 725) // 4, cap=200)
    patch(s, 800, 900, used=150, cap=152)                      # flat but only 2 free: a block-ish, not idle
    r = rev(s)
    assert r["idle"] == [{"start": 600, "end": 720, "supply": 120, "room": 15}]
    assert "Production idle at 120 supply 10:00-12:00 (15 free)" in [f["text"] for f in r["findings"]]
    s = patch(clean(), 600, 680, used=120, cap=135)            # 80 s: too short
    assert rev(s)["idle"] == []


def test_top_fix():
    # supply: blocks from 6:00 on -> two depots every 30 s
    s = patch(clean(), 400, 430, used=70, cap=70)
    tf = rev(s)["top_fix"]
    assert tf["key"] == "supply" and tf["action"] == "From 6:00, two depots every 30 s"
    assert tf["scores"]["supply"] == 3 * 35 + 20
    tf = rev(patch(clean(), 200, 230, used=30, cap=30))["top_fix"]
    assert tf["key"] == "supply" and tf["action"] == "Next depot when supply is 6 below the cap"
    # gas: 300 s gas-heavy (150 points) beats one short block (3 * 5 + 20 = 35); over 800 gas -> 6 SCVs
    s = patch(clean(), 400, 695, minerals=100, gas=lambda t: t + 200)
    patch(s, 500, 500, used=74, cap=74)
    tf = rev(s)["top_fix"]
    assert tf["key"] == "gas" and tf["action"] == "Gas over 400: 6 SCVs to minerals" and tf["also"] is None
    # bank: 2:00 over 500 after 4:00; the runner-up (blocks, >= half) rides along
    s = patch(clean(), 300, 415, minerals=900)
    patch(s, 500, 510, used=74, cap=74)
    tf = rev(s)["top_fix"]
    assert tf["key"] == "bank" and tf["action"] == "Spend the bank: more production"
    assert tf["also"] == {"key": "supply", "title": "Supply blocks", "action": "From 6:00, two depots every 30 s"}
    # production: idle + lost fights + behind at 8:00
    s = patch(clean(), 470, 490, plan_supply=90)
    patch(s, 600, 900, used=lambda t: 120 if t < 700 else 100, cap=130)
    tf = rev(s)["top_fix"]
    assert tf["key"] == "production" and tf["action"] == "5 Barracks + 2 Factories by 8:00"
    assert "lost fight" in tf["why"] and "behind plan at 8:00" in tf["why"]
    # early: a loss with buildings gone before 6:00 beats everything
    s = patch(clean(310), 200, 310, cap=lambda t: 39 if t < 270 else 15, used=lambda t: 30 if t < 270 else 12,
              minerals=1100)
    tf = rev(s, result="Defeat")["top_fix"]
    assert tf["key"] == "early" and tf["action"].startswith("Bunker at the natural by 2:30")
    assert "buildings at 4:30 (cap 39->15)" in tf["why"]
    assert rev(s, result="Victory")["top_fix"]["key"] != "early"   # survived it: not the opening's fault


def test_top_fix_ties():
    for k in ("early", "supply", "production", "gas", "bank"):
        assert k in history.FIXES
    f = {"checkpoints": [], "blocks": [], "fights": [], "buildings": [], "idle": [], "bank": [],
         "gas": [{"start": 400, "end": 435, "low": 500, "peak": 500}], "bank_peak": None}
    f["blocks"] = [{"start": 400, "end": 400, "used": 70, "cap": 70}]   # supply 35 vs gas 0.5 * 40 = 20
    assert history.top_fix(f, "Victory")["key"] == "supply"
    f["gas"][0]["end"] = 465   # gas 0.5 * 70 = 35: a tie, FIXES order picks supply
    assert history.top_fix(f, "Victory")["key"] == "supply"


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    for gid, res in ((1, "Victory"), (2, "Defeat")):
        conn.execute("INSERT INTO games (id, started_at, result, length_sec, plan, coach_plan, enemies) "
                     "VALUES (?, 1793400000, ?, 900, 'A', 'A', 'Foe (Terr)')", (gid, res))
    s = patch(clean(), 400, 430, used=70, cap=70)
    conn.executemany("INSERT INTO samples VALUES (1, ?, ?, ?, ?, ?, ?)",
                     [(x["t"], x["minerals"], x["gas"], x["used"], x["cap"], x["plan_supply"]) for x in s])
    conn.execute("INSERT INTO calls VALUES (1, 400, 'helper', 'blocked', 'Blocked', 0)")
    conn.execute("INSERT INTO notes (game_id, created_at, text) VALUES (1, 0, 'Game 1 review: depots')")
    conn.commit()
    conn.close()


def test_route(dbp):
    code, body, ctype, cache = history.route("/history/review/1", dbp)
    r = json.loads(body)
    assert code == 200 and ctype == "application/json" and cache == "no-store"
    assert r["id"] == 1 and r["top_fix"]["key"] == "supply" and r["calls"] == {"blocked": 1}
    assert r["hand_review"] == "Game 1 review: depots"
    code, body, *_ = history.route("/history/review/latest", dbp)
    assert code == 200 and json.loads(body)["id"] == 2 and json.loads(body)["series"] == []
    for bad in ("/history/review/abc", "/history/review/-1", "/history/review/1.5", "/history/review/",
                "/history/review/1/x", "/history/review/12345678901"):
        code, body, ctype, _ = history.route(bad, dbp)
        assert code == 400 and ctype == "application/json" and "error" in json.loads(body), bad
    code, body, *_ = history.route("/history/review/999", dbp)
    assert code == 404 and json.loads(body) == {"error": "no game 999"}
    code, body, *_ = history.route("/history/review/latest", Path(dbp).with_name("missing.db"))
    assert code == 500 and "error" in json.loads(body)


def test_latest_empty(dbp):
    empty = Path(dbp).with_name("empty.db")
    conn = sqlite3.connect(empty)
    conn.executescript(db.SCHEMA)
    conn.close()
    assert history.route("/history/review/latest", empty)[0] == 404


def test_read_only(dbp):
    before = Path(dbp).read_bytes()
    for p in ("/history/review/1", "/history/review/latest", "/history/review/999"):
        history.route(p, dbp)
    assert Path(dbp).read_bytes() == before


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        dbp = Path(d) / "test.db"
        make_db(dbp)
        tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
        for t in tests:
            if t.__code__.co_argcount:
                t(dbp)
            else:
                t()
    print(f"  test_review: {len(tests)} tests ok")
