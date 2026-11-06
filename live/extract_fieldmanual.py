"""Pull the 1v1 build drill (one opener, split per matchup) out of a saved copy of the Terran Field Manual page.

Usage: python extract_fieldmanual.py <saved-field-manual.html>  -> writes plans_1v1.json next to this file
with plans Z (TvZ), P (TvP), T (TvT). Re-run after the page's drill changes.
"""
import json
import re
import sys
from pathlib import Path

html = Path(sys.argv[1]).read_text(encoding="utf-8")
m = re.search(r"var RAW = (\[.*?\]);\s*\n", html, re.S)
if not m:
    sys.exit("RAW build list not found in the page")
# The page holds a JS literal: quote its keys so it parses as JSON.
raw = json.loads(re.sub(r'([{,]\s*)([A-Za-z_]+):', r'\1"\2":', m.group(1)))


def secs(t):
    mm, ss = t.split(":")
    return int(mm) * 60 + int(ss)


NAMES = {"z": ("Z", "TvZ"), "p": ("P", "TvP"), "t": ("T", "TvT")}
HIT = {"z": "6:30", "p": "6:30", "t": "7:00"}
plans = {}
for mu, (pid, name) in NAMES.items():
    steps = []
    for r in raw:
        if "m" in r and mu not in r["m"]:
            continue
        steps.append({"sec": secs(r["t"]), "t": r["t"], "supply": r.get("s"), "action": r["a"], "note": r.get("n", "")})
    plans[pid] = {"name": f"{name} Field Manual", "tag": "1v1", "hit": HIT[mu],
                  "summary": "1v1: Reaper expand into Factory and Starport, 3 CC bio. "
                             "Supply is the trigger before 3:00, the clock after. Checkpoint rows are SCV targets.",
                  "title": f"Reaper expand, 3 CC bio ({name})", "steps": steps}

dest = Path(__file__).with_name("plans_1v1.json")
dest.write_text(json.dumps({"plans": plans}, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"wrote {dest}: " + ", ".join(f"{k} {len(v['steps'])} steps" for k, v in plans.items()))
