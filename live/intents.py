"""Intent lane for the push-to-talk mic: a callout that misses the keyword fast lane is sorted into one of seven
intents by Jev (TypeSafe System One, ~0.1 s), and a few of them are answered at once.

    IntentLane().classify(text)  -> Verdict(intent, conf, talk) or None   one request, ~0.6 s budget, never raises
    decide(intent, conf, in_game, text, talk) -> Action                   pure: what mic.py does (no network)

One request asks two questions in parallel: a `Choice` over INTENTS (chatter listed first: Jev leans to the first
option, so a doubtful line errs toward the safe slow path) and a `Noul` "is he talking to the coach about the game in
progress?" (`talk`, catches the ally on voice, the TV, swearing). The model is pinned (MODEL) so tools/eval_intents.py
reproduces what the lane does.

Rules (from tools/eval_intents.py: Jev 96% accurate on the fixture, p50 ~100 ms; at confidence >= 0.7 it acts on
91% of lines with 98% accuracy):
  - only in a game; talk < 0.3 is chatter, talk < 0.7 is unsure: both take the slow path;
  - act only when talk >= TALK_MIN (0.7) and the intent confidence >= THRESHOLD (0.7);
  - pause / resume also need their keyword in the text (pause|stop|hold, resume|go again|unpause|continue);
  - counter_unit answers from live/counters.json when a known unit is named; an unknown unit goes to the coach;
  - enemy_shows / behind / ask_build are tagged and passed to the coach (no canned replies yet);
  - chatter, low confidence, no game, a failed guard or an API error: the slow path, unchanged (MISS + the coach).

Off switch: SC2_INTENTS=0. Otherwise on when typesafe_sdk (live/.pkgs) and TYPESAFE_API_KEY (live/.env.typesafe,
read via brain.load_key; never printed or logged) are both there.
Stdlib only; the SDK is imported lazily.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Literal, NamedTuple

from brain import load_key

HERE = Path(__file__).resolve().parent
COUNTERS = HERE / "counters.json"
MODEL = "jev-1.14.0"  # pinned (what jev-latest pointed at on 2026-10-30) so the offline eval reproduces the lane
THRESHOLD = 0.7      # act only at or above this confidence (the eval's knee: 91% coverage, 98% accuracy)
TALK_MIN = 0.7       # Noul "talking to the coach about this game": act only at or above this
TALK_CHATTER = 0.3   # below this the line is chatter (logged as such); in between: unsure, slow path
TIMEOUT = 0.6        # seconds for one classification: RetryPolicy's timeout is the TOTAL budget across tries
MAX_CALLS = 300      # per mic run: a runaway loop can't spend the API budget

# tools/eval_intents.py imports these, so the eval scores exactly what the lane asks.
# chatter first: Jev leans to the first option, and chatter is the safe fallback (the slow path).
INTENTS: dict[str, str] = {
    "chatter": "Not an in-game callout: software/dev talk, setup, testing the mic, small talk, feedback on the tool.",
    "pause": "The player asks to pause or stop the coach's clock or calls.",
    "resume": "The player asks to resume, unpause or restart the coach's clock or calls.",
    "counter_unit": "The player asks how to beat or counter an enemy unit or army composition.",
    "enemy_shows": "The player reports what the enemy is doing or showing: a rush, proxy, double gas, DTs, a tech "
                   "building, a unit type, many gateways, the enemy races.",
    "behind": "The player says he is behind or late on a building, unit or upgrade.",
    "ask_build": "The player asks what to build or do next in the game: expand, add production, pivot, push.",
}
INSTRUCTIONS = ("A StarCraft II Terran player speaks to his voice coach during or between games. "
                "Which intent is this transcribed line?")
TALK = "Is the owner talking to the coach about the StarCraft game in progress?"
TALK_CRITERIA = {"true": "The line is meant for his StarCraft coach about this game: a callout of what the enemy "
                         "has or does, a question about units, counters or what to build, saying he is behind, "
                         "or asking the coach to pause or resume. Short, informal or partial lines count.",
                 "false": "The line is clearly not for the coach about this game: talk to his ally or someone in "
                          "the room, a reaction to TV or a stream, swearing, or talk about the software, the mic "
                          "or the setup."}
# Sent with every line: without it Jev reads "pause the clock" as not about a game (talk ~0.3 on real callouts).
CONTEXT = ("Dickie, a Terran player, holds his push-to-talk key during a StarCraft II game in progress; "
           "his voice coach listens. 'line' is the transcript of what he said.")


def state(text: str) -> dict[str, str]:
    """The system_one state for one line (shared with tools/eval_intents.py)."""
    return {"context": CONTEXT, "line": text}

PAUSE_RX = re.compile(r"\b(pause|stop|hold)\b", re.I)                      # "unpause" has no \b before "pause"
RESUME_RX = re.compile(r"\b(resume|go again|unpause|continue)\b", re.I)
# Local replies only. TODO(coach): the 2v2 Live Coach page clock (ArtifactData coach/live `run`) is the coach's to
# drive; this lane only says it on the local page and logs it.
REPLIES: dict[str, str] = {"pause": "Paused.", "resume": "Back on."}
PASS = ("enemy_shows", "behind", "ask_build")   # tagged for the coach, answered by the coach

Act = Literal["say", "pass", "slow"]


class Action(NamedTuple):
    """decide()'s result. act: say = answer `line` now; pass = tag it and let the coach answer; slow = the
    unchanged slow path. tag is the single-token action for the INTENT log line (say:Thor, slow:low-conf, ...)."""
    act: Act
    line: str | None
    tag: str


class Verdict(NamedTuple):
    """classify()'s answer: the intent, its confidence, and P(talking to the coach about this game)."""
    intent: str
    conf: float
    talk: float


class Counter(NamedTuple):
    unit: str
    rx: re.Pattern[str]
    line: str


def load_counters(path: Path = COUNTERS) -> list[Counter]:
    """counters.json -> [Counter] in file order. Keys match whole words, any case; a space also matches a hyphen."""
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for unit, e in cfg["units"].items():
        keys = sorted(e["keys"], key=len, reverse=True)
        alts = "|".join(r"[\s-]+".join(map(re.escape, k.split())) for k in keys)
        out.append(Counter(unit, re.compile(rf"\b(?:{alts})\b", re.I), str(e["line"])))
    return out


_COUNTERS: list[Counter] | None = None


def counters() -> list[Counter]:
    global _COUNTERS
    if _COUNTERS is None:
        _COUNTERS = load_counters()
    return _COUNTERS


def find_unit(text: str, table: list[Counter] | None = None) -> Counter | None:
    """The unit named first in the text (earliest match; on a tie the first in the table), or None."""
    best: tuple[int, int, Counter] | None = None
    for i, c in enumerate(counters() if table is None else table):
        m = c.rx.search(text)
        if m and (best is None or (m.start(), i) < best[:2]):
            best = (m.start(), i, c)
    return best[2] if best else None


def all_lines(table: list[Counter] | None = None) -> list[str]:
    """Every line the lane can speak (for voice/render_counters.py)."""
    return list(dict.fromkeys([*REPLIES.values(), *(c.line for c in (counters() if table is None else table))]))


def decide(intent: str | None, conf: float, in_game: bool, text: str, talk: float = 1.0,
           table: list[Counter] | None = None) -> Action:
    """What the mic does with a classified callout. Pure: no network, no state. talk is the Noul answer
    (P(talking to the coach about this game)); the default 1.0 means "not asked"."""
    if not in_game:
        return Action("slow", None, "slow:no-game")
    if intent is None:
        return Action("slow", None, "slow:error")
    if talk < TALK_CHATTER:
        return Action("slow", None, "slow:not-talk")
    if talk < TALK_MIN:
        return Action("slow", None, "slow:unsure-talk")
    if conf < THRESHOLD:
        return Action("slow", None, "slow:low-conf")
    if intent == "pause" or intent == "resume":
        rx = PAUSE_RX if intent == "pause" else RESUME_RX
        if not rx.search(text):
            return Action("slow", None, "slow:no-keyword")
        return Action("say", REPLIES[intent], "say")
    if intent == "counter_unit":
        c = find_unit(text, table)
        return Action("say", c.line, f"say:{c.unit.replace(' ', '')}") if c else Action("slow", None, "slow:no-unit")
    if intent in PASS:
        return Action("pass", None, "pass")
    return Action("slow", None, "slow:chatter" if intent == "chatter" else "slow:unknown")


def questions(choice_cls: Any, noul_cls: Any) -> dict[str, Any]:
    """The two questions of one lane request (also used by tools/eval_intents.py). Classes passed in: the SDK is
    only imported where it is installed."""
    return {"intent": choice_cls(instructions=INSTRUCTIONS, criteria=INTENTS),
            "talk": noul_cls(instructions=TALK, criteria=TALK_CRITERIA)}


def parse(answers: Mapping[str, Any]) -> Verdict:
    """A system_one response's answers -> Verdict (raises on a malformed answer; classify() catches it)."""
    a, t = answers["intent"], answers["talk"]
    return Verdict(str(a.choice), float(a.confidence), float(t.noul))


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """SC2_INTENTS=0 (or off/false/no) turns the lane off; anything else, or unset, leaves it on."""
    v = (os.environ if env is None else env).get("SC2_INTENTS", "1").strip().lower()
    return v not in ("0", "off", "false", "no")


class IntentLane:
    """One Jev Choice per callout. Construct once at mic start; `ready` says whether classify() can call out
    (`why` says why not). classify() returns None on anything unexpected: the mic then takes the slow path."""

    def __init__(self, key_path: Path = HERE / ".env.typesafe", pkgs: Path = HERE / ".pkgs",
                 timeout: float = TIMEOUT, model: str = MODEL, max_calls: int = MAX_CALLS) -> None:
        self.timeout, self.max_calls = timeout, max_calls
        self.calls = 0
        self.ms: list[float] = []          # latency of the answered calls
        self.errors: list[str] = []        # exception class names only (never the message: it could echo a header)
        self.client: Any = None
        self._questions: dict[str, Any] = {}
        self.why = ""
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        try:
            if str(pkgs) not in sys.path and pkgs.is_dir():
                sys.path.append(str(pkgs))   # mic.ps1 already puts .pkgs on PYTHONPATH; append never shadows it
            from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeClient
        except Exception as e:
            self.why = f"typesafe_sdk not importable ({type(e).__name__})"
            return
        key = load_key(key_path)
        if not key:
            self.why = f"no TYPESAFE_API_KEY in {key_path.name}"
            return
        try:
            self.client = TypeSafeClient(api_key=key, model=model,
                                         retry=RetryPolicy(max_retries=0, timeout=timeout))
        except Exception as e:
            self.why = f"client init failed ({type(e).__name__})"
            return
        finally:
            del key
        self._questions = questions(Choice, Noul)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="intent")

    @property
    def ready(self) -> bool:
        return self.client is not None and self._pool is not None

    def _ask(self, text: str) -> Verdict:
        t0 = time.perf_counter()
        r = self.client.system_one(state=state(text), questions=self._questions, timeout=self.timeout)
        v = parse(r.answers)
        self.ms.append((time.perf_counter() - t0) * 1000)
        return v

    def classify(self, text: str) -> Verdict | None:
        """Verdict(intent, conf, talk) for one transcribed line, or None (not ready, call cap, timeout, any error,
        or an answer outside INTENTS). Blocks the caller at most ~timeout seconds; a late answer is dropped."""
        if not self.ready or not text.strip() or self._pool is None:
            return None
        with self._lock:
            if self.calls >= self.max_calls:
                return None
            self.calls += 1
        try:
            fut = self._pool.submit(self._ask, text.strip()[:300])
            v = fut.result(timeout=self.timeout + 0.05)
        except FutureTimeout:
            self.errors.append("Timeout")
            return None
        except Exception as e:
            self.errors.append(type(e).__name__)
            return None
        return v if v.intent in INTENTS else None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            if self.client is not None:
                self.client.close()
        except Exception:
            pass


def log_line(clock: str, v: Verdict | None, action: Action, text: str) -> str:
    """The mic.log line the coach's monitor greps:  INTENT [m:ss] <intent> <conf> <action> talk=<p> | text
    (an API error/timeout logs `none 0.00 slow:error`)."""
    if v is None:
        return f"INTENT [{clock}] none 0.00 {action.tag} | {text}"
    return f"INTENT [{clock}] {v.intent} {v.conf:.2f} {action.tag} talk={v.talk:.2f} | {text}"


def note_tag(intent: str, conf: float) -> str:
    """Appended to the owner-callout note so the coach sees the line was classified."""
    return f" [intent: {intent} {conf:.2f}]"
