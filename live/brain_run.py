"""Booth brain, live: poll the helper's /state, run the rules brain, speak its calls as pre-rendered coach clips.

OFF by default: nothing starts this; run it by hand (Windows, project root) when the owner wants it:
    live\\.python\\python.exe live\\brain_run.py              # live: POST /say {text, audio, kind} to :8765
    live\\.python\\python.exe live\\brain_run.py --dry-run    # log only, post nothing
Options: --port N (helper port; tests use a fake /state server on another port), --seconds S (stop after S),
--interval 0.5 (poll seconds). Logs `BRAIN [m:ss] kind text` to live/out/brain.log and stdout.
SC2_RULES=rust: the decision comes from coach-core (:8767 POST /rules/tick, same calls); Python rules if it's down.
Read-only toward the game: it only reads the helper's /state and posts lines to the local page.
Clips come from live/voice/render_brain.py; a missing clip goes out text-only (browser speech).
"""
import argparse
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "voice"))
import brain  # noqa: E402
import looptime  # noqa: E402
import models  # noqa: E402
import rules_client  # noqa: E402
from models import BrainCall, Sample, StateData  # noqa: E402

LOG = HERE / "out" / "brain.log"
CLIPS = HERE / "voice" / "clips"


def mmss(sec: float | None) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def get_state(port: int, timeout: float = 1.0) -> StateData:
    """The helper's /state, validated (models.STATE): a malformed one raises ValidationError, which the poll loop
    logs like an unreachable helper and retries."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=timeout) as r:
        return models.STATE.validate_json(r.read())


class Runner:
    """One game's history + the rules brain. feed(state) -> the call made (or None).
    rules: a rules_client.RulesClient; when it answers, coach-core decides (the local brain only notes the call,
    so a fallback mid-game keeps the gaps); when it is off or down, the local brain decides."""

    def __init__(self, say: Callable[[BrainCall], object], log: Callable[[str], object] = print,
                 rules: rules_client.RulesClient | None = None) -> None:
        self.say, self.log, self.rules = say, log, rules
        self.brain = brain.RulesBrain()
        self.hist: list[Sample] = []
        self.last_t = -1.0
        self.seen_says: set[tuple[Any, Any]] = set()

    def reset(self) -> None:
        self.brain.reset()
        self.hist, self.last_t = [], -1.0
        if self.rules:
            self.rules.new_game()

    def feed(self, st: StateData) -> BrainCall | None:
        if not st.get("in_game"):
            if self.hist:
                self.reset()
            return None
        t = float(st.get("clock") or 0.0)
        if t < self.last_t - 5:   # a new game
            self.reset()
        # anyone else speaking (the coach by hand, a helper call) holds the brain's global gap; a helper "bank"
        # call also counts as the brain's bank call (at the latest bank, so the brain repeats only once it grows).
        # The brain's own lines come back with kind "brain-*".
        ext: list[str | None] = []   # the same notes, for coach-core
        for s in st.get("says") or []:
            key = (s.get("id"), s.get("wall"))
            if key in self.seen_says:
                continue
            self.seen_says.add(key)
            if not str(s.get("kind", "")).startswith("brain-"):
                ext.append(s.get("kind") if s.get("from") != "coach" else None)
                if self.hist:
                    self.brain.note_external(t, ext[-1], self.hist[-1])
        p = st.get("panel")
        race = brain.race_of(st.get("race"))   # the followed player's; an older helper sends none: Terran
        rust = self.rules.brain(t, p, st.get("key") or "", ext, race) if self.rules else None
        if not p or t <= self.last_t:
            return None
        self.last_t = t
        self.hist.append({"t": t, "minerals": int(p["minerals"]), "gas": int(p["gas"]), "used": int(p["used"]),
                          "cap": int(p["cap"])})
        if len(self.hist) > 3000:   # ~25 min at 0.5 s; the windows look back 60 s, the CC flag only matters < 3:15
            del self.hist[:600]
        if rust is None:
            call = self.brain.decide(self.hist, st.get("key") or "", race=race)
        else:
            call = rust.get("brain")
            if call:
                self.brain.note_external(t, call["kind"], self.hist[-1])
        if call:
            self.log(f"BRAIN [{mmss(t)}] {call['kind']} {call['text']}")
            self.say(call)
        return call


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--seconds", type=float, default=0, help="stop after this many seconds (0 = run until Ctrl+C)")
    ap.add_argument("--dry-run", action="store_true", help="log calls, post nothing")
    a = ap.parse_args()
    from coach_say import clip_name, post_say  # voice/ (studio reads its .env at import)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    logf = LOG.open("a", encoding="utf-8")

    def log(line: str) -> None:
        print(line, flush=True)
        logf.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        logf.flush()

    def say(call: BrainCall) -> None:
        if a.dry_run:
            return
        name = clip_name(call["clip_text"])
        audio = name if (CLIPS / name).exists() else None
        try:
            post_say(a.port, call["text"], audio, kind=f"brain-{call['kind']}")
        except Exception as e:
            log(f"BRAIN say failed: {e}")

    trace = looptime.tracer("brain")   # SC2_TIMING_TRACE=1: poll / decide / post times per call
    timed: dict[str, float] = {}


    def say_timed(call: BrainCall) -> None:
        t = time.perf_counter()
        say(call)
        timed["post"] = (time.perf_counter() - t) * 1000

    run = Runner(say_timed if trace.on else say, log, rules_client.RulesClient("brain", log=log))
    log(f"BRAIN up: polling :{a.port} every {a.interval}s{' [dry run]' if a.dry_run else ''}")
    stop = time.time() + a.seconds if a.seconds else None
    misses = 0
    try:
        while not stop or time.time() < stop:
            t0 = time.time()
            try:
                p0 = time.perf_counter()
                st = get_state(a.port)
                p1 = time.perf_counter()
                call = run.feed(st)
                if call and trace.on:
                    trace.emit({"k": "brain", "w": round(t0, 3), "poll": round((p1 - p0) * 1000, 2),
                                "decide": round((time.perf_counter() - p1) * 1000 - timed.get("post", 0), 2),
                                "post": round(timed.pop("post", 0), 2), "kind": call["kind"],
                                "text": call["text"][:60]})
                misses = 0
            except Exception as e:
                misses += 1
                if misses in (1, 20) or misses % 240 == 0:
                    log(f"BRAIN /state unavailable ({type(e).__name__}); retrying")
            time.sleep(max(0.0, a.interval - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    log("BRAIN stopped")


if __name__ == "__main__":
    main()
