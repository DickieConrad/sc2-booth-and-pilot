"""Boundary model tests (live/models.py): good payloads pass, bad ones are rejected, the extra-fields policy holds,
and the callers fall back / answer 400 instead of crashing. Run by ci.sh via tools/checks.py tests (needs pydantic:
bash tools/dev-setup.sh). Fake servers on ephemeral localhost ports only (never :8765-8767)."""
import http.client
import http.server
import json
import sys
import threading
import warnings
from pathlib import Path
from typing import get_args

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain  # noqa: E402
import brain_run  # noqa: E402
import history  # noqa: E402
import models  # noqa: E402
import rules_client  # noqa: E402
import timing  # noqa: E402
from pydantic import ValidationError  # noqa: E402

PANEL = {"minerals": 455, "gas": 40, "used": 19, "cap": 23, "raw": "455 | 40 | 19/23"}
CALL = {"kind": "depot", "text": "Depot now, 19 of 23", "clip_text": "Depot now.", "confidence": 1.0}


def rejects(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except ValidationError as e:
        return e
    raise AssertionError(f"accepted: {args!r}")


# ---------- panel reader ----------

def test_panel_read():
    j = json.dumps(PANEL).encode()
    assert models.PANEL_READ.validate_json(j, strict=True) == PANEL
    assert models.PANEL_READ.validate_json(b"null", strict=True) is None   # unreadable crop
    bad = dict(PANEL, minerals="455")
    rejects(models.PANEL_READ.validate_json, json.dumps(bad), strict=True)            # strict: no "455" for 455
    rejects(models.PANEL_READ.validate_json, json.dumps(dict(PANEL, gas=-1)), strict=True)
    rejects(models.PANEL_READ.validate_json, json.dumps(dict(PANEL, used=True)), strict=True)
    e = rejects(models.PANEL_READ.validate_json, json.dumps({"minerals": 1}), strict=True)
    assert "gas" in models.errors(e)
    # extra fields from a newer reader are ignored (dropped), not an error
    assert models.PANEL_READ.validate_json(json.dumps(dict(PANEL, conf=0.9)), strict=True) == PANEL


# ---------- coach-core ----------

def test_rules_tick_response():
    good = {"game": "g1", "fresh": False, "helper": {"kind": "blocked", "text": "Supply blocked 19/19: depot now"},
            "brain": CALL}
    assert models.RULES_TICK.validate_json(json.dumps(good), strict=True) == good
    none = {"game": "g1", "fresh": True, "helper": None, "brain": None}
    assert models.RULES_TICK.validate_json(json.dumps(none), strict=True) == none
    rejects(models.RULES_TICK.validate_json, json.dumps(dict(none, helper={"kind": "nag", "text": "x"})), strict=True)
    rejects(models.RULES_TICK.validate_json, json.dumps(dict(none, brain=dict(CALL, kind="dance"))), strict=True)
    rejects(models.RULES_TICK.validate_json, json.dumps(dict(none, brain=dict(CALL, confidence=2))), strict=True)
    rejects(models.RULES_TICK.validate_json, json.dumps({"game": "g1", "fresh": True}), strict=True)
    rejects(models.RULES_TICK.validate_json, b"{not json", strict=True)
    assert "ns" not in models.RULES_TICK.validate_json(json.dumps(dict(none, ns=1400)), strict=True)


def test_cue_responses():
    tick = {"events": [{"type": "target", "keys": ["SCVs nonstop"], "sec": 0}], "lateness": 0.0, "shift": 0}
    assert models.CUE_TICK.validate_json(json.dumps(tick), strict=True) == tick
    rejects(models.CUE_TICK.validate_json, json.dumps(dict(tick, shift=0.5)), strict=True)
    rejects(models.CUE_TICK.validate_json, json.dumps(dict(tick, events=[{"type": "soon", "keys": [], "sec": 1}])),
            strict=True)
    assert models.CUE_START.validate_json(b'{"ok": true, "steps": 2}', strict=True) == {"ok": True, "steps": 2}
    rejects(models.CUE_START.validate_json, b'{"error": "unknown plan"}', strict=True)


def test_kinds_match_the_code():
    assert set(brain.KINDS) == set(get_args(models.BrainKind))
    call = brain.RulesBrain().decide([{"t": t, "minerals": m, "gas": 40, "used": u, "cap": 23}
                                      for t, m, u in ((70, 200, 17), (85, 250, 18), (100, 280, 19))], "T")
    assert models.TypeAdapter(models.BrainCall).validate_python(call, strict=True) == call
    helper_kinds = set(get_args(models.HelperKind))
    steps = [{"sec": 0, "t": "0:00", "action": "SCV", "supply": 12}, {"sec": 200, "t": "3:20", "action": "Command "
              "Center", "supply": 40}]
    seen = set()
    for panel, t in (({"minerals": 50, "gas": 0, "used": 19, "cap": 19}, 60), (dict(PANEL, minerals=900), 100),
                     ({"minerals": 50, "gas": 0, "used": 20, "cap": 60}, 230)):
        for race in ("T", "P"):
            verdict = timing.judge(panel, t, steps, race)
            seen.add(verdict and verdict[0])
    assert seen == helper_kinds, seen   # every kind judge() says is one the model (and coach-core) knows


# ---------- SC2 client API ----------

def test_game_info():
    g = {"isReplay": False, "displayTime": 61, "players": [
        {"id": 1, "name": "TestPilot", "type": "user", "race": "Terr", "result": "Undecided"}]}
    out = models.GAME.validate_json(json.dumps(g))
    assert out["displayTime"] == 61.0 and isinstance(out["displayTime"], float)
    assert out["players"][0] == {"name": "TestPilot", "race": "Terr", "result": "Undecided"}   # id/type dropped
    assert models.GAME.validate_json(b'{"displayTime": 0.0, "players": []}')["players"] == []   # in the menus
    rejects(models.GAME.validate_json, b'{"players": []}')
    rejects(models.GAME.validate_json, json.dumps(dict(g, players=[{"name": "x"}])))


# ---------- the helper's /state ----------

def full_state():
    st = json.loads(timing.STATE.json())
    st.update(in_game=True, clock=61.5, panel={"minerals": 455, "gas": 40, "used": 19, "cap": 23},
              plan_supply=20, next=[{"sec": 70, "t": "1:10", "action": "Depot", "supply": 19, "note": ""}],
              says=[{"id": 1, "t": "1:01", "text": "Depot now", "kind": "blocked", "from": "helper", "wall": 1.0}],
              cues=[{"type": "target", "keys": ["Depot"], "sec": 60, "id": 1}])
    return st


def test_state():
    st = full_state()
    out = models.STATE.validate_json(json.dumps(st))
    assert out["panel"] == st["panel"] and out["says"] == st["says"] and out["cues"] == st["cues"]
    assert models.STATE.validate_json(b'{"in_game": false, "clock": 0}') == {"in_game": False, "clock": 0.0}
    rejects(models.STATE.validate_json, json.dumps(dict(st, panel={"minerals": "lots"})))
    rejects(models.STATE.validate_json, json.dumps(dict(st, says=[{"text": "no id"}])))
    rejects(models.STATE.validate_json, json.dumps({"clock": 1}))


class StateServer:
    """GET /state answers `body` (bytes)."""

    def __init__(self, body):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        assert self.port not in (8765, 8766, 8767)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_brain_run_get_state():
    good, bad = StateServer(json.dumps(full_state()).encode()), StateServer(b'{"in_game": "maybe", "clock": 1}')
    try:
        assert brain_run.get_state(good.port)["panel"]["used"] == 19
        rejects(brain_run.get_state, bad.port)   # the poll loop logs it like an unreachable helper
    finally:
        good.close()
        bad.close()


# ---------- rules client: a bad coach-core answer = Python rules ----------

def test_rules_client_rejects_bad_answer():
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            out = b'{"game": "x", "fresh": false, "helper": {"kind": "nag", "text": "?"}, "brain": null}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logs = []
    try:
        c = rules_client.RulesClient("helper", ("127.0.0.1", srv.server_address[1]), on=True, log=logs.append)
        assert c.helper(60, PANEL, "A", "T") is None and c.up is False and c.retry > 0
    finally:
        srv.shutdown()
        srv.server_close()
    assert logs == ["rules (helper): python (coach-core unavailable: ValidationError)"]


# ---------- POST /say and /note bodies ----------

def test_say_body():
    b = models.SayBody.model_validate_json(b'{"text": "  Bunker now  ", "audio": "coach-bunker-1a2b3c4d.wav"}')
    assert (b.text, b.audio, b.kind) == ("Bunker now", "coach-bunker-1a2b3c4d.wav", "coach")
    assert models.SayBody.model_validate_json(json.dumps({"text": "x" * 300})).text == "x" * 200
    assert models.SayBody.model_validate_json(b'{"text": "x", "audio": ""}').audio is None
    assert models.SayBody.model_validate_json(b'{"text": 42}').text == "42"   # as str(body["text"]) did
    rejects(models.SayBody.model_validate_json, b'{"text": "x", "audio": "../coach.db"}')
    rejects(models.SayBody.model_validate_json, b'{"text": "x", "audio": "manifest.json"}')   # not a .wav
    rejects(models.SayBody.model_validate_json, b'{"audio": "a.wav"}')
    e = rejects(models.SayBody.model_validate_json, b'{"text": "x", "audo": "a.wav"}')   # typo: forbidden extra
    assert "audo" in models.errors(e)
    n = models.NoteBody.model_validate_json(b'{"text": " Review: depots late ", "game_id": 12}')
    assert (n.text, n.game_id) == ("Review: depots late", 12)
    assert models.NoteBody.model_validate_json(b'{"text": "x"}').game_id is None
    rejects(models.NoteBody.model_validate_json, b'{"text": "x", "game_id": "latest"}')
    rejects(models.NoteBody.model_validate_json, b'{"text": "x", "game": 3}')


def post(port, path, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", path, body, {"Content-Type": "application/json"})
    r = c.getresponse()
    out = r.status, json.loads(r.read())
    c.close()
    return out


def test_helper_routes():
    class Rec:
        def __init__(self):
            self.notes, self.calls = [], []

        def note(self, text, game_id=None):
            self.notes.append((text, game_id))
            return len(self.notes)

        def call(self, *a):
            self.calls.append(a)

    old = timing.REC
    timing.REC = rec = Rec()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), timing.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        assert post(port, "/say", b'{"text": "Wall off", "kind": "brain-depot"}') == (200, {"ok": True})
        last = timing.STATE.data["says"][-1]
        assert (last["text"], last["kind"], last["from"]) == ("Wall off", "brain-depot", "coach")
        code, body = post(port, "/say", b'{"text": "x", "audio": "x.mp3"}')
        assert code == 400 and body["error"] == "bad audio name"
        code, body = post(port, "/say", b'{"txt": "x"}')
        assert code == 400 and body["error"] == 'send {"text": "..."}' and "txt" in body["detail"]
        assert post(port, "/say", b"not json")[0] == 400
        assert post(port, "/note", b'{"text": " Note ", "game_id": 3}') == (200, {"ok": True, "note_id": 1})
        code, body = post(port, "/note", b'{"text": "x", "extra": 1}')
        assert code == 400 and "extra" in body["error"]
        assert rec.notes == [("Note", 3)]
        assert post(port, "/nope", b"{}")[0] == 404
    finally:
        timing.REC = old
        srv.shutdown()
        srv.server_close()


# ---------- post-game review JSON ----------

def test_review_shape():
    samples = [{"t": t, "minerals": 650 if 300 <= t <= 400 else 150, "gas": 100, "used": min(190, 12 + t // 8),
                "cap": min(200, 22 + t // 8) if not 420 <= t <= 440 else 12 + t // 8,
                "plan_supply": 14 + t // 8} for t in range(5, 900, 5)]
    game = {"id": 7, "started_at": 1793400000.0, "result": "Defeat", "length_sec": 900.0, "plan": "A",
            "coach_plan": "A", "enemies": "Foe (Terr)", "matchup": "T"}
    r = history.review(game, samples, [{"kind": "bank"}], [{"text": "Review: spend"}])
    assert models.REVIEW.validate_python(r, strict=True) == r   # the code produces exactly the declared shape
    with warnings.catch_warnings():
        warnings.simplefilter("error")   # a value that doesn't fit the schema would warn while serializing
        out = json.loads(models.REVIEW.dump_json(r))
    assert out == json.loads(json.dumps(r))   # same content as the old json.dumps
    rejects(models.REVIEW.validate_python, dict(r, top_fix=None), strict=True)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  test_models: {len(tests)} tests ok")
