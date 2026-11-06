"""Pure logic for the sparring bots: steering-file parser, build-order steps, macro decisions (supply ahead, spending
the bank, Gateway mix, auto-gg, where the waiting army stages), attack-when-ahead / retreat maths, config overrides,
map lookup, seat planning and the time limit. Stdlib only (no burnysc2), so test_core.py runs in WSL CI without
StarCraft II."""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
COMMAND_FILE = HERE / "out" / "command.json"
MAPS_DIR = HERE / "maps"
BOT_GAME_FILE = HERE.parent / "live" / "out" / "bot_game.json"   # --coach-test: who the coach should follow


# ---------------------------------------------------------------------------------------------------------------------
# Steering file: bot/out/command.json, read by every bot every `poll_seconds` of game time.
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Command:
    cheese: bool = False  # jump straight into the all-in / send the cheese now
    attack: bool = False  # send every army unit now, whatever the wave size
    stop: bool = False  # pull the army home and hold: no waves until attack or resume
    resume: bool = False  # undo stop: waves continue on the normal schedule
    gg: bool = False  # the bot types gg and leaves (ends the drill)
    pivot: bool = False  # ProxyRax: leave the cheese for the macro game now (other bots ignore it)
    seq: int | None = None  # optional; a command with seq <= the last seen seq is ignored

    @property
    def empty(self) -> bool:
        return not (self.cheese or self.attack or self.stop or self.resume or self.gg or self.pivot)

    def describe(self) -> str:
        keys = ("gg", "stop", "resume", "cheese", "pivot", "attack")
        return " ".join(k for k in keys if getattr(self, k)) or "nothing"


ACTION_KEYS = ("cheese", "attack", "stop", "resume", "gg", "pivot")


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"now", "true", "yes", "on", "1", "go"}
    return False


def parse_command(text: str) -> Command | None:
    """Parse the steering JSON. None for unreadable JSON or a non-object; unknown keys are ignored.
    Values: true / "now" / "yes" / 1 switch an action on, e.g. {"cheese": "now"}, {"stop": true}."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    seq = data.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, (int, float)):
        seq = None
    return Command(seq=None if seq is None else int(seq), **{k: _truthy(data.get(k)) for k in ACTION_KEYS})


class CommandReader:
    """Polls the steering file at most every `interval` seconds (game time) and returns each NEW command once.
    New = the file text changed and, when it carries a seq, the seq went up. prime() at game start marks whatever
    is already in the file as seen, so a command left over from the last game never fires."""

    def __init__(self, path: Path | str = COMMAND_FILE, interval: float = 2.0) -> None:
        self.path = Path(path)
        self.interval = float(interval)
        self._next = 0.0
        self._last_text: str | None = None
        self._last_seq: int | None = None

    def _read(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def prime(self) -> None:
        self._last_text = self._read()
        cmd = parse_command(self._last_text) if self._last_text is not None else None
        self._last_seq = cmd.seq if cmd else None

    def poll(self, now: float) -> Command | None:
        if now < self._next:
            return None
        self._next = now + self.interval
        text = self._read()
        if text is None or text == self._last_text:
            return None
        self._last_text = text
        cmd = parse_command(text)
        if cmd is None:
            return None
        if cmd.seq is not None:
            if self._last_seq is not None and cmd.seq <= self._last_seq:
                return None
            self._last_seq = cmd.seq
        return None if cmd.empty else cmd


def write_command(path: Path | str, seq: int, retries: int = 5, wait: float = 0.05, **actions: bool) -> dict:
    """Write a steering command atomically (temp file + rename) so a bot never reads half a file. The temp name is
    unique per call (two steer calls at once don't share one), and the rename is retried: on Windows it raises
    PermissionError while a bot has the file open for its read."""
    unknown = set(actions) - set(ACTION_KEYS)
    if unknown:
        raise ValueError(f"unknown action(s): {', '.join(sorted(unknown))}; valid: {', '.join(ACTION_KEYS)}")
    data = {"seq": int(seq), **{k: True for k, v in actions.items() if v}}
    write_json_atomic(path, data, retries, wait)
    return data


def write_json_atomic(path: Path | str, data: dict, retries: int = 5, wait: float = 0.05) -> None:
    """JSON to `path` via a unique temp file + rename (retried: Windows refuses it while a reader has the file open)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8", newline="\n")
    try:
        for attempt in range(retries + 1):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == retries:
                    raise
                time.sleep(wait * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)   # gone after a good rename; cleaned up if it never happened


# ---------------------------------------------------------------------------------------------------------------------
# Build orders: ordered steps "at supply S (and not before second T) have N of ITEM".
# ---------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Step:
    supply: int  # start once supply used >= this
    item: str  # unit/structure name as in python-sc2's UnitTypeId, CamelCase: "SupplyDepot", "Barracks"
    count: int  # how many we want in total (done + in progress) once this step is met
    at: float = 0.0  # earliest game second (0 = no time gate)


def steps(*rows) -> tuple[Step, ...]:
    """steps((14, "SupplyDepot", 1), (15, "Barracks", 1, 40), ...) -> tuple of Step."""
    return tuple(Step(*r) for r in rows)


def next_step(order: tuple[Step, ...], supply: float, counts: dict[str, int], now: float = 0.0) -> Step | None:
    """The first unmet step, if its supply and time gates are open. Never skips ahead: while the first unmet step
    is gated, nothing later is returned either (a build order is a sequence)."""
    for s in order:
        if counts.get(s.item, 0) >= s.count:
            continue
        return s if supply >= s.supply and now >= s.at else None
    return None


def gate_supply(supply: float, all_in: bool, workers: float, worker_cap: int, training: bool) -> float:
    """The supply next_step should gate on. The gates pace the build to worker production; when that has stopped
    supply only rises if army is training, so a gate above it would stall the build for good ("cheese now" stops
    workers before the 19-supply Cyber Core; DT with defenders=0 sits at 22 Probes below the 23-supply Dark Shrine).
    Then the gates are open (200) and the build goes on as money allows."""
    stalled = all_in or (workers >= worker_cap and not training)
    return 200.0 if stalled else supply


def known_orders(orders, abilities) -> list:
    """The unit orders (protos with .ability_id) whose ability is in `abilities` (python-sc2's game_data.abilities).
    A newer SC2 client can issue ability ids the installed library doesn't know; those orders are skipped instead of
    raising KeyError for the whole step."""
    return [o for o in orders if o.ability_id in abilities]


def opening_done(order: tuple[Step, ...], counts: dict[str, int]) -> bool:
    return all(counts.get(s.item, 0) >= s.count for s in order)


# ---------------------------------------------------------------------------------------------------------------------
# Macro decisions after the opening (pure, so they are tested without SC2): supply ahead, spending the bank, gg.
# ---------------------------------------------------------------------------------------------------------------------
SUPPLY_PER_BUILDING = 8  # a Supply Depot or Pylon


def supply_margin(prod: int, townhalls: int, base: int = 2, per_prod: int = 2, per_townhall: int = 2) -> int:
    """Free supply to keep ahead: each production building and townhall can add ~2 supply while a depot/pylon
    builds (a Marine every 18 s, a Zealot every 27 s, ~25 s for an SCV to walk out and build)."""
    return base + per_prod * max(0, prod) + per_townhall * max(0, townhalls)


def supply_pending_cap(prod: int, hard_cap: int = 3) -> int:
    """How many supply buildings may be under way at once: one, plus one per 3 production buildings (6-rax needs
    two), at most hard_cap."""
    return max(1, min(hard_cap, 1 + max(0, prod) // 3))


def need_supply(supply_left: float, supply_cap: float, pending: float, margin: int, pending_cap: int,
                per_building: int = SUPPLY_PER_BUILDING) -> bool:
    """Start another supply building? Supply already under way counts as free (no double-building), the 200 cap
    stops it, and no more than pending_cap are under way at once."""
    if supply_cap >= 200 or pending >= pending_cap:
        return False
    return supply_left + per_building * pending < margin


def want_production(minerals: float, now: float, opening_complete: bool, have: int, pending: float, cap: int,
                    bank: int, after: float = 0.0) -> bool:
    """Spend the bank: after the opening (and not before second `after`), with at least `bank` minerals, add one
    more production building up to `cap` (done + in progress). One under way at a time, two once the bank is double."""
    if not opening_complete or now < after or have >= cap or bank <= 0 or minerals < bank:
        return False
    return pending < min(2, int(minerals // bank))


GATEWAY_COST = {"DarkTemplar": (125, 125), "Stalker": (125, 50), "Adept": (100, 25), "Zealot": (100, 0)}
GATEWAY_MIX = ("Zealot", "Stalker", "Adept")  # what an idle Gateway rotates through when it can't make a DT


def gateway_unit(minerals: float, gas: float, shrine: bool, core_ready: bool, dts: int, hunters: int, bank: int,
                 turn: int = 0) -> str | None:
    """What an idle Gateway trains (DT rush after its opening). The Dark Shrine is the point of the bot, so:
    - Shrine done: a DT whenever it's affordable. While fewer than `hunters` DTs exist the gas stays reserved for DTs
      (Zealots only, with spare minerals). After that the mix rotates Zealot / Stalker / Adept from `turn`.
    - Shrine not done: gas stays for the tech/DTs; only minerals at or above `bank` buy Zealots (the DT timing is
      unchanged; the Stalker defenders are handled by the bot).
    None = nothing worth making now."""
    def ok(name: str) -> bool:
        m, g = GATEWAY_COST[name]
        return minerals >= m and gas >= g

    if not shrine:
        return "Zealot" if minerals >= bank and ok("Zealot") else None
    if ok("DarkTemplar"):
        return "DarkTemplar"
    if dts < hunters:
        return "Zealot" if minerals >= bank and ok("Zealot") else None
    for i in range(len(GATEWAY_MIX)):
        name = GATEWAY_MIX[(turn + i) % len(GATEWAY_MIX)]
        if (core_ready or name == "Zealot") and ok(name):
            return name
    return None


def staging_point(home: tuple[float, float], target: tuple[float, float], proxy: tuple[float, float] | None = None,
                  proxy_alive: bool = False, forward: float = 0.0) -> tuple[float, float]:
    """Where the army waits for its next wave (new units walk there; waves leave from there).
    - A proxy bot with a proxy Barracks still standing: at the proxy (near the enemy natural), so its Marines don't
      walk home and every wave doesn't cross the map twice (seen in a ProxyRax game: waves of 4 arriving piecemeal).
    - Otherwise `forward` (0..1) of the way from home toward the target: 0 = home (the waiting army defends the
      base), 0.5 = mid-map."""
    if proxy is not None and proxy_alive:
        return (float(proxy[0]), float(proxy[1]))
    f = min(max(forward, 0.0), 1.0)
    return (home[0] + (target[0] - home[0]) * f, home[1] + (target[1] - home[1]) * f)


def has_lost(now: float, townhalls: int, units: int, gg_time: float = 360.0, min_units: int = 6,
             no_base_units: int = 5) -> bool:
    """Clearly lost: no townhall and fewer than no_base_units units, or (from gg_time on) fewer than min_units
    army + workers in total. Then the bot types gg and leaves, so Dickie isn't hunting buildings."""
    if townhalls <= 0 and units < no_base_units:
        return True
    return now >= gg_time and units < min_units


def gg_due(lost: bool, since: float | None, now: float, confirm: float = 5.0) -> tuple[bool, float | None]:
    """Debounce has_lost: gg only once it has held for `confirm` game seconds (a townhall dying while the next one
    finishes, a blink of the observation). Returns (leave now?, the new `since`)."""
    if not lost:
        return False, None
    if since is None:
        return confirm <= 0, now
    return now - since >= confirm, since


def parse_time_limit(text: str | None) -> int | None:
    """run.py --time-limit: 'm:ss' or seconds -> whole game seconds (python-sc2's game_time_limit). None/'' = no
    limit. Raises ValueError for anything unreadable or not positive."""
    if text is None or not str(text).strip():
        return None
    try:
        sec = mmss(text)
    except ValueError:
        raise ValueError(f"--time-limit needs m:ss or seconds, got {text!r}") from None
    if not 0 < sec <= 24 * 3600:
        raise ValueError(f"--time-limit must be between 0:01 and 24 hours, got {text!r}")
    return int(round(sec))


def viking_pull_back(dist: float, leash: float, air_in_cover: bool) -> bool:
    """A Viking `dist` from its wave's closest bio unit flies back to it: past `leash`, unless it's chasing air the bio
    covers (an air target near the bio), and never past twice the leash. leash 0 = off."""
    return leash > 0 and dist > leash and (not air_in_cover or dist > 2 * leash)


def enemy_surrendered(chat, me: int) -> bool:
    """Someone other than us typed gg / surrender (the built-in AI types gg when it gives up).
    chat: (player_id, text)."""
    for pid, text in chat:
        if pid != me and any(w.strip(".!,?") in ("gg", "surrender") for w in str(text).lower().split()):
            return True
    return False


def surrender_results(results: list, surrendered: list[bool], victory) -> list:
    """A bot that left because the enemy surrendered counts as a Victory (leaving is a Defeat to the API)."""
    return [victory if i < len(surrendered) and surrendered[i] else r for i, r in enumerate(results)]


def desynced(results: list, teams: list[int] | None) -> bool:
    """Bot vs bot: more than one team got a Victory, so SC2's clients simulated different games and the game does
    not count. DT vs MMM scan-dt-1 and -6 (2026-11-04): from ~1:30 each client's copy dropped the other bot's orders
    (MMM's replay: DtRush never paid for its Cyber Core; DtRush's log: Core, five Gateways, 86 supply), and each bot
    won its own copy. teams: each participant's team, in result order."""
    won = {t for r, t in zip(results, teams or []) if getattr(r, "name", None) == "Victory"}
    return len(won) > 1


def describe_result(results: list, time_limit: int | None = None, teams: list[int] | None = None) -> str:
    """One line for the end of run.py: each participant's result by name (Victory / Defeat / Tie / an error), and
    a Tie under --time-limit says the limit stopped the game (no winner), not that the game was drawn."""
    names = [getattr(r, "name", None) or f"{type(r).__name__}: {r}" for r in results]
    line = ", ".join(names) or "no result"
    if time_limit and "Tie" in names:
        line += f" (time limit {clock(time_limit)} reached: stopped, no winner)"
    if desynced(results, teams):
        line += " (DESYNC: both teams won, the SC2 clients played different games; it does not count)"
    return line


def check_order(order: tuple[Step, ...]) -> list[str]:
    """Sanity rules for a build order: supply 12..200, counts >= 1, supply never decreasing, counts per item rising."""
    errs, last_supply, last_count = [], 0, {}
    for i, s in enumerate(order):
        where = f"step {i} ({s.supply} {s.item} x{s.count})"
        if not 12 <= s.supply <= 200:
            errs.append(f"{where}: supply out of range")
        if s.count < 1:
            errs.append(f"{where}: count < 1")
        if s.supply < last_supply:
            errs.append(f"{where}: supply goes down")
        if s.count <= last_count.get(s.item, 0):
            errs.append(f"{where}: count does not rise for {s.item}")
        if not s.item or not s.item[0].isupper() or not s.item.isalnum():
            errs.append(f"{where}: item must be a CamelCase unit name")
        last_supply, last_count[s.item] = s.supply, s.count
    return errs


# ---------------------------------------------------------------------------------------------------------------------
# Config overrides: run.py --set key=value
# ---------------------------------------------------------------------------------------------------------------------
def mmss(v: str | float | int) -> float:
    """'3:30' -> 210.0; plain numbers are seconds."""
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).strip()
    if ":" in v:
        m, s = v.split(":", 1)
        return int(m) * 60 + float(s)
    return float(v)


def apply_overrides(cfg, pairs: list[str], ignore_unknown: bool = False):
    """Return a copy of the config dataclass with key=value overrides applied (types follow the defaults;
    *_time fields also take m:ss). Tuples (build orders) are not overridable from the command line.
    ignore_unknown: skip keys this config doesn't have (2v2 with two different bots)."""
    by_name = {f.name: f for f in fields(cfg)}
    changes = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--set needs key=value, got {pair!r}")
        key, raw = (x.strip() for x in pair.split("=", 1))
        if key not in by_name:
            if ignore_unknown:
                continue
            raise ValueError(f"unknown setting {key!r}; valid: {', '.join(sorted(by_name))}")
        cur = getattr(cfg, key)
        if isinstance(cur, bool):
            if raw.lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError(f"{key} needs true/false, got {raw!r}")
            changes[key] = raw.lower() in {"true", "1", "yes", "on"}
        elif isinstance(cur, int):
            changes[key] = int(raw)
        elif isinstance(cur, float):
            changes[key] = mmss(raw) if key.endswith("_time") else float(raw)
        elif isinstance(cur, str):
            changes[key] = raw
        else:
            raise ValueError(f"{key} can't be set from the command line")
    return replace(cfg, **changes)


# ---------------------------------------------------------------------------------------------------------------------
# Maps and seats
# ---------------------------------------------------------------------------------------------------------------------
def resolve_map(arg: str | None, maps_dir: Path = MAPS_DIR) -> Path | str:
    """A map argument -> an .SC2Map Path inside the project (sent to SC2 as bytes) or a bare name (looked up in the
    SC2 install's Maps folder by python-sc2). Order: existing file path; maps_dir/<name>.SC2Map (case-insensitive,
    subfolders too); otherwise the name. No argument: the only map in maps_dir."""
    found = sorted(maps_dir.rglob("*.SC2Map")) if maps_dir.is_dir() else []
    if not arg:
        if len(found) == 1:
            return found[0]
        names = ", ".join(p.stem for p in found) or "none"
        raise ValueError(f"--map is required (maps in {maps_dir}: {names})")
    p = Path(arg)
    if p.suffix.lower() == ".sc2map" and p.is_file():
        return p.resolve()
    stem = p.stem if p.suffix.lower() == ".sc2map" else arg
    for f in found:
        if f.stem.lower() == stem.lower():
            return f
    return stem


@dataclass(frozen=True)
class Seat:
    kind: str  # "human" | "bot" | "computer"
    race: str  # "terran" | "protoss" | "zerg" | "random"
    team: int  # 1 or 2 (intended; see README: the map's lobby decides real teams)
    bot: str | None = None  # bot key for kind == "bot"
    difficulty: str | None = None  # for kind == "computer"


BOT_RACES = {"sixrax": "terran", "dt": "protoss", "proxy": "terran", "mmm": "terran"}
KEEP_WINS_VS = ("veryhard", "cheatvision", "cheatmoney", "cheatinsane")  # wins vs these go to replays/keep


def replays_to_prune(files: list[tuple[str, float]], now: float, max_age_days: float = 14, max_count: int = 40
                     ) -> list[str]:
    """Replay names to delete from bot/out/replays (not keep/): (name, mtime) older than max_age_days, and beyond the
    newest max_count (Dickie: \"clean up replays on a rolling timeline if they're not flagged\")."""
    newest = sorted(files, key=lambda f: f[1], reverse=True)
    cutoff = now - max_age_days * 86400
    return [name for i, (name, mtime) in enumerate(newest) if i >= max_count or mtime < cutoff]


def keep_replay(won: bool, vs: str) -> bool:
    """Flag a replay as interesting (moved to replays/keep, never pruned): a win vs Elite or a cheating AI."""
    return won and vs in KEEP_WINS_VS


def replay_name(bot: str, vs: str, ai_race: str | None, stamp: str) -> str:
    """Default replay file name for a bot game: mmm-vs-veryhard-zerg-20261031-010613.SC2Replay."""
    race = f"-{ai_race}" if ai_race and vs != "human" else ""
    return f"{bot}-vs-{vs}{race}-{stamp}.SC2Replay"


# in-game names: Very Easy .. Harder, Very Hard, Elite, then Cheater 1 (Vision), 2 (Resources), 3 (Insane)
DIFFICULTIES = (
    "veryeasy", "easy", "medium", "mediumhard", "hard", "harder", "veryhard",
    "cheatvision", "cheatmoney", "cheatinsane",
)


BOT_RACES = {"sixrax": "terran", "dt": "protoss", "proxy": "terran", "mmm": "terran", "skytoss": "protoss"}


def plan_seats(
    bot: str,
    vs: str,
    two_v_two: bool = False,
    bot2: str | None = None,
    human_race: str = "terran",
    ally_race: str = "protoss",
    ally_difficulty: str = "hard",
    ai_race: str = "random",
) -> list[Seat]:
    """Who sits where, in create-game order (humans first so Dickie's window opens first).
    1v1 vs human: Dickie vs bot.        1v1 vs AI: bot vs built-in AI.
    2v2 vs human: Dickie + AI ally vs bot + bot2.   2v2 vs AI: bot + bot2 vs two built-in AIs.
    1v1 vs bot: bot vs bot2 (default: the same bot), both on the custom runner."""
    if bot not in BOT_RACES or (bot2 is not None and bot2 not in BOT_RACES):
        raise ValueError(f"bot must be one of {', '.join(BOT_RACES)}")
    if vs not in ("human", "bot") and vs not in DIFFICULTIES:
        raise ValueError(f"--vs must be human, bot or one of {', '.join(DIFFICULTIES)}")
    if vs == "bot":
        if two_v_two:
            raise ValueError("--vs bot is 1v1 (bot vs bot2)")
        b2key = bot2 or bot
        return [Seat("bot", BOT_RACES[bot], 1, bot=bot), Seat("bot", BOT_RACES[b2key], 2, bot=b2key)]
    if ally_difficulty not in DIFFICULTIES:
        raise ValueError(f"ally difficulty must be one of {', '.join(DIFFICULTIES)}")
    b1 = Seat("bot", BOT_RACES[bot], 2 if vs == "human" else 1, bot=bot)
    if not two_v_two:
        if vs == "human":
            return [Seat("human", human_race, 1), b1]
        return [b1, Seat("computer", ai_race, 2, difficulty=vs)]
    b2key = bot2 or bot
    b2 = Seat("bot", BOT_RACES[b2key], b1.team, bot=b2key)
    if vs == "human":
        return [Seat("human", human_race, 1), Seat("computer", ally_race, 1, difficulty=ally_difficulty), b1, b2]
    return [b1, b2, Seat("computer", ai_race, 2, difficulty=vs), Seat("computer", ai_race, 2, difficulty=vs)]


def needs_custom_runner(seats: list[Seat]) -> bool:
    """python-sc2's stock run_game handles one participant + AIs, or exactly two participants and no AI."""
    participants = sum(s.kind != "computer" for s in seats)
    computers = len(seats) - participants
    return participants > 2 or (participants == 2 and computers > 0)


# ---------------------------------------------------------------------------------------------------------------------
# --coach-test: the bot announces itself to the coach's timing helper (live/out/bot_game.json), so the helper follows
# the bot (its name, race and result) and cues the bot's own build instead of Dickie's Terran plan.
# ---------------------------------------------------------------------------------------------------------------------
# The coach's cue engine needs a clock time per step; a bot build is gated by supply only. So each step gets an
# estimated time from its supply (standard-opening times from live/plans*.json: 14 depot/pylon ~0:18, 16 ~0:40,
# 19 ~1:25, 20 ~1:40, then ~10 s per supply), never before its `at` gate and at least STEP_GAP after the step before
# it (several buildings at one supply wait for minerals). The engine's "now" catch-up cues still go by supply.
SUPPLY_SEC = {12: 0, 13: 10, 14: 18, 15: 30, 16: 40, 17: 50, 18: 62, 19: 85, 20: 100, 21: 115, 22: 130, 23: 140,
              24: 150}
STEP_GAP = 15
BOT_GAME_STALE = 600.0   # the helper ignores an announcement older than this (a crashed run.py leaves it behind)


def supply_sec(supply: int) -> int:
    """Estimated game second a standard opening reaches `supply`."""
    if supply <= 12:
        return 0
    top = max(SUPPLY_SEC)
    return SUPPLY_SEC[supply] if supply <= top else SUPPLY_SEC[top] + 10 * (supply - top)


def clock(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def item_name(item: str) -> str:
    """'CyberneticsCore' -> 'Cybernetics Core' (the plan files' wording)."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", item)


def coach_steps(build: tuple[Step, ...]) -> list[dict]:
    """A bot build as coach plan steps {sec, t, supply, action, note}, in order, sec never going back."""
    out: list[dict] = []
    prev: int | None = None
    for s in build:
        sec = max(supply_sec(s.supply), int(s.at))
        if prev is not None:
            sec = max(sec, prev + STEP_GAP)
        action = item_name(s.item) + (f" #{s.count}" if s.count > 1 else "")
        out.append({"sec": sec, "t": clock(sec), "supply": s.supply, "action": action,
                    "note": f"Bot step at {s.supply} supply (time estimated)"})
        prev = sec
    return out


def bot_game(player: str, race: str, bot: str, summary: str, build: tuple[Step, ...], now: float) -> dict:
    """The announcement run.py --coach-test writes before the game: who to follow and their build as a plan."""
    return {"player": player, "race": race, "bot": bot, "written_at": now,
            "plan": {"name": f"{player}: {summary}", "steps": coach_steps(build)}}


def clear_bot_game(path: Path | str, written_at: float) -> bool:
    """Delete the announcement after the game, unless another run has replaced it since."""
    path = Path(path)
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("written_at") != written_at:
            return False
        path.unlink()
        return True
    except (OSError, ValueError, AttributeError):
        return False


def wave_size(cfg: Any, t: float) -> int:
    """Units a later wave waits for: cfg.wave_size, or cfg.late_wave from cfg.late_time on (0 = never)."""
    late = cfg.late_time > 0 and t >= cfg.late_time
    return int(max(cfg.wave_size, cfg.late_wave)) if late else int(cfg.wave_size)


# ---------------------------------------------------------------------------------------------------------------------
# Attack only when ahead, regroup when losing (every bot's army_control). Supply = the units' food cost.
# ---------------------------------------------------------------------------------------------------------------------
class EnemyMemory:
    """The enemy army seen lately: each combat unit by tag with its supply and when it was last seen. A unit counts
    until `keep` game seconds after it was last seen (it went back into the fog, it is still out there) or until it
    dies (forget). The caller passes only combat units: no workers, overlords or structures."""

    def __init__(self, keep: float = 75.0) -> None:
        self.keep = float(keep)
        self._seen: dict[int, tuple[float, float]] = {}  # tag -> (supply, last seen)

    def see(self, units, now: float) -> None:
        """units: (tag, supply) pairs visible now."""
        for tag, supply in units:
            self._seen[tag] = (float(supply), now)

    def forget(self, tag: int) -> None:
        self._seen.pop(tag, None)

    def supply(self, now: float) -> float:
        """Remembered enemy army supply (drops the ones not seen for `keep` seconds)."""
        self._seen = {t: (s, at) for t, (s, at) in self._seen.items() if now - at <= self.keep}
        return sum(s for s, _ in self._seen.values())

    def __len__(self) -> int:
        return len(self._seen)


def army_ahead(ours: float, theirs: float, ratio: float) -> bool:
    """May a wave leave? Our attacking army supply must be at least `ratio` x the remembered enemy army. Nothing
    seen yet (theirs 0: a cheese's first wave) or ratio <= 0 (off): yes."""
    if ratio <= 0 or theirs <= 0:
        return True
    return ours >= ratio * theirs


def third_due(scvs: int, now: float, supply: float, third_scvs: int, third_time: float, third_supply: float) -> bool:
    """Is the third Command Center due? At third_scvs SCVs or third_time (both only while third_time > 0), or at
    third_supply supply used (> 0; Dickie, 2026-10-31: "force a third at like 100 supply": the baseline bot mined out
    two bases by 13:00 with the initiative). Neither switch on: never (ba15df0)."""
    if third_time > 0 and (scvs >= third_scvs or now >= third_time):
        return True
    return third_supply > 0 and supply >= third_supply


def third_fly_action(at_spot: bool, flying: bool, ready: bool, busy: bool, contested: bool) -> str:
    """What the third Command Center does this step (MMM cfg.third_fly: built in the main, flown out; Dickie,
    2026-11-04: "we haven't figured out third base yet": 0-5 vs Insane Zerg on Acropolis, the CC built in place at
    5:30 sat alone for 100 s and died). at_spot: landed within 3 of the third; contested: enemy ground at the spot.
    "wait" (building, or training/morphing), "lift", "land", "hover" (fly back over the main until the spot is clear),
    "landed" (done: morph it)."""
    if at_spot and not flying:
        return "landed"
    if not ready or busy:
        return "wait"
    if not flying:
        return "wait" if contested else "lift"
    return "hover" if contested else "land"


def third_morph(enemy_race: str, pf_races: str, ebay_ready: bool) -> str:
    """The landed third: a Planetary Fortress (150/150, needs an Engineering Bay) vs the races in pf_races (comma
    list, "" = never), else an Orbital like the other bases."""
    races = {r.strip().lower() for r in pf_races.split(",") if r.strip()}
    return "pf" if enemy_race.lower() in races and ebay_ready else "orbital"


def third_stage_point(natural: tuple[float, float], third: tuple[float, float], frac: float) -> tuple[float, float]:
    """Where the waiting army gathers while the third is wanted: `frac` (0..1) of the way from the natural toward
    the third, so an attack on the third meets the bio first."""
    return staging_point(natural, third, forward=frac)


def should_recall(home_threat: float, wave: float, frac: float, min_supply: float = 6.0) -> bool:
    """Bring the sent wave home? Yes when the enemy at our buildings has at least `min_supply` and at least `frac` of
    the wave's supply: a wave that keeps hitting his base while his army kills ours loses the base race (Dickie,
    2026-10-31, vs Insane Protoss on Acropolis). frac <= 0: off (ba15df0)."""
    if frac <= 0 or wave <= 0:
        return False
    return home_threat >= min_supply and home_threat >= frac * wave


def should_retreat(local_ours: float, local_theirs: float, ratio: float) -> bool:
    """Pull a sent group back? Yes while the enemy army near it outweighs it: ours < ratio x theirs. ratio <= 0
    (off) or no enemy army near: no."""
    if ratio <= 0 or local_theirs <= 0:
        return False
    return local_ours < ratio * local_theirs


def local_fight(ours: list[tuple[float, float, float]], theirs: list[tuple[float, float, float]],
                radius: float = 12.0, reach: float | None = None) -> tuple[float, float]:
    """(our supply, their supply) at the front of a sent group. ours / theirs: (x, y, supply) per unit. The front is
    the centre of our units with an enemy army unit within `radius` (the ones fighting, so a second wave trailing
    behind doesn't pull the centre away). Theirs are summed within `radius` of it; ours within `reach` (default
    `radius`), so the rest of a wave seconds from arriving counts (test 18: the 2 lead Marines of 17 met 6 supply and
    the whole wave turned back). No contact: (0, 0)."""
    r2 = radius * radius
    reach2 = (reach if reach is not None else radius) ** 2

    def dist2(a, b) -> float:
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    engaged = [u for u in ours if any(dist2(u, e) <= r2 for e in theirs)]
    if not engaged:
        return 0.0, 0.0
    c = (sum(u[0] for u in engaged) / len(engaged), sum(u[1] for u in engaged) / len(engaged))
    return (sum(u[2] for u in ours if dist2(u, c) <= reach2), sum(e[2] for e in theirs if dist2(e, c) <= r2))


def enemy_to_beat(remembered: float, beaten_by: float, beaten_at: float | None, now: float, hold: float) -> float:
    """The enemy army a wave must out-number: the remembered army, or for `hold` s after a retreat the army at the
    front that made us retreat, whichever is bigger. Demo 22: after retreating from 12 supply the next wave left on
    "8 vs 6 remembered" and lost again; Dickie: "they're not massing up, they're pacing around the map"."""
    if beaten_at is None or hold <= 0 or now - beaten_at > hold:
        return remembered
    return max(remembered, beaten_by)


def wave_supply(ours: list[tuple[float, float, float]]) -> float:
    """Supply of a whole sent wave, (x, y, supply) per unit: what a retreat compares with the enemy at its front."""
    return float(sum(u[2] for u in ours))


def cooled(last: float | None, now: float, cooldown: float) -> bool:
    """True once `cooldown` seconds have passed since `last` (None = never happened)."""
    return last is None or now - last >= cooldown


# ---------------------------------------------------------------------------------------------------------------------
# ProxyRax pivot: once its cheese has stalled, the proxy bot turns into a 2-base bio macro game (bio.BioBot).
# ---------------------------------------------------------------------------------------------------------------------
def held_since(holding: bool, since: float | None, now: float) -> float | None:
    """When a condition started holding without a break: `since` (or now, if it just started) while it holds, None
    once it doesn't."""
    if not holding:
        return None
    return now if since is None else since


def pivot_reasons(now: float, pivot_time: float, bank_held: float, bank_hold: float, proxy_alive: bool,
                  retreats: int, holds: int, min_retreats: int = 1, min_holds: int = 1) -> list[str]:
    """Why ProxyRax should leave its cheese for macro now ([] = not yet). Never before pivot_time (0 = never); from
    then on any sign the cheese has stalled: the bank stayed at pivot_bank+ for `bank_hold` s (`bank_held` s so far),
    the proxy Barracks are gone, `min_retreats` waves were pulled back or `min_holds` waves were held for not being
    ahead (min_* 0 = that sign off)."""
    if pivot_time <= 0 or now < pivot_time:
        return []
    why = []
    if bank_held >= bank_hold:
        why.append("bank")
    if not proxy_alive:
        why.append("proxy dead")
    if min_retreats > 0 and retreats >= min_retreats:
        why.append("retreats")
    if min_holds > 0 and holds >= min_holds:
        why.append("waves held")
    return why


def pivot_config(cfg: Any) -> Any:
    """The ProxyRax config after the pivot (a copy; the frozen original stays): SCVs to pivot_workers, Barracks up to
    pivot_rax from a pivot_bank bank, waves of pivot_wave that gather at home, supply for Reactor Barracks."""
    wave = int(cfg.pivot_wave)
    return replace(cfg, worker_cap=cfg.pivot_workers, prod_cap=cfg.pivot_rax, spend_bank=cfg.pivot_bank,
                   first_wave=wave, min_wave=wave, wave_size=wave, late_wave=max(wave, cfg.late_wave),
                   stage_forward=0.0, supply_per_prod=max(cfg.supply_per_prod, 3))


def gas_target(townhalls: int, cap: int) -> int:
    """Refineries wanted after the pivot: two per finished townhall, at most `cap`."""
    return max(0, min(cap, 2 * townhalls))


# ---------------------------------------------------------------------------------------------------------------------
# MMM (2-base Terran macro): placement, add-ons, research, unit mix and the first push. Pure, so tested without SC2.
# ---------------------------------------------------------------------------------------------------------------------
Point = tuple[float, float]
UNIT_SUPPLY = {"Marine": 1, "Reaper": 1, "Marauder": 2, "Medivac": 2, "Siege Tank": 3, "Viking": 2}


def _dist(a: Point, b: Point) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def wall_depot(corners: list[Point], taken: list[Point], tol: float = 1.0) -> Point | None:
    """The next ramp-wall depot spot: the first corner (in a stable order) with no supply depot on it yet. None once
    both corners are taken (later depots go behind the mineral line) or the ramp has no wall spots."""
    for c in sorted(corners):
        if all(_dist(c, t) > tol for t in taken):
            return (float(c[0]), float(c[1]))
    return None


def behind_minerals(townhall: Point, minerals: Point, extra: float = 3.0) -> Point:
    """A spot just behind the mineral line (out of the way of the army and the mining): from the townhall through the
    centre of its mineral fields, `extra` further on."""
    d = _dist(townhall, minerals)
    if d < 1e-6:
        return (float(minerals[0]), float(minerals[1]))
    k = (d + extra) / d
    return (townhall[0] + (minerals[0] - townhall[0]) * k, townhall[1] + (minerals[1] - townhall[1]) * k)


def production_spots(start: Point, minerals: Point, step_x: int = 7, step_y: int = 4, reach: int = 3,
                     min_dist: float = 7.0) -> list[Point]:
    """Candidate centres for Barracks / Factory / Starport in the main, nearest the townhall first. A grid (7 apart
    in x: 3 building + 2 add-on + a 2-wide lane; 4 apart in y: a 1-wide lane), never on the mineral-line side of
    the townhall and not hugging it. Offsets are whole numbers, so a start at .5 keeps 3x3 centres on .5 and the
    add-on (+2.5, -0.5) on whole numbers. SC2 still has the last word (can_place)."""
    mx, my = minerals[0] - start[0], minerals[1] - start[1]
    out = []
    for i in range(-reach, reach + 1):
        for j in range(-2 * reach, 2 * reach + 1):
            dx, dy = i * step_x, j * step_y
            if (dx, dy) == (0, 0) or dx * mx + dy * my > 0:
                continue
            p = (start[0] + dx, start[1] + dy)
            if _dist(p, start) >= min_dist:
                out.append(p)
    return sorted(out, key=lambda p: (_dist(p, start), p))


def depot_action(lowered: bool, enemy_near: bool) -> str | None:
    """Wall depots: lowered so our units walk out, raised while enemy ground units are at them."""
    if enemy_near and lowered:
        return "raise"
    if not enemy_near and not lowered:
        return "lower"
    return None


def reaper_action(hp_frac: float, healing: bool, weapon_ready: bool, threat_dist: float | None, worker_near: bool,
                  leave_hp: float = 0.5, back_hp: float = 0.95, kite: float = 7.0, threats: int = 0) -> str:
    """What a raiding Reaper does this step (Dickie: \"he got some kills but then just took potshots at the ling
    pool\"): 'heal' (walk home to regenerate below leave_hp, until back_hp), 'kite' (a threat within `kite` while
    reloading), 'hunt' (shoot the closest worker), else 'scout' (walk on toward the enemy base, never shooting
    buildings)."""
    if hp_frac < leave_hp or (healing and hp_frac < back_hp):
        return "heal"
    if threat_dist is not None and threat_dist < kite and (not weapon_ready or threats >= 2):
        return "kite"  # reloading, or 2+ threats close: never trade into a crowd (Dickie: "more cautious")
    return "hunt" if worker_near else "scout"


def should_cancel(progress: float, health_frac: float, enemy_near: bool, below: float = 0.5) -> bool:
    """An unfinished building under attack is cancelled (75% refund) before it dies (Dickie, demo 23: "could have
    also cancelled the natural expo rather than let it get cooked"). Health is judged against what the building has
    at this progress (10% at the start, growing to 100%): plain health < 35% cancelled every fresh depot and
    Refinery an enemy walked past ("10% health, 0% built", demos 28, 31)."""
    return 0 < progress < 1 and enemy_near and health_frac < below * (0.1 + 0.9 * progress)


def mining_side(p: Point, th: Point, minerals: Point, pad: float = 5.0) -> bool:
    """Whether p is on the mineral-line side of a townhall, within reach of its minerals (+pad): no production
    building goes there (Dickie, 2026-10-31: "bot is trying to build a factory in the mineral line")."""
    mx, my = minerals[0] - th[0], minerals[1] - th[1]
    return (p[0] - th[0]) * mx + (p[1] - th[1]) * my > 0 and _dist(p, th) < _dist(th, minerals) + pad


def main_cc_spots(start: Point, minerals: Point, grid: list[Point], keep: int, reach: int = 22,
                  min_dist: float = 8.0) -> list[Point]:
    """Candidate centres for a Command Center built in the main to fly out (MMM third_fly), nearest the townhall
    first: off its mining side, not hugging it, and clear of the first `keep` production-grid spots (3x3 building +
    add-on at +2.5, -0.5, plus a 1-wide lane) that the Barracks / Factory / Starports will take. The rest of the grid
    is fair game: production_spots tiles the whole main, so keeping all of it clear leaves no room (game 1,
    2026-11-04: the third was built at the spot instead). Whole offsets from `start` keep the 5x5 centre on its .5.
    SC2 still has the last word (can_place, plus a same-height check in the bot)."""
    kept = grid[:keep]

    def clear(p: Point) -> bool:
        for g in kept:
            if abs(p[0] - g[0]) < 5 and abs(p[1] - g[1]) < 5:  # CC half 2.5 + building half 1.5 + lane 1
                return False
            if abs(p[0] - (g[0] + 2.5)) < 4.5 and abs(p[1] - (g[1] - 0.5)) < 4.5:  # add-on half 1
                return False
        return True

    out = []
    for dx in range(-reach, reach + 1):
        for dy in range(-reach, reach + 1):
            p = (start[0] + dx, start[1] + dy)
            d = _dist(p, start)
            if min_dist <= d <= reach and not mining_side(p, start, minerals) and clear(p):
                out.append(p)
    return sorted(out, key=lambda p: (_dist(p, start), p))


def depot_spots(th: Point, minerals: Point, behind: Point, production: list[Point], grid: list[Point],
                radius: int = 13) -> list[Point]:
    """Depot centres (whole numbers, 2x2) around the main's townhall that keep clear of every production
    building's add-on square (centre +2.5, -0.5), of the free production-grid spots (3x3 plus add-on), of the
    townhall and of its mining area; the ones nearest `behind` (behind the mineral line) first (Dickie, 2026-10-31:
    "the bot builds supply depots where the starport add-on would go"). SC2 still has the last word (can_place)."""
    def clear(p: Point) -> bool:
        for b in production:
            if abs(p[0] - (b[0] + 2.5)) < 2 and abs(p[1] - (b[1] - 0.5)) < 2:
                return False
        for g in grid:
            if abs(p[0] - g[0]) < 2.5 and abs(p[1] - g[1]) < 2.5:
                return False
            if abs(p[0] - (g[0] + 2.5)) < 2 and abs(p[1] - (g[1] - 0.5)) < 2:
                return False
        return True

    reach = _dist(th, minerals)
    cx, cy = round(th[0]), round(th[1])
    out = []
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            p = (float(cx + dx), float(cy + dy))
            if abs(p[0] - th[0]) < 3.5 and abs(p[1] - th[1]) < 3.5:
                continue  # the townhall itself (5x5) plus a lane
            if _dist(p, th) > radius or (_dist(p, th) < reach + 1 and _dist(p, minerals) < reach):
                continue  # too far, or in the mining area between the townhall and its minerals
            if clear(p):
                out.append(p)
    return sorted(out, key=lambda p: (_dist(p, behind), p))


def port_slots(has_reactor: bool, has_add_on: bool, wants_reactor: bool) -> int:
    """How many units a Starport can queue now: 0 while it should still get its Reactor (cfg.starport_reactor,
    spot not blocked), 2 with a Reactor, else 1."""
    if wants_reactor and not has_add_on:
        return 0
    return 2 if has_reactor else 1


def wall_passing(passing: bool, now: float, open_until: float, hold: float) -> tuple[bool, float]:
    """Whether the wall counts as open for our own units, and the new open-until time. A passer keeps it open for
    `hold` seconds after it was last seen, so the wall doesn't raise under SCVs mid-walk and lower again as the
    next one arrives (Dickie, 2026-10-31: SCVs stuck at depots opening and closing over and over). hold 0 = ba15df0."""
    if passing:
        return True, now + hold
    return now < open_until, open_until


def wall_closed(
    enemy_near_any: bool, zerg: bool, army_supply: float, safe_supply: float, passing: bool = False
) -> bool:
    """Whether the whole ramp wall stays raised. One rule for every wall depot: each depot judging only the enemies
    near itself left the far one lowered and Zerglings ran through the gap (demo 23 vs Elite Zerg: main lost by
    3:30; Dickie: "they didn't actually close the base"). Against Zerg it stays shut until the army can hold, but
    opens for our own units on the move at the wall while no enemy is near: the natural's SCV was locked in at
    2:00 with two Reapers and no Marines yet (demo 29; Dickie: "they've blocked themselves in")."""
    if enemy_near_any:
        return True
    return zerg and army_supply < safe_supply and not passing


def next_research(order: tuple[str, ...] | list[str], have: set[str]) -> str | None:
    """The first upgrade in `order` not done or under way (Stimpack -> Combat Shield -> Concussive)."""
    for u in order:
        if u not in have:
            return u
    return None


def barracks_addon(reactors: int, techlabs: int, want_techlabs: int, opening_reactor: bool = True) -> str | None:
    """Add-on for a Barracks that has none: Tech Labs (for the upgrades and Marauders) until `want_techlabs`, then
    Reactors. None before the opening's own Reactor is placed (that Barracks makes the Reaper/Marines first)."""
    if not opening_reactor:
        return None
    return "TechLab" if techlabs < want_techlabs else "Reactor"


def bio_unit(has_techlab: bool, marines: int, marauders: int, marauder_share: float = 0.25) -> str:
    """What an idle Barracks trains: Marauders from Tech Labs while they are under `marauder_share` of the bio,
    otherwise Marines (Reactors and plain Barracks: always Marines)."""
    if has_techlab and marauders < marauder_share * (marines + marauders + 1):
        return "Marauder"
    return "Marine"


def want_vikings(big: int, vikings: int, per: int = 2, cap: int = 10) -> bool:
    """Starports make Vikings while there are fewer than `per` per big target seen (Colossus, Carrier, Tempest,
    Brood Lord, Battlecruiser), at most `cap` (Dickie, demo 25 vs Insane Protoss: \"when you see a ton of Colossus,
    rebuilding Marines ain't gonna do it\")."""
    return big > 0 and vikings < min(cap, per * big)


def want_medivac(medivacs: int, bio: int, per: int = 8, cap: int = 8) -> bool:
    """One Medivac per `per` bio units (at least one once there is bio), at most `cap`."""
    if bio <= 0 or medivacs >= cap:
        return False
    return medivacs < max(1, bio // max(1, per))


def army_supply(counts: dict[str, int]) -> int:
    """Supply of an army from unit counts by name (UNIT_SUPPLY; unknown names count 1)."""
    return sum(UNIT_SUPPLY.get(k, 1) * max(0, n) for k, n in counts.items())


def push_ready(first: bool, supply: float, stim_done: bool, now: float, push_supply: int, attack_time: float,
               stim_wait: float = 45.0) -> bool:
    """MMM's extra gate on a wave (on top of the unit counts in army_control): the first push waits for Stimpack
    and push_supply army supply, or goes at attack_time; without stim it waits stim_wait more seconds at most.
    Later waves are not gated here."""
    if not first:
        return True
    if not stim_done and now < attack_time + stim_wait:
        return False
    return supply >= push_supply or now >= attack_time


# ---------------------------------------------------------------------------------------------------------------------
# Bio micro (micro.py): Marine splits vs Banelings, stutter-step vs melee, focus fire. Pure geometry and decisions.
# ---------------------------------------------------------------------------------------------------------------------
def unit_vector(frm: Point, to: Point, fallback: Point = (1.0, 0.0)) -> Point:
    """The unit vector from `frm` toward `to`; `fallback` (normalised) when the two points coincide."""
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    d = (dx * dx + dy * dy) ** 0.5
    if d < 1e-6:
        dx, dy = fallback
        d = (dx * dx + dy * dy) ** 0.5 or 1.0
    return (dx / d, dy / d)


def rotate(v: Point, degrees: float) -> Point:
    """`v` turned counter-clockwise by `degrees`."""
    r = math.radians(degrees)
    c, s = math.cos(r), math.sin(r)
    return (v[0] * c - v[1] * s, v[0] * s + v[1] * c)


def escape_direction(pos: Point, threat: Point, way_out: Point, pull: float = 1.0) -> Point:
    """Which way a Reaper backs off: away from the threat plus `pull` x toward its way out of the enemy main. Straight
    away from the threat ran it into the back of the main with no cliff left to jump (Dickie, test 32). The threat
    right between it and the exit: sideways, around the threat."""
    away = unit_vector(threat, pos)
    out = unit_vector(pos, way_out)
    v = (away[0] + pull * out[0], away[1] + pull * out[1])
    if (v[0] * v[0] + v[1] * v[1]) ** 0.5 < 0.2:
        v = rotate(away, 90)
    return unit_vector((0.0, 0.0), v)


def step_points(pos: Point, direction: Point, dist: float, turns: tuple[float, ...] = (0, 45, -45, 90, -90)
                ) -> list[Point]:
    """Where a unit at `pos` can step: `dist` along `direction`, then the same turned by each of `turns` degrees (a
    cliff or the map edge straight behind: step sideways). The caller takes the first walkable one."""
    out = []
    for t in turns:
        vx, vy = rotate(direction, t) if t else direction
        out.append((pos[0] + vx * dist, pos[1] + vy * dist))
    return out


def split_direction(pos: Point, bane: Point, mates: list[Point], spread: float = 0.5) -> Point:
    """Which way a Marine runs from a Baneling: away from it, plus `spread` x away from the centre of the Marines
    near it (`mates`, not counting itself), so a clump fans out instead of running as one ball. Normalised; a
    Marine on top of the Baneling or a perfect cancel runs sideways."""
    away = unit_vector(bane, pos, fallback=(1.0, 0.0))
    side = (-away[1], away[0])
    vx, vy = away
    if mates and spread > 0:
        cx, cy = sum(m[0] for m in mates) / len(mates), sum(m[1] for m in mates) / len(mates)
        push = unit_vector((cx, cy), pos, fallback=side)
        vx, vy = vx + spread * push[0], vy + spread * push[1]
    return unit_vector((0.0, 0.0), (vx, vy), fallback=side)


def split_active(since: float | None, now: float, window: float, fight: float = 2.0) -> bool:
    """A Marine splits for `window` seconds after a Baneling first came near (since), then fights for `fight` x
    window, then splits again if Banelings are still near (a second wave). None = no Baneling near (the next one
    starts a new window)."""
    if since is None or window <= 0:
        return False
    return (now - since) % (window * (1 + fight)) < window


def should_kite(cooldown_left: float, cooldown_full: float, gap: float, kite_range: float, frac: float = 0.35) -> bool:
    """Stutter-step: step back from a melee enemy `gap` away (edge to edge) while the weapon reloads. Only while
    more than `frac` of the full cooldown is left, so the step back and the turn fit before the next shot; never
    with the weapon ready (cooldown_left 0), an unknown full cooldown, or the melee enemy beyond `kite_range` (0 =
    off)."""
    if kite_range <= 0 or gap > kite_range or cooldown_left <= 0 or cooldown_full <= 0:
        return False
    return cooldown_left > frac * cooldown_full


def target_key(baneling: bool, attacks: bool, hp: float, dist: float) -> tuple[int, float, float]:
    """Focus-fire order for enemies in range (lowest first): Banelings (closest first), then units that fight back
    (lowest HP + shields first, so shots finish one off instead of spreading), then the rest (Overlords, eggs,
    workers that can't hurt: same HP order)."""
    if baneling:
        return (0, dist, hp)
    return (1 if attacks else 2, hp, dist)


def focus_target(cands: list[tuple[int, bool, bool, float, float]], current: int | None) -> int | None:
    """Which enemy to shoot. cands: (tag, baneling, attacks, hp, dist) for the enemies in range. Keeps the current
    target (returns None: no new order) while it is still in range and in the best tier, so a shot is never cancelled
    for a 1-HP difference; otherwise the best by target_key. None also when nothing is in range."""
    if not cands:
        return None
    keyed = {c[0]: target_key(c[1], c[2], c[3], c[4]) for c in cands}
    best = min(keyed, key=lambda t: (keyed[t], t))
    if current is not None and current in keyed and keyed[current][0] <= keyed[best][0]:
        return None
    return best


# Skytoss (2-base Protoss air): tech order, what a Stargate / Gateway makes, chrono, the first attack. Pure, tested.
# ---------------------------------------------------------------------------------------------------------------------
PROTOSS_REQUIRES = {  # structure -> the structure it needs (finished) before it can be started
    "Gateway": "Pylon",
    "Forge": "Pylon",
    "CyberneticsCore": "Gateway",
    "ShieldBattery": "CyberneticsCore",
    "PhotonCannon": "Forge",
    "Stargate": "CyberneticsCore",
    "TwilightCouncil": "CyberneticsCore",
    "FleetBeacon": "Stargate",
    "DarkShrine": "TwilightCouncil",
}


def tech_order_errors(order: tuple[Step, ...], requires: dict[str, str] = PROTOSS_REQUIRES) -> list[str]:
    """A build step whose required structure comes later in the order (or never) would never be met: next_step never
    skips ahead, so the whole build would stall on it. One message per such step."""
    errs, seen = [], set()
    for i, s in enumerate(order):
        need = requires.get(s.item)
        if need is not None and need not in seen:
            errs.append(f"step {i} ({s.supply} {s.item}): needs {need} earlier in the build")
        seen.add(s.item)
    return errs


def stargate_unit(beacon: bool, carriers: int, void_rays: int, phoenixes: int, enemy_air: bool,
                  void_ray_cap: int = 5, phoenix_cap: int = 2, carrier_cap: int = 16) -> str | None:
    """What an idle Stargate builds (counts = done + training).
    - Fleet Beacon done: Carriers up to carrier_cap, then Void Rays (the gas goes to Carriers first).
    - Before: a few Phoenixes once enemy air has been seen (Banshees, Medivacs, Oracles, Mutalisks), else Void Rays
      up to void_ray_cap; then None: the gas is saved for the Fleet Beacon and the first Carriers."""
    if beacon:
        return "Carrier" if carriers < carrier_cap else "VoidRay"
    if enemy_air and phoenixes < phoenix_cap:
        return "Phoenix"
    if void_rays < void_ray_cap:
        return "VoidRay"
    return None


def gateway_pick(made: int, defenders: int, threatened: bool, core_ready: bool, minerals: float, gas: float,
                 zealot_bank: int) -> str | None:
    """What an idle Gateway trains in an air build: the first `defenders` units (an Adept, then Stalkers) once the
    Cybernetics Core is done; more only while the base is threatened (Stalkers, Zealots without the gas) or from a
    mineral bank of zealot_bank+ (Zealots: the gas stays for the Stargates). None = nothing."""
    if not core_ready:
        return "Zealot" if threatened and minerals >= 100 else None
    if made < defenders:
        return "Adept" if made == 0 else "Stalker"
    if threatened:
        return "Stalker" if minerals >= 125 and gas >= 50 else "Zealot"
    return "Zealot" if zealot_bank > 0 and minerals >= zealot_bank else None


def chrono_pick(busy: dict[str, int], order: tuple[str, ...] | list[str]) -> str | None:
    """Which kind of structure a Nexus chrono-boosts: the first kind in `order` with a busy, not yet boosted one
    (busy: kind -> how many). None = nothing worth boosting."""
    for kind in order:
        if busy.get(kind, 0) > 0:
            return kind
    return None


def fleet_ready(first: bool, carriers: int, supply: float, now: float, push_carriers: int, push_supply: float,
                attack_time: float, wave_supply: float) -> bool:
    """Skytoss's extra gate on a wave (on top of the unit counts and the ahead gate in army_control). The first
    attack waits for the fleet: push_carriers Carriers or push_supply army supply at home, or attack_time. Later
    waves: wave_supply army supply at home (a lone new Void Ray doesn't fly in)."""
    if first:
        return carriers >= push_carriers or supply >= push_supply or now >= attack_time
    return supply >= wave_supply


def placement_stuck(since: float | None, now: float, give_up: float) -> bool:
    """A build step whose placement has failed since `since` stops holding the bank after give_up seconds (it keeps
    retrying), so a blocked spot never stalls the whole build. None = not failing."""
    return since is not None and now - since >= give_up


def step_saving(affordable: bool, short_minerals: bool, placed: bool, place_fail_spends: bool = True) -> bool:
    """Whether a due build-order step holds the bank ("saving": no units, no auto depots). Unaffordable: only while
    the minerals are short (gas alone holds nothing, demo 14). Affordable: never once placed; not placed only with
    place_fail_spends off. Holding on a failed placement kept the bot at 33/31 supply with 1,312 minerals from 4:00
    to 5:30+ while the lost natural's Command Center failed to place every step (Acropolis vs Insane Zerg,
    2026-10-31)."""
    if not affordable:
        return short_minerals
    return not placed and not place_fail_spends


# ------------------------------------------------------------------ Scan on cloak (micro/scan_cloak.py, mmm.py)
SCAN_RADIUS = 13.0  # Scanner Sweep reveals this far from where it lands


def mule_ok(energies: list[float], i: int, keep: float) -> bool:
    """Orbital i may spend 50 energy on a MULE and still leave `keep` energy across all Orbitals (core.scan_keep).
    energies: this step's Orbital energies, lowered by 50 for each MULE already called this step."""
    return energies[i] >= 50 and sum(energies) - 50 >= keep


def scan_keep(on: bool, cloak_seen: bool, protoss: bool, now: float, from_time: float) -> float:
    """Energy the MULEs leave for one Scanner Sweep: once cloak is seen, or vs Protoss from from_time (a DT shows up
    before any Dark Shrine is scouted, and 50 energy takes ~63 s to come back). 0: MULEs at 50 as before."""
    if not on:
        return 0.0
    return 50.0 if cloak_seen or (protoss and from_time > 0 and now >= from_time) else 0.0


def scan_spot(cloaked: list[tuple[float, float]], army: list[tuple[float, float]],
              recent: list[tuple[float, float, float]], now: float, min_army: int = 2, army_reach: float = 10.0,
              hold: float = 4.0) -> tuple[float, float] | None:
    """Where to Scanner Sweep, or None. cloaked: undetected cloaked enemies that attack; army: our army units;
    recent: (x, y, cast time) of our scans. A spot needs min_army army units within army_reach (no one to shoot it, no
    scan) and no scan of ours within SCAN_RADIUS - 1 in the last `hold` s (the reveal lags the cast a few steps).
    Of those, the cloaked unit with the most cloaked units within SCAN_RADIUS / 2 (the first on ties)."""
    best, score = None, 0
    near2, half2, reach2 = (SCAN_RADIUS - 1) ** 2, (SCAN_RADIUS / 2) ** 2, army_reach ** 2
    for c in cloaked:
        if any(now - t < hold and (x - c[0]) ** 2 + (y - c[1]) ** 2 <= near2 for x, y, t in recent):
            continue
        if sum((a[0] - c[0]) ** 2 + (a[1] - c[1]) ** 2 <= reach2 for a in army) < min_army:
            continue
        n = sum((d[0] - c[0]) ** 2 + (d[1] - c[1]) ** 2 <= half2 for d in cloaked)
        if n > score:
            best, score = c, n
    return best
