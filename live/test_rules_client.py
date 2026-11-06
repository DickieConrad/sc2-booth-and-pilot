"""SC2_RULES=rust client tests (stdlib; run by ci.sh via tools/checks.py tests). A fake coach-core on an ephemeral
localhost port (never :8767); a closed port stands in for coach-core being down."""
import http.server
import json
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_run  # noqa: E402
import rules_client  # noqa: E402


class Fake:
    """Answers POST /rules/tick with `reply(body)` over HTTP/1.1 keep-alive (like coach-core); records the bodies and
    the client ports (one per connection). drop=True: the server closes each connection after answering without
    saying so, like coach-core restarting or the relay timing out between ticks (the client's next send is stale)."""

    def __init__(self, reply, drop=False):
        self.bodies, self.peers, fake = [], [], self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.bodies.append((self.path, body))
                fake.peers.append(self.client_address[1])
                out = json.dumps(reply(body)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                if drop:
                    self.close_connection = True

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        assert self.port not in (8765, 8766, 8767)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_flag():
    import os
    old = os.environ.get("SC2_RULES")
    try:
        os.environ["SC2_RULES"] = "Rust "
        assert rules_client.enabled() and rules_client.RulesClient("x").on
        os.environ["SC2_RULES"] = "python"
        assert not rules_client.enabled()
        del os.environ["SC2_RULES"]
        c = rules_client.RulesClient("x")
        assert not c.on and c.helper(60, None, "A", "T") is None   # default OFF: never posts
    finally:
        if old is not None:
            os.environ["SC2_RULES"] = old


def test_helper_tick_body_and_new_game():
    fake = Fake(lambda b: {"game": b["game"], "fresh": False, "helper": None, "brain": None})
    logs = []
    try:
        c = rules_client.RulesClient("helper", ("127.0.0.1", fake.port), on=True, log=logs.append)
        c.new_game()
        panel = {"minerals": 455, "gas": 40, "used": 19, "cap": 23, "raw": "455 | 40 | 19/23"}
        assert c.helper(61.5, panel, "A", "T") is not None
        assert c.helper(62.0, None, "A", "T") is not None
        game = c.game
        c.new_game()
        c.helper(5.0, None, "A", "T")
    finally:
        fake.close()
    (p1, b1), (_, b2), (_, b3) = fake.bodies
    assert p1 == "/rules/tick"
    assert b1["new_game"] and b1["game"] == game and b1["t"] == 61.5
    assert b1["sample"] == {"minerals": 455, "gas": 40, "used": 19, "cap": 23}   # no "raw"
    assert b1["helper"]["plan"] == "A" and b1["helper"]["race"] == "T" and b1["helper"]["wall"] > 1e9
    assert "new_game" not in b2 and b2["sample"] is None and b2["game"] == game
    assert b3["new_game"] and b3["game"] != game
    assert logs == ["rules (helper): rust coach-core"]


def test_down_falls_back_and_waits():
    logs = []
    c = rules_client.RulesClient("helper", ("127.0.0.1", closed_port()), on=True, log=logs.append)
    assert c.helper(60, None, "A", "T") is None
    assert c.retry > 0 and c.up is False
    assert c.helper(61, None, "A", "T") is None   # inside the 30 s retry wait: no connection attempt
    assert logs == ["rules (helper): python (coach-core unavailable: ConnectionRefusedError)"]


def states():
    bank = {"id": 1, "wall": 1, "kind": "bank", "from": "helper"}
    coach = {"id": 2, "wall": 2, "kind": "coach", "from": "coach"}
    own = {"id": 3, "wall": 3, "kind": "brain-depot", "from": "coach"}   # the brain's own line, back from /state
    return [{"in_game": True, "clock": t, "key": "T", "says": says,
             "panel": {"minerals": m, "gas": 40, "used": u, "cap": 23}}
            for t, m, u, says in ((70, 200, 17, []), (85, 250, 18, [bank]), (100, 280, 19, [coach, own]))]


def test_runner_uses_rust_decision_and_sends_externals():
    call = {"kind": "depot", "text": "Depot now, 19 of 23", "clip_text": "Depot now.", "confidence": 1.0}
    fake = Fake(lambda b: {"game": b["game"], "fresh": False, "helper": None,
                           "brain": call if b["t"] == 100 else None})
    said = []
    try:
        rules = rules_client.RulesClient("brain", ("127.0.0.1", fake.port), on=True, log=lambda _: None)
        run = brain_run.Runner(said.append, lambda _: None, rules)
        for st in states():
            run.feed(st)
    finally:
        fake.close()
    assert said == [call]
    bodies = [b for _, b in fake.bodies]
    assert [b["brain"]["external"] for b in bodies] == [[], ["bank"], [None]]   # coach -> None; brain-* skipped
    assert all(b["brain"]["matchup"] == "T" for b in bodies) and bodies[2]["sample"]["used"] == 19
    assert all(b["brain"]["race"] == "T" for b in bodies)   # no race in /state (an older helper): Terran
    # the local brain noted the Rust call, so a fallback right now would hold the gap
    assert run.brain.last["depot"] == 100 and not run.brain.gap_open(110)


def test_runner_falls_back_to_python_rules():
    said = []
    rules = rules_client.RulesClient("brain", ("127.0.0.1", closed_port()), on=True, log=lambda _: None)
    run = brain_run.Runner(said.append, lambda _: None, rules)
    for st in states():
        run.feed(dict(st, says=[]))
    assert [c["kind"] for c in said] == ["depot"]


def test_runner_sends_the_followed_race():
    fake = Fake(lambda b: {"game": b["game"], "fresh": False, "helper": None, "brain": None})
    try:
        rules = rules_client.RulesClient("brain", ("127.0.0.1", fake.port), on=True, log=lambda _: None)
        run = brain_run.Runner(lambda _: None, lambda _: None, rules)
        for st in states():
            run.feed(dict(st, race="P"))
    finally:
        fake.close()
    assert [b["brain"]["race"] for _, b in fake.bodies] == ["P"] * 3


def test_one_send_puts_headers_and_body_in_one_write():
    sent = []

    class Sock:
        def sendall(self, data):
            sent.append(bytes(data))

    conn = rules_client.OneSendConnection("127.0.0.1", 9)
    conn.sock = Sock()
    conn.request("POST", "/tick", json.dumps({"t": 30}), {"Content-Type": "application/json"})
    assert len(sent) == 1
    head, body = sent[0].split(b"\r\n\r\n", 1)
    assert head.startswith(b"POST /tick HTTP/1.1") and b"Content-Length: 9" in head
    assert body == b'{"t": 30}'


def test_one_send_round_trip_against_a_real_server():
    fake = Fake(lambda b: {"game": b["game"], "fresh": True, "helper": None, "brain": None})
    try:
        c = rules_client.RulesClient("helper", ("127.0.0.1", fake.port), on=True, log=lambda line: None)
        c.new_game()
        assert c.helper(1.0, None, "A", "T") is not None
        assert c.helper(1.5, None, "A", "T") is not None   # keep-alive reuse after a one-write request
    finally:
        fake.close()
    assert len(fake.bodies) == 2
    assert fake.peers[0] == fake.peers[1], fake.peers   # really one connection, reused


def test_stale_keepalive_retries_once_on_a_fresh_connection():
    logs = []
    fake = Fake(lambda b: {"game": b["game"], "fresh": False, "helper": None, "brain": None}, drop=True)
    try:
        c = rules_client.RulesClient("helper", ("127.0.0.1", fake.port), on=True, log=logs.append)
        c.new_game()
        for t in (1.0, 1.5, 2.0):
            assert c.helper(t, None, "A", "T") is not None, (t, logs)   # never a 30 s drop to Python
    finally:
        fake.close()
    assert [b["t"] for _, b in fake.bodies] == [1.0, 1.5, 2.0]   # each tick reached coach-core exactly once
    assert len(set(fake.peers)) == 3 and logs == ["rules (helper): rust coach-core"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  test_rules_client: {len(tests)} tests ok")
