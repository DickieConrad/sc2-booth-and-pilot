"""tools/import_test_games.py tests: temp copies shaped like live/coach-test.db and an older coach.db (stdlib; run by
ci.sh via tools/checks.py tests). The real databases are never opened."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["SC2_PLAYER"] = "TestPilot"   # the owner's name for these tests, whatever live/player.json says
sys.path[:0] = [str(HERE), str(HERE.parent / "live")]
import db  # noqa: E402
import import_test_games as imp  # noqa: E402

OLD_GAMES = """
CREATE TABLE games (id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, me TEXT, ally TEXT,
    ally_race TEXT, enemies TEXT, matchup TEXT, plan TEXT, coach_plan TEXT, result TEXT, length_sec REAL,
    source TEXT DEFAULT 'helper');
"""


def old(path, games, notes=(), samples=(), calls=()):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_GAMES)
    conn.executescript(db.SCHEMA)
    conn.executemany("INSERT INTO games (id, started_at, me, ally, ally_race, enemies, plan, coach_plan, result, "
                     "length_sec) VALUES (?, ?, 'TestPilot', ?, ?, ?, 'A', 'A', ?, ?)", games)
    conn.executemany("INSERT INTO notes (id, game_id, created_at, author, text) VALUES (?, ?, ?, 'coach', ?)", notes)
    conn.executemany("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)", samples)
    conn.executemany("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?)", calls)
    conn.commit()
    conn.close()


def make(d):
    src, dst = Path(d) / "coach-test.db", Path(d) / "coach.db"
    # ids overlap with the main file on purpose (game 31, note 348 exist in both)
    old(src,
        [(31, 1793408505.0, "SixRax, Player 2", "Terr, random", "?", "Unknown", 309.0),
         (33, 1793409309.0, "DtRush, A.I. 1 (Medium)", "Prot, Terr", "?", "Unknown", 423.0),
         (35, 1793410307.0, "Someone, Else, Third", "Terr, Zerg, Prot", "?", "Undecided", None),
         (36, 1793410400.0, "Mystery", "Zerg", "?", "Unknown", 100.0)],
        notes=[(348, 31, 1793408512.0, "TEST GAME: sparring bot sixrax vs Easy AI"),
               (349, 33, 1793409746.0, "TEST GAME: sparring bot dt vs medium AI (terran)"),
               (350, 35, 1793410310.0, "TEST GAME: sparring bot proxy vs hard AI")],
        samples=[(31, 5.0, 50, 0, 12, 15, None), (31, 10.0, 60, 0, 13, 15, None), (33, 5.0, 50, 0, 12, 15, 12)],
        calls=[(31, 0.0, "helper", "plan", "Plan A, Steel & Blink.", 1.0), (33, 60.0, "helper", "bank", "Bank", 2.0)])
    old(dst, [(31, 1793300000.0, "", "", "Foe (Zerg)", "Victory", 600.0)],
        notes=[(348, 31, 1793300600.0, "Dickie's own note")])
    return src, dst


def rows(path, sql):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute(sql)]
    conn.close()
    return out


def test_players():
    g = {"ally": "SixRax, A.I. 1 (Medium)", "ally_race": "Terr, random", "enemies": "?"}
    assert imp.bot_player(g, []) == "SixRax"
    assert imp.bot_player({**g, "ally": "Player 2"}, ["TEST GAME: sparring bot dt vs x"]) == "DtRush"
    assert imp.bot_player({**g, "ally": "Player 2"}, ["no hint"]) == "bot"
    f = imp.fixed_game({"id": 1, **g, "me": "TestPilot", "matchup": None}, "SixRax")
    assert (f["me"], f["ally"], f["enemies"], f["matchup"], f["player"]) == (
        "SixRax", "", "A.I. 1 (Medium) (random)", "?", "SixRax")
    assert "id" not in f


def test_dry_run(d):
    src, dst = make(d)
    before = dst.read_bytes()
    assert imp.main(["--src", str(src), "--dst", str(dst), "--dry-run"]) == 0
    assert dst.read_bytes() == before and not list(Path(d).glob("coach.db.bak-*"))
    todo = imp.plan(src, dst)
    assert [(t["id"], t["player"]) for t in todo] == [(31, "SixRax"), (33, "DtRush"), (35, "ProxyRax"), (36, "bot")]


def test_import(d):
    src, dst = make(d)
    assert imp.main(["--src", str(src), "--dst", str(dst)]) == 0
    assert len(list(Path(d).glob("coach.db.bak-*"))) == 1   # backup first
    games = rows(dst, "SELECT * FROM games ORDER BY id")
    assert [(g["id"], g["player"]) for g in games] == [
        (31, "TestPilot"), (32, "SixRax"), (33, "DtRush"), (34, "ProxyRax"), (35, "bot")]   # migrated + remapped
    six = games[1]
    assert (six["me"], six["enemies"], six["matchup"], six["ally"]) == ("SixRax", "Player 2 (random)", "?", "")
    assert games[2]["enemies"] == "A.I. 1 (Medium) (Terr)" and games[2]["matchup"] == "T"
    assert games[3]["ally"] == "Someone, Else, Third"   # not a 1v1 with a known bot: left as it was
    assert rows(dst, "SELECT game_id, count(*) AS n FROM samples GROUP BY game_id") == [
        {"game_id": 32, "n": 2}, {"game_id": 33, "n": 1}]
    assert [c["game_id"] for c in rows(dst, "SELECT * FROM calls ORDER BY rowid")] == [32, 33]
    notes = rows(dst, "SELECT * FROM notes ORDER BY id")
    assert [(n["id"], n["game_id"]) for n in notes] == [(348, 31), (349, 32), (350, 33), (351, 34)]
    assert notes[0]["text"] == "Dickie's own note"
    # twice: nothing new, no second backup needed for a no-op
    assert imp.main(["--src", str(src), "--dst", str(dst)]) == 0
    assert len(rows(dst, "SELECT id FROM games")) == 5 and len(list(Path(d).glob("coach.db.bak-*"))) == 1


def test_missing_source(d):
    assert imp.main(["--src", str(Path(d) / "none.db"), "--dst", str(Path(d) / "x.db")]) == 0
    assert not (Path(d) / "x.db").exists()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        if t.__code__.co_argcount:
            with tempfile.TemporaryDirectory() as d:
                t(d)
        else:
            t()
    print(f"  test_import_test_games: {len(tests)} tests ok")
