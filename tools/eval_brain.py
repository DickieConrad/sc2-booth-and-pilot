#!/usr/bin/env python3
"""Replay recorded games from live/coach.db through the booth brain(s) and score them against the coach's calls.

A brain call matches a coach call when both are the same kind (family) within +-20 s. The coach's lines are split
into sentences and classified with a small keyword map; sentences that match no kind are judgment calls (listed,
not scored). The DB is opened read-only; the running helper stays its only writer.

    python3 tools/eval_brain.py                      # rules brain, games 19-22 (WSL or Windows)
    live\\.python\\python.exe tools\\eval_brain.py --jev   # + Jev (TypeSafe; Windows wheels in live/.pkgs)
    ... --games 19 20 --window 20 --verbose
"""
import argparse
import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "live"
sys.path.insert(0, str(LIVE))
import brain  # noqa: E402

# keyword map: coach sentence -> kinds (families). Order doesn't matter; a sentence can carry several.
# From game 28 the coach speaks numbers as words ("Sixty-seven of seventy", "Six hundred gas"): TENS catches those.
TENS = r"(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|one-oh|one hundred|a hundred)"
KEYWORDS = [
    ("lost_depot", r"lost a depot|check your main"),
    ("gas_idle", r"gas stopped|check your refinery"),
    ("depot", r"\bdepots? (now|next)|\b\d+ of \d+\b|\bdepots? and\b|\bof " + TENS + r"|\b(two|three) depots\b"),
    ("cc", r"command center now|\bcc (now|at (the )?natural|at 400)|if no cc"),
    ("orbital", r"\borbital\b"),
    ("stuck", r"supply stuck|only \d+ supply|supply only"),
    ("bank", r"banked|\bspend \d+|^\d{3,4}:|\b\d+ barracks\b|barracks times|\bbarracks now"
             r"|big bank|bank'?s piling|\bbank \w+-\w+|^\w+ hundred\.$"),
    ("gas", r"\bgas \d+|\d+ gas\b|hundred gas|gas (is (high|up)|over|\w+ hundred)|(burn|with) that gas"),
    ("turret", r"\bturrets?\b"),
    ("bunker", r"\bbunker\b"),
    ("stall", r"stall minute"),
]


def classify(text):
    """[(sentence, {kinds})] for one coach line."""
    out = []
    for s in re.split(r"(?<=[.!?])\s+", text.strip()):
        low = s.lower()
        if re.search(r"\b(3rd|third) cc\b", low):   # when to take a third base is a judgment call
            out.append((s, set()))
            continue
        kinds = {k for k, rx in KEYWORDS if re.search(rx, low)}
        if "cc" in kinds:   # "435: CC now" is saving for the CC, not a bank
            kinds.discard("bank")
        out.append((s, kinds))
    return out


def connect():
    # not as_uri(): SQLite rejects the //wsl.localhost authority (same as db.py); read-only, no locks (helper writes)
    return sqlite3.connect(f"file:{LIVE / 'coach.db'}?mode=ro&nolock=1", uri=True)


def load(conn, gid):
    g = conn.execute("SELECT matchup, length_sec FROM games WHERE id = ?", (gid,)).fetchone()
    hist = [dict(zip(("t", "minerals", "gas", "used", "cap"), r)) for r in conn.execute(
        "SELECT t, minerals, gas, used, cap FROM samples WHERE game_id = ? ORDER BY t", (gid,))]
    calls = conn.execute("SELECT t, text FROM calls WHERE game_id = ? AND source = 'coach' ORDER BY t",
                         (gid,)).fetchall()
    return (g[0] or ""), (g[1] or (hist[-1]["t"] if hist else 0)), hist, calls


def replay(br, hist, matchup):
    """Feed the samples one at a time (the DB has one every ~5 s); return calls and per-decision latency (ms)."""
    br.reset()
    out, lat = [], []
    for i in range(len(hist)):
        t0 = time.perf_counter()
        c = br.decide(hist[:i + 1], matchup)
        lat.append((time.perf_counter() - t0) * 1000)
        if c:
            out.append({"t": hist[i]["t"], **c})
    return out, lat


def score(bcalls, items, window):
    """items: coach formulaic (t, family). Greedy one-to-one matching by time distance."""
    used_b, matched = set(), 0
    for (t, fam) in items:
        cands = [(abs(b["t"] - t), j) for j, b in enumerate(bcalls)
                 if j not in used_b and brain.family(b["kind"]) == fam and abs(b["t"] - t) <= window]
        if cands:
            used_b.add(min(cands)[1])
            matched += 1
    return matched, len(used_b)


def mmss(s):
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "   n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, nargs="+", default=[19, 20, 21, 22])
    ap.add_argument("--window", type=float, default=20.0)
    ap.add_argument("--jev", action="store_true", help="also run the Jev brain (TypeSafe API, counts toward budget)")
    ap.add_argument("--conf-floor", type=float, default=0.5)
    ap.add_argument("--budget", type=int, default=400, help="total Jev API calls allowed (live/out/jev_usage.json)")
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()

    conn = connect()
    games = {gid: load(conn, gid) for gid in a.games}
    brains = [brain.RulesBrain()]
    if a.jev:
        try:
            brains.append(brain.JevBrain(conf_floor=a.conf_floor, budget=a.budget))
        except Exception as e:
            print(f"Jev brain unavailable ({type(e).__name__}: {str(e)[:200]}); rules only.\n")

    # coach items + judgment sentences
    print("== coach calls (keyword map)")
    items, judgment = {}, []
    for gid, (_, _, _, calls) in games.items():
        items[gid] = []
        for t, text in calls:
            for s, kinds in classify(text):
                if t < 5:   # the 0:00 briefing sets the game up; not a live call
                    judgment.append((gid, t, s, "briefing"))
                    continue
                for k in sorted(kinds):
                    items[gid].append((t, k))
                if not kinds:
                    judgment.append((gid, t, s, "judgment"))
        if a.verbose:
            for t, text in calls:
                tags = " | ".join(",".join(sorted(k)) or "-" for _, k in classify(text))
                print(f"  g{gid} {mmss(t):>5}  [{tags}]  {text}")
    n_items = sum(len(v) for v in items.values())
    print(f"  {n_items} formulaic call items, {sum(1 for j in judgment if j[3] == 'judgment')} judgment sentences, "
          f"{sum(1 for j in judgment if j[3] == 'briefing')} briefing sentences\n")

    rows, details = [], []
    for br in brains:
        tot = {"m": 0, "items": 0, "mb": 0, "calls": 0, "minutes": 0.0, "lat": []}
        for gid, (matchup, length, hist, _) in games.items():
            n_api = len(getattr(br, "stats", {}).get("api_ms", []))
            bcalls, lat = replay(br, hist, matchup)
            if hasattr(br, "stats"):   # Jev: latency of the decisions that went to the API (gap-closed ones skip it)
                lat = br.stats["api_ms"][n_api:]
            m, mb = score(bcalls, items[gid], a.window)
            minutes = length / 60
            extra = len(bcalls) - mb
            rows.append((br.name, f"g{gid} {matchup}", len(bcalls), pct(mb, len(bcalls)), pct(m, len(items[gid])),
                         f"{extra / minutes:4.2f}", lat))
            for k, v in (("m", m), ("items", len(items[gid])), ("mb", mb), ("calls", len(bcalls)),
                         ("minutes", minutes)):
                tot[k] += v
            tot["lat"] += lat
            details.append((br.name, gid, bcalls, items[gid]))
        rows.append((br.name, "TOTAL", tot["calls"], pct(tot["mb"], tot["calls"]), pct(tot["m"], tot["items"]),
                     f"{(tot['calls'] - tot['mb']) / tot['minutes']:4.2f}", tot["lat"]))

    print(f"== results (match = same kind within +-{a.window:g} s)")
    print("| brain | game | calls | precision | recall | extra/min | latency p50 | latency p95 |")
    print("|---|---|---|---|---|---|---|---|")
    for name, game, n, p, r, x, lat in rows:
        q = sorted(lat)
        p50 = statistics.median(q) if q else 0
        p95 = q[min(len(q) - 1, int(round(0.95 * (len(q) - 1))))] if q else 0
        f = (lambda v: f"{v * 1000:.0f} us") if p95 < 1 else (lambda v: f"{v:.0f} ms")
        b = "**" if game == "TOTAL" else ""
        print(f"| {name} | {b}{game}{b} | {n} | {p.strip()} | {r.strip()} | {x} | {f(p50)} | {f(p95)} |")

    for br in brains[1:]:
        s = br.stats
        c = s["conf"]
        print(f"\n== {br.name}: {s['calls']} API calls this run, {br._used()} of {br.budget} budget used in total; "
              f"mean confidence {statistics.mean(c) if c else 0:.2f}, agreed with rules {s['agree_rules']}/"
              f"{len(c)}, low-confidence fallbacks {s['fallback_low_conf']}, error fallbacks {s['fallback_error']}, "
              f"tokens {s['tokens_in']} in / {s['tokens_out']} out")
        for err in s["errors"]:
            print(f"   error: {err}")

    print("\n== judgment calls (the brain should not try these; Claude keeps them)")
    for gid, t, s, why in judgment:
        if why == "judgment":
            print(f"  g{gid} {mmss(t):>5}  {s}")

    if a.verbose:
        for name, gid, bcalls, its in details:
            print(f"\n-- {name} g{gid}: brain calls (* = matched a coach call)")
            for b in bcalls:
                hit = any(fam == brain.family(b["kind"]) and abs(t - b["t"]) <= a.window for t, fam in its)
                print(f"  {'*' if hit else ' '} {mmss(b['t']):>5} {b['kind']:<10} {b['confidence']:.2f}  {b['text']}")
            missed = [(t, f) for t, f in its if not any(brain.family(b["kind"]) == f and abs(t - b["t"]) <= a.window
                                                        for b in bcalls)]
            if missed:
                print("  missed: " + ", ".join(f"{mmss(t)} {f}" for t, f in missed))
    for br in brains[1:]:
        br.close()


if __name__ == "__main__":
    main()
