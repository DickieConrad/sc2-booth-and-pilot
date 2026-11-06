"""Latency trace for the live loop (game state -> call on the page -> audio), and its summary.

OFF by default. Turn it on with the environment variable SC2_TIMING_TRACE=1 before starting a process
(timing.py, voice/coach_say.py, brain_run.py); each then appends compact JSON lines to
live/out/looptrace-<proc>.log (gitignored). The local page sees `trace: true` in /state and reports its own
receipt / audio-start times on its next /notes poll. With tracing off nothing is recorded and nothing changes.

Writer (used by the processes):
    TRACE = looptime.tracer("helper")      # a no-op unless SC2_TIMING_TRACE is set
    g = TRACE.span("api", fetch_game)      # time one call; on the tick thread it adds to the current tick
    TRACE.begin_tick(); ...; TRACE.end_tick(t=...)
The helper's writes are buffered and flushed by a background thread once a second, so the tick thread
never waits on the (\\\\wsl.localhost) disk.

Records ("k"): tick (per-stage ms, interval, work), call (a say added to the page state), cue (coach-core
events), sse (state version written to a page), page (page receipt + audio start), say (coach_say.py),
brain (brain_run.py), span (a timed call off the tick thread).

Summary:
    python live/looptime.py [FILES...] [--slow 10] [--tick 0.5]
p50/p95/max per tick stage, tick jitter and overruns, per-call breakdown and end-to-end
("state change -> call text on page" and "-> audio start"), coach_say render vs cache, slowest ticks.
"""
import argparse
import atexit
import json
import math
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, ParamSpec, TypeVar
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
ENV = "SC2_TIMING_TRACE"
TICK = 0.5   # timing.TICK (not imported: importing timing.py here would pull the whole helper)
P = ParamSpec("P")
R = TypeVar("R")
Rec = dict[str, Any]          # one trace record (a JSON line)
Stats = dict[str, Any]        # {"n", "p50", "p95", "max"}


# ---------------------------------------------------------------- writer

class Tracer:
    """Off: every method is a cheap pass-through. On: records go to a buffer, flushed by a daemon thread
    (buffered=True, long-running processes) or written at once (buffered=False, short scripts)."""

    def __init__(self, path: str | Path | None = None, buffered: bool = True) -> None:
        self.on = path is not None
        self.path = Path(path) if path else None
        self.buffered = buffered
        self.lock = threading.Lock()
        self.buf: list[str] = []
        self.tick_thread: int | None = None
        self.cur: dict[str, float] | None = None   # stage -> ms for the tick in progress
        self.tick_t0: int | None = None
        self.prev_t0: int | None = None
        self.tick_wall = 0.0
        self.ended: dict[str, float] = {}   # stage -> wall time its last span ended (e.g. "cap" = frame grabbed)
        self.streak: dict[str, float] = {}  # condition kind -> wall of the first capture that showed it (helper rules)
        self.marks: dict[int, list[tuple[str, int]]] = {}   # state version -> [(key, perf_ns)] waiting for a page
        if self.on and buffered:
            threading.Thread(target=self._flusher, daemon=True).start()
            atexit.register(self.flush)   # the last second of records on a normal exit

    # -- output
    def emit(self, rec: Rec) -> None:
        if not self.on:
            return
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with self.lock:
            self.buf.append(line)
        if not self.buffered:
            self.flush()

    def flush(self) -> None:
        with self.lock:
            lines, self.buf = self.buf, []
        if lines and self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as f:
                    f.write("".join(lines))
            except OSError:
                pass   # tracing must never break the helper

    def _flusher(self) -> None:
        while True:
            time.sleep(1.0)
            self.flush()

    # -- timing
    def span(self, name: str, fn: Callable[P, R], *args: P.args, **kw: P.kwargs) -> R:
        if not self.on:
            return fn(*args, **kw)
        t0 = time.perf_counter_ns()
        try:
            return fn(*args, **kw)
        finally:
            self.add(name, (time.perf_counter_ns() - t0) / 1e6)

    def add(self, name: str, ms: float) -> None:
        if not self.on:
            return
        self.ended[name] = time.time()
        if self.cur is not None and threading.get_ident() == self.tick_thread:
            self.cur[name] = self.cur.get(name, 0.0) + ms
        else:
            self.emit({"k": "span", "w": round(time.time(), 3), "n": name, "ms": round(ms, 3)})

    def begin_tick(self) -> None:
        if not self.on:
            return
        self.tick_thread = threading.get_ident()
        self.prev_t0, self.tick_t0 = self.tick_t0, time.perf_counter_ns()
        self.tick_wall = time.time()
        self.cur = {}

    def end_tick(self, **extra: Any) -> None:
        if not self.on or self.cur is None or self.tick_t0 is None:
            return
        rec = {"k": "tick", "w": round(self.tick_wall, 3),
               "work": round((time.perf_counter_ns() - self.tick_t0) / 1e6, 3)}
        if self.prev_t0 is not None:
            rec["iv"] = round((self.tick_t0 - self.prev_t0) / 1e6, 3)
        rec.update({k: round(v, 3) for k, v in self.cur.items()})
        rec.update(extra)
        self.cur = None
        self.emit(rec)

    # -- helper calls
    def seen(self, kind: str | None) -> None:
        """The rule verdict for this read (None = nothing): remembers when the current streak started."""
        if self.on:
            self.streak = {kind: self.streak.get(kind, self.ended.get("cap", time.time()))} if kind else {}

    def call(self, say: Mapping[str, Any], version: int) -> None:
        """A say was added to the page state (any source). Helper calls carry their detection times."""
        if not self.on:
            return
        rec = {"k": "call", "id": say["id"], "kind": say["kind"], "src": say["from"], "w": round(say["wall"], 3),
               "t": say["t"], "text": say["text"][:60]}
        if say["from"] == "helper" and threading.get_ident() == self.tick_thread:
            rec["cap"] = round(self.ended.get("cap", say["wall"]), 3)
            if say["kind"] in self.streak:
                rec["seen"] = round(self.streak.pop(say["kind"]), 3)
        self.mark(version, f"s{say['id']}")
        self.emit(rec)

    def cues(self, events: Sequence[Mapping[str, Any]], version: int, t: float, shift: int) -> None:
        if not self.on:
            return
        cap = self.ended.get("cap")
        for ev in events:
            self.mark(version, f"c{ev['id']}")
            self.emit({"k": "cue", "id": ev["id"], "type": ev.get("type"), "sec": ev.get("sec"), "t": t,
                       "shift": shift, "w": round(time.time(), 3), "cap": round(cap, 3) if cap else None,
                       "keys": ev.get("keys", [])[:3]})

    def mark(self, version: int, key: str) -> None:
        with self.lock:
            self.marks.setdefault(version, []).append((key, time.perf_counter_ns()))
            for v in sorted(self.marks)[:-200]:   # keep the last 200 versions
                del self.marks[v]

    def sse_sent(self, prev: int, seen: int, conn: int) -> None:
        """An /events stream wrote state version `seen` (the previous write was `prev`)."""
        if not self.on or prev < 0:   # a page's first write is the whole backlog, not a new change
            return
        now = time.perf_counter_ns()
        with self.lock:
            hits = [(k, t) for v, items in self.marks.items() if prev < v <= seen for k, t in items]
        for key, t in hits:
            self.emit({"k": "sse", "key": key, "c": conn, "w": round(time.time(), 3), "lag": round((now - t) / 1e6, 3)})

    def page_report(self, path: str) -> None:
        """/notes?trace=[...] from the page: its receipt / audio-start times (epoch ms)."""
        if not self.on or "?" not in path:
            return
        for raw in parse_qs(urlsplit(path).query).get("trace", []):
            try:
                items = json.loads(raw)
            except ValueError:
                continue
            for it in items if isinstance(items, list) else []:
                if isinstance(it, dict):
                    self.emit({**{k: it[k] for k in list(it)[:12]}, "k": "page"})   # the page can't rename k


def tracer(proc: str, buffered: bool = True) -> Tracer:
    """Tracer for one process; a no-op unless SC2_TIMING_TRACE is set (1/on/true, or a directory for the logs)."""
    v = os.environ.get(ENV, "").strip()
    if not v or v.lower() in ("0", "off", "false", "no"):
        return Tracer()
    d = OUT if v.lower() in ("1", "on", "true", "yes") else Path(v)
    return Tracer(d / f"looptrace-{proc}.log", buffered)


# ---------------------------------------------------------------- summary

def pct(xs: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..1) of a non-empty list; None for an empty one."""
    if not xs:
        return None
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))]


def stats(xs: Iterable[float | None]) -> Stats:
    vals = [x for x in xs if x is not None]
    return {"n": len(vals), "p50": pct(vals, 0.5), "p95": pct(vals, 0.95), "max": max(vals) if vals else None}


def parse(lines: Iterable[str]) -> list[Rec]:
    """JSON lines -> list of dicts (bad lines skipped)."""
    out: list[Rec] = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and "k" in rec:
            out.append(rec)
    return out


TICK_META = {"k", "w", "iv", "work", "t", "in_game", "src"}


def _secs(epoch_ms: Any) -> float | None:
    """A page-reported epoch-ms time in seconds (None when missing or 0)."""
    return float(epoch_ms) / 1000 if epoch_ms else None



def analyse(recs: Sequence[Rec], tick: float = TICK) -> dict[str, Any]:
    """-> dict of named stats + joined rows (everything looptime prints)."""
    ticks = [r for r in recs if r["k"] == "tick"]
    game_ticks = [r for r in ticks if r.get("in_game")]
    stages: dict[str, list[float]] = {}
    for r in game_ticks or ticks:
        for k, v in r.items():
            if k not in TICK_META and isinstance(v, (int, float)) and not isinstance(v, bool):
                stages.setdefault(k, []).append(v)
    tk = game_ticks or ticks
    res: dict[str, Any] = {"stages": {k: stats(v) for k, v in sorted(stages.items())},
           "work": stats([r["work"] for r in tk]),
           "interval": stats([r["iv"] for r in tk if "iv" in r]),
           "jitter": stats([r["iv"] - tick * 1000 for r in tk if "iv" in r]),
           "overruns": sum(r["work"] > tick * 1000 for r in tk),
           "ticks": len(tk)}

    sse: dict[str, float] = {}
    for r in recs:
        if r["k"] == "sse":
            sse[r["key"]] = min(r["lag"], sse.get(r["key"], math.inf))
    page: dict[str, Rec] = {}
    for r in recs:
        if r["k"] == "page" and "key" in r:
            page.setdefault(r["key"], r)   # first page to report wins

    def ms(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else (b - a) * 1000

    calls: list[Rec] = []
    for r in recs:
        if r["k"] != "call":
            continue
        key = f"s{r['id']}"
        p = page.get(key, {})
        recv = _secs(p.get("recv"))
        start = _secs(p.get("start"))
        calls.append({"key": key, "kind": r["kind"], "src": r["src"], "t": r.get("t"), "text": r.get("text", ""),
                      "confirm": ms(r.get("seen"), r.get("cap")), "decide": ms(r.get("cap"), r["w"]),
                      "sse": sse.get(key), "to_page": ms(r["w"], recv), "audio": ms(recv, start),
                      "how": p.get("how"), "dec": p.get("dec"), "wait": p.get("wait"),
                      "e2e_text": ms(r.get("seen"), recv), "e2e_audio": ms(r.get("seen"), start),
                      "w": r["w"], "recv_w": recv, "start_w": start})
    cues: list[Rec] = []
    for r in recs:
        if r["k"] != "cue":
            continue
        key = f"c{r['id']}"
        p = page.get(key, {})
        recv = _secs(p.get("recv"))
        start = _secs(p.get("start"))
        late = None
        if r.get("type") == "target" and r.get("sec") is not None and r.get("t") is not None:
            late = (r["t"] - (r["sec"] - 5 + (r.get("shift") or 0))) * 1000
        cues.append({"key": key, "type": r.get("type"), "keys": r.get("keys"), "late": late,
                     "decide": ms(r.get("cap"), r["w"]), "sse": sse.get(key), "to_page": ms(r["w"], recv),
                     "audio": ms(recv, start), "how": p.get("how"), "dec": p.get("dec"), "wait": p.get("wait"),
                     "e2e_text": ms(r.get("cap"), recv), "e2e_audio": ms(r.get("cap"), start)})
    # coach_say / brain_run lines: joined to the helper's call by text (the first call within 10 s after)
    says: list[Rec] = []
    for r in recs:
        if r["k"] not in ("say", "brain"):
            continue
        c = next((c for c in calls if c["src"] == "coach" and c["text"] == r.get("text", "")[:60]
                  and 0 <= c["w"] - r["w"] < 10), None)
        says.append({**r, "call": c, "e2e_text": ms(r["w"], c["recv_w"] if c else None),
                     "e2e_audio": ms(r["w"], c["start_w"] if c else None)})
    res.update(calls=calls, cues=cues, says=says,
               slow=sorted(tk, key=lambda r: r["work"], reverse=True))
    return res


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}" if abs(v) < 100 else f"{v:.0f}"


def _row(name: str, s: Stats, unit: str = "ms") -> str:
    return f"  {name:<24} {s['n']:>6}  {_fmt(s['p50']):>8}  {_fmt(s['p95']):>8}  {_fmt(s['max']):>8} {unit}"


def report(res: Mapping[str, Any], slow: int = 10, tick: float = TICK) -> str:
    out = []
    head = f"  {'stage':<24} {'n':>6}  {'p50':>8}  {'p95':>8}  {'max':>8}"
    out.append(f"== helper tick ({res['ticks']} ticks, TICK {tick * 1000:.0f} ms)")
    out.append(head)
    for k, s in res["stages"].items():
        out.append(_row(k, s))
    out.append(_row("work (whole tick)", res["work"]))
    out.append(_row("interval", res["interval"]))
    out.append(_row("jitter (interval-TICK)", res["jitter"]))
    out.append(f"  overruns (work > TICK): {res['overruns']}")

    def block(title: str, rows: Sequence[Rec], cols: Sequence[tuple[str, str]]) -> None:
        if not rows:
            return
        out.append(f"== {title} ({len(rows)})")
        out.append(head)
        for c, label in cols:
            out.append(_row(label, stats([r.get(c) for r in rows])))

    helper = [c for c in res["calls"] if c["src"] == "helper"]
    other = [c for c in res["calls"] if c["src"] != "helper"]
    block("helper calls (from the first read that showed the condition)", helper,
          [("confirm", "confirm wait"), ("decide", "capture -> call added"), ("sse", "call -> SSE written"),
           ("to_page", "call -> page received"), ("dec", "clip fetch+decode"), ("wait", "voice queue wait"),
           ("audio", "page received -> audio"), ("e2e_text", "E2E -> text on page"),
           ("e2e_audio", "E2E -> audio start")])
    if helper:
        out.append(f"  (+ 0..{tick * 1000:.0f} ms before the first read: the change waits for the next capture)")
    block("coach / brain lines on the page", other,
          [("sse", "call -> SSE written"), ("to_page", "call -> page received"), ("dec", "clip fetch+decode"),
           ("wait", "voice queue wait"), ("audio", "page received -> audio")])
    block("engine cues", res["cues"],
          [("late", "target cue vs plan time"), ("decide", "capture -> cue added"), ("sse", "cue -> SSE written"),
           ("to_page", "cue -> page received"), ("dec", "clip fetch+decode"), ("wait", "voice queue wait"),
           ("audio", "page received -> audio"), ("e2e_text", "E2E capture -> page"),
           ("e2e_audio", "E2E capture -> audio")])
    says = [s for s in res["says"] if s["k"] == "say"]
    if says:
        out.append(f"== coach_say.py ({len(says)})")
        out.append(head)
        for how in ("cached", "rendered", "fallback"):
            rows = [s for s in says if s.get("how") == how]
            if rows:
                out.append(_row(f"{how}: before POST", stats([s.get("prep") for s in rows])))
        out.append(_row("POST /say", stats([s.get("post") for s in says])))
        out.append(_row("E2E start -> page text", stats([s["e2e_text"] for s in says])))
        out.append(_row("E2E start -> audio start", stats([s["e2e_audio"] for s in says])))
    brains = [s for s in res["says"] if s["k"] == "brain"]
    if brains:
        out.append(f"== brain_run.py ({len(brains)})")
        out.append(head)
        for c in ("poll", "decide", "post"):
            out.append(_row(c, stats([s.get(c) for s in brains])))
    if slow and res["slow"]:
        out.append(f"== slowest {min(slow, len(res['slow']))} ticks")
        for r in res["slow"][:slow]:
            parts = " ".join(f"{k}={_fmt(v)}" for k, v in r.items()
                             if k not in TICK_META and isinstance(v, (int, float)) and not isinstance(v, bool))
            when = time.strftime("%H:%M:%S", time.localtime(r["w"]))
            out.append(f"  {when} game {r.get('t', '-')}: work {_fmt(r['work'])} ms  {parts}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:

    ap = argparse.ArgumentParser(description="Summarize live/out/looptrace-*.log (SC2_TIMING_TRACE=1).")
    ap.add_argument("files", nargs="*", help="trace files (default: live/out/looptrace-*.log)")
    ap.add_argument("--slow", type=int, default=10, help="list this many slowest ticks")
    ap.add_argument("--tick", type=float, default=TICK, help="the helper's TICK in seconds")
    a = ap.parse_args(argv)
    files = [Path(f) for f in a.files] or sorted(OUT.glob("looptrace-*.log"))
    if not files:
        print(f"no trace files; run with {ENV}=1 first")
        return 1
    recs = []
    for f in files:
        recs += parse(f.read_text(encoding="utf-8", errors="replace").splitlines())
    print(f"{len(recs)} records from {', '.join(f.name for f in files)}")
    print(report(analyse(recs, a.tick), a.slow, a.tick))
    return 0


if __name__ == "__main__":
    sys.exit(main())
