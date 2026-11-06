"""Export recorded games from live/coach.db (opened read-only) as replay fixtures for coach-core.

usage: python3 scripts/export_fixtures.py [GAME_ID ...]   (default: 3 5 6 9-15)
Writes tests/fixtures/game_<id>.json =
    {"game_id", "plan", "result", "samples": [{t, used, cap, minerals, gas, plan_supply}]}.
"""
import json
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
DB = HERE.parent.parent / "live" / "coach.db"
OUT = HERE / "tests" / "fixtures"

ids = [int(a) for a in sys.argv[1:]] or [3, 5, 6, 9, 10, 11, 12, 13, 14, 15]
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
OUT.mkdir(parents=True, exist_ok=True)
for gid in ids:
    plan, result = con.execute("select plan, result from games where id = ?", (gid,)).fetchone()
    rows = con.execute("select t, used, cap, minerals, gas, plan_supply from samples where game_id = ? order by rowid",
                       (gid,)).fetchall()
    samples = [dict(zip(("t", "used", "cap", "minerals", "gas", "plan_supply"), r)) for r in rows]
    doc = {"game_id": gid, "plan": plan, "result": result, "samples": samples}
    (OUT / f"game_{gid}.json").write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    ts = [s["t"] for s in samples]
    back = sum(1 for a, b in zip(ts, ts[1:]) if b < a)
    print(f"game {gid}: plan {plan}, {len(samples)} samples, t {ts[0]:.0f}..{ts[-1]:.0f}, backwards steps {back}")
con.close()
