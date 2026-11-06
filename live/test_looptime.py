"""looptime.py tests (stdlib; run by ci.sh via tools/checks.py tests): percentiles, parsing, the tracer (off = no-op,
on = JSON lines), the helper's State hooks, and the joins/summary over a hand-made trace. Temp files only; no network
except a throwaway /events + /notes server on an OS-picked port (never the helper's :8765)."""
import http.client
import http.server
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import looptime  # noqa: E402
import timing  # noqa: E402


def test_pct():
    assert looptime.pct([], 0.5) is None
    assert looptime.pct([7], 0.95) == 7
    xs = list(range(1, 101))   # 1..100
    assert looptime.pct(xs, 0.5) == 50 and looptime.pct(xs, 0.95) == 95 and looptime.pct(xs, 1.0) == 100
    assert looptime.pct(xs, 0.0) == 1
    assert looptime.pct([5, 1, 3], 0.5) == 3   # unsorted input
    s = looptime.stats([2.0, None, 4.0, 1.0])
    assert s == {"n": 3, "p50": 2.0, "p95": 4.0, "max": 4.0}
    assert looptime.stats([]) == {"n": 0, "p50": None, "p95": None, "max": None}


def test_parse():
    lines = ['{"k":"tick","w":1,"work":2}', "garbage", "", '{"no_kind":1}', '{"k":"call","id":1', "[1,2]",
             '  {"k":"sse","key":"s1","lag":0.5}  ']
    recs = looptime.parse(lines)
    assert [r["k"] for r in recs] == ["tick", "sse"]


def test_tracer_off():
    tr = looptime.Tracer()
    assert not tr.on
    assert tr.span("api", lambda a, b=0: a + b, 2, b=3) == 5
    tr.begin_tick()
    tr.add("x", 1.0)
    tr.end_tick(t=1)
    tr.seen("bank")
    tr.call({"id": 1, "kind": "k", "from": "helper", "wall": 0, "t": "0:00", "text": "x"}, 1)
    tr.sse_sent(0, 1, 1)
    tr.page_report("/notes?trace=%5B%5D")
    assert tr.buf == [] and tr.marks == {}


def test_tracer_env(monkey_env=None):
    import os
    old = os.environ.pop(looptime.ENV, None)
    try:
        assert not looptime.tracer("x").on
        os.environ[looptime.ENV] = "0"
        assert not looptime.tracer("x").on
        with tempfile.TemporaryDirectory() as d:
            os.environ[looptime.ENV] = d
            tr = looptime.tracer("unit", buffered=False)
            assert tr.on and tr.path == Path(d) / "looptrace-unit.log"
            tr.emit({"k": "span", "n": "x", "ms": 1})
            assert json.loads(tr.path.read_text(encoding="utf-8")) == {"k": "span", "n": "x", "ms": 1}
    finally:
        os.environ.pop(looptime.ENV, None)
        if old is not None:
            os.environ[looptime.ENV] = old


def test_tick_and_call():
    with tempfile.TemporaryDirectory() as d:
        tr = looptime.Tracer(Path(d) / "t.log", buffered=False)
        tr.begin_tick()
        tr.span("api", time.sleep, 0.002)
        tr.span("cap", lambda: None)
        tr.span("read", lambda: None)
        tr.span("read", lambda: None)   # two spans of one stage add up
        tr.seen("bank")
        tr.end_tick(t=60.0, in_game=True)
        tr.begin_tick()
        tr.span("cap", lambda: None)
        tr.seen("bank")                 # same streak: keeps the first capture
        say = {"id": 7, "kind": "bank", "from": "helper", "wall": time.time(), "t": "1:00", "text": "Bank 400, spend"}
        tr.call(say, version=3)
        tr.end_tick(t=60.5, in_game=True)
        th = threading.Thread(target=tr.span, args=("db", lambda: None))   # off the tick thread: its own record
        th.start()
        th.join()
        tr.sse_sent(-1, 3, 1)           # a page's first write (backlog): not a delay
        tr.sse_sent(2, 3, 1)
        tr.page_report("/notes?trace=" + json.dumps([{"key": "s7", "recv": 1, "start": 2, "how": "clip"}]))
        tr.page_report("/notes?trace=notjson")
        tr.page_report("/notes?trace=" + json.dumps([{"k": "tick", "key": "s8"}]))   # can't pose as a tick
        recs = looptime.parse(tr.path.read_text(encoding="utf-8").splitlines())
    kinds = [r["k"] for r in recs]
    assert kinds == ["tick", "call", "tick", "span", "sse", "page", "page"], kinds
    assert recs[6]["key"] == "s8"
    t1, call, t2 = recs[0], recs[1], recs[2]
    assert t1["api"] >= 2 and "read" in t1 and "iv" not in t1 and t1["in_game"] and t1["t"] == 60.0
    assert t2["iv"] > 0 and t2["work"] >= 0
    assert call["id"] == 7 and call["seen"] <= call["cap"] <= call["w"] + 1 and call["text"] == "Bank 400, spend"
    assert recs[3]["n"] == "db" and recs[4]["key"] == "s7" and recs[5]["how"] == "clip"


def trace_fixture():
    """A hand-made trace: 4 ticks (one overrun), a helper call seen 1 s before it fired, a cue, a coach line."""
    w = 1000.0
    recs = [
        {"k": "tick", "w": w, "work": 5.0, "api": 2.0, "cap": 1.0, "read": 1.0, "t": 60, "in_game": True},
        {"k": "tick", "w": w + 0.5, "iv": 500.0, "work": 6.0, "api": 3.0, "cap": 1.0, "read": 1.0, "t": 60.5,
         "in_game": True},
        {"k": "tick", "w": w + 1.0, "iv": 520.0, "work": 700.0, "api": 650.0, "cap": 1.0, "read": 2.0, "db": 40.0,
         "t": 61, "in_game": True},
        {"k": "tick", "w": w + 1.7, "iv": 700.0, "work": 4.0, "api": 2.0, "cap": 1.0, "read": 1.0, "t": 61.7,
         "in_game": True},
        {"k": "tick", "w": w + 9, "work": 1.0, "api": 1.0},   # out of game: left out when game ticks exist
        {"k": "call", "id": 5, "kind": "bank", "src": "helper", "w": w + 1.01, "t": "1:01", "text": "Bank 400",
         "seen": w, "cap": w + 1.0},
        {"k": "sse", "key": "s5", "c": 1, "w": w + 1.02, "lag": 3.0},
        {"k": "sse", "key": "s5", "c": 2, "w": w + 1.02, "lag": 9.0},
        {"k": "page", "key": "s5", "recv": (w + 1.02) * 1000, "start": (w + 1.12) * 1000, "dec": 60, "wait": 40,
         "how": "clip"},
        {"k": "cue", "id": 3, "type": "target", "sec": 66, "t": 61.5, "shift": 0, "w": w + 1.71, "cap": w + 1.7,
         "keys": ["Depot"]},
        {"k": "page", "key": "c3", "recv": (w + 1.72) * 1000, "start": (w + 3.72) * 1000, "dec": 0, "wait": 2000,
         "how": "clip"},
        {"k": "say", "w": w + 2, "how": "rendered", "prep": 1410.0, "post": 4.0, "text": "Bunker now"},
        {"k": "call", "id": 6, "kind": "coach", "src": "coach", "w": w + 3.42, "t": "1:03", "text": "Bunker now"},
        {"k": "page", "key": "s6", "recv": (w + 3.43) * 1000, "start": (w + 3.53) * 1000, "dec": 100, "wait": 0,
         "how": "clip"},
        {"k": "brain", "w": w + 4, "poll": 2.0, "decide": 0.3, "post": 5.0, "kind": "depot", "text": "Depot now"},
    ]
    return recs


def close(a, b):
    return a is not None and abs(a - b) < 1e-6 * max(1, abs(b))


def test_analyse():
    res = looptime.analyse(trace_fixture(), tick=0.5)
    assert res["ticks"] == 4 and res["overruns"] == 1
    assert res["stages"]["api"]["max"] == 650.0 and res["stages"]["api"]["p50"] == 2.0
    assert res["stages"]["db"]["n"] == 1
    assert res["interval"]["n"] == 3 and res["jitter"]["max"] == 200.0 and res["jitter"]["p50"] == 20.0
    c = next(x for x in res["calls"] if x["key"] == "s5")
    assert close(c["confirm"], 1000) and close(c["decide"], 10) and c["sse"] == 3.0
    assert close(c["to_page"], 10) and close(c["audio"], 100) and c["dec"] == 60
    assert close(c["e2e_text"], 1020) and close(c["e2e_audio"], 1120)
    cue = res["cues"][0]
    assert close(cue["late"], 500) and close(cue["e2e_text"], 20) and close(cue["e2e_audio"], 2020)
    say = next(s for s in res["says"] if s["k"] == "say")
    assert say["call"]["key"] == "s6" and close(say["e2e_text"], 1430) and close(say["e2e_audio"], 1530)
    assert res["slow"][0]["work"] == 700.0
    text = looptime.report(res, slow=2, tick=0.5)
    for want in ("== helper tick (4 ticks", "overruns (work > TICK): 1", "E2E -> audio start", "engine cues (1)",
                 "rendered: before POST", "== brain_run.py (1)", "== slowest 2 ticks", "api=650"):
        assert want in text, want


def test_main(capsys=None):
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "looptrace-x.log"
        f.write_text("\n".join(json.dumps(r) for r in trace_fixture()) + "\nbroken line\n", encoding="utf-8")
        assert looptime.main([str(f), "--slow", "1"]) == 0
        assert looptime.analyse([])["ticks"] == 0
        print(looptime.report(looptime.analyse([])))   # empty trace: no crash


def test_helper_hooks():
    """timing.State with a live tracer: add_say/add_cues mark versions; an /events stream reports SSE lag;
    /notes?trace= stores the page's report and still answers the notes."""
    with tempfile.TemporaryDirectory() as d:
        tr = looptime.Tracer(Path(d) / "h.log", buffered=False)
        old = timing.TRACE
        timing.TRACE = tr
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), timing.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("GET", "/events")
            resp = conn.getresponse()
            resp.fp.readline()                  # first frame: the backlog
            resp.fp.readline()
            timing.STATE.add_say("Depot now", "blocked", 50.0)
            timing.STATE.add_cues([{"type": "target", "keys": ["Depot"], "sec": 60}], 0.0, 0)
            for _ in range(4):                  # up to two more frames (the two changes may coalesce)
                line = resp.fp.readline()
                if b"cue_seq" in line and b'"Depot"' in line and b"Depot now" in line:
                    break
            conn.close()
            c2 = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            rep = json.dumps([{"key": "s1", "recv": 1, "start": 2, "how": "tts"}])
            from urllib.parse import quote
            c2.request("GET", "/notes?trace=" + quote(rep))
            r2 = c2.getresponse()
            assert r2.status == 200 and json.loads(r2.read()) == []
            c2.close()
            time.sleep(0.2)
        finally:
            timing.TRACE = old
            srv.shutdown()
            srv.server_close()
        recs = looptime.parse((Path(d) / "h.log").read_text(encoding="utf-8").splitlines())
    kinds = {r["k"] for r in recs}
    assert {"call", "cue", "sse", "page"} <= kinds, kinds
    call = next(r for r in recs if r["k"] == "call")
    assert call["src"] == "helper" and "seen" not in call   # not from the tick thread: no detection times
    assert all(r["lag"] >= 0 for r in recs if r["k"] == "sse")


if __name__ == "__main__":
    test_pct()
    test_parse()
    test_tracer_off()
    test_tracer_env()
    test_tick_and_call()
    test_analyse()
    test_main()
    test_helper_hooks()
    print("test_looptime: ok")
