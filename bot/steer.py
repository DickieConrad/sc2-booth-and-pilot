#!/usr/bin/env python3
"""Steer a running sparring bot: writes bot/out/command.json (atomically, with a fresh seq).
Stdlib only; works from WSL or Windows while a game runs.

    python3 bot/steer.py cheese      # go all-in now (send the cheese)
    python3 bot/steer.py attack      # attack with everything now
    python3 bot/steer.py stop        # pull back home and hold
    python3 bot/steer.py resume      # waves continue on schedule
    python3 bot/steer.py gg          # the bot types gg and leaves
    python3 bot/steer.py pivot       # ProxyRax: switch from the cheese to the 2-base macro game now"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import ACTION_KEYS, COMMAND_FILE, write_command  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or any(a not in ACTION_KEYS for a in argv):
        print(__doc__)
        return 2
    data = write_command(COMMAND_FILE, time.time_ns() // 1_000_000, **{a: True for a in argv})
    print(f"{COMMAND_FILE}: {data}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
