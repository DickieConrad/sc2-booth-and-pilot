#!/usr/bin/env python3
"""Parity: replay every recorded game in coach.db through the Python call rules and the Rust ones (coach-core)
and diff the calls (time, kind, text; brain calls also clip text). Target: identical.

Per tick, both sides get the same input: game clock, the panel read (or None), and for the brain what others said
since the last poll (the Python helper's calls of that tick, the coach's lines and the plan line from the DB).
- Python helper: timing.judge + the main loop's CONFIRM streak + Pusher.say's gaps (repeat_gap; wall = game clock).
- Python brain:  brain_run.Runner.feed (the live runner, rules off -> brain.RulesBrain).
- Rust:          `coach-core rules-replay` (the /rules/tick code, one hub), or with --http a `coach-core serve`
                 on a free port (started and stopped here, never :8767).
The DB is opened read-only (no locks: the helper is its only writer).

    python3 tools/rules_parity.py --db live/coach.db   # recorded ~5 s cadence
    python3 tools/rules_parity.py --db ... --step 0.5    # upsampled to the helper's 0.5 s tick (reads held <= 6 s)
    python3 tools/rules_parity.py --db ... --http        # through the HTTP route (adds round-trip times)
    python3 tools/rules_parity.py --db ... --export rust/coach-core/tests/fixtures/rules_parity.json
    python3 tools/rules_parity.py --synthetic [--export rust/coach-core/tests/fixtures/rules_parity_synth.json]
                                  # made-up Protoss / Zerg / Terran games instead of the DB (race wording)
Build first: (cd rust/coach-core && cargo build --release). Exit 1 on any difference.
"""
import argparse
import contextlib
import http.client
import io
import json
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "live"
sys.path.insert(0, str(LIVE))
import brain_run  # noqa: E402
import timing  # noqa: E402

HOLD = 6.0   # upsampling: a recorded read is held this long, then the panel is unread (None)
PROTOSS_PLANS = {"PX", "PT", "PZ", "PP"}


def synthetic_trace():
    """A made-up 10-minute panel trace every 5 s that walks the brain through most kinds: supply calls near the cap,
    a stall at 2:30, a CC-sized cap jump, a gas pile, banks, a lost 8-cap building and a lost 15-cap one.
    Deterministic, so the fixture never changes."""
    out, used, cap, minerals, gas = [], 12, 15, 50, 0
    for t in range(0, 601, 5):
        if t < 150 or 180 <= t < 400 or t >= 450:
            used = min(used + (1 if t % 10 == 0 else 0), cap)
        if t in (60, 100, 130, 220, 260, 300, 340, 380, 470, 510, 550):
            cap = min(200, cap + 8)
        if t == 200:
            cap += 15   # a CC / Nexus finished
        if t == 420:
            cap -= 8    # a pylon / depot / overlord died
        if t == 440:
            cap -= 15   # a CC / Nexus died
        if t < 90:
            minerals = min(minerals + 25, 340)
        elif t == 90:
            minerals = 20
        elif t < 200:
            minerals += 20
        elif t < 330:
            minerals = 400 + (t - 200) * 6
        elif t == 330:
            minerals = 150
        else:
            minerals = min(minerals + 30, 1400)
        if 100 <= t < 250 or t >= 300:
            gas = min(gas + 12, 700)
        out.append({"t": float(t), "minerals": minerals, "gas": gas, "used": used, "cap": cap})
    return out


# id, race, matchup, plan (the helper's; None = brain only)
SYNTHETIC = [(9001, "P", "?", "PX"), (9002, "Z", "T", None), (9003, "T", "Z", "A"), (9004, "P", "TP", None)]


def synthetic_games():
    return [{"id": gid, "race": race, "matchup": matchup, "plan": plan, "samples": synthetic_trace(), "others": []}
            for gid, race, matchup, plan in SYNTHETIC]


def load_games(db, ids=None):
    conn = sqlite3.connect(f"file:{Path(db).expanduser()}?mode=ro&nolock=1", uri=True)
    try:
        games = []
        for gid, matchup, plan, coach_plan in conn.execute(
                "SELECT id, matchup, plan, coach_plan FROM games ORDER BY id"):
            if ids and gid not in ids:
                continue
            samples = [dict(zip(("t", "minerals", "gas", "used", "cap"), r)) for r in conn.execute(
                "SELECT t, minerals, gas, used, cap FROM samples WHERE game_id = ? ORDER BY rowid", (gid,))]
            # what the brain hears besides the helper's rule calls: the coach and the helper's plan line
            others = conn.execute("SELECT t, source, kind FROM calls WHERE game_id = ? AND (source = 'coach' OR "
                                  "kind = 'plan') ORDER BY t, rowid", (gid,)).fetchall()
            if samples:
                games.append({"id": gid, "matchup": matchup or "", "plan": plan or coach_plan, "samples": samples,
                              "others": others})
        return games
    finally:
        conn.close()


def ticks_of(samples, step):
    """[(t, panel or None)]: the recorded reads, or upsampled every `step` s holding each read for HOLD s."""
    out = []
    for i, s in enumerate(samples):
        panel = {k: s[k] for k in ("minerals", "gas", "used", "cap")}
        out.append((float(s["t"]), panel))
        if step and i + 1 < len(samples):
            k = 1
            while s["t"] + k * step < samples[i + 1]["t"] - 1e-9:
                out.append((s["t"] + k * step, panel if k * step <= HOLD else None))
                k += 1
    return out


class _Clock:
    """Stands in for the time module inside timing.py: wall clock = game clock."""
    now = 0.0

    @staticmethod
    def time():
        return _Clock.now


def py_helper(ticks, steps, race):
    """The helper's rule path from timing.run(), tick for tick. -> (calls [(i, t, kind, text)], latencies ms)."""
    pusher = timing.Pusher.__new__(timing.Pusher)   # no __init__: it would create out/ dirs
    pusher.dry_run, pusher.last_say, pusher.recent = True, 0.0, {}
    hits, calls, lat = {}, [], []
    real_time, timing.time = timing.time, _Clock
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            for i, (t, panel) in enumerate(ticks):
                _Clock.now = t
                t0 = time.perf_counter()
                if panel is None:
                    hits = {}
                else:
                    verdict = timing.judge(panel, t, steps, race)
                    kind = verdict[0] if verdict else None
                    hits = {kind: hits.get(kind, 0) + 1} if kind else {}
                    if kind and hits[kind] >= timing.CONFIRM and pusher.say(
                            verdict[1], kind, t, False, timing.repeat_gap(kind, panel["minerals"])):
                        hits = {}
                        calls.append((i, t, kind, verdict[1]))
                lat.append((time.perf_counter() - t0) * 1000)
    finally:
        timing.time = real_time
    return calls, lat


def externals(game, ticks, helper_calls):
    """Per tick, the says the brain sees for the first time: [(kind, from)], DB lines then the helper's call.
    The live brain's own recorded lines (kind "brain-*") are left out: the replayed brains make their own calls
    (Runner.feed skips them anyway; sent to coach-core as the coach they would hold its gap)."""
    by_tick = {i: kind for i, _, kind, _ in helper_calls}
    others, j = game["others"], 0
    out = []
    for i, (t, _) in enumerate(ticks):
        now = []
        while j < len(others) and others[j][0] <= t:
            _, src, kind = others[j]
            if not str(kind).startswith("brain-"):
                now.append((kind, "coach" if src == "coach" else "helper"))
            j += 1
        if i in by_tick:
            now.append((by_tick[i], "helper"))
        out.append(now)
    return out


def py_brain(ticks, matchup, ext, race="T"):
    runner = brain_run.Runner(lambda call: None, lambda line: None)
    calls, lat, n = [], [], 0
    for i, (t, panel) in enumerate(ticks):
        says = []
        for kind, src in ext[i]:
            n += 1
            says.append({"id": n, "wall": n, "kind": kind, "from": src})
        st = {"in_game": True, "clock": t, "key": matchup, "race": race, "says": says, "panel": panel}
        t0 = time.perf_counter()
        call = runner.feed(st)
        lat.append((time.perf_counter() - t0) * 1000)
        if call:
            calls.append((i, t, call["kind"], call["text"], call["clip_text"]))
    return calls, lat


def requests_for(game, ticks, ext, race):
    reqs = []
    for i, (t, panel) in enumerate(ticks):
        r = {"game": f"g{game['id']}", "t": t, "sample": panel,
             "brain": {"matchup": game["matchup"], "external": [k if src != "coach" else None for k, src in ext[i]],
                       "race": race}}
        if game["plan"]:
            r["helper"] = {"plan": game["plan"], "race": race, "wall": t}
        reqs.append(r)
    return reqs


def rust_replay(binary, reqs):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("".join(json.dumps(r) + "\n" for r in reqs))
        path = f.name
    try:
        out = subprocess.run([binary, "rules-replay", "--live", str(LIVE), path], capture_output=True, text=True,
                             check=True).stdout
    finally:
        Path(path).unlink()
    return [json.loads(line) for line in out.splitlines()]


class RustServer:
    """`coach-core serve --port 0` started (and stopped) here; requests over one kept-open connection."""

    def __init__(self, binary):
        self.proc = subprocess.Popen([binary, "serve", "--port", "0", "--live", str(LIVE)],
                                     stderr=subprocess.PIPE, text=True)
        line = self.proc.stderr.readline()
        m = re.search(r"127\.0\.0\.1:(\d+)", line)
        if not m:
            self.close()
            raise RuntimeError(f"coach-core serve did not start: {line!r}")
        self.port = int(m.group(1))
        assert self.port not in (8765, 8766, 8767)
        self.conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def tick(self, req):
        t0 = time.perf_counter()
        self.conn.request("POST", "/rules/tick", json.dumps(req), {"Content-Type": "application/json"})
        resp = self.conn.getresponse()
        out = json.loads(resp.read())
        out["rtt_ms"] = (time.perf_counter() - t0) * 1000
        return out

    def close(self):
        self.proc.terminate()   # the process this tool started, by its own handle
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else 0.0


def fmt_ms(v):
    return f"{v * 1000:.1f} us" if v < 1 else f"{v:.2f} ms"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(LIVE / "coach.db"))
    ap.add_argument("--bin", default=str(ROOT / "rust" / "coach-core" / "target" / "release" / "coach-core"))
    ap.add_argument("--games", type=int, nargs="+")
    ap.add_argument("--step", type=float, default=0.0, help="upsample to this tick (s); 0 = recorded cadence")
    ap.add_argument("--http", action="store_true", help="go through POST /rules/tick on a private coach-core")
    ap.add_argument("--export", metavar="JSON", help="write inputs + the Python calls as a cargo test fixture")
    ap.add_argument("--synthetic", action="store_true", help="replay the made-up race games, not the DB")
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()
    if not Path(a.bin).exists():
        sys.exit(f"no coach-core binary at {a.bin}: (cd rust/coach-core && cargo build --release), or pass --bin")

    plans = timing.load_plans()["plans"]
    games = synthetic_games() if a.synthetic else load_games(a.db, set(a.games or []))
    server = RustServer(a.bin) if a.http else None
    rows, diffs, fixture = [], 0, []
    lat = {"py_helper": [], "py_brain": [], "py_total": [], "rust": [], "rtt": []}
    try:
        for g in games:
            ticks = ticks_of(g["samples"], a.step)
            # the followed player's race, for the helper's and the brain's wording (recorded games: from the plan)
            race = g.get("race") or ("P" if g["plan"] in PROTOSS_PLANS else "T")
            steps = plans[g["plan"]]["steps"] if g["plan"] in plans else None
            if g["plan"] and steps is None:
                print(f"g{g['id']}: plan {g['plan']} not in live/plans*.json; helper skipped")
                g["plan"] = None
            ph, lh = py_helper(ticks, steps, race) if steps else ([], [0.0] * len(ticks))
            ext = externals(g, ticks, ph)
            pb, lb = py_brain(ticks, g["matchup"], ext, race)
            reqs = requests_for(g, ticks, ext, race)
            res = [server.tick(r) for r in reqs] if server else rust_replay(a.bin, reqs)
            if server:
                lat["rtt"] += [r["rtt_ms"] for r in res]
            else:
                lat["rust"] += [r["ns"] / 1e6 for r in res]
            lat["py_helper"] += lh
            lat["py_brain"] += lb
            lat["py_total"] += [x + y for x, y in zip(lh, lb)]
            errs = [r for r in res if "error" in r]
            if errs:
                sys.exit(f"g{g['id']}: coach-core error {errs[0]}")
            rh = [(i, ticks[i][0], r["helper"]["kind"], r["helper"]["text"]) for i, r in enumerate(res) if r["helper"]]
            rb = [(i, ticks[i][0], r["brain"]["kind"], r["brain"]["text"], r["brain"]["clip_text"])
                  for i, r in enumerate(res) if r["brain"]]
            dh = sorted(set(ph) ^ set(rh))
            db_ = sorted(set(pb) ^ set(rb))
            diffs += len(dh) + len(db_)
            rows.append((g, len(ticks), ph, rh, dh, pb, rb, db_))
            if a.export:
                fixture.append({
                    "id": g["id"], "plan": g["plan"], "race": race, "matchup": g["matchup"],
                    "ticks": [[t, *(list(p.values()) if p else [None] * 4), [k if s != "coach" else None
                                                                              for k, s in ext[i]]]
                              for i, (t, p) in enumerate(ticks)],
                    "helper": [list(c) for c in ph], "brain": [list(c) for c in pb]})
    finally:
        if server:
            server.close()

    mode = f"upsampled to {a.step:g} s" if a.step else "recorded cadence"
    print(f"== parity, {len(rows)} games, {mode}{', via HTTP' if a.http else ''}")
    print("| game | plan | vs | ticks | helper py/rust | brain py/rust | result |")
    print("|---|---|---|---|---|---|---|")
    for g, n, ph, rh, dh, pb, rb, db_ in rows:
        ok = "identical" if not dh and not db_ else f"**{len(dh) + len(db_)} diffs**"
        print(f"| g{g['id']} | {g['plan'] or '-'} | {g['matchup'] or '-'} | {n} | {len(ph)}/{len(rh)} | "
              f"{len(pb)}/{len(rb)} | {ok} |")
    th = sum(len(r[2]) for r in rows)
    tb = sum(len(r[5]) for r in rows)
    print(f"| **total** | | | {sum(r[1] for r in rows)} | {th}/{sum(len(r[3]) for r in rows)} | "
          f"{tb}/{sum(len(r[6]) for r in rows)} | {'identical' if not diffs else f'**{diffs} diffs**'} |")
    for g, _, ph, _, dh, pb, _, db_ in rows:
        for d in dh + db_:
            side = "py" if d in ph or d in pb else "rust"
            print(f"  g{g['id']} only in {side}: {timing.mmss(d[1])} {d[2:]}")
        if a.verbose:
            for c in ph:
                print(f"  g{g['id']} helper {timing.mmss(c[1])} {c[2]}: {c[3]}")
            for c in pb:
                print(f"  g{g['id']} brain  {timing.mmss(c[1])} {c[2]}: {c[3]} | {c[4]}")

    print("\n== per-tick time (helper + brain rules for one tick)")
    print("| side | ticks | p50 | p95 | max | mean |")
    print("|---|---|---|---|---|---|")
    for name, key in (("Python helper (judge + gate)", "py_helper"), ("Python brain (Runner.feed)", "py_brain"),
                      ("Python total", "py_total"), ("Rust hub.tick (in-process)", "rust"),
                      ("Rust via HTTP (round trip)", "rtt")):
        xs = lat[key]
        if xs:
            print(f"| {name} | {len(xs)} | {fmt_ms(q(xs, .5))} | {fmt_ms(q(xs, .95))} | {fmt_ms(max(xs))} | "
                  f"{fmt_ms(statistics.mean(xs))} |")

    if a.export:
        Path(a.export).write_text(json.dumps({"step": a.step, "games": fixture}, separators=(",", ":")) + "\n")
        print(f"\nwrote {a.export} ({len(fixture)} games)")
    sys.exit(1 if diffs else 0)


if __name__ == "__main__":
    main()
