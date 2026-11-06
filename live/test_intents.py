"""Intent lane tests (stdlib; run by ci.sh via tools/checks.py tests). No network: the SDK is faked or hidden."""
import json
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import intents  # noqa: E402
from intents import decide  # noqa: E402

NO_PKGS = Path(tempfile.gettempdir()) / "no-such-pkgs-dir"


def test_decide_gates():
    t = "how do I beat thors"
    assert decide("counter_unit", 0.95, False, t).tag == "slow:no-game"
    assert decide(None, 0.0, True, t).tag == "slow:error"
    assert decide("counter_unit", 0.69, True, t).tag == "slow:low-conf"
    assert decide("counter_unit", 0.70, True, t).act == "say"          # threshold is inclusive
    assert decide("chatter", 0.99, True, "testing the mic").tag == "slow:chatter"
    assert decide("something_else", 0.99, True, t).tag == "slow:unknown"


def test_decide_talk_gate():
    t = "how do I beat thors"
    assert decide("counter_unit", 0.95, True, t, talk=0.1).tag == "slow:not-talk"
    assert decide("counter_unit", 0.95, True, t, talk=0.29).tag == "slow:not-talk"
    assert decide("counter_unit", 0.95, True, t, talk=0.3).tag == "slow:unsure-talk"
    assert decide("counter_unit", 0.95, True, t, talk=0.69).tag == "slow:unsure-talk"
    assert decide("counter_unit", 0.95, True, t, talk=0.7).act == "say"
    assert decide("counter_unit", 0.5, True, t, talk=0.99).tag == "slow:low-conf"   # both must pass


def test_decide_pause_resume_guard():
    a = decide("pause", 0.9, True, "pause the clock")
    assert a.act == "say" and a.line == "Paused."
    assert decide("pause", 0.9, True, "hold on a second").act == "say"
    assert decide("pause", 0.9, True, "stop calling stuff").act == "say"
    assert decide("pause", 0.9, True, "wait a moment").tag == "slow:no-keyword"
    assert decide("pause", 0.9, True, "unpause").tag == "slow:no-keyword"   # unpause is not pause
    a = decide("resume", 0.9, True, "okay unpause")
    assert a.act == "say" and a.line == "Back on."
    assert decide("resume", 0.9, True, "let's go again").act == "say"
    assert decide("resume", 0.9, True, "continue").act == "say"
    assert decide("resume", 0.9, True, "I'm back").tag == "slow:no-keyword"


def test_decide_counter_and_pass():
    a = decide("counter_unit", 0.9, True, "What beats Brood Lords?")
    assert a.act == "say" and a.tag == "say:BroodLord" and a.line.startswith("Brood Lords:")
    assert decide("counter_unit", 0.9, True, "how do I beat that army").tag == "slow:no-unit"
    for k in ("enemy_shows", "behind", "ask_build"):
        a = decide(k, 0.9, True, "they're going roaches")
        assert a.act == "pass" and a.line is None and a.tag == "pass"


def test_find_unit():
    u = lambda s: (c := intents.find_unit(s)) and c.unit
    assert u("how do I beat thors") == "Thor"
    assert u("what do I do against COLOSSI") == "Colossus"
    assert u("broodlords incoming") == "Brood Lord"
    assert u("brood-lords") == "Brood Lord"
    assert u("how to deal with DTs") == "DT"
    assert u("dark templar in my main") == "DT"
    assert u("siege tanks everywhere") == "Tank"
    assert u("mutas and banes, what do I make") == "Mutalisk"     # the first one named wins
    assert u("banes and mutas") == "Baneling"
    assert u("two BCs") == "Battlecruiser"
    assert u("roaches") == "Roach"
    assert u("libs on my mineral line") == "Liberator"
    assert u("mineral line") is None           # "mine" / "line" are not units
    assert u("thorough") is None and u("tankard") is None   # whole words only
    assert u("how do I beat that") is None


def test_counters_json_shape():
    raw = json.loads(intents.COUNTERS.read_text(encoding="utf-8"))
    units = raw["units"]
    want = {"Thor", "Tempest", "Carrier", "Colossus", "Immortal", "Disruptor", "Lurker", "Ultralisk", "Brood Lord",
            "Mutalisk", "Baneling", "Liberator", "Tank", "Battlecruiser", "Viper", "Infestor", "Roach", "Zealot",
            "Stalker", "Marine", "Hellion", "Banshee", "Oracle", "DT"}
    assert set(units) == want, set(units) ^ want
    seen = set()
    for unit, e in units.items():
        assert set(e) == {"keys", "line"}, unit
        assert e["keys"] and all(isinstance(k, str) and k == k.lower() and k.strip() for k in e["keys"]), unit
        assert not seen.intersection(e["keys"]), f"{unit}: key used twice"
        seen.update(e["keys"])
        line = e["line"]
        assert 10 <= len(line) <= 80 and line.endswith("."), (unit, line)   # short: it is spoken mid-game
        assert not any(ch.isdigit() for ch in line), line                    # clips are pre-rendered: no numbers
        assert intents.find_unit(e["keys"][0]).unit == unit, unit
    lines = intents.all_lines()
    assert "Paused." in lines and "Back on." in lines and len(lines) == len(set(lines))


def test_intents_match_eval():
    assert list(intents.INTENTS)[0] == "chatter"   # Jev leans to the first option: err toward the slow path
    assert set(intents.INTENTS) == {"pause", "resume", "counter_unit", "enemy_shows", "behind", "ask_build",
                                    "chatter"}
    assert intents.MODEL != "jev-latest"           # pinned so the eval reproduces


def test_log_line_and_tag():
    v = intents.Verdict("counter_unit", 0.912, 0.88)
    a = decide(v.intent, v.conf, True, "how do I beat thors", v.talk)
    assert intents.log_line("4:05", v, a, "how do I beat thors") == \
        "INTENT [4:05] counter_unit 0.91 say:Thor talk=0.88 | how do I beat thors"
    assert intents.log_line("4:05", None, decide(None, 0, True, "x"), "x") == "INTENT [4:05] none 0.00 slow:error | x"
    assert intents.note_tag("enemy_shows", 0.934) == " [intent: enemy_shows 0.93]"


def test_enabled():
    assert intents.enabled({})
    assert intents.enabled({"SC2_INTENTS": "1"})
    for v in ("0", "off", "false", "no", " 0 "):
        assert not intents.enabled({"SC2_INTENTS": v}), v


class _Hidden:
    """Hide typesafe_sdk (None in sys.modules -> ImportError) or put a fake in its place."""
    def __init__(self, fake=None):
        self.fake = fake

    def __enter__(self):
        self.old = sys.modules.get("typesafe_sdk", "absent")
        sys.modules["typesafe_sdk"] = self.fake

    def __exit__(self, *a):
        if self.old == "absent":
            del sys.modules["typesafe_sdk"]
        else:
            sys.modules["typesafe_sdk"] = self.old


def fake_sdk(answer=("counter_unit", 0.9, 0.95), delay=0.0, made=None):
    m = types.ModuleType("typesafe_sdk")

    class Q:
        def __init__(self, **kw):
            self.kw = kw

    class Client:
        def __init__(self, api_key, model, retry):
            assert api_key == "test-key-not-real" and model == intents.MODEL
            if made is not None:
                made.append(retry.kw)

        def system_one(self, state, questions, timeout=None):
            assert set(questions) == {"intent", "talk"} and state == intents.state(state["line"])
            time.sleep(delay)
            if answer is None:
                raise RuntimeError("boom")
            c, p, t = answer
            ans = {"intent": types.SimpleNamespace(choice=c, confidence=p), "talk": types.SimpleNamespace(noul=t)}
            return types.SimpleNamespace(answers=ans)

        def close(self):
            pass

    m.Choice = m.Noul = m.RetryPolicy = Q
    m.TypeSafeClient = Client
    return m


def test_lane_without_sdk_or_key():
    with tempfile.TemporaryDirectory() as d:
        key = Path(d) / ".env.typesafe"
        key.write_text("TYPESAFE_API_KEY=test-key-not-real\n")
        with _Hidden(None):
            lane = intents.IntentLane(key_path=key, pkgs=NO_PKGS)
            assert not lane.ready and "typesafe_sdk" in lane.why
            assert lane.classify("how do I beat thors") is None
        made = []
        with _Hidden(fake_sdk(made=made)):
            lane = intents.IntentLane(key_path=Path(d) / "missing", pkgs=NO_PKGS)
            assert not lane.ready and "TYPESAFE_API_KEY" in lane.why and made == []
            assert lane.classify("how do I beat thors") is None
            assert "test-key" not in lane.why


def test_lane_with_fake_sdk():
    with tempfile.TemporaryDirectory() as d:
        key = Path(d) / ".env.typesafe"
        key.write_text("TYPESAFE_API_KEY=test-key-not-real\n")
        made = []
        with _Hidden(fake_sdk(made=made)):
            lane = intents.IntentLane(key_path=key, pkgs=NO_PKGS)
            assert lane.ready and made == [{"max_retries": 0, "timeout": intents.TIMEOUT}]
            assert lane.classify("how do I beat thors") == ("counter_unit", 0.9, 0.95)
            assert lane.classify("   ") is None
            lane.close()
        with _Hidden(fake_sdk(answer=None)):                      # SDK error -> None, never raises
            lane = intents.IntentLane(key_path=key, pkgs=NO_PKGS)
            assert lane.classify("x y") is None and lane.errors == ["RuntimeError"]
        with _Hidden(fake_sdk(answer=("weird", 0.99, 0.99))):      # an answer outside INTENTS -> None
            assert intents.IntentLane(key_path=key, pkgs=NO_PKGS).classify("x y") is None
        with _Hidden(fake_sdk(delay=1.5)):                        # slow API: back within the timeout
            lane = intents.IntentLane(key_path=key, pkgs=NO_PKGS, timeout=0.2)
            t = time.perf_counter()
            assert lane.classify("how do I beat thors") is None
            assert time.perf_counter() - t < 0.6 and lane.errors == ["Timeout"]
            lane.close()
        with _Hidden(fake_sdk()):                                 # call cap
            lane = intents.IntentLane(key_path=key, pkgs=NO_PKGS, max_calls=1)
            assert lane.classify("a b") and lane.classify("a b") is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  test_intents: {len(tests)} tests ok")
