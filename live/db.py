"""Coaching database: live/coach.db (SQLite).

Tables
  games    one row per game: who, matchup, plan, result, length
  samples  resource-panel reads during a game (game clock, minerals, gas, supply, plan supply)
  calls    every call shown on the local page (helper rules and coach lines)
  notes    coach notes: reviews, the one fix, anything worth remembering (optionally tied to a game)

The file sits on the \\wsl.localhost share, where Windows can't take SQLite file locks, so it is opened
with nolock=1. That makes it single-writer: while the timing helper runs it does all writing, and the CLI
below sends notes through the helper (POST /note) instead of writing directly.

CLI (Windows: live\\.python\\python.exe live\\db.py ...; WSL: python3 live/db.py ...)
  games [N]                 last N games with result and call counts
  game ID                   one game: minute-by-minute panel reads and its calls and notes
  note TEXT [--game ID]     add a coach note (to the latest game unless --game is given; --game 0 = no game)
  sql "SELECT ..."          run a read-only query
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from player import OWNER  # the owner's in-game name (live/player.json)

if TYPE_CHECKING:   # db.py stays stdlib-only at run time (the CLI runs on any python)
    from models import NoteRow, Panel

PATH = Path(__file__).resolve().with_name("coach.db")
# Until 2026-10-30 games Dickie wasn't in (sparring-bot tests) went to this separate file; now they go to coach.db
# with their own `player`. Kept only for the one-time import (tools/import_test_games.py).
TEST_PATH = PATH.with_name("coach-test.db")
# OWNER (from player.py) is the default player (history, review) and owns every game from before the column


def sql_str(s: str) -> str:
    """A SQL string literal (for DDL defaults, which can't be bound parameters)."""
    return "'" + s.replace("'", "''") + "'"


SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    started_at REAL NOT NULL,          -- unix time the helper saw the game (or a backfill estimate)
    ended_at REAL,
    player TEXT DEFAULT {owner},   -- whose game: the owner, or a sparring bot's name (SixRax, DtRush...)
    me TEXT, ally TEXT, ally_race TEXT,
    enemies TEXT,                      -- "Name (Race) + Name (Race)"
    matchup TEXT,                      -- enemy key, e.g. TP, ZZ
    plan TEXT,                         -- plan followed (A-F, M = MMM, ...)
    coach_plan TEXT,                   -- Live Coach pick for the matchup
    result TEXT,                       -- Victory / Defeat / Tie / Undecided
    length_sec REAL,
    source TEXT DEFAULT 'helper'       -- helper | backfill
);
CREATE TABLE IF NOT EXISTS samples (
    game_id INTEGER NOT NULL REFERENCES games(id),
    t REAL NOT NULL,                   -- game clock, seconds
    minerals INTEGER, gas INTEGER, used INTEGER, cap INTEGER,
    plan_supply INTEGER
);
CREATE INDEX IF NOT EXISTS samples_game ON samples(game_id, t);
CREATE TABLE IF NOT EXISTS calls (
    game_id INTEGER REFERENCES games(id),
    t REAL,                            -- game clock, seconds
    source TEXT,                       -- helper | coach
    kind TEXT,                         -- blocked | behind | bank | plan | coach ...
    text TEXT,
    wall REAL
);
CREATE INDEX IF NOT EXISTS calls_game ON calls(game_id, t);
CREATE TABLE IF NOT EXISTS bot_games (    -- every bot\\run.py game, with the bot version that played it (2026-10-31)
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    at TEXT,                           -- local start time, "YYYY-MM-DD HH:MM"
    bot TEXT, vs TEXT, opp_race TEXT, map TEXT,
    result TEXT,                       -- Victory / Defeat / Tie
    game_time TEXT,                    -- m:ss
    commit_id TEXT,                    -- git commit the bot ran from
    code TEXT,                         -- hash of bot/*.py: uncommitted changes are their own version
    overrides TEXT,                    -- the --set values, space separated
    coach_test INTEGER, replay TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    game_id INTEGER REFERENCES games(id),
    created_at REAL NOT NULL,
    author TEXT DEFAULT 'coach',
    text TEXT NOT NULL
);
""".replace("{owner}", sql_str(OWNER))


def connect(path: Path = PATH, upgrade: bool = True) -> sqlite3.Connection:
    """Open coach.db (created if missing). upgrade: also run migrate() -- a write, so only for the single writer
    (the helper's Recorder, the import tool); the read-only CLI commands pass False."""
    uri = f"file:{path}?nolock=1"   # not as_uri(): SQLite rejects the //wsl.localhost authority
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if upgrade:
        migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> bool:
    """Bring an older coach.db up to SCHEMA: games.player (added 2026-10-30; every existing game is Dickie's, so
    they get OWNER from the column default). Idempotent; True if it changed anything."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
    changed = False
    if "player" not in cols:
        conn.execute(f"ALTER TABLE games ADD COLUMN player TEXT DEFAULT {sql_str(OWNER)}")
        changed = True
    conn.execute("CREATE INDEX IF NOT EXISTS games_player ON games(player, id)")
    conn.commit()
    return changed


class Recorder:
    """Used by the timing helper (the single writer). Thread-safe: the page server thread writes calls/notes."""

    def __init__(self, path: Path = PATH) -> None:
        self.conn = connect(path)
        self.lock = threading.Lock()
        self.game_id: int | None = None

    def _exec(self, sql: str, args: Sequence[Any] = ()) -> int | None:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def start_game(self, **g: Any) -> int | None:
        cols = ", ".join(g)
        self.game_id = self._exec(f"INSERT INTO games (started_at, {cols}) VALUES (?, {', '.join('?' * len(g))})",
                                  (time.time(), *g.values()))
        return self.game_id

    def end_game(self, result: str, length_sec: float) -> None:
        if self.game_id:
            self._exec("UPDATE games SET ended_at = ?, result = ?, length_sec = ? WHERE id = ?",
                       (time.time(), result, length_sec, self.game_id))

    def sample(self, t: float, panel: Panel, plan_supply: int | None) -> None:
        if self.game_id:
            self._exec("INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (self.game_id, t, panel["minerals"], panel["gas"], panel["used"], panel["cap"], plan_supply))

    def call(self, t: float, source: str, kind: str, text: str) -> None:
        self._exec("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?)", (self.game_id, t, source, kind, text, time.time()))

    def recent_notes(self, n: int = 8) -> list[NoteRow]:
        """Latest notes with their game's label, newest first (for the local page)."""
        with self.lock:
            rows = self.conn.execute("""
                SELECT n.id, n.text, n.created_at, g.id AS game_id, g.result, g.enemies, g.length_sec
                FROM notes n LEFT JOIN games g ON g.id = n.game_id
                ORDER BY n.id DESC LIMIT ?""", (n,)).fetchall()
        return [cast("NoteRow", dict(r)) for r in rows]

    def note(self, text: str, game_id: int | None = None, author: str = "coach") -> int | None:
        gid = game_id if game_id is not None else self.game_id or latest_game(self.conn)
        return self._exec("INSERT INTO notes (game_id, created_at, author, text) VALUES (?, ?, ?, ?)",
                          (gid or None, time.time(), author, text))


    def bot_game(self, g: dict[str, Any]) -> int | None:
        """One bot\\run.py game (POST /botgame)."""
        return self._exec(
            "INSERT INTO bot_games (created_at, at, bot, vs, opp_race, map, result, game_time, commit_id, code, "
            "overrides, coach_test, replay) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), g["at"], g["bot"], g["vs"], g["opp_race"], g["map"], g["result"], g["game_time"],
             g["commit"], g["code"], " ".join(g["overrides"]), int(g["coach_test"]), g["replay"]))


def latest_game(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT max(id) FROM games").fetchone()
    return cast("int | None", row[0])


def mmss(sec: float | None) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


# ---------- CLI ----------

def cmd_games(conn: sqlite3.Connection, n: int = 15) -> None:
    rows = conn.execute("""
        SELECT g.*, (SELECT count(*) FROM calls c WHERE c.game_id = g.id) AS ncalls,
               (SELECT count(*) FROM notes n WHERE n.game_id = g.id) AS nnotes
        FROM games g ORDER BY g.id DESC LIMIT ?""", (n,)).fetchall()
    for r in reversed(rows):
        when = time.strftime("%m-%d %H:%M", time.localtime(r["started_at"]))
        player = r["player"] if "player" in r.keys() else OWNER   # a DB the helper hasn't migrated yet
        who = "" if player in (OWNER, None) else f"[{player}] "
        print(f"#{r['id']:<3} {when}  {who}{r['result'] or '?':<9} {mmss(r['length_sec']):>6}  "
              f"plan {r['plan'] or '?':<2} vs {r['matchup'] or '?':<3} {r['enemies'] or ''}  "
              f"[{r['ncalls']} calls, {r['nnotes']} notes]")


def cmd_game(conn: sqlite3.Connection, gid: int | None) -> None:
    g = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
    if not g:
        sys.exit(f"no game {gid}")
    print(f"#{g['id']} {g['result']} at {mmss(g['length_sec'])}: plan {g['plan']} (Live Coach {g['coach_plan']}) "
          f"vs {g['enemies']}")
    print("\n time   min   gas  supply  plan")
    for r in conn.execute("""SELECT * FROM samples WHERE game_id = ? AND rowid IN
                             (SELECT min(rowid) FROM samples WHERE game_id = ? GROUP BY CAST(t / 60 AS INT))
                             ORDER BY t""", (gid, gid)):
        print(f"{mmss(r['t']):>5} {r['minerals']:>5} {r['gas']:>5} "
              f"{r['used']:>4}/{r['cap']:<4} {r['plan_supply'] or '-'}")
    calls = conn.execute("SELECT * FROM calls WHERE game_id = ? ORDER BY t", (gid,)).fetchall()
    if calls:
        print("\ncalls")
        for c in calls:
            print(f"{mmss(c['t']):>5} {c['source']:<6} {c['text']}")
    notes = conn.execute("SELECT * FROM notes WHERE game_id = ? ORDER BY id", (gid,)).fetchall()
    if notes:
        print("\nnotes")
        for n in notes:
            print(f"- {n['text']}")


def cmd_note(text: str, game_id: int | None) -> None:
    body: dict[str, Any] = {"text": text}
    if game_id is not None:
        body["game_id"] = game_id
    try:  # the running helper is the only writer; let it write
        req = urllib.request.Request("http://127.0.0.1:8765/note", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            print("note saved via helper:", r.read().decode())
            return
    except Exception:
        pass
    conn = connect()   # the helper isn't running: this process is the only writer
    gid = game_id if game_id is not None else latest_game(conn)
    conn.execute("INSERT INTO notes (game_id, created_at, author, text) VALUES (?, ?, 'coach', ?)",
                 (gid or None, time.time(), text))
    conn.commit()
    print(f"note saved directly (helper not running), game {gid}")


def main(argv: list[str]) -> None:

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "note":
        gid = None
        if "--game" in rest:
            i = rest.index("--game")
            gid = int(rest[i + 1])
            rest = rest[:i] + rest[i + 2:]
        cmd_note(" ".join(rest), gid)
        return
    conn = connect(upgrade=False)   # read-only commands: never write while the helper may be writing
    if cmd == "games":
        cmd_games(conn, int(rest[0]) if rest else 15)
    elif cmd == "botgames":
        rows = conn.execute("SELECT * FROM bot_games ORDER BY id DESC LIMIT ?", (int(rest[0]) if rest else 30,))
        for r in reversed(rows.fetchall()):
            print(f"#{r['id']:<3} {r['at']}  {r['bot']:<7} vs {r['vs']} {r['opp_race']:<7} {r['map']:<14} "
                  f"{r['result']:<8} {r['game_time']:>6}  {r['commit_id']}+{r['code']}  {r['overrides']}")
    elif cmd == "game":
        cmd_game(conn, int(rest[0]) if rest else latest_game(conn))
    elif cmd == "sql":
        q = " ".join(rest)
        if not q.lstrip().lower().startswith(("select", "with", "pragma")):
            sys.exit("read-only: SELECT / WITH / PRAGMA only")
        cur = conn.execute(q)
        print(" | ".join(d[0] for d in cur.description))
        for row in cur:
            print(" | ".join("" if v is None else str(v) for v in row))
    else:
        sys.exit(f"unknown command {cmd!r}; see --help")


if __name__ == "__main__":
    main(sys.argv[1:])
