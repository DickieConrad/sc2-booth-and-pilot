#!/usr/bin/env python3
"""One-time import: the sparring-bot test games in live/coach-test.db -> live/coach.db, each under its bot's name.

Until 2026-10-30 the helper kept games Dickie wasn't in out of coach.db (they went to coach-test.db). Now every game
lives in coach.db with a `player` (games.player: the owner's name from live/player.json, else the bot's in-game
name), so the same history and review tools work on bot games. This copies games, samples, calls and notes over with
new ids (the two files' ids overlap), and sets the player from the game itself:
  1. a known bot name among the game's players (the old helper wrote everyone into `ally`: "SixRax, A.I. 1 (Medium)")
  2. else "sparring bot <key>" in one of the game's notes ("TEST GAME: sparring bot dt vs medium AI ...")
  3. else "bot"
For a 1v1 (the bot + one opponent) it also fixes the fields the old helper got wrong when it couldn't find Dickie:
me = the bot, enemies = the opponent, matchup = the opponent's race, ally empty.

Safe to run twice: a game already in coach.db (same started_at) is skipped. coach.db is single-writer: stop the
timing helper first (the tool refuses while http://127.0.0.1:8765 answers, unless --force). A backup
(coach.db.bak-<time>) is made before anything is written.

usage (WSL, repo root):
  python3 tools/import_test_games.py --dry-run      # what would be imported; writes nothing
  python3 tools/import_test_games.py                # backup, then import
  [--src live/coach-test.db] [--dst live/coach.db] [--force]
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

LIVE = Path(__file__).resolve().parent.parent / "live"
sys.path.insert(0, str(LIVE))
import db  # noqa: E402

BOT_NAMES = {"sixrax": "SixRax", "dt": "DtRush", "proxy": "ProxyRax"}   # bot key -> in-game name (bot/*.py NAME)
RACES = {"Terr": "T", "Prot": "P", "Zerg": "Z"}
HELPER = "http://127.0.0.1:8765/state"


def ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro&nolock=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def split_players(names: str | None, races: str | None) -> list[tuple[str, str]]:
    """'SixRax, A.I. 1 (Medium)' + 'Terr, random' -> [('SixRax', 'Terr'), ('A.I. 1 (Medium)', 'random')]."""
    ns = [n.strip() for n in (names or "").split(", ") if n.strip()]
    rs = [r.strip() for r in (races or "").split(", ")]
    return [(n, rs[i] if i < len(rs) else "") for i, n in enumerate(ns)]


def bot_player(game: sqlite3.Row | dict, notes: list[str]) -> str:
    """The game's player name: a known bot among its players, else the bot named in a note, else 'bot'."""
    known = set(BOT_NAMES.values())
    for name, _ in split_players(game["ally"], game["ally_race"]):
        if name in known:
            return name
    for name in re.findall(r"\b([A-Za-z]+) \(", game["enemies"] or ""):
        if name in known:
            return name
    for text in notes:
        m = re.search(r"sparring bot (\w+)", text or "")
        if m and m.group(1).lower() in BOT_NAMES:
            return BOT_NAMES[m.group(1).lower()]
    return "bot"


def fixed_game(game: sqlite3.Row | dict, player: str) -> dict:
    """The game row for coach.db (no id): player set; me/ally/enemies/matchup fixed for a 1v1 against the bot."""
    g = {k: game[k] for k in game.keys() if k not in ("id", "player")}
    g["player"] = player
    seats = split_players(game["ally"], game["ally_race"])
    names = [n for n, _ in seats]
    if player in names and len(seats) == 2 and (game["enemies"] or "?") == "?":
        (opp, race), = [(n, r) for n, r in seats if n != player]
        g.update(me=player, ally="", ally_race="", enemies=f"{opp} ({race})", matchup=RACES.get(race, "?"))
    return g


def helper_running() -> bool:
    try:
        with urllib.request.urlopen(HELPER, timeout=1):
            return True
    except Exception:
        return False


def plan(src: Path, dst: Path) -> list[dict]:
    """What would be imported: per source game {id, player, game (row for coach.db), samples, calls, notes} --
    games already in dst (same started_at) are left out. Reads both files, writes nothing."""
    s = ro(src)
    have: set[float] = set()
    if dst.exists():
        d = ro(dst)
        have = {r[0] for r in d.execute("SELECT started_at FROM games")}
        d.close()
    out = []
    for g in s.execute("SELECT * FROM games ORDER BY id"):
        if g["started_at"] in have:
            continue
        gid = g["id"]
        notes = [dict(n) for n in s.execute("SELECT * FROM notes WHERE game_id = ? ORDER BY id", (gid,))]
        player = bot_player(g, [n["text"] for n in notes])
        out.append({"id": gid, "player": player, "game": fixed_game(g, player),
                    "samples": [dict(r) for r in s.execute("SELECT * FROM samples WHERE game_id = ? ORDER BY t",
                                                           (gid,))],
                    "calls": [dict(r) for r in s.execute("SELECT * FROM calls WHERE game_id = ? ORDER BY rowid",
                                                         (gid,))],
                    "notes": notes})
    s.close()
    return out


def backup(dst: Path) -> Path:
    bak = dst.with_name(f"{dst.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(dst, bak)
    return bak


def apply(todo: list[dict], dst: Path) -> dict[int, int]:
    """Write the planned games into dst in one transaction; -> {old game id: new game id}."""
    conn = db.connect(dst)   # creates / migrates (adds games.player) as the helper would
    ids: dict[int, int] = {}
    try:
        with conn:
            for item in todo:
                g = item["game"]
                cols = ", ".join(g)
                cur = conn.execute(f"INSERT INTO games ({cols}) VALUES ({', '.join('?' * len(g))})",
                                   tuple(g.values()))
                new = cur.lastrowid
                assert new is not None
                ids[item["id"]] = new
                conn.executemany("INSERT INTO samples (game_id, t, minerals, gas, used, cap, plan_supply) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 [(new, r["t"], r["minerals"], r["gas"], r["used"], r["cap"], r["plan_supply"])
                                  for r in item["samples"]])
                conn.executemany("INSERT INTO calls (game_id, t, source, kind, text, wall) VALUES (?, ?, ?, ?, ?, ?)",
                                 [(new, r["t"], r["source"], r["kind"], r["text"], r["wall"]) for r in item["calls"]])
                conn.executemany("INSERT INTO notes (game_id, created_at, author, text) VALUES (?, ?, ?, ?)",
                                 [(new, r["created_at"], r["author"], r["text"]) for r in item["notes"]])
    finally:
        conn.close()
    return ids


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--src", type=Path, default=db.TEST_PATH, help="the old test database (default live/coach-test.db)")
    ap.add_argument("--dst", type=Path, default=db.PATH, help="the main database (default live/coach.db)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be imported; write nothing")
    ap.add_argument("--force", action="store_true", help="import even though the timing helper answers")
    a = ap.parse_args(argv)
    if not a.src.exists():
        print(f"nothing to import: {a.src} doesn't exist")
        return 0
    todo = plan(a.src, a.dst)
    for item in todo:
        g = item["game"]
        print(f"#{item['id']} -> player {item['player']}: {g['result']} {db.mmss(g['length_sec'])} vs "
              f"{g['enemies']} ({len(item['samples'])} samples, {len(item['calls'])} calls, "
              f"{len(item['notes'])} notes)")
    if not todo:
        print("nothing to import: every game is already in", a.dst)
        return 0
    if a.dry_run:
        print(f"dry run: {len(todo)} game(s) would be imported into {a.dst}; nothing written")
        return 0
    if a.dst.resolve() == db.PATH.resolve() and helper_running() and not a.force:
        print("the timing helper is running (it is coach.db's only writer): stop it first, or pass --force",
              file=sys.stderr)
        return 1
    if a.dst.exists():
        print("backup:", backup(a.dst))
    ids = apply(todo, a.dst)
    print(f"imported {len(ids)} game(s) into {a.dst}: " + ", ".join(f"#{o} -> #{n}" for o, n in ids.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
