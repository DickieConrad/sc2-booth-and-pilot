#!/usr/bin/env python3
"""Per-tick cost of the boundary validation (live/models.py) vs the plain json.loads it replaced.

    .tools/typing/bin/python tools/bench_models.py [--n 20000]
    live\\.python\\python.exe tools\\bench_models.py      (Windows, the helper's interpreter; PYTHONPATH=live\\.pkgs)

Helper tick = SC2 /game + panel read + cue /tick + rules /tick (when SC2_RULES=rust), all validate_json;
brain poll = one full /state (30 says, 20 cues, 4 next steps). Payloads are realistic sizes, nothing is contacted.
Prints p50 / p95 per call in microseconds (looptime.stats, nearest rank).
"""
import argparse
import json
import sys
import time
from pathlib import Path

LIVE = Path(__file__).resolve().parent.parent / "live"
sys.path.insert(0, str(LIVE))
import looptime  # noqa: E402
import models  # noqa: E402
import pydantic  # noqa: E402

GAME = {"isReplay": False, "displayTime": 312.4, "players": [
    {"id": i, "name": f"P{i}", "type": "user", "race": r, "result": "Undecided"}
    for i, r in enumerate(("Terr", "Prot", "Zerg", "Terr"), 1)]}
PANEL = {"minerals": 455, "gas": 140, "used": 51, "cap": 62, "raw": "455 | 140 | 51/62"}
CUE = {"events": [{"type": "target", "keys": ["Factory", "Refinery"], "sec": 315}], "lateness": 2.5, "shift": 1}
RULES = {"game": "helper-1793400000000", "fresh": False, "helper": {"kind": "bank", "text": "Bank 450, spend"},
         "brain": {"kind": "depot", "text": "Depot now, 51 of 62", "clip_text": "Depot now.", "confidence": 1.0}}
STATE = {"in_game": True, "clock": 312.4, "clock_wall": 1.79e9, "plan": "A", "plan_name": "Steel & Blink",
         "coach_plan": "A", "cue_engine": True, "cue_seq": 40, "lateness": 2.5, "shift": 1, "vs": "a (Zerg) + b (Prot)",
         "key": "PZ", "panel": {k: PANEL[k] for k in ("minerals", "gas", "used", "cap")}, "plan_supply": 54, "seq": 30,
         "cues": [{"type": "target", "keys": ["Barracks"], "sec": 20 * i, "id": i} for i in range(20)],
         "next": [{"sec": 320 + 10 * i, "t": "5:20", "action": "Medivac x2", "supply": 56, "note": "from the Starport"}
                  for i in range(4)],
         "says": [{"id": i, "t": "4:10", "text": "Behind: 40 supply, plan 44: SCVs + Barracks", "kind": "behind",
                   "from": "helper", "wall": 1.79e9 + i} for i in range(30)], "now": 1.79e9}


def bench(fn, n):
    xs = []
    for _ in range(n):
        t0 = time.perf_counter_ns()
        fn()
        xs.append((time.perf_counter_ns() - t0) / 1000)
    return looptime.stats(xs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    a = ap.parse_args()
    cases = [("SC2 /game", json.dumps(GAME).encode(), models.GAME, False),
             ("panel-reader /read", json.dumps(PANEL).encode(), models.PANEL_READ, True),
             ("coach-core /tick (cues)", json.dumps(CUE).encode(), models.CUE_TICK, True),
             ("coach-core /rules/tick", json.dumps(RULES).encode(), models.RULES_TICK, True)]
    print(f"python {sys.version.split()[0]} on {sys.platform}, pydantic {pydantic.VERSION}, "
          f"n={a.n}")
    print("| payload | json.loads p50 / p95 | validate_json p50 / p95 | added p50 |")
    print("|---|---|---|---|")
    tick = [0.0, 0.0, 0.0]
    for name, raw, ad, strict in cases + [("brain poll: full /state", json.dumps(STATE).encode(), models.STATE,
                                           False)]:
        base = bench(lambda raw=raw: json.loads(raw), a.n)
        val = bench(lambda raw=raw, ad=ad, strict=strict: ad.validate_json(raw, strict=strict), a.n)
        print(f"| {name} | {base['p50']:.1f} / {base['p95']:.1f} us | {val['p50']:.1f} / {val['p95']:.1f} us | "
              f"{val['p50'] - base['p50']:+.1f} us |")
        if not name.startswith("brain"):
            tick = [tick[0] + base["p50"], tick[1] + val["p50"], tick[2] + val["p95"]]
    print(f"\nhelper tick, all four: json.loads {tick[0]:.1f} us -> validate_json {tick[1]:.1f} us p50 "
          f"(sum of p95s {tick[2]:.1f} us); budget 100 us")




if __name__ == "__main__":
    main()
