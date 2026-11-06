"""Booth brain tests (stdlib; run by ci.sh via tools/checks.py tests). Hand-made histories, no DB, no network
except a fake /state server on an ephemeral localhost port (never the helper's :8765)."""
import http.server
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain  # noqa: E402
import brain_run  # noqa: E402


def hist(points, step=5):
    """points: [(t, minerals, gas, used, cap)] -> samples; a single tuple with t=None repeats to fill."""
    return [dict(zip(("t", "minerals", "gas", "used", "cap"), p)) for p in points]


def flat(t0, t1, m, g, used, cap, step=5):
    return hist([(t, m, g, used, cap) for t in range(t0, t1 + 1, step)])


def first(h, matchup="T"):
    c = brain.RulesBrain().decide(h, matchup)
    return c and c["kind"]


def test_describe():
    h = hist([(100, 300, 50, 18, 23), (110, 350, 60, 19, 23), (130, 420, 90, 20, 23)])
    f = brain.describe(h, "T")
    assert f["supply_delta_30s"] == 2 and f["cap_headroom"] == 3 and f["gas_delta_30s"] == 40
    assert f["phase"] == "stall" and f["matchup"] == "T" and not f["cc_started"]
    assert f["depot_room"] == 4 and f["bank_min"] == 400


def test_depot():
    h = hist([(70, 200, 10, 17, 23), (85, 250, 30, 18, 23), (100, 280, 40, 19, 23)])
    assert first(h) == "depot"
    c = brain.RulesBrain().decide(h, "T")
    assert c["text"] == "Depot now, 19 of 23" and not any(ch.isdigit() for ch in c["clip_text"])
    # far from the cap: no depot
    assert first(hist([(70, 100, 10, 14, 23), (100, 120, 40, 15, 23)])) is None


def test_cc_then_not_after_spend():
    h = hist([(80, 300, 30, 19, 23), (85, 330, 40, 19, 23), (90, 370, 50, 19, 23)])
    assert first(h) == "cc"
    spent = h + hist([(95, 20, 60, 19, 23), (100, 380, 70, 19, 23)])   # 350 -> 20: the CC went down
    assert brain.describe(spent)["cc_started"] and first(spent) != "cc"


def test_stuck_and_fight():
    assert first(flat(150, 200, 200, 100, 28, 46)) == "stuck"
    fight = hist([(170, 200, 100, 40, 62), (180, 200, 120, 34, 62), (200, 200, 150, 34, 62)])
    assert first(fight) is None   # supply fell 6: a fight, not a stall


# Bot game 39 (SixRax vs Harder Zerg), 3:49-4:47: 48 -> 33 in one fight at ~4:17, rebuilt to 42 by 4:47, where the
# old rule called "Supply stuck at 42" (supply 30 s before was mid-fight, 44). The DB keeps one read per 5 s; the
# 4:16.5 / 4:17.5 / 4:47 reads are filled in at the live 0.5 s cadence.
TRADE = [(229, 40, 0, 40, 55), (234, 15, 0, 42, 55), (239, 40, 0, 43, 55), (244, 6, 0, 45, 55), (249, 27, 0, 46, 55),
         (253, 0, 0, 48, 63), (256.5, 5, 0, 44, 63), (257.5, 10, 0, 38, 63), (258, 21, 0, 33, 63),
         (263, 45, 0, 34, 63), (268, 18, 0, 36, 63), (273, 45, 0, 37, 63), (278, 29, 0, 39, 63),
         (283, 8, 0, 41, 63), (287, 35, 0, 42, 63)]


def test_stuck_holds_after_a_lost_fight():
    h = hist(TRADE)
    f = brain.describe(h, "T")
    assert dict(brain.RULES)["stuck"](f)                               # the stuck rule itself holds ...
    assert not f["losing_fight"] and f["fight_30s"]                    # ... but a fight was lost in the window
    b = brain.RulesBrain()
    assert "stuck" not in b.available(f["clock_s"], f)
    assert b.decide(h, "T") is None
    # the hold is only for "stuck": supply / lost-building calls in the same window still go
    assert {"depot", "depots", "lost_depot", "lost_cc"} <= set(b.available(f["clock_s"], f))
    # same reads without the fight (supply 42 throughout): a real stall, called
    calm = hist([(t, m, g, 42, c) for t, m, g, _, c in TRADE])
    assert not brain.describe(calm)["fight_30s"] and first(calm) == "stuck"
    # 30 s after the last read inside the fight (4:33), a flat 42 is stuck again
    later = h + flat(292, 320, 50, 0, 42, 63)
    assert not brain.describe(later)["fight_30s"] and first(later, "Z") == "stuck"
    # while still losing the fight, stuck is held too
    assert brain.describe(h[:9])["losing_fight"] and "stuck" not in b.available(258, brain.describe(h[:9]))


def test_bank_and_big_bank():
    h = hist([(230, 450, 100, 30, 46), (240, 480, 110, 31, 46), (250, 520, 120, 32, 46)])
    assert first(h) == "bank"
    big = hist([(400, 1100, 100, 60, 78), (410, 1200, 110, 61, 78), (420, 1300, 120, 62, 78)])
    assert first(big) == "bank_big"


def test_lost_depot_beats_gap_and_cc_loss():
    b = brain.RulesBrain()
    assert b.decide(hist([(390, 800, 100, 51, 78), (395, 900, 100, 51, 78)]), "T")["kind"] == "bank"
    lost = b.decide(hist([(390, 800, 100, 51, 78), (395, 900, 100, 51, 78), (400, 950, 100, 51, 70)]), "T")
    assert lost and lost["kind"] == "lost_depot"   # urgent: skips the 20 s global gap
    cc = hist([(250, 600, 280, 34, 46), (260, 620, 300, 31, 46), (270, 840, 360, 26, 31)])
    assert first(cc, "Z") == "lost_cc"


def test_gas():
    h = hist([(330, 200, 330, 50, 62), (345, 210, 360, 51, 70), (360, 220, 380, 52, 70)])
    assert first(h) == "gas"
    early = hist([(250, 200, 330, 30, 46), (265, 210, 360, 31, 46), (280, 220, 380, 32, 46)])
    assert first(early) == "gas_early"


def test_timed_once_and_matchup():
    b = brain.RulesBrain()
    h = flat(280, 305, 100, 100, 40, 62)
    h = [dict(s, used=s["used"] + i) for i, s in enumerate(h)]   # growing, far from cap
    assert b.decide(h, "T")["kind"] == "turret"
    again = b.decide(h + [dict(h[-1], t=330, used=h[-1]["used"] + 1)], "T")   # still in the window, gap open
    assert again is None or again["kind"] != "turret"
    assert "turret" in b.fired_once and "turret" not in b.available(400)
    assert first(h, "Z") != "turret"   # TvZ turret comes at 6:00


def test_unknown_matchup_counts_as_terran():
    # timing.py publishes key "?" when the opponents are unknown (bot test game)
    assert [brain.known_matchup(m) for m in ("", "?", "T?", "TZ")] == ["T", "T", "T?", "TZ"]
    h = flat(280, 305, 100, 100, 40, 62)
    h = [dict(s, used=s["used"] + i) for i, s in enumerate(h)]
    assert first(h, "?") == "turret"
    assert first(flat(150, 160, 100, 100, 30, 46), "?") == "stall"


def test_cooldowns():
    b = brain.RulesBrain()
    h = hist([(230, 450, 100, 30, 46), (240, 480, 110, 31, 46), (250, 520, 120, 32, 46)])
    assert b.decide(h, "T")["kind"] == "bank"
    assert b.decide(h + hist([(260, 560, 130, 33, 46)]), "T") is None   # global gap
    assert b.decide(h + hist([(275, 600, 140, 34, 46)]), "T") is None   # same pile: no repeat
    assert b.decide(h + hist([(285, 600, 140, 34, 46)]), "T") is None   # still not 1.5x, not 90 s
    assert b.decide(h + hist([(340, 600, 140, 35, 46)]), "T")["kind"] == "bank"  # REPEAT_GAP (90 s) passed
    assert set(brain.KINDS) >= {k for k, _ in brain.RULES} | set(brain.TIMED)
    assert set(brain.CRITERIA) == set(brain.KINDS) | {"none"}
    assert all(not any(ch.isdigit() for ch in p) for p in brain.all_clip_texts())


BANK = hist([(230, 450, 100, 30, 46), (240, 480, 110, 31, 46), (250, 520, 120, 32, 46)])   # bank call at 4:10


def test_bank_escalates_only_when_worse():
    b = brain.RulesBrain()
    assert b.decide(BANK, "T")["kind"] == "bank"
    assert b.last_spend["bank"] == (250, 520)
    assert b.decide(BANK + hist([(275, 800, 130, 33, 46)]), "T") is None      # 1.5x, but under ESCALATE_GAP
    assert b.decide(BANK + hist([(285, 770, 130, 33, 46)]), "T") is None      # 770 < 1.5 x 520
    c = b.decide(BANK + hist([(285, 790, 130, 33, 46)]), "T")                 # 790 >= 780: it got worse
    assert c["kind"] == "bank" and b.last_spend["bank"] == (285, 790)
    # bank and bank_big are one family: a big bank right after a bank call waits unless it is 1.5x worse
    b2 = brain.RulesBrain()
    b2.decide(hist([(390, 700, 100, 51, 78), (400, 720, 100, 52, 78)]), "T")
    assert b2.decide(hist([(390, 700, 100, 51, 78), (435, 1050, 100, 53, 78)]), "T") is None   # 1050 < 1080
    big = b2.decide(hist([(390, 700, 100, 51, 78), (435, 1100, 100, 53, 78)]), "T")
    assert big["kind"] == "bank_big"


def test_gas_family_shares_cooldown():
    b = brain.RulesBrain()
    early = hist([(250, 200, 330, 30, 46), (265, 210, 360, 31, 46), (280, 220, 380, 32, 46)])
    assert b.decide(early, "Z")["kind"] == "gas_early"
    later = early + hist([(305, 220, 420, 33, 46), (330, 220, 480, 34, 46)])   # gas rule now, but same family
    assert b.decide(later, "Z") is None
    worse = early + hist([(305, 220, 420, 33, 46), (330, 220, 600, 34, 46)])   # 600 >= 1.5 x 380
    assert b.decide(worse, "Z")["kind"] == "gas"


def test_wording_rotates_with_matching_text():
    for kind in ("bank", "bank_big", "gas", "gas_early"):
        clips, tpl = brain.KINDS[kind][1], brain.texts(kind)
        assert 2 <= len(clips) <= 4 and len(tpl) == len(clips), kind
    for kind, (_, clips, _) in brain.KINDS.items():
        assert len(brain.texts(kind)) in (1, len(clips)), kind
    b = brain.RulesBrain()
    calls = []
    for i in range(4):   # each call 90 s after the last one, same pile
        t = 250 + 90 * i
        calls.append(b.decide(hist([(t - 20, 520, 100, 30 + i, 62), (t, 520, 100, 31 + i, 62)]), "T"))
    assert [c["kind"] for c in calls] == ["bank"] * 4
    clips = [c["clip_text"] for c in calls]
    assert len(set(clips[:3])) == 3 and clips[3] == clips[0]   # 3 variants, then wrap
    assert [c["text"] for c in calls[:3]] == ["520 banked: 2 Barracks", "Spend 520: 2 more Barracks",
                                              "520 banked: Starport, Barracks, Marines"]
    # every clip phrasing is a pre-rendered line (render_brain.py renders all_clip_texts())
    assert set(clips) <= set(brain.all_clip_texts())


def test_spend_calls_hold_while_losing_a_fight():
    fight = hist([(390, 900, 500, 70, 94), (400, 950, 520, 66, 94), (410, 1000, 540, 58, 94)])
    f = brain.describe(fight, "T")
    assert f["losing_fight"]
    b = brain.RulesBrain()
    avail = b.available(f["clock_s"], f)
    assert not {"bank", "bank_big", "gas", "gas_early"} & set(avail)
    assert {"depot", "depots", "lost_depot", "lost_cc"} <= set(avail)   # supply calls still go
    assert b.decide(fight, "T") is None
    # a lost depot in the fight is still called
    lost = fight + hist([(415, 1000, 540, 57, 86)])
    assert b.decide(lost, "T")["kind"] == "lost_depot"
    # 20 s after the drop, the fight is over: the bank call comes
    after = lost + hist([(440, 1100, 560, 59, 94)])
    assert not brain.describe(after)["losing_fight"]
    assert b.decide(after, "T")["kind"] == "bank_big"
    # a slow 9-supply bleed over 20 s is not a lost fight
    assert not brain.describe(hist([(390, 900, 100, 70, 94), (410, 900, 100, 61, 94)]))["losing_fight"]


def test_helper_bank_call_sets_the_pile():
    b = brain.RulesBrain()
    b.note_external(250, "bank", {"minerals": 600, "gas": 100})
    assert b.decide(BANK + hist([(285, 650, 120, 33, 46)]), "T") is None
    assert b.decide(BANK + hist([(285, 900, 120, 33, 46)]), "T")["kind"] == "bank"
    b.reset()
    b.note_external(250, "bank")   # no reading: wait the full REPEAT_GAP
    assert b.decide(BANK + hist([(285, 2000, 120, 33, 46)]), "T") is None
    assert b.decide(BANK + hist([(340, 600, 120, 33, 46)]), "T")["kind"] == "bank"


def test_runner_against_fake_state_server():
    states = [{"in_game": True, "clock": t, "key": "T", "says": [],
               "panel": {"minerals": m, "gas": 40, "used": u, "cap": 23}}
              for t, m, u in ((70, 200, 17), (85, 250, 18), (100, 280, 19))]
    box = {"i": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(states[min(box["i"], len(states) - 1)]).encode()
            box["i"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    assert port != 8765
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    said, logged = [], []
    run = brain_run.Runner(said.append, logged.append)
    try:
        for _ in states:
            run.feed(brain_run.get_state(port))
    finally:
        srv.shutdown()
    assert [c["kind"] for c in said] == ["depot"], said
    assert logged == ["BRAIN [1:40] depot Depot now, 19 of 23"], logged
    run.feed({"in_game": False})
    assert run.hist == []


# ---------- race (brain.rs has the same cases) ----------

def call_as(h, matchup, race):
    return brain.RulesBrain().decide(h, matchup, race=race)


def growing(t0, t1, m, g, used, cap):
    return [dict(s, used=s["used"] + i) for i, s in enumerate(flat(t0, t1, m, g, used, cap))]


def test_supply_and_worker_words_follow_the_race():
    h = hist([(70, 200, 10, 17, 23), (85, 250, 30, 18, 23), (100, 280, 40, 19, 23)])

    def said(r):
        c = call_as(h, "T", r)
        return c["kind"], c["text"], c["clip_text"]
    assert said("P") == ("depot", "Pylon now, 19 of 23", "Pylon now.")
    assert said("Z") == ("depot", "Overlord now, 19 of 23", "Overlord now.")
    assert said("T") == said("") == said("random") == ("depot", "Depot now, 19 of 23", "Depot now.")
    stuck = flat(150, 200, 200, 100, 28, 46)
    assert call_as(stuck, "T", "P")["text"] == "Supply stuck at 28: queue Probes"
    assert call_as(stuck, "T", "Z")["text"] == "Supply stuck at 28: make Drones"
    b = brain.RulesBrain()
    h = hist([(390, 800, 100, 51, 78), (395, 900, 100, 51, 78)])
    assert b.decide(h, "T", race="P")["text"] == "900 banked: 2 Gateways"
    lost = b.decide(h + hist([(400, 950, 100, 51, 70)]), "T", race="P")
    assert (lost["kind"], lost["text"]) == ("lost_depot", "Lost a pylon (8 cap), check your main")
    gas = hist([(330, 200, 330, 50, 62), (345, 210, 360, 51, 70), (360, 220, 380, 52, 70)])
    c = call_as(gas, "T", "Z")
    assert (c["kind"], c["clip_text"]) == ("gas", "Gas is piling up. Gas units and upgrades.")


def test_terran_only_kinds_are_never_said_for_protoss_or_zerg():
    cc = hist([(80, 300, 30, 19, 23), (85, 330, 40, 19, 23), (90, 370, 50, 19, 23)])
    orbital = hist([(190, 300, 100, 30, 31), (200, 320, 110, 31, 46)])
    stall = flat(150, 160, 100, 100, 30, 46)
    turret = growing(280, 305, 100, 100, 40, 62)
    bunker = growing(165, 190, 100, 100, 20, 46)

    def kind(h, m, r):
        c = call_as(h, m, r)
        return c and c["kind"]
    cases = [(cc, "T", "cc"), (orbital, "T", "orbital"), (stall, "?", "stall"), (turret, "T", "turret"),
             (bunker, "Z", "bunker")]
    for h, m, k in cases:
        assert kind(h, m, "T") == k, k
        for r in ("P", "Z"):
            assert kind(h, m, r) != k, (r, k)
    lost = hist([(250, 600, 280, 34, 46), (260, 620, 300, 31, 46), (270, 840, 360, 26, 31)])
    c = call_as(lost, "Z", "P")
    assert (c["kind"], c["text"]) == ("lost_cc", "Lost a Nexus (15 cap): army home")
    assert kind(lost, "Z", "Z") != "lost_cc"   # a Hatchery is 6 supply
    avail = brain.RulesBrain().available(100, brain.describe(cc, "T", "Z"))
    assert not {"cc", "orbital", "lost_cc", "stall"} & set(avail)


def test_race_wording_has_no_terran_words_or_digits():
    terran = ("SCV", "Marine", "Barracks", "depot", "Depot", "Command", "CC", "Orbital", "Bunker", "Turret",
              "refinery", "Tank", "Medivac", "Stim")
    assert set(brain.RACE_KINDS["P"]) == set(brain.RACE_KINDS["Z"]) == set(brain.KINDS)
    for r in ("P", "Z"):
        for k in brain.KINDS:
            w = brain.wording(k, r)
            if w is None:
                continue
            clips, tpl = w
            assert len(tpl) in (1, len(clips)), (r, k)
            assert not [s for s in clips + tpl if any(word in s for word in terran)], (r, k)
            assert all(not any(ch.isdigit() for ch in c) and c in brain.all_clip_texts() for c in clips)
    for k in brain.KINDS:   # Terran wording is KINDS, unchanged
        assert brain.wording(k, "T") == (brain.KINDS[k][1], brain.texts(k))
    assert len(brain.all_clip_texts()) == 60   # same count as brain.rs


def test_runner_follows_the_state_race():
    said = []
    run = brain_run.Runner(said.append, lambda _: None)
    for t, m, u in ((70, 200, 17), (85, 250, 18), (100, 280, 19)):
        run.feed({"in_game": True, "clock": t, "key": "?", "race": "P", "says": [],
                  "panel": {"minerals": m, "gas": 40, "used": u, "cap": 23}})
    assert [c["text"] for c in said] == ["Pylon now, 19 of 23"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  test_brain: {len(tests)} tests ok")
