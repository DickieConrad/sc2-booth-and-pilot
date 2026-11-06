"""Game history dashboard tests (stdlib; run by ci.sh via tools/checks.py tests). A temp SQLite DB built from
db.SCHEMA; the real coach.db is never opened."""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import history  # noqa: E402

XSS = "<script>alert(1)</script>"


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    games = [
        (1, 1793400000.0, "Victory", 600.0, "A", "D", f"Evil {XSS} (Zerg)"),
        (2, 1793401000.0, "Defeat", 200.0, "D", "D", "Short (Prot)"),
        (3, 1793402000.0, "Undecided", None, "A", "A", "Live (Terr)"),
    ]
    conn.executemany("INSERT INTO games (id, started_at, result, length_sec, plan, coach_plan, enemies) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", games)
    # Game 1: blocked at 1:50 (two reads, one block), an OCR miss, blocked again at 2:20, then 200/200 (not a block)
    s1 = [(100, 50, 0, 20, 23, 21), (110, 80, 0, 23, 23, 22), (120, 90, 0, 23, 23, 22),
          (125, None, None, None, None, 22),
          (130, 100, 0, 24, 31, 24), (140, 120, 10, 31, 31, 26), (150, 140, 20, 31, 39, 28),
          (250, 900, 300, 50, 62, 52), (300, 1200, 400, 55, 70, 60), (355, 400, 200, 60, 70, 66),
          (500, 300, 100, 200, 200, 120)]
    conn.executemany("INSERT INTO samples VALUES (1, ?, ?, ?, ?, ?, ?)", s1)
    s2 = [(60, 50, 0, 14, 15, 14), (190, 80, 0, 30, 31, 31)]
    conn.executemany("INSERT INTO samples VALUES (2, ?, ?, ?, ?, ?, ?)", s2)
    calls = [(1, t, "helper" if t % 40 else "coach", "blocked", f"call {t} <b>bold</b>", 0.0)
             for t in range(0, 300, 20)]
    conn.executemany("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?)", calls)
    conn.execute("INSERT INTO notes (game_id, created_at, author, text) VALUES (1, 1793400700, 'coach', ?)",
                 (f"Depots earlier & {XSS}",))
    conn.commit()
    conn.close()


def rows(path, db_path):
    code, body, ctype, cache = history.route(path, db_path)
    return code, body


def test_metrics():
    s = [{"t": t, "used": u, "cap": c, "plan_supply": p, "minerals": m}
         for t, u, c, p, m in [(100, 20, 23, 21, 50), (110, 23, 23, 22, 0), (125, None, None, 22, None),
                               (130, 23, 23, 22, 0), (140, 24, 31, 24, 0), (350, 60, 70, 66, 700), (360, 61, 70, 67, 0),
                               (500, 200, 200, 120, 900)]]
    assert history.blocks(s) == [110]   # the OCR miss doesn't split the block; 200/200 is maxed, not blocked
    assert history.supply_at(s) == (61, 67)
    assert history.supply_at(s[:5]) is None   # game over before 6:00 (last read far from 6:00)
    assert history.peak_bank(s) == 900
    assert history.mmss(None) == "-" and history.mmss(741) == "12:21"
    assert history.nice(118) == (50, 150) and history.nice(0.5) == (5, 5)


def test_list(dbp):
    code, body = rows("/history/games", dbp)
    assert code == 200
    assert XSS not in body and "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert body.index("#3<") < body.index("#2<") < body.index("#1<")   # newest first
    assert 'hx-get="/history/game/1"' in body and 'hx-target="#detail"' in body
    # game 1: two blocks, supply 60 at 6:00 vs plan 66 (behind by more than 3 -> bad), peak bank 1200 after 4:00
    row1 = body[body.index('hx-get="/history/game/1"'):]
    assert '<b class="bad">60</b><small>/66</small>' in row1
    assert '<span class="blk bad" title="supply blocks">2</span>' in row1 and '<span class="bank">1200</span>' in row1
    row2 = body[body.index('hx-get="/history/game/2"'):body.index('hx-get="/history/game/1"')]
    assert '<span class="dim">-</span>' in row2   # 3:20 game: no supply at 6:00
    assert '<b class="win">1W</b> <b class="loss">1L</b> (50%)' in body


def test_filters(dbp):
    _, body = rows("/history/games?result=Victory", dbp)
    assert "/history/game/1" in body and "/history/game/2" not in body
    _, body = rows("/history/games?plan=D", dbp)
    assert "/history/game/2" in body and "/history/game/1" not in body
    _, body = rows("/history/games?result=Victory&plan=D", dbp)
    assert "No games match" in body
    _, body = rows("/history/games?result=bogus&plan=", dbp)   # unknown filters are ignored, not errors
    assert all(f"/history/game/{i}" in body for i in (1, 2, 3))


def test_detail(dbp):
    code, body = rows("/history/game/1", dbp)
    assert code == 200
    assert XSS not in body and "<b>bold</b>" not in body and "&lt;b&gt;bold&lt;/b&gt;" in body
    assert "Depots earlier &amp; &lt;script&gt;" in body
    assert "<svg" in body and ">6:00</text>" in body
    assert 'class="blab"' in body and ">1:50</text>" in body and ">2:20</text>" in body   # block markers
    assert "(Live Coach D)" in body
    # 15 calls: 12 shown, the rest behind the Alpine expander
    assert body.count('x-show="all"') == 3 and "Show all 15" in body
    assert 'class="helper"' in body and 'class="coach"' in body
    code, body = rows("/history/game/3", dbp)   # no samples, no calls: still renders
    assert code == 200 and "No panel reads" in body


def test_404(dbp):
    assert rows("/history/game/999", dbp)[0] == 404
    assert rows("/history/game/abc", dbp)[0] == 404
    assert rows("/history/nope", dbp)[0] == 404
    for bad in ("/vendor/../db.py", "/vendor/x.js", "/vendor/", "/vendor/htmx.min.js.map"):
        assert rows(bad, dbp)[0] == 404, bad
    code, body, ctype, cache = history.route("/vendor/htmx.min.js", dbp)
    assert code == 200 and ctype == "application/javascript" and "max-age" in cache and "htmx" in body
    # an id too big for SQLite's integer: a 404, not an uncaught OverflowError
    assert rows("/history/game/" + "9" * 30, dbp)[0] == 404


def test_vendor_file_missing(dbp):
    # allowlisted but not on disk (live/vendor not fetched): a 404, not an uncaught FileNotFoundError
    here = history.HERE
    with tempfile.TemporaryDirectory() as d:
        history.HERE = Path(d)
        try:
            assert rows("/vendor/alpine.min.js", dbp)[0] == 404
        finally:
            history.HERE = here


def test_opening_wait_is_not_a_block():
    # 15/15 at 0:20-0:40 waiting on the first depot: not a block in the games list (same 60 s floor as the review)
    s = [{"t": t, "used": u, "cap": c} for t, u, c in [(20, 15, 15), (30, 15, 15), (45, 16, 23), (90, 23, 23)]]
    assert history.blocks(s) == [90]
    assert history.BLOCK_FROM == 60


def test_page(dbp):
    code, body = rows("/history", dbp)
    assert code == 200 and '<option value="A">A</option><option value="D">D</option>' in body
    assert 'src="/vendor/htmx.min.js"' in body and "cdn" not in body.lower()
    code, body = rows("/history", Path(dbp).with_name("missing.db"))   # shell still loads without a database
    assert code == 200 and "<!--plans-->" not in body
    assert rows("/history/games", Path(dbp).with_name("missing.db"))[0] == 500


def test_read_only(dbp):
    before = Path(dbp).read_bytes()
    for p in ("/history", "/history/games", "/history/game/1"):
        rows(p, dbp)
    assert Path(dbp).read_bytes() == before
    conn = history.connect(dbp)
    try:
        conn.execute("INSERT INTO notes (created_at, text) VALUES (0, 'x')")
        raise AssertionError("history.connect must be read-only")
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        dbp = Path(d) / "test.db"
        make_db(dbp)
        tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
        for t in tests:
            if t.__code__.co_argcount:
                t(dbp)
            else:
                t()
    print(f"  test_history: {len(tests)} tests ok")
