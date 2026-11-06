"""Pull the plan data (matchup picks + Terran steps) out of a saved copy of the Live Coach page.

Usage: python extract_plans.py <saved-live-coach.html>   -> writes plans.json next to this file.
Re-run after the page's plans change (read the artifact with the Artifact tool to get a fresh copy).
"""
import json
import re
import sys
from pathlib import Path

html = Path(sys.argv[1]).read_text(encoding="utf-8")
m = re.search(r"const DATA = (\{.*?\});\s*\n", html, re.S)
if not m:
    sys.exit("DATA block not found in the page")
data = json.loads(m.group(1))


def secs(t):
    mm, ss = t.split(":")
    return int(mm) * 60 + int(ss)


out = {
    "patch": data["meta"]["patch"],
    "matchups": {mu["vs"]: mu["picks"] for mu in data["matchups"]},
    "plans": {
        p["id"]: {
            "name": p["name"],
            "tag": p["tag"],
            "hit": p["hit"],
            "summary": p["summary"],
            "title": p["terran"]["title"],
            "steps": [{"sec": secs(s["t"]), "t": s["t"], "supply": s["s"], "action": s["a"], "note": s["n"]}
                      for s in p["terran"]["steps"]],
            "ally_title": p["protoss"]["title"],
            "ally": [{"sec": secs(s["t"]), "t": s["t"], "supply": s["s"], "action": s["a"], "note": s["n"]}
                     for s in p["protoss"]["steps"]],
        }
        for p in data["plans"]
    },
}
dest = Path(__file__).with_name("plans.json")
dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
print(f"wrote {dest}: plans {', '.join(out['plans'])}; matchups {out['matchups']}")
