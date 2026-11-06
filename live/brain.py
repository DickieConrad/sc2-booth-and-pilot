"""Booth brain: the coach's formulaic live calls, made in milliseconds from structured state (no pixels).

Idea from jev-plays-doom: turn raw samples into a few derived facts (describe), then pick ONE call from a fixed
set (or none). Every rule is an explicit numeric threshold; vague "intent" wording is what failed there.
Claude keeps the judgment calls (army moves, scouting reads, praise, plan changes); this covers the arithmetic.

    describe(history, matchup, race) -> facts dict from the panel samples (last ~60 s, plus a few whole-game flags)
    RulesBrain().decide(history, matchup, race=race) -> {kind, text, clip_text, confidence} or None
    (race: the followed player's, "T" | "P" | "Z"; Protoss / Zerg get their own wording, Terran-only kinds go quiet)
    JevBrain(...)               -> the same call set as a TypeSafe Choice (optional; tools/eval_brain.py)

A sample is {"t": game seconds, "minerals", "gas", "used", "cap"}. The page text keeps the numbers;
`clip_text` has none, so every phrasing is pre-rendered once (live/voice/render_brain.py) and plays at once.
Phrasings rotate: the n-th call of a kind uses clip n and page text n (same index, so they say the same thing).
Spend calls (bank, gas) don't nag: they come back only when the pile grew 1.5x, or after 90 s, and stay quiet
while Dickie is losing a fight (supply down 10+ in 20 s) -- he is busy; depot and lost-building calls still go.
"Supply stuck" is held too while he lost a fight in the last 30 s (fight_30s): units traded and rebuilt read flat.
Stdlib only (the Jev brain imports typesafe_sdk from live/.pkgs lazily; the call/sample types come from models.py
for the type checker only).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict

if TYPE_CHECKING:
    from models import BrainCall, BrainKind, Sample

HERE = Path(__file__).resolve().parent
WINDOW = 60.0        # seconds of history describe() looks at
GLOBAL_GAP = 20.0    # seconds between any two brain calls (URGENT kinds skip it)
COOLDOWN = 60.0      # default per-kind gap: the coach never repeated a call within ~60 s
# Spend families (bank, gas): one cooldown for the whole family, keyed to the pile at the last call.
Pile = Literal["minerals", "gas"]
SPEND_AMOUNT: dict[str, Pile] = {"bank": "minerals", "gas": "gas"}   # family -> the pile it is about
REPEAT_GAP = 90.0    # the same pile is called again only after 90 s ...
ESCALATE = 1.5       # ... or sooner once it is 1.5x what it was at the last call (it got worse)
ESCALATE_GAP = 30.0  # ... but never within 30 s (the coach escalated 500 -> 650 -> 1200 at ~30 s steps, game 30)
FIGHT_WINDOW = 20    # seconds: supply down FIGHT_DROP+ inside this window = losing a fight
FIGHT_DROP = 10

# ---------- the fixed call set ----------
# kind: (cooldown s, [clip phrasings without numbers], [page-text templates with numbers]); clip i <-> text i.
KINDS: dict[BrainKind, tuple[float, list[str], str | list[str]]] = {
    "lost_cc":    (60, ["You lost a Command Center. Army home, now."],
                   "Lost a Command Center ({cap_dropped} cap): army home"),
    "lost_depot": (60, ["You lost a depot. Check your main.", "Supply depot down. Look at your main."],
                   "Lost a depot ({cap_dropped} cap), check your main"),
    "depot":      (60, ["Depot now.", "Depot now, you're close to the cap."],
                   "Depot now, {used} of {cap}"),
    "depots":     (60, ["Two depots now.", "Two depots, you're near the cap."],
                   "2 depots now, {used} of {cap}"),
    "cc":         (30, ["Command Center now.", "Command Center at the natural, now."],
                   "Command Center now, {minerals} minerals"),
    "stuck":      (60, ["Supply is stuck. Queue SCVs.", "Supply hasn't moved. SCVs and Marines, now."],
                   "Supply stuck at {used}: queue SCVs"),
    "orbital":    (120, ["Command Center's done. Orbital Command, SCVs from both."],
                   "CC done: Orbital Command, SCVs from both"),
    "gas_idle":   (90, ["Gas stopped rising. Check your refinery."],
                   "Gas stuck at {gas}: check your refinery"),
    # spend kinds: the family cooldown (REPEAT_GAP / ESCALATE) applies, not the number here
    "bank_big":   (REPEAT_GAP, ["Way too much money. Four Barracks, Marines from every one.",
                                "Huge bank. Spend it: Barracks and depots, now.",
                                "Big bank. Second Factory and two Barracks."],
                   ["{minerals} banked! 4 Barracks, Marines from every one",
                    "{minerals} banked! Barracks and depots, now",
                    "Big bank, {minerals}: 2nd Factory, 2 Barracks"]),
    "bank":       (REPEAT_GAP, ["Money's piling up. Two Barracks, now.", "Spend it. Two more Barracks.",
                                "Bank's piling. Starport, Barracks, Marines."],
                   ["{minerals} banked: 2 Barracks", "Spend {minerals}: 2 more Barracks",
                    "{minerals} banked: Starport, Barracks, Marines"]),
    "gas_early":  (REPEAT_GAP, ["Gas is up. Stim and a Tank.", "Spend the gas. Stim, then a Tank.",
                                "Gas is high. Combat Shield with that gas."],
                   ["Gas {gas}: Stim and a Tank", "Gas {gas}: Stim, then a Tank", "Gas {gas}: Combat Shield"]),
    "gas":        (REPEAT_GAP, ["Gas is piling up. Tanks and Medivacs.",
                                "Spend the gas. Medivacs, Tanks, an upgrade.",
                                "Too much gas. Three SCVs to minerals, Tanks and upgrades."],
                   ["Gas {gas}: Tanks and Medivacs", "Gas {gas}: Medivacs, Tanks, upgrade",
                    "Gas {gas}: 3 SCVs to minerals, Tanks, upgrades"]),
    "turret":     (0, ["Turret in each mineral line."], "Turret in each mineral line"),
    "bunker":     (0, ["Bunker at the natural. Watch for lings."], "Bunker at the natural, watch for lings"),
    "stall":      (0, ["Stall minute. Marines nonstop, SCVs from every CC."],
                   "Stall minute: Marines nonstop, SCVs from every CC"),
}
# Fire-once reminders at fixed game times: kind -> (matchup letter any of, from s, to s)
TIMED: dict[BrainKind, list[tuple[str, int, int]]] = {
    "turret": [("TP", 300, 330), ("Z", 360, 390)],
    "bunker": [("Z", 165, 195)],
    "stall":  [("TPZR", 150, 165)],
}
URGENT: set[str] = {"lost_cc", "lost_depot"}
# same coach habit, different wording
FAMILY: dict[str, str] = {"depots": "depot", "bank_big": "bank", "gas_early": "gas"}

# ---------- race wording ----------
# The followed player's race (timing.py's "T" | "P" | "Z"; anything else counts as "T"). KINDS is the Terran
# wording; for Protoss and Zerg each kind has its own (clip phrasings, page texts), clip i <-> text i as in KINDS,
# or None: never said for that race (no sensible translation). Same cooldowns and thresholds. Same as brain.rs.
Wording = tuple[list[str], list[str]]
_GAS_EARLY_PZ: Wording = (["Gas is up. Spend it on tech.", "Spend the gas. Tech and upgrades."],
                          ["Gas {gas}: spend it on tech", "Gas {gas}: tech and upgrades"])
RACE_KINDS: dict[str, dict[BrainKind, Wording | None]] = {
    "P": {
        "lost_cc":    (["You lost a Nexus. Army home, now."], ["Lost a Nexus ({cap_dropped} cap): army home"]),
        "lost_depot": (["You lost a pylon. Check your main.", "Pylon down. Look at your main."],
                       ["Lost a pylon ({cap_dropped} cap), check your main"]),
        "depot":      (["Pylon now.", "Pylon now, you're close to the cap."], ["Pylon now, {used} of {cap}"]),
        "depots":     (["Two pylons now.", "Two pylons, you're near the cap."], ["2 pylons now, {used} of {cap}"]),
        "cc":         None,
        "stuck":      (["Supply is stuck. Queue Probes.", "Supply hasn't moved. Probes and units, now."],
                       ["Supply stuck at {used}: queue Probes"]),
        "orbital":    None,
        "gas_idle":   (["Gas stopped rising. Check your assimilator."], ["Gas stuck at {gas}: check your assimilator"]),
        "bank_big":   (["Way too much money. Four Gateways, warp in nonstop.",
                        "Huge bank. Spend it: Gateways and pylons, now."],
                       ["{minerals} banked! 4 Gateways, warp in nonstop",
                        "{minerals} banked! Gateways and pylons, now"]),
        "bank":       (["Money's piling up. Two Gateways, now.", "Spend it. Two more Gateways."],
                       ["{minerals} banked: 2 Gateways", "Spend {minerals}: 2 more Gateways"]),
        "gas_early":  _GAS_EARLY_PZ,
        "gas":        (["Gas is piling up. Gas units and upgrades.", "Too much gas. Three Probes to minerals."],
                       ["Gas {gas}: gas units and upgrades", "Gas {gas}: 3 Probes to minerals"]),
        "turret":     None,
        "bunker":     None,
        "stall":      None,
    },
    "Z": {
        "lost_cc":    None,   # a Hatchery is 6 supply: a 14+ cap drop is not one
        "lost_depot": (["Overlord down. Make more, now.", "You lost an overlord. Replace it."],
                       ["Lost overlords ({cap_dropped} cap): make more"]),
        "depot":      (["Overlord now.", "Overlord now, you're close to the cap."], ["Overlord now, {used} of {cap}"]),
        "depots":     (["Two overlords now.", "Two overlords, you're near the cap."],
                       ["2 overlords now, {used} of {cap}"]),
        "cc":         None,
        "stuck":      (["Supply is stuck. Make Drones.", "Supply hasn't moved. Drones and units, now."],
                       ["Supply stuck at {used}: make Drones"]),
        "orbital":    None,
        "gas_idle":   (["Gas stopped rising. Check your extractor."], ["Gas stuck at {gas}: check your extractor"]),
        "bank_big":   (["Way too much money. Two Hatcheries, now.", "Huge bank. Spend it: Hatcheries and Queens, now."],
                       ["{minerals} banked! 2 Hatcheries", "{minerals} banked! Hatcheries and Queens, now"]),
        "bank":       (["Money's piling up. Another Hatchery, now.", "Spend it. Drones and units, now."],
                       ["{minerals} banked: another Hatchery", "Spend {minerals}: Drones and units"]),
        "gas_early":  _GAS_EARLY_PZ,
        "gas":        (["Gas is piling up. Gas units and upgrades.", "Too much gas. Three Drones to minerals."],
                       ["Gas {gas}: gas units and upgrades", "Gas {gas}: 3 Drones to minerals"]),
        "turret":     None,
        "bunker":     None,
        "stall":      None,
    },
}
RACES = ("T", "P", "Z")


def race_of(race: str | None) -> str:
    """The brain's race letter: "P" / "Z", anything else (missing, "", "random") is "T". Same as brain.rs."""
    return race if race in ("P", "Z") else "T"


def wording(kind: BrainKind, race: str = "T") -> Wording | None:
    """(clip phrasings, page-text templates) of a kind for a race; None = not said for that race."""
    r = race_of(race)
    if r == "T":
        tpl = KINDS[kind][2]
        return KINDS[kind][1], [tpl] if isinstance(tpl, str) else tpl
    return RACE_KINDS[r][kind]


def family(kind: str) -> str:
    return FAMILY.get(kind, kind)


def all_clip_texts() -> list[str]:
    """Every clip phrasing once: Terran first (KINDS order), then the Protoss and Zerg ones."""
    return list(dict.fromkeys(p for r in RACES for k in KINDS for p in (wording(k, r) or ([], []))[0]))


# ---------- facts ----------

class Facts(TypedDict):
    """describe()'s output: what every rule (and the Jev prompt) looks at."""
    clock_s: int
    phase: str
    matchup: str
    race: str
    minerals: int
    gas: int
    used: int
    cap: int
    cap_headroom: int
    depot_room: int
    bank_min: int
    supply_delta_30s: int
    growth_per_min: float
    gas_delta_30s: int
    gas_flat_30s: bool
    bank_trend: int
    supply_drop_30s: int
    losing_fight: bool
    fight_30s: bool
    cap_dropped: int
    cap_jump: int
    cc_started: bool
    gas_seen: bool
    history_s: int


def _at(hist: Sequence[Sample], t: float) -> Sample:
    """The latest sample at or before game time t (else the earliest)."""
    best = hist[0]
    for s in hist:
        if s["t"] <= t:
            best = s
        else:
            break
    return best


def describe(history: Sequence[Sample], matchup: str = "", race: str = "T") -> Facts | None:
    """Derived facts from panel samples (sorted by t). Windowed facts use the last WINDOW seconds;
    cc_started / gas_seen look at the whole game (they are one-way flags). race: the followed player's (wording,
    which kinds are said). None for an empty history."""
    if not history:
        return None
    now = history[-1]
    t = now["t"]
    win = [s for s in history if s["t"] >= t - WINDOW]
    ago30 = _at(history, t - 30)
    last30 = [s for s in history if s["t"] >= t - 30]
    span = t - win[0]["t"]
    # a single 300+ mineral drop between reads after 1:15 is the natural CC (nothing else costs that much then);
    # a 14+ cap jump means a CC finished
    pairs = list(zip(history, history[1:]))
    cc_started = any(b["t"] >= 75 and a["minerals"] - b["minerals"] >= 300 and b["t"] - a["t"] <= 10
                     or b["cap"] - a["cap"] >= 14 for a, b in pairs)
    # losing_fight held at some read of the last 30 s (supply traded away and rebuilt reads flat, not stuck)
    lo = next(i for i, s in enumerate(history) if s["t"] >= t - 30 - FIGHT_WINDOW)
    first30 = next(i for i, s in enumerate(history) if s["t"] >= t - 30)
    fight_30s = any(max(s["used"] for s in history[lo:j + 1] if s["t"] >= history[j]["t"] - FIGHT_WINDOW)
                    - history[j]["used"] >= FIGHT_DROP for j in range(first30, len(history)))
    return {
        "clock_s": round(t),
        "phase": "opening" if t < 120 else "stall" if t < 180 else "mid",
        "matchup": matchup or "",
        "race": race_of(race),
        "minerals": now["minerals"], "gas": now["gas"], "used": now["used"], "cap": now["cap"],
        "cap_headroom": now["cap"] - now["used"],
        "depot_room": 4 if now["cap"] <= 31 else 4 + now["used"] // 6,   # headroom that warrants a depot
        "bank_min": 400 if t < 300 else 500,                              # minerals that count as a bank
        "supply_delta_30s": now["used"] - ago30["used"],
        "growth_per_min": round((now["used"] - win[0]["used"]) * 60 / span, 1) if span >= 10 else 0.0,
        "gas_delta_30s": now["gas"] - ago30["gas"],
        "gas_flat_30s": (t - last30[0]["t"] >= 25 and len({s["gas"] for s in last30}) == 1),
        "bank_trend": now["minerals"] - ago30["minerals"],
        "supply_drop_30s": max(s["used"] for s in last30) - now["used"],
        "losing_fight": max(s["used"] for s in history if s["t"] >= t - FIGHT_WINDOW) - now["used"] >= FIGHT_DROP,
        "fight_30s": fight_30s,
        "cap_dropped": max(0, max(s["cap"] for s in last30) - now["cap"]),
        "cap_jump": max(0, now["cap"] - min(s["cap"] for s in last30)),
        "cc_started": cc_started,
        "gas_seen": any(s["gas"] > 0 for s in history),
        "history_s": round(t - history[0]["t"]),
    }


# ---------- rules ----------
# Each rule: (kind, predicate on facts). Checked in this order; the first that holds and is off cooldown wins.
# Thresholds were set from the coach's calls in games 19-22 (tools/eval_brain.py) and SC2 timings.

RULES: list[tuple[BrainKind, Callable[[Facts], bool]]] = [
    # cap fell in the last 30 s past 1:00: by 14+ and not a multiple of 8 -> a CC died; by 8+ -> depot(s)
    ("lost_cc", lambda f: f["clock_s"] >= 60 and f["cap_dropped"] >= 14 and f["cap_dropped"] % 8 != 0),
    ("lost_depot", lambda f: f["clock_s"] >= 60 and f["cap_dropped"] >= 8 and f["cap_dropped"] % 8 == 0),
    # 1:00+, cap < 200, headroom <= depot_room (4 while cap <= 31, else 4 + used/6), supply grew >= 1 in 30 s
    ("depot",  lambda f: f["clock_s"] >= 60 and f["cap"] < 200 and f["used"] < 36
               and f["cap_headroom"] <= f["depot_room"] and f["supply_delta_30s"] >= 1),
    ("depots", lambda f: f["clock_s"] >= 60 and f["cap"] < 200 and f["used"] >= 36
               and f["cap_headroom"] <= f["depot_room"] and f["supply_delta_30s"] >= 1),
    # 1:20-3:15, natural CC not started, minerals >= 350 (an SCV is walking; it lands at ~400)
    ("cc",     lambda f: 80 <= f["clock_s"] <= 195 and not f["cc_started"] and f["minerals"] >= 350),
    # 2:30+, supply not up over 30 s and not down 3+ (a fight, not a stall), not blocked, under 190
    ("stuck",  lambda f: f["clock_s"] >= 150 and f["supply_delta_30s"] <= 0 and f["supply_drop_30s"] <= 2
               and f["cap_headroom"] > 0 and f["used"] < 190 and f["history_s"] >= 30),
    # cap jumped 14+ in 30 s, not a multiple of 8 (that's depots), 2:00-7:00 -> a CC finished
    ("orbital", lambda f: 120 <= f["clock_s"] < 420 and f["cap_jump"] >= 14 and f["cap_jump"] % 8 != 0),
    # gas mined before, but the exact same gas value for 30 s, under 300 (not a bank) from 1:40
    ("gas_idle", lambda f: f["clock_s"] >= 100 and f["gas_seen"] and f["gas_flat_30s"] and f["gas"] < 300),
    # 1000+ minerals from 2:30
    ("bank_big", lambda f: f["clock_s"] >= 150 and f["minerals"] >= 1000),
    # minerals >= bank_min (400; 500 from 5:00) from 2:30, not already falling by 100+ in 30 s
    ("bank",   lambda f: f["clock_s"] >= 150 and f["minerals"] >= f["bank_min"]
               and f["bank_trend"] > -100 and (f["cc_started"] or f["clock_s"] > 195)),
    # gas >= 350 and rose >= 20 in 30 s: before 5:00 Stim + Tank, after it Tanks/Medivacs
    ("gas_early", lambda f: 180 <= f["clock_s"] < 300 and f["gas"] >= 350 and f["gas_delta_30s"] >= 20),
    ("gas",    lambda f: f["clock_s"] >= 300 and f["gas"] >= 350 and f["gas_delta_30s"] >= 20),
]


def known_matchup(matchup: str) -> str:
    """The matchup letters for the timed reminders: unknown ("" or the helper's "?" for opponents it couldn't read,
    e.g. a bot test game) counts as "T". Same as brain.rs."""
    return "T" if matchup in ("", "?") else matchup


def _timed(f: Facts) -> Iterator[BrainKind]:
    m = known_matchup(f["matchup"])
    for kind, slots in TIMED.items():
        for letters, a, b in slots:
            if a <= f["clock_s"] <= b and any(c in letters for c in m):
                yield kind


def texts(kind: BrainKind, race: str = "T") -> list[str]:
    """Page-text templates of a kind for a race ([] when that race never hears it)."""
    w = wording(kind, race)
    return w[1] if w else []


def render_text(kind: BrainKind, f: Facts, n: int = 0) -> str:
    """Page text for the n-th variant (0-based, wraps), in the facts' race wording."""
    tpl = texts(kind, f["race"]) or texts(kind)
    return tpl[n % len(tpl)].format(**f)


class RulesBrain:
    """Stateful (cooldowns, fire-once reminders). Call reset() at a new game."""
    name = "rules"

    def __init__(self, global_gap: float = GLOBAL_GAP) -> None:
        self.global_gap = global_gap
        self.reset()

    def reset(self) -> None:
        self.last: dict[str, float] = {}   # kind -> game time it last fired
        self.last_spend: dict[str, tuple[float, int | None]] = {}   # spend family -> (game time, pile then)
        self.last_any = -1e9
        self.fired_once: set[str] = set()
        self.count: dict[str, int] = {}

    def spend_open(self, fam: str, t: float, f: Facts | None = None) -> bool:
        """A spend family may speak: never called, REPEAT_GAP passed, or the pile is ESCALATE x worse."""
        if fam not in self.last_spend:
            return True
        t0, pile0 = self.last_spend[fam]
        if t - t0 >= REPEAT_GAP:
            return True
        if f is None or pile0 is None or t - t0 < ESCALATE_GAP:
            return False
        return f[SPEND_AMOUNT[fam]] >= ESCALATE * max(pile0, 1)

    def available(self, t: float, f: Facts | None = None) -> list[BrainKind]:
        """Kinds off cooldown at game time t (timed kinds: not yet fired this game). With facts f, spend kinds
        also escalate early when the pile grew, are held while Dickie is losing a fight ("stuck" also while he lost
        one in the last 30 s), and kinds the facts' race never hears (Terran-only: CC, orbital, reminders) are
        left out."""
        out: list[BrainKind] = []
        busy = bool(f and f.get("losing_fight"))
        fought = bool(f and f.get("fight_30s"))   # "stuck" over a window with a lost fight in it is trading
        for kind, (cd, _, _) in KINDS.items():
            fam = family(kind)
            if f is not None and wording(kind, f["race"]) is None:
                continue
            if kind == "stuck" and (busy or fought):
                continue
            if kind in TIMED:
                if kind not in self.fired_once:
                    out.append(kind)
            elif fam in SPEND_AMOUNT:
                if not busy and self.spend_open(fam, t, f):
                    out.append(kind)
            elif t - self.last.get(kind, -1e9) >= (cd or COOLDOWN):
                out.append(kind)
        return out

    def gap_open(self, t: float) -> bool:
        return t - self.last_any >= self.global_gap

    def note_external(self, t: float, kind: str | None = None, sample: Sample | None = None) -> None:
        """Something else spoke (the coach, or a helper call of the same habit): respect the gaps.
        sample (the latest panel reading) lets a helper bank call set the pile the brain escalates from."""
        self.last_any = max(self.last_any, t)
        if kind in KINDS:
            self.last[kind] = t
            fam = family(kind)
            if fam in SPEND_AMOUNT:
                self.last_spend[fam] = (t, sample[SPEND_AMOUNT[fam]] if sample else None)

    def pick(self, f: Facts, avail: Sequence[str]) -> BrainKind | None:
        """The rule decision (kind or None) for facts f among available kinds; no state change."""
        for kind in _timed(f):
            if kind in avail:
                return kind
        for kind, ok in RULES:
            if kind in avail and ok(f):
                return kind
        return None

    def commit(self, kind: BrainKind, f: Facts, confidence: float = 1.0) -> BrainCall:
        t = f["clock_s"]
        self.last[kind] = self.last_any = t
        if kind in TIMED:
            self.fired_once.add(kind)
        fam = family(kind)
        if fam in SPEND_AMOUNT:
            self.last_spend[fam] = (t, f[SPEND_AMOUNT[fam]])
        n = self.count[kind] = self.count.get(kind, 0) + 1
        w = wording(kind, f["race"])
        phr = w[0] if w else KINDS[kind][1]   # available() never offers a kind the race doesn't hear
        return {"kind": kind, "text": render_text(kind, f, n - 1), "clip_text": phr[(n - 1) % len(phr)],
                "confidence": round(confidence, 3)}

    def decide(self, history: Sequence[Sample], matchup: str = "", facts: Facts | None = None,
               race: str = "T") -> BrainCall | None:
        f = facts or describe(history, matchup, race)
        if not f:
            return None
        avail = self.available(f["clock_s"], f)
        if not self.gap_open(f["clock_s"]):
            avail = [k for k in avail if k in URGENT]
        kind = self.pick(f, avail)
        return self.commit(kind, f) if kind else None


# ---------- Jev (TypeSafe) ----------

CRITERIA: dict[str, str] = {
    "lost_cc": "clock_s >= 60, cap_dropped >= 14 and cap_dropped is not a multiple of 8.",
    "lost_depot": "clock_s >= 60, cap_dropped >= 8 and cap_dropped is a multiple of 8.",
    "depot": "clock_s >= 60, cap < 200, used < 36, supply_delta_30s >= 1 and cap_headroom <= depot_room.",
    "depots": "clock_s >= 60, cap < 200, used >= 36, supply_delta_30s >= 1 and cap_headroom <= depot_room.",
    "cc": "clock_s between 80 and 195, cc_started is false, and minerals >= 350.",
    "stuck": "clock_s >= 150, supply_delta_30s <= 0, supply_drop_30s <= 2, cap_headroom > 0, used < 190, "
             "fight_30s is false.",
    "orbital": "clock_s between 120 and 419, cap_jump >= 14 and cap_jump is not a multiple of 8.",
    "gas_idle": "clock_s >= 100, gas_seen is true, gas_flat_30s is true and gas < 300.",
    "bank_big": "clock_s >= 150 and minerals >= 1000.",
    "bank": "clock_s >= 150, minerals >= bank_min, bank_trend > -100, "
            "and cc_started is true or clock_s > 195.",
    "gas_early": "clock_s between 180 and 299, gas >= 350 and gas_delta_30s >= 20.",
    "gas": "clock_s >= 300, gas >= 350 and gas_delta_30s >= 20.",
    "turret": "matchup contains T or P and clock_s between 300 and 330, or matchup contains Z and clock_s "
              "between 360 and 390.",
    "bunker": "matchup contains Z and clock_s between 165 and 195.",
    "stall": "clock_s between 150 and 165.",
    "none": "No other option's numeric conditions are all met.",
}
INSTRUCTIONS = ("A StarCraft II Terran coach makes one short call from the numbers in the state, or none. "
                "Pick the first option, in the listed order, whose numeric conditions are ALL met by the state; "
                "pick none if no option's conditions are met.")


def load_key(path: Path = HERE / ".env.typesafe") -> str | None:
    """TYPESAFE_API_KEY from live/.env.typesafe (never printed or logged). Returns None if missing."""
    try:
        raw = path.read_bytes().replace(b"\x00", b"").decode("utf-8", "replace").lstrip("﻿")
    except OSError:
        return None
    for line in raw.splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "TYPESAFE_API_KEY":
            v = v.strip().strip('"').strip("'")
            return v or None
    return None


class BudgetExceeded(RuntimeError):
    pass


class JevBrain(RulesBrain):
    """Same cooldowns and call set; the choice comes from Jev. Below conf_floor (or on any API error, or with the
    budget spent) the rules decide. Only kinds off cooldown are offered, so a pick is always speakable.
    Every API call is counted in usage_file; past `budget` calls in total it stops calling."""
    name = "jev"

    def __init__(self, conf_floor: float = 0.5, budget: int = 400, usage_file: Path = HERE / "out" / "jev_usage.json",
                 model: str = "jev-latest") -> None:
        super().__init__()
        import json
        import sys
        sys.path.insert(0, str(HERE / ".pkgs"))
        from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient  # noqa: F401 (Choice used below)
        key = load_key()
        if not key:
            raise RuntimeError("no TYPESAFE_API_KEY in live/.env.typesafe")
        self._json, self._Choice = json, Choice
        self.client = TypeSafeClient(api_key=key, model=model, retry=RetryPolicy(max_retries=0, timeout=15.0))
        del key
        self.conf_floor, self.budget, self.usage_file = conf_floor, budget, Path(usage_file)
        self.stats: dict[str, Any] = {"calls": 0, "fallback_low_conf": 0, "fallback_error": 0, "errors": [], "conf": [],
                      "agree_rules": 0, "tokens_in": 0, "tokens_out": 0, "api_ms": []}

    def _used(self) -> int:
        try:
            return int(self._json.loads(self.usage_file.read_text())["calls"])
        except Exception:
            return 0

    def _count(self) -> int:
        n = self._used() + 1
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        self.usage_file.write_text(self._json.dumps({"calls": n, "updated": time.time()}))
        return n

    def ask(self, f: Facts, avail: Sequence[str]) -> tuple[str, float]:
        if self._used() >= self.budget:
            raise BudgetExceeded(f"Jev budget of {self.budget} calls spent")
        opts = [k for k in CRITERIA if k == "none" or k in avail]
        state = {k: v for k, v in f.items() if k != "history_s"}
        self._count()
        self.stats["calls"] += 1
        t0 = time.perf_counter()
        r = self.client.system_one(state=state, questions={"call": self._Choice(
            instructions=INSTRUCTIONS, criteria={k: CRITERIA[k] for k in opts})})
        if r.usage:
            self.stats["tokens_in"] += r.usage.input_tokens or 0
            self.stats["tokens_out"] += r.usage.output_tokens or 0
        self.stats["api_ms"].append((time.perf_counter() - t0) * 1000)
        a = r.answers["call"]
        return a.choice, float(a.confidence)

    def decide(self, history: Sequence[Sample], matchup: str = "", facts: Facts | None = None,
               race: str = "T") -> BrainCall | None:
        f = facts or describe(history, matchup, race)
        if not f or not self.gap_open(f["clock_s"]):
            return None
        avail = self.available(f["clock_s"], f)
        rule = self.pick(f, avail)
        try:
            choice, conf = self.ask(f, avail)
        except Exception as e:   # error or budget: rules decide (the message never contains the key)
            self.stats["fallback_error"] += 1
            if len(self.stats["errors"]) < 5:
                self.stats["errors"].append(f"{type(e).__name__}: {str(e)[:160]}")
            return self.commit(rule, f) if rule else None
        self.stats["conf"].append(conf)
        self.stats["agree_rules"] += (choice == (rule or "none"))
        if conf < self.conf_floor:
            self.stats["fallback_low_conf"] += 1
            return self.commit(rule, f) if rule else None
        kind = next((k for k in avail if k == choice), None)   # Jev's answer is free text: only offered kinds count
        return self.commit(kind, f, conf) if kind else None


    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
