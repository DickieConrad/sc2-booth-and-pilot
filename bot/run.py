#!/usr/bin/env python3
"""Start a local game against a sparring bot (Blizzard's official SC2 bot API, via python-sc2 / burnysc2).

Run on WINDOWS (StarCraft II runs there), from the repo root, with the project's Python:
    live\\.python\\python.exe bot\\run.py --bot sixrax --vs human --map <name>
    live\\.python\\python.exe bot\\run.py --bot dt --vs medium --map <name>          (bot vs built-in AI, test)
    live\\.python\\python.exe bot\\run.py --bot sixrax --bot2 dt --vs human --2v2 --map <2v2 map>
    live\\.python\\python.exe bot\\run.py --list                                     (bots and their settings)
--dry-run prints the seats and map without starting StarCraft II. See bot/README.md."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import fields
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / ".pkgs")]

from config import CONFIGS, PRESETS  # noqa: E402
from core import (  # noqa: E402
    BOT_GAME_FILE,
    BOT_RACES,
    COMMAND_FILE,
    DIFFICULTIES,
    MAPS_DIR,
    apply_overrides,
    bot_game,
    clear_bot_game,
    clock,
    describe_result,
    desynced,
    keep_replay,
    parse_time_limit,
    plan_seats,
    replay_name,
    replays_to_prune,
    resolve_map,
    surrender_results,
    write_json_atomic,
)


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Play against a sparring bot (local game, official SC2 API).")
    ap.add_argument("--bot", choices=sorted(BOT_RACES), default="sixrax", help="the bot (default sixrax)")
    ap.add_argument("--bot2", choices=sorted(BOT_RACES), help="--2v2: the second bot (default: same as --bot)")
    ap.add_argument(
        "--vs",
        default="human",
        choices=("human", "bot", *DIFFICULTIES),
        help="human = you play it in realtime; a difficulty = the bot plays the built-in AI; "
        "bot = 1v1 vs --bot2 (default: the same bot)",
    )
    ap.add_argument(
        "--map",
        help=f"map name or .SC2Map path; names are looked up in {MAPS_DIR} first, then in the "
        "SC2 install's Maps folder (default: the only map in bot/maps)",
    )
    ap.add_argument(
        "--2v2",
        dest="two_v_two",
        action="store_true",
        help="vs human: you + built-in AI ally vs two bots; vs AI: two bots vs two AIs",
    )
    ap.add_argument("--race", default="terran", choices=("terran", "protoss", "zerg", "random"), help="your race")
    ap.add_argument("--ally-race", default="protoss", choices=("terran", "protoss", "zerg", "random"))
    ap.add_argument("--ally-difficulty", default="hard", choices=DIFFICULTIES)
    ap.add_argument(
        "--ai-race",
        default="random",
        choices=("terran", "protoss", "zerg", "random"),
        help="race of the built-in AI opponent(s) when --vs is a difficulty",
    )
    ap.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a bot setting, e.g. --set attack_time=3:15 (repeatable; see --list)",
    )
    ap.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        help="a named set of --set values (config.PRESETS), applied before your own --set (which win); "
        "baseline = the ba15df0 bot that beat Insane Zerg twice, every change since switched off",
    )
    ap.add_argument("--realtime", action="store_true", help="bot vs AI at real speed (always on vs human)")
    ap.add_argument(
        "--speed",
        type=float,
        default=0.0,
        help="bot vs AI, not realtime: at most this many times real speed (e.g. 2; 0 = as fast as it goes)",
    )
    ap.add_argument(
        "--time-limit",
        metavar="M:SS",
        help="bot vs AI only: stop the game at this game time (m:ss or seconds); it then ends as a Tie "
        "(python-sc2's game_time_limit), reported as 'time limit reached'",
    )
    ap.add_argument("--window", default="1600x900", help="your window size WxH (default 1600x900)")
    ap.add_argument("--fullscreen", action="store_true", help="your window fullscreen")
    ap.add_argument(
        "--coach-test",
        action="store_true",
        help="bot vs AI for testing the coach: realtime, the bot's window fullscreen (the coach reads its panel); "
        "the bot announces itself in live/out/bot_game.json, so the helper follows it (race, result, its own build "
        "as the plan) and records the game under the bot's name (player column in coach.db)",
    )
    ap.add_argument(
        "--replay",
        help="save the replay to this .SC2Replay path (Windows path); default bot/out/replays/<bot>-vs-<ai>-<time>"
        "; 'none' to skip",
    )
    ap.add_argument(
        "--stock",
        action="store_true",
        help="1v1 only: use python-sc2's own run_game instead of bot/match.py (fallback)",
    )
    ap.add_argument("--list", action="store_true", help="list bots and their settings, then exit")
    ap.add_argument("--games", action="store_true", help="print the last 30 games (bot/out/games.jsonl), then exit")
    ap.add_argument("--dry-run", action="store_true", help="print seats and map, don't start StarCraft II")
    return ap.parse_args(argv)


def game_bots(args) -> set[str]:
    """Every bot seated this game: bot, plus bot2 in 2v2 or --vs bot (bot2 defaults to bot)."""
    return {args.bot} | ({args.bot2 or args.bot} if args.two_v_two or args.vs == "bot" else set())


def preset_overrides(args) -> list[str]:
    """The --preset's --set values, minus keys none of this game's bots has (baseline's MMM-only switches on
    sixrax). They go before the explicit --set values, so those win."""
    if not args.preset:
        return []
    known = {f.name for b in game_bots(args) for f in fields(CONFIGS[b])}
    return [p for p in PRESETS[args.preset] if p.split("=", 1)[0].strip() in known]


def build_configs(args) -> dict:
    """One config per bot in the game, --set applied (each key must exist in at least one of them)."""
    keys = {p.split("=", 1)[0].strip() for p in args.overrides}
    bots = game_bots(args)
    known = {f.name for b in bots for f in fields(CONFIGS[b])}
    unknown = keys - known
    if unknown:
        raise ValueError(
            f"unknown setting(s) for {', '.join(sorted(bots))}: {', '.join(sorted(unknown))}; "
            f"valid: {', '.join(sorted(known - {'build'}))}"
        )
    return {b: apply_overrides(CONFIGS[b](), args.overrides, ignore_unknown=True) for b in bots}


def list_bots() -> None:
    for key, cls in CONFIGS.items():
        cfg = cls()
        print(f"{key} ({BOT_RACES[key]}): {(cls.__doc__ or '').strip()}")
        for f in fields(cfg):
            if f.name != "build":
                print(f"    {f.name} = {getattr(cfg, f.name)}")
        print(
            "    build: "
            + ", ".join(f"{s.supply} {s.item}" + (f" x{s.count}" if s.count > 1 else "") for s in cfg.build)
        )


def window_size(text: str) -> tuple[int, int]:
    w, h = text.lower().split("x")
    return int(w), int(h)


def make_players(seats, configs):
    from dtrush import DtRushBot
    from mmm import MmmBot
    from proxyrax import ProxyRaxBot
    from sc2.data import Difficulty, Race
    from sc2.player import Bot, Computer, Human
    from sixrax import SixRaxBot
    from skytoss import SkytossBot

    classes = {"sixrax": SixRaxBot, "dt": DtRushBot, "proxy": ProxyRaxBot, "mmm": MmmBot, "skytoss": SkytossBot}
    diff = {
        d: Difficulty[n]
        for d, n in zip(
            DIFFICULTIES,
            ("VeryEasy", "Easy", "Medium", "MediumHard", "Hard", "Harder", "VeryHard", "CheatVision", "CheatMoney",
             "CheatInsane"),
        )
    }
    players, names = [], set()
    for s in seats:
        race = Race[s.race.capitalize()]
        if s.kind == "human":
            players.append(Human(race, name="Dickie"))
        elif s.kind == "computer":
            players.append(Computer(race, diff[s.difficulty]))
        else:
            ai = classes[s.bot](configs[s.bot])
            name = ai.NAME if ai.NAME not in names else ai.NAME + "2"
            names.add(name)
            players.append(Bot(race, ai, name=name))
    return players


def replay_path(given: str | None, bot: str, vs: str, ai_race: str | None, stamp: str) -> str | None:
    """--replay as given, None for 'none', else bot/out/replays/<core.replay_name> (the folder is created)."""
    if given:
        return None if given.lower() == "none" else given
    folder = HERE / "out" / "replays"
    folder.mkdir(parents=True, exist_ok=True)
    files = [(f.name, f.stat().st_mtime) for f in folder.glob("*.SC2Replay")]  # keep/ is a subfolder: never pruned
    for name in replays_to_prune(files, time.time()):
        (folder / name).unlink(missing_ok=True)
    return str(folder / replay_name(bot, vs, ai_race, stamp))


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.list:
        list_bots()
        return 0
    if args.games:
        print_games()
        return 0
    preset = preset_overrides(args)
    args.overrides = [*preset, *args.overrides]  # the games log records the preset's values too
    try:
        configs = build_configs(args)
        seats = plan_seats(
            args.bot, args.vs, args.two_v_two, args.bot2, args.race, args.ally_race, args.ally_difficulty, args.ai_race
        )
        map_ref = resolve_map(args.map)
        window = window_size(args.window)
        time_limit = parse_time_limit(args.time_limit)
        vs_name, vs_race = (args.bot2 or args.bot, None) if args.vs == "bot" else (args.vs, args.ai_race)
        args.replay = replay_path(args.replay, args.bot, vs_name, vs_race, time.strftime("%Y%m%d-%H%M%S"))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.coach_test and args.vs == "human":
        print("error: --coach-test is bot vs AI (the coach watches the bot's window)", file=sys.stderr)
        return 2
    realtime = args.realtime or args.vs == "human" or args.coach_test
    fullscreen = args.fullscreen or args.coach_test
    if time_limit is not None and args.vs == "human":
        print("error: --time-limit is for bot vs AI tests (your own games end when you leave)", file=sys.stderr)
        return 2
    if args.stock and len(seats) > 2:
        print("error: --stock is 1v1 only (python-sc2's run_game can't seat a 2v2)", file=sys.stderr)
        return 2

    where = f"{map_ref} (sent to SC2 as bytes)" if isinstance(map_ref, Path) else f"{map_ref} (SC2 Maps folder)"
    print(f"Map: {where}")
    for s in seats:
        who = {"human": "you", "computer": f"built-in AI ({s.difficulty})", "bot": f"bot {s.bot}"}[s.kind]
        print(f"  team {s.team}: {who}, {s.race}")
    limit = f"  time limit: {clock(time_limit)}" if time_limit else ""
    print(f"realtime={realtime}  steering file: {COMMAND_FILE}{limit}")
    if args.preset:
        print(f"preset {args.preset}: {' '.join(preset) or '(nothing for this bot)'}")
        mine = args.overrides[len(preset):]
        if mine:
            print(f"  then your --set: {' '.join(mine)}")
    if args.dry_run:
        return 0

    players = make_players(seats, configs)
    if args.speed > 0 and not realtime:
        for p in players:
            if getattr(p, "ai", None) is not None:
                p.ai.speed = args.speed
    announced = announce(seats, players, configs) if args.coach_test else None
    started = time.time()
    try:
        result = play(args, map_ref, players, realtime, window, fullscreen, time_limit)
    finally:
        if announced is not None:
            clear_bot_game(BOT_GAME_FILE, announced)
    results = result if isinstance(result, list) else [result]
    bots = [p.ai for p in players if getattr(p, "ai", None) is not None]  # run_game returns one result per bot
    if any(getattr(b, "won_by_surrender", False) for b in bots):
        from sc2.data import Result

        results = surrender_results(results, [getattr(b, "won_by_surrender", False) for b in bots], Result.Victory)
        print("the enemy surrendered (typed gg): counted as a Victory")
    teams = [s.team for s in seats if s.kind != "computer"]  # one result per participant, in seat order
    print(f"Result: {describe_result(results, time_limit, teams)}")
    file_replay(args.replay, results, args.vs)
    log_game(args, seats, players, results, started, desynced(results, teams))
    return 0


GAMES_FILE = HERE / "out" / "games.jsonl"


def git_commit(root: Path) -> str:
    """The commit checked out at root, read from .git directly: Windows git refuses the \\\\wsl.localhost share.
    In a worktree .git is a file, "gitdir: /home/.../.git/worktrees/<name>", a Linux path that Windows reads under
    root's own \\\\wsl.localhost\\<distro> anchor; the branches live in the main .git (the gitdir's "commondir").
    Worktree games logged no commit before (2026-11-04)."""
    git = root / ".git"
    try:
        if git.is_file():
            target = git.read_text().strip().removeprefix("gitdir:").strip()
            git = Path(root.anchor) / target.lstrip("/") if target.startswith("/") else root / target
        common = git / (git / "commondir").read_text().strip() if (git / "commondir").exists() else git
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        for d in (git, common):
            if (d / ref).exists():
                return (d / ref).read_text().strip()
        for line in (common / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        pass
    return ""


def bot_version(here: Path = HERE) -> dict:
    """Which bot played: the git commit plus a hash of bot/*.py and bot/micro/*.py, so uncommitted changes show as
    their own version (Dickie, 2026-10-31: "I wanna know which bot won which games")."""
    import hashlib

    h = hashlib.sha1()
    for p in sorted([*here.glob("*.py"), *here.glob("micro/*.py")]):
        h.update(p.relative_to(here).as_posix().encode())
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return {"commit": git_commit(here.parent)[:7], "code": h.hexdigest()[:8]}


def log_game(args, seats, players, results, started: float, desync: bool = False) -> None:
    """One JSON line per game in bot/out/games.jsonl: when, bot, opponent, map, result, game time, version, overrides,
    replay. `run.py --games` prints them. A desynced bot-vs-bot game (core.desynced) is logged as "Desync"."""
    import json

    bot_seat = next((n for n, s in enumerate(seats) if s.kind == "bot"), None)
    ai = getattr(players[bot_seat], "ai", None) if bot_seat is not None else None
    game_time = getattr(ai, "time", None) if ai is not None else None
    opp = [s for s in seats if s.team != (seats[bot_seat].team if bot_seat is not None else 0)]
    row = {
        "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(started)),
        "bot": args.bot,
        "vs": f"bot:{opp[0].bot}" if opp and opp[0].kind == "bot" else args.vs,
        "opp_race": opp[0].race if opp else "",
        "map": Path(str(args.map or "")).stem,
        "result": "Desync" if desync else str(results[0]).split(".")[-1] if results else "?",
        "game_time": clock(game_time) if game_time else "",
        **bot_version(),
        "overrides": list(args.overrides or []),
        "coach_test": bool(args.coach_test),
        "replay": Path(args.replay).name if args.replay else "",
    }
    try:
        GAMES_FILE.parent.mkdir(exist_ok=True)
        with GAMES_FILE.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        print(f"games log: {e}", file=sys.stderr)
    # coach.db through the helper, its one writer; the file above is the backup when the helper is down
    import urllib.request

    try:
        req = urllib.request.Request("http://127.0.0.1:8765/botgame", data=json.dumps(row).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            print(f"coach.db: bot game #{json.loads(r.read())['id']} ({row['commit']}+{row['code']})")
    except Exception as e:
        print(f"coach.db: not recorded ({e}); it's in {GAMES_FILE}")


def print_games(n: int = 30) -> None:
    import json

    if not GAMES_FILE.exists():
        print("no games logged yet")
        return
    rows = [json.loads(line) for line in GAMES_FILE.read_text(encoding="utf-8").splitlines() if line.strip()][-n:]
    for r in rows:
        sets = " ".join(r["overrides"])
        print(f"{r['at']}  {r['bot']:<7} vs {r['vs']} {r['opp_race']:<7} {r['map']:<14} {r['result']:<8} "
              f"{r['game_time']:>6}  {r['commit']}+{r['code']}  {sets}")


def file_replay(path: str | None, results: list, vs: str) -> None:
    """A win vs Elite or a cheating AI moves to replays/keep (never pruned, core.keep_replay)."""
    if not path or not Path(path).exists():
        return
    won = bool(results) and str(results[0]).endswith("Victory")
    if keep_replay(won, vs):
        keep = Path(path).parent / "keep"
        keep.mkdir(exist_ok=True)
        Path(path).replace(keep / Path(path).name)
        print(f"replay kept: {keep / Path(path).name}")


def announce(seats, players, configs, path=BOT_GAME_FILE) -> float:
    """--coach-test: tell the timing helper which player to follow (the first bot: its window is the fullscreen one
    the coach reads) and give it that bot's build as the plan. Returns the announcement's written_at."""
    import time

    i = next(n for n, s in enumerate(seats) if s.kind == "bot")
    seat = seats[i]
    name = getattr(players[i], "name", None) or seat.bot
    summary = (CONFIGS[seat.bot].__doc__ or seat.bot).strip().split(":", 1)[0]
    data = bot_game(name, seat.race, seat.bot, summary, configs[seat.bot].build, time.time())
    write_json_atomic(path, data)
    print(f"coach: following {name} ({seat.race}), {len(data['plan']['steps'])} plan steps -> {path}")
    return data["written_at"]


def play(args, map_ref, players, realtime, window, fullscreen, time_limit=None):
    if args.stock:
        from sc2 import maps
        from sc2.main import run_game

        m = maps.Map(map_ref) if isinstance(map_ref, Path) else maps.get(map_ref)
        return run_game(m, players, realtime=realtime, save_replay_as=args.replay, game_time_limit=time_limit)
    import match

    return asyncio.run(
        match.play(
            map_ref,
            players,
            realtime,
            save_replay=args.replay,
            game_time_limit=time_limit,
            window=window,
            fullscreen=fullscreen,
            # --fullscreen in bot vs AI: the bot's window goes fullscreen (it was always the small 640x480 one)
            watch=args.coach_test or (args.fullscreen and args.vs != "human"),
        )
    )


if __name__ == "__main__":
    sys.exit(main())
