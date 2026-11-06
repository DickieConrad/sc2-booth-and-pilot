"""The owner's in-game name: the default player in coach.db, the history page and the review, and the name timing.py
follows to tell your team from the enemy's.

Read once at import, from (first match wins):
  1. the SC2_PLAYER environment variable
  2. live/player.json  {"name": "YourName"}   (git-ignored; copy live/player.example.json)
  3. DEFAULT ("Player")
Stdlib only, like db.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PATH = Path(__file__).resolve().with_name("player.json")
ENV = "SC2_PLAYER"
DEFAULT = "Player"


def name(path: Path = PATH) -> str:
    env = os.environ.get(ENV, "").strip()
    if env:
        return env
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT
    n = data.get("name") if isinstance(data, dict) else None
    return n.strip() if isinstance(n, str) and n.strip() else DEFAULT


OWNER = name()
