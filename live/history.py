"""Game history dashboard (GET /history on the timing helper): server-rendered HTML fragments for htmx.

  /history                 page shell (live/history.html) with the player + plan filter options filled in
  /history/games           games list + trend strip (?player=<owner> by default, ?result=Victory|Defeat, ?plan=A)
  /history/game/<id>       one game (any player): summary, supply/bank chart, calls, notes (404 if no such game)
  /history/review/<id>     post-game review as JSON, any player's game; /history/review/latest[?player=X] is the
                           latest game of that player (default: the owner, live/player.json): findings + the one top fix
  /vendor/<file>           the vendored htmx / Alpine builds (allowlist; no CDN)

Read-only: every request opens its own short-lived mode=ro connection; the helper stays coach.db's only writer.
Players (games.player): Dickie's games are the default everywhere; sparring-bot games (bot/run.py --coach-test) carry
the bot's name and show when picked. A coach.db from before the column (not yet migrated by the helper) is all
Dickie's.
No numpy (the Windows python has none). Everything from the database goes through esc(). The review JSON has a
declared shape (models.Review) and is serialized through models.REVIEW.
"""
import html
import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from models import (
    REVIEW,
    Also,
    BehindWindow,
    BlockWindow,
    Checkpoint,
    DropEvent,
    Finding,
    Findings,
    Fix,
    IdleWindow,
    PileWindow,
    Review,
    Window,
)
from player import OWNER  # the owner's in-game name (= db.OWNER; live/player.json): the default player

Row = Mapping[str, Any]   # a sqlite3.Row or a dict with db.SCHEMA columns (values may be None)
Route = tuple[int, str, str, str]   # (status, body, content type, cache-control)

HERE = Path(__file__).resolve().parent
DB = HERE / "coach.db"
PAGE = HERE / "history.html"
VENDOR = ("htmx.min.js", "alpine.min.js")   # the only files /vendor/ serves
HTML = "text/html; charset=utf-8"
JSON = "application/json"
SIX = 360           # supply checkpoint (6:00)
NEAR = 15           # a sample within this many seconds before 6:00 counts as "at 6:00" (samples are ~10 s apart)
BEHIND_BY = 3       # same threshold as the helper's "behind" call
BANK_FROM = 240     # peak bank only counts after 4:00 (openings bank on purpose)
BLOCK_FROM = 60     # the opening 15/15 wait for the first depot is part of every build, not a block (list + review)
TREND = 10
CALLS_SHOWN = 12    # calls visible before "show all"

def esc(v: object) -> str:
    return html.escape("" if v is None else str(v))


def mmss(sec: float | None) -> str:
    if sec is None:
        return "-"
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def connect(path: str | Path = DB) -> sqlite3.Connection:
    # Same URI form as db.connect (nolock: the share can't lock), but read-only and without the CREATE script
    conn = sqlite3.connect(f"file:{path}?mode=ro&nolock=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- metrics ----------

def blocks(samples: Iterable[Row]) -> list[float]:
    """Game times where supply became blocked (used reaches cap below 200). Counted on entering the block, so one
    long block is one block; OCR misses (None) keep the previous state instead of splitting a block in two.
    Before BLOCK_FROM (the opening 15/15) nothing counts, same as the review's block_windows()."""
    out: list[float] = []
    was = False
    for s in samples:
        used, cap = s["used"], s["cap"]
        if used is None or not cap:
            continue
        now = s["t"] >= BLOCK_FROM and used >= cap and cap < 200
        if now and not was:
            out.append(s["t"])
        was = now
    return out


def supply_at(samples: Iterable[Row], t: float = SIX) -> tuple[int, int | None] | None:
    """(used, plan_supply) from the last read at or before t, if it is recent enough; None for shorter games."""
    best = None
    for s in samples:
        if s["t"] <= t and s["used"] is not None:
            best = s
    if best is None or t - best["t"] > NEAR:
        return None
    return best["used"], best["plan_supply"]


def peak_bank(samples: Iterable[Row], after: float = BANK_FROM) -> int | None:
    vals: list[int] = [s["minerals"] for s in samples if s["t"] >= after and s["minerals"] is not None]
    return max(vals) if vals else None


def metrics(samples: Sequence[Row]) -> dict[str, Any]:
    return {"six": supply_at(samples), "blocks": blocks(samples), "bank": peak_bank(samples)}


# ---------- small renderers ----------

def result_cls(result: str | None) -> str:
    return {"Victory": "win", "Defeat": "loss"}.get(result or "", "other")


def six_html(six: tuple[int, int | None] | None) -> str:
    if not six:
        return '<span class="dim">-</span>'
    used, plan = six
    if plan is None:
        return f"<b>{esc(used)}</b>"
    cls = "ok" if used >= plan else "bad" if used < plan - BEHIND_BY else "warn"
    return f'<b class="{cls}">{esc(used)}</b><small>/{esc(plan)}</small>'


def when(ts: float | None, fmt: str = "%a %d %b %H:%M") -> str:
    return time.strftime(fmt, time.localtime(ts)) if ts else "?"


def samples_by_game(conn: sqlite3.Connection, ids: list[int]) -> dict[int, list[Row]]:
    out: dict[int, list[Row]] = {i: [] for i in ids}
    if ids:
        q = f"SELECT * FROM samples WHERE game_id IN ({','.join('?' * len(ids))}) ORDER BY game_id, t"
        for r in conn.execute(q, ids):
            out[r["game_id"]].append(r)
    return out


def plan_options(conn: sqlite3.Connection) -> str:
    plans = [r[0] for r in conn.execute("SELECT DISTINCT plan FROM games WHERE plan IS NOT NULL AND plan != '' "
                                        "ORDER BY plan")]
    return "".join(f'<option value="{esc(p)}">{esc(p)}</option>' for p in plans)


def has_player(conn: sqlite3.Connection) -> bool:
    """games.player exists (the helper migrates coach.db on start; this side only reads)."""
    return any(r[1] == "player" for r in conn.execute("PRAGMA table_info(games)"))


def player_list(conn: sqlite3.Connection) -> list[str]:
    """Dickie first, then every other player with games (sparring bots), by name."""
    others: list[str] = []
    if has_player(conn):
        others = [r[0] for r in conn.execute("SELECT DISTINCT player FROM games WHERE player IS NOT NULL "
                                             "AND player != '' AND player != ? ORDER BY player", (OWNER,))]
    return [OWNER, *others]


def player_options(conn: sqlite3.Connection) -> str:
    return "".join(f'<option value="{esc(p)}">{esc(p)}{" (you)" if p == OWNER else ""}</option>'
                   for p in player_list(conn))


def player_where(conn: sqlite3.Connection, player: str | None) -> tuple[str, list[Any]]:
    """SQL condition + args for one player's games ('' = no filter: before the column every game is Dickie's)."""
    player = player or OWNER
    if not has_player(conn):
        return ("", []) if player == OWNER else ("0", [])
    if player == OWNER:   # NULL: a row written without a player is Dickie's (the column's default)
        return "(player = ? OR player IS NULL)", [OWNER]
    return "player = ?", [player]


# ---------- fragments ----------

def page(conn: sqlite3.Connection | None = None) -> str:
    shell = PAGE.read_text(encoding="utf-8")
    players = player_options(conn) if conn else f'<option value="{OWNER}">{OWNER} (you)</option>'
    owner = json.dumps(OWNER).replace("<", "\\u003c")   # a JS string literal, safe inside <script>
    return (shell.replace("<!--plans-->", plan_options(conn) if conn else "").replace("<!--players-->", players)
            .replace('"<!--owner-->"', owner))


def games_fragment(conn: sqlite3.Connection, result: str | None = None, plan: str | None = None,
                   limit: int = 200, player: str | None = None) -> str:
    where: list[str] = []
    args: list[Any] = []
    cond, pargs = player_where(conn, player)
    if cond:
        where.append(cond); args += pargs
    if result in ("Victory", "Defeat"):
        where.append("result = ?"); args.append(result)
    if plan:
        where.append("plan = ?"); args.append(plan)
    sql = "SELECT * FROM games" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    games = conn.execute(sql, (*args, limit)).fetchall()
    ss = samples_by_game(conn, [g["id"] for g in games])
    ms = {g["id"]: metrics(ss[g["id"]]) for g in games}
    if not games:
        return '<p class="empty">No games match.</p>'

    # Trend: the last TREND shown games, oldest left, so the newest is where the eye ends
    chips = []
    for g in reversed(games[:TREND]):
        letter = {"Victory": "W", "Defeat": "L"}.get(g["result"], "?")
        six = ms[g["id"]]["six"]
        chips.append(f'<li class="{result_cls(g["result"])}" title="#{g["id"]} {esc(g["result"])} vs '
                     f'{esc(g["enemies"])}"><b>{letter}</b><small>{esc(six[0]) if six else "-"}</small></li>')
    wins = sum(g["result"] == "Victory" for g in games)
    losses = sum(g["result"] == "Defeat" for g in games)
    rate = f" ({100 * wins // (wins + losses)}%)" if wins + losses else ""
    trend = (f'<div class="trend"><h2>Last {min(TREND, len(games))}</h2><ol>{"".join(chips)}</ol>'
             f'<span class="tally"><b class="win">{wins}W</b> <b class="loss">{losses}L</b>{rate} '
             f'· {len(games)} games · small number = supply at 6:00</span></div>')

    rows = []
    for g in games:
        gid, m = int(g["id"]), ms[g["id"]]
        nb = len(m["blocks"])
        rows.append(
            f'<a class="row" href="/history?game={gid}" hx-get="/history/game/{gid}" hx-target="#detail" '
            f'hx-push-url="/history?game={gid}" @click="sel = {gid}" :class="sel === {gid} && \'sel\'">'
            f'<span class="id">#{gid}</span>'
            f'<span class="dt">{esc(when(g["started_at"]))}</span>'
            f'<span class="res {result_cls(g["result"])}">{esc(g["result"] or "?")}</span>'
            f'<span class="len">{mmss(g["length_sec"])}</span>'
            f'<span class="pl">{esc(g["plan"] or "?")}</span>'
            f'<span class="opp">{esc(g["enemies"] or "?")}</span>'
            f'<span class="six" title="supply at 6:00 / plan">{six_html(m["six"])}</span>'
            f'<span class="blk{" bad" if nb else ""}" title="supply blocks">{nb}</span>'
            f'<span class="bank">{esc(m["bank"]) if m["bank"] is not None else "-"}</span></a>')
    head = ('<div class="row head"><span class="id">#</span><span class="dt">When</span><span class="res">Result</span>'
            '<span class="len">Length</span><span class="pl">Plan</span><span class="opp">Opponents</span>'
            '<span class="six">Sup 6:00</span><span class="blk">Blocks</span>'
            '<span class="bank">Bank 4:00+</span></div>')
    return trend + f'<div class="games">{head}{"".join(rows)}</div>'


def game_fragment(conn: sqlite3.Connection, gid: int) -> str | None:
    """The detail panel for one game, or None if there is no such game (the route turns that into a 404)."""
    g = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
    if not g:
        return None
    samples = conn.execute("SELECT * FROM samples WHERE game_id = ? ORDER BY t", (gid,)).fetchall()
    calls = conn.execute("SELECT * FROM calls WHERE game_id = ? ORDER BY t, rowid", (gid,)).fetchall()
    notes = conn.execute("SELECT * FROM notes WHERE game_id = ? ORDER BY id", (gid,)).fetchall()
    m = metrics(samples)

    coach = f' (Live Coach {esc(g["coach_plan"])})' if g["coach_plan"] and g["coach_plan"] != g["plan"] else ""
    who = g["player"] if "player" in g.keys() else None
    bot = f' <span class="tag">{esc(who)}</span>' if who and who != OWNER else ""
    ally = f' · ally {esc(g["ally"])}' + (f' ({esc(g["ally_race"])})' if g["ally_race"] else "") if g["ally"] else ""
    head = (f'<header class="dhead"><h1>#{int(g["id"])} <span class="{result_cls(g["result"])}">'
            f'{esc(g["result"] or "?")}</span> <span class="dim">{mmss(g["length_sec"])}</span>{bot}</h1>'
            f'<p>Plan <b>{esc(g["plan"] or "?")}</b>{coach} vs <b>{esc(g["enemies"] or "?")}</b>{ally}</p>'
            f'<p class="dim">{esc(when(g["started_at"], "%A %d %B %Y, %H:%M"))} · {esc(g["source"])}</p></header>')
    bl = ", ".join(mmss(t) for t in m["blocks"])
    tiles = ('<div class="tiles">'
             f'<div class="tile"><small>Supply 6:00</small>{six_html(m["six"])}</div>'
             f'<div class="tile"><small>Supply blocks</small><b class="{"bad" if m["blocks"] else "ok"}">'
             f'{len(m["blocks"])}</b><span>{esc(bl)}</span></div>'
             f'<div class="tile"><small>Peak bank 4:00+</small><b>{esc(m["bank"]) if m["bank"] is not None else "-"}'
             f'</b></div><div class="tile"><small>Calls</small><b>{len(calls)}</b></div></div>')

    chart = chart_svg(samples, m["blocks"]) if samples else '<p class="empty">No panel reads for this game.</p>'

    items = []
    for i, c in enumerate(calls):
        src = "coach" if c["source"] == "coach" else "helper"
        hide = ' x-show="all" x-cloak' if i >= CALLS_SHOWN else ""
        items.append(f'<li class="{src}"{hide}><span class="t">{mmss(c["t"])}</span><span class="src">{src}</span>'
                     f'<span class="tx">{esc(c["text"])}</span></li>')
    more = (f'<button type="button" @click="all = !all" x-text="all ? \'Show fewer\' : \'Show all {len(calls)}\'">'
            f'Show all {len(calls)}</button>' if len(calls) > CALLS_SHOWN else "")
    calls_html = (f'<section class="calls" x-data="{{all: false}}"><h2>Calls</h2>'
                  f'<ol>{"".join(items)}</ol>{more}</section>' if calls else "")

    nitems = "".join(f'<li><span class="t">{esc(when(n["created_at"], "%d %b %H:%M"))}</span>'
                     f'<span class="src">{esc(n["author"])}</span><span class="tx">{esc(n["text"])}</span></li>'
                     for n in notes)
    notes_html = f'<section class="notes"><h2>Notes</h2><ol>{nitems}</ol></section>' if notes else ""
    return head + tiles + chart + calls_html + notes_html


# ---------- chart ----------

W, L, R = 640, 44, 12          # viewBox width and left/right margins
PANELS = (("supply", 170), ("bank", 120))
TOP, GAP, BOTTOM = 18, 30, 22


def nice(vmax: float, n: int = 4) -> tuple[int, int]:
    """A round tick step and the axis max it gives (about n ticks)."""
    for step in (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000):
        if vmax / step <= n:
            break
    return step, max(step, -(-int(vmax) // step) * step)


def path(points: Iterable[tuple[float, float | None]]) -> str:
    """SVG path through (x, y) points; a None y breaks the line instead of drawing to zero."""
    d, pen = [], False
    for x, y in points:
        if y is None:
            pen = False
            continue
        d.append(f"{'L' if pen else 'M'}{x:.1f},{y:.1f}")
        pen = True
    return " ".join(d)


def chart_svg(samples: Sequence[Row], block_ts: Sequence[float]) -> str:
    tmax = max(60, max(s["t"] for s in samples))
    pw = W - L - R
    def x(t: float) -> float:
        return float(L + pw * t / tmax)
    xstep = 60 if tmax <= 720 else 120 if tmax <= 1500 else 300
    height = TOP + sum(h for _, h in PANELS) + GAP * (len(PANELS) - 1) + BOTTOM
    out = [f'<svg class="chart" viewBox="0 0 {W} {height}" role="img" '
           f'aria-label="Supply and bank over game time">']
    top = TOP
    for name, h in PANELS:
        if name == "supply":
            series = [("used", "s-used"), ("plan_supply", "s-plan")]
            vmax = max([s[k] or 0 for s in samples for k in ("used", "plan_supply")] + [20])
            title = "Supply: used vs plan"
        else:
            series = [("minerals", "s-min"), ("gas", "s-gas")]
            vmax = max([s[k] or 0 for s in samples for k in ("minerals", "gas")] + [100])
            title = "Bank: minerals and gas"
        step, ymax = nice(vmax)
        def y(v: float | None, top: int = top, h: int = h, ymax: int = ymax) -> float | None:
            return None if v is None else top + h - h * min(v, ymax) / ymax

        def yv(v: float, top: int = top, h: int = h, ymax: int = ymax) -> float:
            return top + h - h * min(v, ymax) / ymax
        out.append(f'<text class="ptitle" x="{L}" y="{top - 6}">{title}</text>')
        for v in range(0, ymax + 1, step):
            out.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{yv(v):.1f}" y2="{yv(v):.1f}"/>'
                       f'<text class="ylab" x="{L - 6}" y="{yv(v) + 4:.1f}">{v}</text>')
        for t in range(0, int(tmax) + 1, xstep):
            out.append(f'<line class="grid" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{top}" y2="{top + h}"/>')
        if name == "supply":
            # The cap as a faint ceiling: blocks are where the blue line meets it
            out.append(f'<path class="s-cap" d="{path([(x(s["t"]), y(s["cap"])) for s in samples])}"/>')
            last, row = -1e9, 0
            for bt in block_ts:
                bx = x(bt)
                row = (row + 1) % 3 if bx - last < 70 else 0   # close blocks: step labels down so they don't collide
                last = bx
                end = bx > W - R - 70   # near the right edge: label on the left of the line
                anchor = ' text-anchor="end"' if end else ""
                out.append(f'<line class="blk" x1="{bx:.1f}" x2="{bx:.1f}" y1="{top}" y2="{top + h}"/>'
                           f'<text class="blab" x="{bx - 3 if end else bx + 3:.1f}" y="{top + 10 + 12 * row}"'
                           f'{anchor}>{mmss(bt)}</text>')
        for key, cls in series:
            out.append(f'<path class="{cls}" d="{path([(x(s["t"]), y(s[key])) for s in samples])}"/>')
        top += h + GAP
    base = top - GAP
    for t in range(0, int(tmax) + 1, xstep):
        out.append(f'<text class="xlab" x="{x(t):.1f}" y="{base + 16}">{mmss(t)}</text>')
    # Hover: one invisible strip per read, with a native tooltip (no chart library, no script)
    for i, s in enumerate(samples):
        a = x(samples[i - 1]["t"] + (s["t"] - samples[i - 1]["t"]) / 2) if i else L
        b = x(s["t"] + (samples[i + 1]["t"] - s["t"]) / 2) if i + 1 < len(samples) else W - R
        plan = f' (plan {s["plan_supply"]})' if s["plan_supply"] is not None else ""
        tip = f'{mmss(s["t"])}  supply {s["used"]}/{s["cap"]}{plan}  ·  {s["minerals"]} min  {s["gas"]} gas'
        out.append(f'<rect class="hit" x="{a:.1f}" y="{TOP}" width="{max(b - a, 1):.1f}" height="{base - TOP}">'
                   f'<title>{esc(tip)}</title></rect>')
    out.append("</svg>")
    legend = ('<ul class="legend"><li><i class="k-used"></i>Supply used</li><li><i class="k-plan"></i>Plan supply</li>'
              '<li><i class="k-cap"></i>Cap</li><li><i class="k-blk"></i>Supply block</li>'
              '<li><i class="k-min"></i>Minerals</li><li><i class="k-gas"></i>Gas</li></ul>')
    return f'<section class="chartbox">{legend}<div class="scroll">{"".join(out)}</div></section>'


# ---------- post-game review (GET /history/review/<id> | /history/review/latest, JSON) ----------
# The coach's hand review, automated: "Behind 3-9 supply 3:00-6:30; bank 500-1250 minerals 3:30-5:30; blocked 70/70
# at 7:24; ...", then the ONE fix that matters most. review() is pure (rows in, JSON-ready dict out).
# Reads are ~5 s apart and OCR sometimes misses one (None): every detector skips None values.

CHECKS = (180, 360, 480)       # supply vs plan at 3:00, 6:00, 8:00
READ = 5                       # one read covers ~5 s: a window of one read lasts READ seconds
JOIN = 12                      # a window survives one odd or missing read in between
BEHIND_FOR = 20                # behind-plan windows shorter than this are step-timing noise
BANK_OVER = 500                # mineral bank window: minerals > 500
GAS_OVER, MIN_UNDER, GAS_JOIN = 400, 200, 30   # gas-heavy: gas > 400 with minerals < 200; 30 s gaps don't split it
FIGHT_DROP, FIGHT_SPAN = 15, 30                # lost fight: used falls >= 15 below its max of the last 30 s
CAP_DROP, CAP_JOIN = 8, 60                     # lost buildings: cap falls >= 8 (a depot) in drops <= 60 s apart
IDLE_FROM, IDLE_BAND, IDLE_FOR, IDLE_ROOM = 300, 2, 90, 8   # idle: used within +-2 for 90 s+, 8+ free supply, 5:00+
EARLY = 360                    # a lost fight / lost buildings before 6:00 in a loss = the opening didn't hold

# Top fix: the category with the highest score wins (ties: the order below). Scores are "seconds that hurt":
#   early      1000 if the game was lost and a lost fight or lost buildings started before 6:00 (it always wins)
#   supply     3 per blocked second + 20 per block (a block stops every production building at once)
#   production 1.5 per idle second + 40 per lost fight + 10 per supply behind plan at 8:00
#   gas        0.5 per gas-heavy second (you still spend minerals, but the gas buys nothing)
#   bank       1 per second over 500 minerals after 4:00
# The runner-up rides along as "also" when it scores at least half the winner (the hand reviews often name two).
FIXES = {
    "early": "Opening didn't hold",
    "supply": "Supply blocks",
    "production": "Not enough production",
    "gas": "Gas-heavy, mineral-starved",
    "bank": "Unspent bank",
}


def runs(samples: Iterable[Row], test: Callable[[Row], bool], join: float = JOIN) -> list[list[Row]]:
    """Groups of reads where test(read) is true; true reads at most `join` s apart stay in one group."""
    out: list[list[Row]] = []
    for s in samples:
        if test(s):
            if out and s["t"] - out[-1][-1]["t"] <= join:
                out[-1].append(s)
            else:
                out.append([s])
    return out


def span(run: Sequence[Row]) -> float:
    return float(run[-1]["t"] - run[0]["t"] + READ)


def ok(*vals: object) -> bool:
    return all(v is not None for v in vals)


def behind_windows(samples: Sequence[Row]) -> list[BehindWindow]:
    rs = runs(samples, lambda s: ok(s["used"], s["plan_supply"]) and s["plan_supply"] - s["used"] >= BEHIND_BY)
    return [{"start": r[0]["t"], "end": r[-1]["t"], "min": min(s["plan_supply"] - s["used"] for s in r),
             "max": max(s["plan_supply"] - s["used"] for s in r)} for r in rs if span(r) >= BEHIND_FOR]


def block_windows(samples: Sequence[Row]) -> list[BlockWindow]:
    rs = runs(samples, lambda s: s["t"] >= BLOCK_FROM and ok(s["used"]) and bool(s["cap"]) and s["used"] >= s["cap"]
              and s["cap"] < 200)
    return [{"start": r[0]["t"], "end": r[-1]["t"], "used": r[0]["used"], "cap": r[0]["cap"]} for r in rs]


def bank_windows(samples: Sequence[Row]) -> list[PileWindow]:
    rs = runs(samples, lambda s: ok(s["minerals"]) and s["minerals"] > BANK_OVER)
    return [{"start": r[0]["t"], "end": r[-1]["t"], "low": min(s["minerals"] for s in r),
             "peak": max(s["minerals"] for s in r)} for r in rs]


def gas_windows(samples: Sequence[Row]) -> list[PileWindow]:
    rs = runs(samples, lambda s: ok(s["gas"], s["minerals"]) and s["gas"] > GAS_OVER and s["minerals"] < MIN_UNDER,
              GAS_JOIN)
    return [{"start": r[0]["t"], "end": r[-1]["t"], "low": min(s["gas"] for s in r),
             "peak": max(s["gas"] for s in r)} for r in rs]


def steady(pts: Sequence[Row], key: str) -> list[Row]:
    """The reads (all with a value for `key`) minus one-read OCR outliers: a read below the one before it counts only
    if the next read is below that too (cap 70 -> 7 -> 70 is a misread; a drop on the last read is unconfirmed), and a
    read above it is dropped when the next read is back at or under it (70 -> 170 -> 70). Real losses last two reads."""
    out: list[Row] = []
    for i, s in enumerate(pts):
        nxt = pts[i + 1][key] if i + 1 < len(pts) else None
        if out:
            prev = out[-1][key]
            if s[key] < prev and (nxt is None or nxt >= prev):
                continue
            if s[key] > prev and nxt is not None and nxt <= prev:
                continue
        out.append(s)
    return out


def lost_fights(samples: Sequence[Row]) -> list[DropEvent]:
    """Used falls >= FIGHT_DROP below its max of the last FIGHT_SPAN s. Overlapping drops are one fight:
    before = the max it fell from, after = the lowest read until the drop ends. One-read misreads don't count."""
    pts = steady([s for s in samples if s["used"] is not None], "used")
    out: list[DropEvent] = []
    last: list[float] = []   # per fight: the clock of its last read below the peak
    lo = 0
    for i, s in enumerate(pts):
        while s["t"] - pts[lo]["t"] > FIGHT_SPAN:
            lo += 1
        top = max(pts[lo:i + 1], key=lambda p: (p["used"], p["t"]))   # the last read at the peak
        if top["used"] - s["used"] < FIGHT_DROP:
            continue
        f = out[-1] if out and s["t"] - last[-1] <= FIGHT_SPAN else None
        if f is None:
            out.append({"start": top["t"], "end": s["t"], "before": top["used"], "after": s["used"]})
            last.append(s["t"])
        else:
            last[-1] = s["t"]
            if s["used"] < f["after"]:
                f["after"], f["end"] = s["used"], s["t"]
    return out


def lost_buildings(samples: Sequence[Row]) -> list[DropEvent]:
    """Cap drops (depots / CCs dying). Drops <= CAP_JOIN s apart are one event; it counts at >= CAP_DROP in total.
    One-read misreads (cap 70 -> 7 -> 70) don't count."""
    pts = steady([s for s in samples if s["cap"]], "cap")
    out: list[DropEvent] = []
    for a, b in zip(pts, pts[1:]):
        if b["cap"] >= a["cap"]:
            continue
        if out and b["t"] - out[-1]["end"] <= CAP_JOIN:
            out[-1]["end"], out[-1]["after"] = b["t"], min(out[-1]["after"], b["cap"])
        else:
            out.append({"start": b["t"], "end": b["t"], "before": a["cap"], "after": b["cap"]})
    return [e for e in out if e["before"] - e["after"] >= CAP_DROP]


def idle_windows(samples: Sequence[Row]) -> list[IdleWindow]:
    """After 5:00: used stays within +-IDLE_BAND (range <= 2 * band) for IDLE_FOR s or more while there was room to
    build (free supply cap - used averages >= IDLE_ROOM over the window, so blocks and maxed-out don't count)."""
    pts = [s for s in samples if s["used"] is not None and s["t"] >= IDLE_FROM]
    out: list[IdleWindow] = []
    i = 0
    while i < len(pts):
        j, lo, hi = i, pts[i]["used"], pts[i]["used"]
        while j + 1 < len(pts) and max(hi, pts[j + 1]["used"]) - min(lo, pts[j + 1]["used"]) <= 2 * IDLE_BAND:
            j += 1
            lo, hi = min(lo, pts[j]["used"]), max(hi, pts[j]["used"])
        if pts[j]["t"] - pts[i]["t"] >= IDLE_FOR:
            w = pts[i:j + 1]
            rooms = [s["cap"] - s["used"] for s in w if s["cap"]]
            if rooms and sum(rooms) / len(rooms) >= IDLE_ROOM:
                used = sorted(s["used"] for s in w)
                out.append({"start": w[0]["t"], "end": w[-1]["t"], "supply": used[len(used) // 2],
                            "room": round(sum(rooms) / len(rooms))})
            i = j + 1
        else:
            i += 1
    return out


def secs(ws: Iterable[Window], after: float = 0) -> int:
    """Seconds covered by windows (one read = READ s), counting only the part from `after` on."""
    return round(sum(max(0, w["end"] - max(w["start"], after) + READ) for w in ws if w["end"] >= after))


def rng(a: float, b: float) -> str:
    return mmss(a) if b - a < READ else f"{mmss(a)}-{mmss(b)}"


def top_fix(f: Findings, result: str | None) -> Fix:
    blocks_, fights = f["blocks"], f["fights"]
    late = [c for c in f["checkpoints"] if c["t"] == 480 and c["plan"] is not None]
    scores: dict[str, float] = {
        "early": 1000 if result == "Defeat" and any(e["start"] < EARLY for e in fights + f["buildings"]) else 0,
        "supply": 3 * secs(blocks_) + 20 * len(blocks_),
        "production": 1.5 * secs(f["idle"]) + 40 * len(fights) + 10 * sum(max(0, -(c["diff"] or 0)) for c in late),
        "gas": 0.5 * secs(f["gas"]),
        "bank": secs(f["bank"], 240),
    }
    key = max(FIXES, key=lambda k: scores[k])   # max() keeps the first of equal scores: FIXES order breaks ties
    if scores[key] <= 0:
        return {"key": "none", "title": "Clean game", "action": "Same again: depots ahead, production nonstop",
                "why": "No blocks, banks, gas floats, lost fights or idle production found", "also": None,
                "scores": scores}
    after6 = any(b["end"] >= 360 for b in blocks_)
    peak_gas = max((g["peak"] for g in f["gas"]), default=0)
    actions = {
        "early": "Bunker at the natural by 2:30, Marines nonstop, pull SCVs to repair; spend the bank on units",
        "supply": "From 6:00, two depots every 30 s" if after6 else "Next depot when supply is 6 below the cap",
        "production": "5 Barracks + 2 Factories by 8:00",
        "gas": f"Gas over 400: {6 if peak_gas > 800 else 3} SCVs to minerals",
        "bank": "Spend the bank: more production",
    }
    why = {
        "early": "Lost " + ", ".join(
            [f"a fight at {mmss(e['start'])} ({e['before']}->{e['after']})" for e in fights if e["start"] < EARLY]
            + [f"buildings at {mmss(e['start'])} (cap {e['before']}->{e['after']})"
               for e in f["buildings"] if e["start"] < EARLY]) + " before 6:00",
        "supply": f"{len(blocks_)} supply block{'s' if len(blocks_) != 1 else ''}, {secs(blocks_)} s blocked",
        "production": ", ".join(x for x in (
            f"{secs(f['idle'])} s idle" if f["idle"] else "",
            f"{len(fights)} lost fight{'s' if len(fights) != 1 else ''} to rebuild from" if fights else "",
            f"{-(late[0]['diff'] or 0)} behind plan at 8:00" if late and (late[0]["diff"] or 0) < 0 else "") if x),
        "gas": f"{secs(f['gas'])} s with gas over 400 and minerals under 200 (peak {peak_gas} gas)",
        "bank": f"{secs(f['bank'], 240)} s over 500 minerals after 4:00 (peak {f['bank_peak']})",
    }[key]
    second = max((k for k in FIXES if k != key), key=lambda k: scores[k])
    also: Also | None = ({"key": second, "title": FIXES[second], "action": actions[second]}
            if scores[second] > 0 and scores[second] >= scores[key] / 2 else None)
    return {"key": key, "title": FIXES[key], "action": actions[key], "why": why, "also": also, "scores": scores}


def findings_text(f: Findings) -> list[Finding]:
    """The hand-review lines, in the coach's order."""
    out: list[Finding] = []

    def add(kind: str, t: float, text: str) -> None:
        out.append({"kind": kind, "t": t, "text": text})

    for w in f["behind"]:
        d = f"{w['min']}" if w["min"] == w["max"] else f"{w['min']}-{w['max']}"
        add("behind", w["start"], f"Behind plan by {d} supply {rng(w['start'], w['end'])}")
    if f["bank"]:
        top = sorted(sorted(f["bank"], key=lambda w: -(w["end"] - w["start"]))[:4], key=lambda w: w["start"])
        more = f" (+{len(f['bank']) - len(top)} more)" if len(f["bank"]) > len(top) else ""
        add("bank", top[0]["start"], "Bank over 500 minerals " + ", ".join(
            f"{rng(w['start'], w['end'])} (peak {w['peak']})" for w in top) + more)
    for b in f["blocks"]:
        add("block", b["start"], f"Blocked {b['used']}/{b['cap']} at {rng(b['start'], b['end'])}")
    if f["gas"]:
        g = f["gas"]
        add("gas", g[0]["start"], f"Gas banked {min(w['low'] for w in g)}-{max(w['peak'] for w in g)} with minerals "
            f"under 200: {', '.join(rng(w['start'], w['end']) for w in g)} ({secs(g)} s)")
    for e in f["fights"]:
        add("fight", e["start"], f"Lost a fight {rng(e['start'], e['end'])} ({e['before']}->{e['after']})")
    for e in f["buildings"]:
        add("buildings", e["start"], f"Lost buildings {rng(e['start'], e['end'])} (cap {e['before']}->{e['after']})")
    for iw in f["idle"]:
        add("idle", iw["start"], f"Production idle at {iw['supply']} supply {rng(iw['start'], iw['end'])} "
            f"({iw['room']} free)")
    return out


def review(game: Row, samples: Iterable[Row], calls: Iterable[Row] = (), notes: Iterable[Row] = ()) -> Review:
    """One game's review as a JSON-ready dict (models.Review). game/samples/calls/notes: sqlite Rows or dicts
    (db.SCHEMA columns)."""
    rows: list[Row] = [dict(s) for s in samples]
    samples = rows
    checkpoints: list[Checkpoint] = []
    for t in CHECKS:
        at = supply_at(samples, t)
        if at:
            used, plan = at
            checkpoints.append({"t": t, "used": used, "plan": plan, "diff": None if plan is None else used - plan})
    mins = [s for s in rows if s["minerals"] is not None]
    gas = [s for s in rows if s["gas"] is not None]
    f: Findings = {"checkpoints": checkpoints, "behind": behind_windows(rows), "blocks": block_windows(rows),
                   "bank": bank_windows(rows), "gas": gas_windows(rows), "fights": lost_fights(rows),
                   "buildings": lost_buildings(rows), "idle": idle_windows(rows),
                   "bank_peak": max((s["minerals"] for s in mins), default=None),
                   "gas_peak": max((s["gas"] for s in gas), default=None)}
    kinds: dict[str, int] = {}
    for c in calls:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    hand = [n["text"] for n in notes if re.search(r"\breview\b", n["text"] or "", re.I)]
    g = dict(game)
    return {
        "id": g["id"], "player": g.get("player", OWNER), "result": g["result"], "length": g["length_sec"],
        "length_text": mmss(g["length_sec"]),
        "plan": g["plan"], "coach_plan": g["coach_plan"], "enemies": g["enemies"], "matchup": g.get("matchup"),
        "started_at": g["started_at"],
        **f,
        "findings": findings_text(f),
        "top_fix": top_fix(f, g["result"]),
        "calls": kinds,
        "hand_review": hand[-1] if hand else None,
        "series": [[s["t"], s["used"], s["plan_supply"], s["cap"]] for s in samples],
    }


def review_for(conn: sqlite3.Connection, which: int | str, player: str | None = None) -> Review | None:
    """which: a game id (int, any player's game) or 'latest' (the player's latest game; default Dickie's).
    None if there is no such game."""
    if which == "latest":
        cond, args = player_where(conn, player)
        row = conn.execute("SELECT max(id) FROM games" + (f" WHERE {cond}" if cond else ""), args).fetchone()
        latest = row[0] if row else None
        if latest is None:
            return None
        which = int(latest)

    g = conn.execute("SELECT * FROM games WHERE id = ?", (which,)).fetchone()
    if not g:
        return None
    def q(sql: str) -> list[Row]:
        return conn.execute(sql, (which,)).fetchall()

    return review(g, q("SELECT * FROM samples WHERE game_id = ? ORDER BY t"),
                  q("SELECT * FROM calls WHERE game_id = ? ORDER BY t, rowid"),
                  q("SELECT * FROM notes WHERE game_id = ? ORDER BY id"))


def review_route(p: str, db: str | Path, player: str | None = None) -> Route:
    m = re.fullmatch(r"/history/review/(\d{1,9}|latest)", p)
    if not m:
        return 400, json.dumps({"error": "game id must be a number or 'latest'"}), JSON, "no-store"
    which: int | str = m.group(1) if m.group(1) == "latest" else int(m.group(1))
    try:
        conn = connect(db)
    except sqlite3.Error as e:
        return 500, json.dumps({"error": f"database unavailable: {e}"}), JSON, "no-store"
    try:
        r = review_for(conn, which, player)
    except sqlite3.Error as e:
        return 500, json.dumps({"error": f"query failed: {e}"}), JSON, "no-store"
    finally:
        conn.close()
    if r is None:
        whose = f" for {player}" if which == "latest" and player else ""
        return 404, json.dumps({"error": f"no game {which}{whose}"}), JSON, "no-store"
    return 200, REVIEW.dump_json(r).decode("utf-8"), JSON, "no-store"


# ---------- routing (called from timing.py's do_GET) ----------

def route(path: str, db: str | Path = DB) -> Route:

    """-> (status, body, content type, cache-control) for a /history or /vendor/ path."""
    u = urlsplit(path)
    p, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
    if p.startswith("/vendor/"):
        name = p[len("/vendor/"):]
        if name not in VENDOR:
            return 404, "not found", "text/plain; charset=utf-8", "no-store"
        try:
            js = (HERE / "vendor" / name).read_text(encoding="utf-8")
        except OSError:   # allowlisted but not vendored on this machine (live/vendor is fetched separately)
            return 404, "not found", "text/plain; charset=utf-8", "no-store"
        return 200, js, "application/javascript", "max-age=86400"
    if p.startswith("/history/review/"):
        return review_route(p, db, q.get("player") or None)
    m = re.fullmatch(r"/history/game/(\d{1,9})", p)   # bounded: a huge id would overflow SQLite's integer
    if p not in ("/history", "/history/", "/history/games") and not m:
        return 404, '<p class="empty">Not found.</p>', HTML, "no-store"
    try:
        conn = connect(db)
    except sqlite3.Error as e:
        if p.startswith("/history/"):
            return 500, f'<p class="empty">Database unavailable: {esc(e)}</p>', HTML, "no-store"
        return 200, page(), HTML, "no-store"   # the shell still loads; the list shows the error
    try:
        if m:
            body = game_fragment(conn, int(m.group(1)))
            if body is None:
                return 404, f'<p class="empty">No game #{int(m.group(1))}.</p>', HTML, "no-store"
            return 200, body, HTML, "no-store"
        if p == "/history/games":
            return 200, games_fragment(conn, q.get("result"), q.get("plan") or None,
                                       player=q.get("player") or None), HTML, "no-store"
        return 200, page(conn), HTML, "no-store"
    except sqlite3.Error as e:
        return 500, f'<p class="empty">Query failed: {esc(e)}</p>', HTML, "no-store"
    finally:
        conn.close()
