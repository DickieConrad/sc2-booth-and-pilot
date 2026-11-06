"""Fast lane matching for mic.py (stdlib, so test_fastlane.py runs without numpy / a mic).

voice/fastlane.json maps callout keywords to a pre-rendered coach clip. Most entries are counters to what the ENEMY
shows ("drop", "banshee", "reaper"), and those must only fire on a report of the enemy ("he's got a drop", "banshee
in my mineral line", "they have reapers"). A question or a plan about Dickie's own play ("Should I drop him?",
"I need to start port with a tech lab to get a banshee", "To research cloaking", "Where am I going with the
reaper?", "drop another command center") is not a callout: no fast reply, it goes on to the intent / slow lane.
Entries marked "own": true (supply blocked, under attack, gg, what now) are about Dickie by design and skip the guard.
"""
import json
import re
from pathlib import Path

FASTLANE = Path(__file__).resolve().parent / "voice" / "fastlane.json"

# Dickie asking about or planning his own play. Any of these anywhere in the utterance turns enemy counters off.
OWN_PLAY = re.compile(
    r"|".join([
        # "should I", "do I", "can we", "where am I", "how do I", "what should I", "have I"
        r"\b(?:should|shall|do|does|did|can|could|would|will|must|am|have)\s+(?:i|we)\b",
        # "I need to", "I want", "I'll", "I'm going to", "I'm gonna", "we should", "I feel like"
        r"\b(?:i|we)(?:\s+(?:need|want|wanna|gotta|have\s+to|has\s+to|should|will|would|plan|feel\s+like)\b"
        r"|['’]ll\b|\s+am\s+(?:going|gonna|about)\b"
        r"|['’]m\s+(?:going|gonna|about|building|making|researching|starting|teching|dropping|planning)\b"
        r"|['’]re\s+(?:going|gonna|building|making|dropping)\b)",
        r"\blet(?:['’]s|\s+me)\b",
        # "to research cloaking", "research cloak" (an order to himself), "to build a Starport"
        r"\bto\s+(?:research|build|make|start)\b",
        r"^\W*(?:research|build|make|start)\b",
        # "drop" as placing a building or a MULE: "drop another command center", "drop a depot"
        r"\bdrop(?:s|ped|ping)?\s+(?:(?:a|an|the|another|one|two|three|some|my|\d+)\s+)?"
        r"(?:command\s*cent|cc\b|ccs\b|orbital|planetary|depot|supply|barracks|rax\b|factory|starport|bunker"
        r"|turret|refinery|mule|scan|nexus|pylon|gateway|hatch)",
        # Dickie's own drop: "drop him", "dropping them", "build some drop", "make a drop"
        r"\bdrop(?:s|ped|ping)?\s+(?:him|her|them|his|their|the\s+(?:enemy|zerg|toss|protoss|terran))\b",
        r"\b(?:build|make|do|get|try|go\s+for)\s+(?:a\s+|some\s+|another\s+|the\s+)?drops?\b",
    ]),
    re.I,
)
# "my reaper", "my own banshee", "my first drop": the key right after "my" (one word allowed between) is his unit
MY_UNIT = re.compile(r"\bmy\s+(?:\w+\s+)?$", re.I)


def load_fastlane(path=FASTLANE):
    """fastlane.json -> (rules [(compiled regex, line, own)], max_words). Keys match whole words, any case; a space in
    a key also matches a hyphen ("void ray" = "void-ray"). Order is priority: the first entry that matches wins.
    own: the entry is about Dickie himself ("own": true), so the own-play guard doesn't apply."""
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], 0
    rules = []
    for e in cfg["entries"]:
        keys = sorted(e["keys"], key=len, reverse=True)
        alts = "|".join(r"[\s-]+".join(map(re.escape, k.split())) for k in keys)
        rules.append((re.compile(rf"\b(?:{alts})\b", re.I), e["line"], bool(e.get("own"))))
    return rules, int(cfg.get("max_words", 0))


def own_play(text):
    """True when the utterance asks about or plans Dickie's own play (no enemy counter may fire on it)."""
    return OWN_PLAY.search(text) is not None


def fast_match(rules, text):
    """The fastlane line for this callout (first matching entry), or None. Enemy counters only fire on a report of
    the enemy: never on own_play(text), and never on a key that is "my <unit>"."""
    mine = own_play(text)
    for rx, line, *own in rules:
        if own and own[0]:
            if rx.search(text):
                return line
            continue
        if mine:
            continue
        if any(not MY_UNIT.search(text[:m.start()]) for m in rx.finditer(text)):
            return line
    return None
