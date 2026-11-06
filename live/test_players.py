"""Players (games.player) and sparring-bot games: the DB migration, history/review filtered by player, and the helper
following an announced bot (bot_game.json -> follow / plan BOT / no relay). Stdlib + pydantic; run by ci.sh via
tools/checks.py tests. Temp files only: the real coach.db, live/out and the running helper are never touched."""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

os.environ["SC2_PLAYER"] = "TestPilot"   # the owner's name for these tests, whatever live/player.json says
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import history  # noqa: E402
import models  # noqa: E402
import player as player_cfg  # noqa: E402
import timing  # noqa: E402

# coach.db as it was before the player column (db.SCHEMA of 2026-10-29, games table only differs)
OLD_GAMES = """
CREATE TABLE games (id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, me TEXT, ally TEXT,
    ally_race TEXT, enemies TEXT, matchup TEXT, plan TEXT, coach_plan TEXT, result TEXT, length_sec REAL,
    source TEXT DEFAULT 'helper');
"""


def old_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_GAMES)
    conn.executescript(db.SCHEMA)   # samples / calls / notes (games exists already: kept in its old shape)
    assert "player" not in [r[1] for r in conn.execute("PRAGMA table_info(games)")]
    conn.executemany("INSERT INTO games (id, started_at, me, enemies, plan, result, length_sec) VALUES "
                     "(?, ?, 'TestPilot', ?, 'A', ?, 600)",
                     [(1, 1793400000.0, "Foe (Zerg)", "Victory"), (2, 1793401000.0, "Bar (Prot)", "Defeat")])
    conn.commit()
    conn.close()


def test_migration(d):
    p = Path(d) / "old.db"
    old_db(p)
    # history reads an unmigrated file read-only: every game is Dickie's, a bot has none
    conn = history.connect(p)
    assert not history.has_player(conn) and history.player_list(conn) == ["TestPilot"]
    conn.close()
    assert history.route("/history/review/latest", p)[0] == 200
    assert history.route("/history/review/latest?player=SixRax", p)[0] == 404
    code, body = history.route("/history/games", p)[:2]
    assert code == 200 and "/history/game/1" in body and "/history/game/2" in body

    conn = db.connect(p)   # what the helper's Recorder does on start
    cols = [r[1] for r in conn.execute("PRAGMA table_info(games)")]
    assert "player" in cols
    assert [r[0] for r in conn.execute("SELECT player FROM games ORDER BY id")] == ["TestPilot", "TestPilot"]
    assert db.migrate(conn) is False   # idempotent
    assert conn.execute("SELECT count(*) FROM games").fetchone()[0] == 2
    conn.close()

    # the read-only CLI path never migrates
    q = Path(d) / "old2.db"
    old_db(q)
    db.connect(q, upgrade=False).close()
    conn = sqlite3.connect(q)
    assert "player" not in [r[1] for r in conn.execute("PRAGMA table_info(games)")]
    conn.close()


def samples(rec, n=20):
    for i in range(n):
        rec.sample(5.0 * (i + 1), {"minerals": 100, "gas": 0, "used": 12 + i, "cap": 23 + i}, 12 + i)


def mixed_db(d):
    """Dickie: games 1, 2 (via an old file, migrated); SixRax: 3, DtRush: 4 (recorded like the helper does)."""
    p = Path(d) / "mixed.db"
    old_db(p)
    rec = db.Recorder(p)
    rec.game_id = 1
    samples(rec)
    for player, enemy in (("SixRax", "A.I. 1 (Medium) (Zerg)"), ("DtRush", "A.I. 1 (Hard) (Terr)")):
        rec.start_game(player=player, me=player, ally="", ally_race="", enemies=enemy, matchup="Z", plan="BOT",
                       coach_plan="BOT", result="Undecided")
        samples(rec)
        rec.call(30.0, "helper", "plan", f"Following {player}")
        rec.end_game("Victory", 300.0)
    rec.conn.close()
    return p


def test_history_filter(d):
    p = mixed_db(d)
    code, body = history.route("/history/games", p)[:2]   # default: Dickie only
    assert code == 200 and "/history/game/1" in body and "/history/game/2" in body
    assert "/history/game/3" not in body and "/history/game/4" not in body
    body = history.route("/history/games?player=TestPilot", p)[1]
    assert "/history/game/2" in body and "/history/game/3" not in body
    body = history.route("/history/games?player=SixRax", p)[1]
    assert "/history/game/3" in body and "/history/game/1" not in body and "/history/game/4" not in body
    body = history.route("/history/games?player=Nobody", p)[1]
    assert "No games match" in body
    # the dropdown: Dickie first (selected by default), then the bots
    page = history.route("/history", p)[1]
    assert ('<option value="TestPilot">TestPilot (you)</option><option value="DtRush">DtRush</option>'
            '<option value="SixRax">SixRax</option>') in page
    assert 'name="player"' in page and "<!--players-->" not in page
    # per-game routes work for any player's game; a bot game is tagged with its name
    code, body = history.route("/history/game/3", p)[:2]
    assert code == 200 and '<span class="tag">SixRax</span>' in body
    code, body = history.route("/history/game/1", p)[:2]
    assert code == 200 and 'class="tag"' not in body


def test_review_latest_per_player(d):
    p = mixed_db(d)
    def review(path):
        code, body = history.route(path, p)[:2]
        return code, json.loads(body)
    code, r = review("/history/review/latest")   # the Angular panel's call: Dickie's latest, not the bot game after it
    assert code == 200 and r["id"] == 2 and r["player"] == "TestPilot"
    models.REVIEW.validate_python(r, strict=True)
    assert review("/history/review/latest?player=TestPilot")[1]["id"] == 2
    code, r = review("/history/review/latest?player=SixRax")
    assert code == 200 and r["id"] == 3 and r["player"] == "SixRax" and r["series"]
    assert review("/history/review/latest?player=DtRush")[1]["id"] == 4
    code, r = review("/history/review/latest?player=Nobody")
    assert code == 404 and "Nobody" in r["error"]
    code, r = review("/history/review/4")   # by id: any player's game
    assert code == 200 and r["player"] == "DtRush"


def test_null_player_is_dickies(d):
    p = Path(d) / "null.db"
    conn = db.connect(p)
    conn.execute("INSERT INTO games (id, started_at, player, result) VALUES (1, 1.0, NULL, 'Victory')")
    conn.execute("INSERT INTO games (id, started_at, player, result) VALUES (2, 2.0, 'SixRax', 'Defeat')")
    conn.commit()
    conn.close()
    assert json.loads(history.route("/history/review/latest", p)[1])["id"] == 1
    assert "/history/game/1" in history.route("/history/games", p)[1]


# ---------- the helper following a bot ----------

BOT = {"player": "SixRax", "race": "terran", "bot": "sixrax", "written_at": 0.0,
       "plan": {"name": "SixRax: 6-Barracks Marine all-in (Terran)",
                "steps": [{"sec": 30, "t": "0:30", "supply": 15, "action": "Barracks"},
                          {"sec": 18, "t": "0:18", "supply": 14, "action": "Supply Depot"}]}}


def players(*names):
    return [{"name": n, "race": r, "result": "Undecided"} for n, r in names]


def test_read_bot_game(d):
    f = Path(d) / "bot_game.json"
    assert timing.read_bot_game(f) is None   # no file
    now = time.time()
    f.write_text(json.dumps({**BOT, "written_at": now - 30}), encoding="utf-8")
    bot = timing.read_bot_game(f, now=now)
    assert bot is not None and bot["player"] == "SixRax" and len(bot["plan"]["steps"]) == 2
    assert timing.read_bot_game(f, now=now + timing.BOT_STALE) is None   # left behind by a crashed run
    f.write_text(json.dumps({**BOT, "player": ""}), encoding="utf-8")
    assert timing.read_bot_game(f, now=0.0) is None   # invalid: ignored, not an exception
    f.write_text("{half", encoding="utf-8")
    assert timing.read_bot_game(f) is None


def test_follow():
    bot = models.BOT_GAME.validate_python(BOT)
    game = players(("SixRax", "Terr"), ("A.I. 1 (Medium)", "Zerg"))
    assert timing.follow(game, "TestPilot", bot) == ("SixRax", bot)
    assert timing.enemy_key(game, "SixRax")[0] == "Z"
    # Dickie in the game: always Dickie, whatever the file says
    assert timing.follow(players(("TestPilot", "Terr"), ("SixRax", "Terr")), "TestPilot", bot) == ("TestPilot", None)
    # the file names someone who isn't in this game, or there is no file: today's behaviour
    assert timing.follow(players(("DtRush", "Prot"), ("X", "Zerg")), "TestPilot", bot) == ("TestPilot", None)
    assert timing.follow(game, "TestPilot", None) == ("TestPilot", None)


def test_family_plan():
    plans = timing.load_plans()
    assert timing.family_plan(plans, "P", True, "T") == "PT"
    assert timing.family_plan(plans, "P", False, "TZ") == "PX"
    assert timing.family_plan(plans, "T", True, "Z") == "Z"
    assert timing.family_plan(plans, "T", True, "?") == "M"
    assert timing.family_plan(plans, "T", False, "TP") == plans["matchups"]["TP"][0]


def test_bot_plan_file(d):
    f = Path(d) / "out" / "plans_bot.json"
    assert timing.write_bot_plan({**BOT, "plan": None}, f) is None and not f.exists()
    plan = timing.write_bot_plan(BOT, f)
    assert plan is not None and [s["sec"] for s in plan["steps"]] == [18, 30]   # sorted for plan_status / the page
    assert json.loads(f.read_text(encoding="utf-8")) == {"plans": {"BOT": plan}}
    # /plans (load_plans) serves it next to the real plans; a broken bot file is skipped, never fatal
    saved = timing.BOT_PLANS
    timing.BOT_PLANS = f
    try:
        plans = timing.load_plans()
        assert plans["plans"]["BOT"] == plan and "A" in plans["plans"]
        f.write_text("{half", encoding="utf-8")
        assert "BOT" not in timing.load_plans()["plans"]
    finally:
        timing.BOT_PLANS = saved
    # the bot's build drives the helper rules too: behind its own plan, Terran wording
    exp, step, nxt = timing.plan_status(plan["steps"], 45)
    assert (exp, step["action"], nxt) == (15, "Barracks", None)


def test_cue_engine_waits_for_its_plan():
    """An older coach-core without the bot plan answers /start 404: no /tick may reach the previous game's engine."""
    cue = timing.CueEngine()
    posts = []

    def post(path, body):
        posts.append(path)
        if path == "/start" and body["plan"] == "BOT":
            raise RuntimeError("HTTP 404")
        return b'{"ok": true, "steps": 2}' if path == "/start" else b'{"events": [], "lateness": 0.0, "shift": 0}'
    cue._post = post
    assert cue.start("BOT") is False
    assert cue.tick(10.0, 14) is None and posts == ["/start"]   # waiting out the retry: nothing sent
    cue.retry = 0.0
    assert cue.tick(10.5, 14) is None and posts == ["/start", "/start"]   # retried /start, still unknown
    cue.retry = 0.0
    assert cue.start("A") is True   # next game, a plan it knows: ticks flow again
    assert cue.tick(11.0, 14) == {"events": [], "lateness": 0.0, "shift": 0} and posts[-1] == "/tick"


def test_state_and_bot_game_models():
    s = models.STATE.validate_python({"in_game": True, "clock": 1.0, "player": "SixRax"})
    assert s["player"] == "SixRax"
    assert timing.STATE.data["player"] == ""   # the helper always sends it (empty until a game starts)


def test_owner_name(d):
    assert db.OWNER == history.OWNER == "TestPilot"   # SC2_PLAYER wins over live/player.json
    f = Path(d) / "player.json"
    env = os.environ.pop("SC2_PLAYER")
    try:
        assert player_cfg.name(f) == player_cfg.DEFAULT                    # no file
        f.write_text('{"name": " Someone "}', encoding="utf-8")
        assert player_cfg.name(f) == "Someone"
        f.write_text('{"name": ""}', encoding="utf-8")
        assert player_cfg.name(f) == player_cfg.DEFAULT                    # empty name
        f.write_text("not json", encoding="utf-8")
        assert player_cfg.name(f) == player_cfg.DEFAULT
        os.environ["SC2_PLAYER"] = "FromEnv"
        assert player_cfg.name(f) == "FromEnv"
    finally:
        os.environ["SC2_PLAYER"] = env
    shell = history.page()
    assert 'player: "TestPilot"' in shell and "<!--owner-->" not in shell   # the page's default player filter
    assert db.sql_str("O'Neil") == "'O''Neil'"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        if t.__code__.co_argcount:
            with tempfile.TemporaryDirectory() as d:
                t(d)
        else:
            t()
    print(f"  test_players: {len(tests)} tests ok")
