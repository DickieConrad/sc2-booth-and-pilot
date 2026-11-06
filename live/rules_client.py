"""Client for the Rust call rules in coach-core (:8767, POST /rules/tick). OFF unless SC2_RULES=rust.

The helper (timing.py) and the booth brain (brain_run.py) each ask coach-core for their call when the flag is on,
with their own game key. A tick returns the response ({game, fresh, helper, brain}, validated by
models.RULES_TICK); None means "use the Python rules": the flag is off, or coach-core is down or answered something
that doesn't validate (then it is retried after 30 s, like the panel reader and cue engine).
Calls come back with the same kind and text as the Python rules (tools/rules_parity.py checks that on coach.db).
"""
import http.client
import json
import os
import time
from collections.abc import Callable

from models import RULES_TICK, BrainIn, HelperIn, Panel, RulesTickRequest, RulesTickResponse

ADDR = ("127.0.0.1", 8767)
RETRY = 30.0   # seconds before trying coach-core again after a failure
# a reused keep-alive connection the server already closed: retried once on a fresh connection (RemoteDisconnected
# is a ConnectionResetError)
STALE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class OneSendConnection(http.client.HTTPConnection):
    """Headers + body in ONE send(). http.client sends them separately, and over the Windows -> WSL localhost
    relay the small second write then waits ~40 ms (Nagle + delayed ACK): 43 ms -> 0.26 ms per POST, measured."""
    _buffer: list[bytes]   # http.client's pending header lines (private, untyped in typeshed)

    def _send_output(self, message_body: object = None, encode_chunked: bool = False) -> None:
        if isinstance(message_body, str):
            message_body = message_body.encode("utf-8")
        if isinstance(message_body, bytes):
            self._buffer.extend((b"", b""))
            msg = b"\r\n".join(self._buffer) + message_body
            del self._buffer[:]
            self.send(msg)
        else:
            super()._send_output(message_body, encode_chunked)  # type: ignore[misc]


def enabled() -> bool:
    return os.environ.get("SC2_RULES", "").strip().lower() == "rust"


def panel_body(panel: Panel | None) -> Panel | None:
    """The sample coach-core takes: the four numbers (a reader's `raw` text stays here)."""
    if not panel:
        return None
    return {"minerals": int(panel["minerals"]), "gas": int(panel["gas"]), "used": int(panel["used"]),
            "cap": int(panel["cap"])}


class RulesClient:
    def __init__(self, who: str, addr: tuple[str, int] = ADDR, on: bool | None = None,
                 log: Callable[[str], None] | None = None, timeout: float = 0.3) -> None:
        self.who, self.addr, self.timeout = who, addr, timeout
        self.on = enabled() if on is None else on
        self.log: Callable[[str], None] = log or (lambda line: print(line, flush=True))
        self.conn: OneSendConnection | None = None
        self.retry = 0.0
        self.up: bool | None = None
        self.game: str | None = None
        self.fresh = True

    def new_game(self) -> None:
        """A fresh rules state on coach-core for the next tick (new key + new_game)."""
        self.game, self.fresh = f"{self.who}-{time.time_ns() // 1_000_000}", True

    def _post(self, body: RulesTickRequest) -> RulesTickResponse:
        payload = json.dumps(body)
        if self.conn is not None:
            try:
                return self._send(self.conn, payload)
            except STALE:
                # the kept-alive connection went stale (coach-core restarted between games, relay timeout): the
                # server dropped it before reading the request, so one retry on a fresh connection is safe
                self.conn.close()
                self.conn = None
        self.conn = OneSendConnection(self.addr[0], self.addr[1], timeout=self.timeout)
        return self._send(self.conn, payload)

    @staticmethod
    def _send(conn: OneSendConnection, payload: str) -> RulesTickResponse:
        conn.request("POST", "/rules/tick", payload, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {data[:80]!r}")
        return RULES_TICK.validate_json(data, strict=True)   # ValidationError -> fallback, like a dead service

    def tick(self, t: float, panel: Panel | None, helper: HelperIn | None = None,
             brain: BrainIn | None = None) -> RulesTickResponse | None:
        if not self.on or time.time() < self.retry:
            return None
        if self.game is None:
            self.new_game()
        assert self.game is not None
        body: RulesTickRequest = {"game": self.game, "t": t, "sample": panel_body(panel)}
        if helper is not None:
            body["helper"] = helper
        if brain is not None:
            body["brain"] = brain
        if self.fresh:
            body["new_game"] = True
        try:
            out = self._post(body)
        except Exception as e:
            self.conn, self.retry = None, time.time() + RETRY
            if self.up is not False:
                self.log(f"rules ({self.who}): python (coach-core unavailable: {type(e).__name__})")
            self.up = False
            return None
        self.fresh = False
        if self.up is not True:
            self.log(f"rules ({self.who}): rust coach-core")
        self.up = True
        return out

    def helper(self, t: float, panel: Panel | None, plan: str, race: str) -> RulesTickResponse | None:
        """The helper's tick (panel None = unread / rejected). -> response or None (use judge())."""
        return self.tick(t, panel, helper={"plan": plan, "race": race, "wall": time.time()})

    def brain(self, t: float, panel: Panel | None, matchup: str,
              external: list[str | None], race: str = "T") -> RulesTickResponse | None:
        """The brain runner's poll; external = kinds others said since the last poll (None for the coach);
        race = the followed player's ("T" | "P" | "Z")."""
        return self.tick(t, panel, brain={"matchup": matchup, "external": external, "race": race})
