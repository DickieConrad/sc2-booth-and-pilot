"""Fast lane matcher tests (stdlib; run by ci.sh via tools/checks.py tests) on the real voice/fastlane.json.
Enemy counters fire on reports of the enemy, never on Dickie's questions or plans about his own play."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastlane import fast_match, load_fastlane, own_play  # noqa: E402

RULES, MAX_WORDS = load_fastlane()
LINE = {key: line for key, line in [
    ("drop", "Drop. Pull SCVs, send a squad home."),
    ("banshee", "Banshee. Turrets in the mineral lines, keep a scan ready."),
    ("reaper", "Reaper. Keep a Marine home, Bunker if it harasses."),
    ("roach", "Roaches. Bunker at the natural, Marauders."),
    ("stalker", "Blink Stalkers. Marauders, and watch the cliffs in your main."),
    ("immortal", "Immortals. Go Marine heavy, add Ghosts, skip Tanks."),
    ("ling", "Lings. Wall up, Bunker at the natural, Hellions out."),
    ("dt", "Dark Templar. Turrets in the mineral lines, save a scan."),
    ("proxy", "Proxy. Bunker the ramp, pull SCVs to hold."),
    ("gg", "Good game, Dickie. Breathe. Tell me the one thing that went wrong."),
    ("what now", "Macro check. SCVs, Depots, spend the bank, then scout."),
    ("blocked", "Supply blocked. Two Depots, now."),
    ("attack", "Under attack. Army home, then counter."),
]}

# Tonight's wrong replies (mic.log, 2026-10-30) and the other own-play shapes: no enemy counter may fire.
OWN_PLAY = [
    "Should I build some drop?",
    "Should I drop another command center?",
    "Should I drop him? I feel like dropping him.",
    "To research cloaking.",
    "I need to start port with a tech lab to get a banshee.",
    "Where am I going with the reaper?",
    "Should I send an SCV scout or wait for my reaper?",   # mic.log 0:49
    "How do I deal with carriers?",                        # mic.log 11:55: a question, the intent lane answers it
    "Do I need a banshee?",
    "Can I drop his main now?",
    "I'm going to make reapers this game.",
    "I'm gonna go banshee.",
    "I'll drop him with two medivacs.",
    "We should proxy next game.",
    "Let's go cloak banshee.",
    "My reaper is scouting his natural.",
    "My banshee died.",
    "Drop a MULE.",
    "Dropping a depot at the ramp.",
    "Make a drop with the marines.",
    "Research cloak now.",
    "What should I build after the Starport?",   # not a key either: no reply at all
]

# Real enemy callouts: (utterance, expected key). The first four are from mic.log; the rest are the shapes Dickie
# uses for enemy reports.
ENEMY = [
    ("roach warren.", "roach"),                                                   # mic.log
    ("He's got his stalkers there, but I think I could smoke him.", "stalker"),   # mic.log 10:27
    ("They got reapers and what are they, Immortals?", "immortal"),               # mic.log 6:57
    ("Sorry, I mean zerglings.", "ling"),                                         # mic.log 2:50
    ("He's got a drop.", "drop"),
    ("Drop incoming.", "drop"),
    ("Drop in my main!", "drop"),
    ("He's dropping my main.", "drop"),
    ("I'm getting dropped.", "drop"),
    ("Warp prism.", "drop"),
    ("Medivacs in my main.", "drop"),
    ("Banshee in my mineral line.", "banshee"),
    ("Cloaked banshee!", "banshee"),
    ("They have reapers.", "reaper"),
    ("Reaper.", "reaper"),
    ("DTs in my natural.", "dt"),
    ("Proxy barracks.", "proxy"),
]

# Entries about Dickie himself ("own": true) still fire on first-person speech.
OWN_ENTRIES = [
    ("I think I lost.", "gg"),                                                   # mic.log 16:02
    ("All right, what should I do now? I need to pivot a little bit.", "what now"),   # mic.log 7:59
    ("I'm supply blocked.", "blocked"),
    ("I'm under attack.", "attack"),
]


def test_fastlane_json_marks_own_entries():
    assert RULES and MAX_WORDS
    own = [line for _, line, own in RULES if own]
    assert len(own) == 5, own
    assert LINE["gg"] in own and LINE["what now"] in own and LINE["drop"] not in own


def test_own_play_never_fires_an_enemy_counter():
    for text in OWN_PLAY:
        assert own_play(text) or "my " in text.lower(), text
        assert fast_match(RULES, text) is None, (text, fast_match(RULES, text))


def test_enemy_callouts_still_fire():
    for text, key in ENEMY:
        assert not own_play(text), text
        assert fast_match(RULES, text) == LINE[key], (text, fast_match(RULES, text))


def test_own_entries_fire_on_first_person():
    for text, key in OWN_ENTRIES:
        assert fast_match(RULES, text) == LINE[key], (text, fast_match(RULES, text))


def test_my_unit_only_blocks_that_key():
    # "my" before a different word doesn't hide the enemy unit
    assert fast_match(RULES, "Banshee over my mineral line.") == LINE["banshee"]
    # "my reaper" is mine, but the enemy's roaches in the same line are still a callout
    assert fast_match(RULES, "Roaches at my natural, my reaper saw them.") == LINE["roach"]
    # the same key once as mine and once as his: his counts
    assert fast_match(RULES, "My reaper ran into his reaper.") == LINE["reaper"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  test_fastlane: {len(tests)} tests ok")
