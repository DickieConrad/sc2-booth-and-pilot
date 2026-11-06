"""Claude's in-game feed from the helper's /state (GET only, read-only). Event-driven: one line when something
worth judgement happens (supply block, lost fight, lost buildings, bank, behind plan, idle production), plus a
heartbeat snapshot after HEARTBEAT game seconds without a line. --mode=snapshots keeps the old 30 s feed.
Run under a Monitor:  ./.python/python.exe -u watch.py [--heartbeat=60] [--mode=events|snapshots]
(each stdout line is a turn for Claude). Detection is the pure Watcher class (test_watch.py); /state is validated
(models.STATE) before it gets there, and a malformed one is reported on stderr, never on the feed."""

import argparse
import sys
import time
import urllib.request
from collections.abc import Mapping, Sequence
from typing import TypeGuard

import models
from models import StateData
from pydantic import ValidationError

STATE = "http://127.0.0.1:8765/state"
POLL = 2  # wall seconds between polls
EVERY = 30  # game seconds between snapshots in --mode=snapshots
HEARTBEAT = 60  # game seconds without any line before a heartbeat snapshot
BLOCK_FROM = 60  # game seconds: the opening 15/15 wait for the first depot is not a supply block
REWIND = 5  # game seconds: the clock jumping back more than this mid-game is a new game (same as timing.py)
BLOCK_REARM = 15 # game seconds clear (same cap) before the same block can fire again
DROP_SUPPLY, DROP_WINDOW = 10, 20  # supply lost within this many game seconds = lost fight
CAP_DROP, CAP_WINDOW = 8, 60  # supply cap lost within this many game seconds = lost buildings
BANK_MIN = 800  # minerals bank alert
REARM_GAP = 200  # a pile alert re-arms only once the pile falls this far below its level (no 790/810 flicker)
BANK_GAS, BANK_GAS_MIN = 600, 200  # gas pile alert while minerals < BANK_GAS_MIN (re-armed once gas below)
BEHIND, BEHIND_REARM = 8, 4  # behind plan by >= 8 supply; re-armed when back within 4
IDLE_AFTER, IDLE_FOR, IDLE_FLAT, IDLE_FREE = 300, 90, 2, 8  # after 5:00: 90 s flat within +-2 with >= 8 free


def mmss(t: float) -> str:
    return f"{int(t) // 60}:{int(t) % 60:02d}"


def snapshot(s: StateData) -> str:
    p: Mapping[str, object] = s.get("panel") or {}
    return (
        f"min {p.get('minerals')} gas {p.get('gas')} supply {p.get('used')}/{p.get('cap')} plan {s.get('plan_supply')}"
    )


def last_call(s: StateData) -> str:
    says = s.get("says") or []
    last = says[-1] if says else None
    return f"last call: {last.get('t') if last else None} {last.get('text') if last else None}"


def next_steps(s: StateData) -> str:
    return "next: " + ", ".join(n["action"] for n in (s.get("next") or [])[:2])


def start_line(s: StateData) -> str:
    """'WATCH start: plan A vs PZ, player Player' (the player the helper follows: the owner or a sparring bot; an older
    helper sends none)."""
    who = f", player {s['player']}" if s.get("player") else ""
    return f"WATCH start: plan {s.get('plan')} vs {s.get('vs')}{who}"


def num(v: object) -> TypeGuard[int | float]:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Watcher:
    """Feed /state dicts in order; feed() returns the lines to print (usually none). Pure: no I/O, no wall clock."""

    def __init__(self, mode: str = "events", heartbeat: float = HEARTBEAT) -> None:
        self.mode, self.heartbeat = mode, heartbeat
        self.in_game = False
        self._reset()

    def _reset(self) -> None:
        self.hist: list[tuple[float, float, float]] = []  # (t, used, cap) over the longest window
        self.drop_from = self.cap_from = -1.0  # samples before these clocks are ignored (once per drop)
        self.blocked = self.bank_fired = self.gas_fired = self.behind_fired = self.idle_fired = False
        self.block_cap: float = 0  # the cap of the current block
        self.unblocked_at: float | None = None  # when supply first freed up under that cap
        self.idle_start: tuple[float, float] | None = None  # (t, used) where the current flat stretch began
        self.next_snap: float = 0
        self.last_line: float = 0  # game clock of the last printed line
        self.last_t: float | None = None  # game clock of the last in-game poll (a rewind = a new game)

    def feed(self, s: StateData) -> list[str]:
        out: list[str] = []
        if s.get("in_game") and not self.in_game:
            self.in_game = True
            self._reset()
            out.append(start_line(s))
        elif not s.get("in_game") and self.in_game:
            self.in_game = False
            out.append("WATCH end")
        if not self.in_game:
            return out
        t = s.get("clock") or 0
        if num(s.get("clock")) and self.last_t is not None and t < self.last_t - REWIND:
            # the clock jumped back with in_game never going false (2 s polls can miss a short gap): a new game,
            # like the helper's own restart. Old samples (larger t) would otherwise flood drops and mute heartbeats.
            self._reset()
            out.append(start_line(s))
        self.last_t = t
        if self.mode == "snapshots":
            if t >= self.next_snap:
                out.append(f"[{mmss(t)}] {snapshot(s)} | {next_steps(s)} | {last_call(s)}")
                self.next_snap = (t // EVERY + 1) * EVERY
            return out
        events = self.detect(s, t)
        for kind, detail in events:
            out.append(f"[{mmss(t)}] {kind} {detail} | {snapshot(s)} | {last_call(s)}")
        if events:
            self.last_line = t
        elif t - self.last_line >= self.heartbeat:
            out.append(f"[{mmss(t)}] HEARTBEAT | {snapshot(s)} | {next_steps(s)} | {last_call(s)}")
            self.last_line = t
        return out

    def detect(self, s: StateData, t: float) -> list[tuple[str, str]]:
        p = s.get("panel")
        used, cap, mins, gas = (p.get("used"), p.get("cap"), p.get("minerals"), p.get("gas")) if p else (None,) * 4
        ev: list[tuple[str, str]] = []
        if num(used) and num(cap):
            self.hist.append((t, used, cap))
            self.hist = [h for h in self.hist if h[0] >= t - max(DROP_WINDOW, CAP_WINDOW)]
            ev += self._supply(s, t, used, cap)
        if num(mins):
            if mins >= BANK_MIN and not self.bank_fired:
                ev.append(("BANK", f"{mins} minerals"))
                self.bank_fired = True
            elif mins < BANK_MIN - REARM_GAP:
                self.bank_fired = False
        if num(gas):
            if gas >= BANK_GAS and num(mins) and mins < BANK_GAS_MIN and not self.gas_fired:
                ev.append(("GAS_BANK", f"{gas} gas, {mins} minerals"))
                self.gas_fired = True
            elif gas < BANK_GAS - REARM_GAP:
                self.gas_fired = False
        return ev

    def _supply(self, s: StateData, t: float, used: float, cap: float) -> list[tuple[str, str]]:
        ev: list[tuple[str, str]] = []
        if used >= cap and cap < 200 and t >= BLOCK_FROM:
            if not self.blocked:
                ev.append(("SUPPLY_BLOCK", f"{used}/{cap}"))
            self.blocked, self.block_cap, self.unblocked_at = True, cap, None
        elif self.blocked:
            # a new depot (cap up) ends the block at once; units dying under the same cap must stay clear a while,
            # or a fight at 47/47 re-fires the same block (seen at 3:55 and 4:09 in a bot test game)
            self.unblocked_at = t if self.unblocked_at is None else self.unblocked_at
            if cap > self.block_cap or t - self.unblocked_at >= BLOCK_REARM:
                self.blocked = False
        win = [h for h in self.hist if h[0] >= t - DROP_WINDOW and h[0] >= self.drop_from]
        peak = max(h[1] for h in win)
        if peak - used >= DROP_SUPPLY:
            ev.append(("SUPPLY_DROP", f"{peak}->{used} in {DROP_WINDOW}s"))
            self.drop_from = t
        win = [h for h in self.hist if h[0] >= t - CAP_WINDOW and h[0] >= self.cap_from]
        top = max(h[2] for h in win)
        if top - cap >= CAP_DROP:
            ev.append(("LOST_BUILDINGS", f"cap {top}->{cap} in {CAP_WINDOW}s"))
            self.cap_from = t
        free = cap - used
        if t < IDLE_AFTER or free < IDLE_FREE:
            self.idle_start = None
        elif self.idle_start is None or abs(used - self.idle_start[1]) > IDLE_FLAT:
            self.idle_start, self.idle_fired = (t, used), False
        elif t - self.idle_start[0] >= IDLE_FOR and not self.idle_fired:
            flat_for = int(t - self.idle_start[0])
            ev.append(("IDLE_PRODUCTION", f"supply ~{self.idle_start[1]} for {flat_for}s, {free} free"))
            self.idle_fired = True
        plan = s.get("plan_supply")
        if num(plan):
            gap = plan - used
            if gap >= BEHIND and not self.behind_fired:
                ev.append(("BEHIND_PLAN", f"{used} vs plan {plan} (-{gap})"))
                self.behind_fired = True
            elif gap <= BEHIND_REARM:
                self.behind_fired = False
        return ev


def utf8_stdio() -> None:
    """stdout/stderr as UTF-8, unencodable characters replaced: on a Windows cp1252 console a plan step with "≈"
    raised UnicodeEncodeError in print and killed the feed."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)   # a TextIOWrapper; a replaced stream may not have it
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> None:
    utf8_stdio()
    ap = argparse.ArgumentParser(description="SC2 coach in-game feed (read-only /state poller)")
    ap.add_argument("--heartbeat", type=float, default=HEARTBEAT, help="game seconds without a line before a snapshot")
    ap.add_argument("--mode", choices=("events", "snapshots"), default="events")
    a = ap.parse_args(argv)
    w = Watcher(a.mode, a.heartbeat)
    while True:
        try:
            with urllib.request.urlopen(STATE, timeout=2) as r:
                raw = r.read()
        except Exception:
            time.sleep(5)
            continue
        try:
            s = models.STATE.validate_json(raw)
        except ValidationError as e:   # a malformed /state: say why on stderr (stdout is Claude's feed), skip it
            print(f"watch: bad /state ({models.errors(e)})", file=sys.stderr, flush=True)
            time.sleep(POLL)
            continue

        for line in w.feed(s):
            print(line, flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
