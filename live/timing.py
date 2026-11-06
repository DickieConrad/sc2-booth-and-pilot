"""SC2 live timing helper (read-only: it never sends input to the game).

- Polls the SC2 client API (localhost:6119/game) once a second for displayTime and player races.
- At game start picks the plan from the enemy races (plans.json "matchups") and emits a push with plan + clock + run.
- OCRs the minerals / gas / supply panel (top right) and emits a short `say` when you are supply blocked,
  behind the plan's supply for this time, or banking. At most one say every 15 s.

Pushes are printed as `PUSH {...}` lines and saved to out/pending/<n>.json. A Claude Code session relays them to
the Live Coach page (ArtifactData), because the page's database is only writable through that tool:
    python timing.py --finalize out/pending/<n>.json   -> out/push.json with a fresh seq (and fresh clock)
Then: ArtifactData set, collection "coach", doc_id "live", file_path out/push.json.

Usage: python timing.py [--me NAME] [--dry-run]     (run via run.ps1)
"""
import argparse
import http.server
import json
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

import looptime
import models
from models import CLIP_RE, BotGameBody, CueTickResponse, GameInfo, NoteBody, PanelRead, SayBody, StateData
from player import OWNER
from pydantic import ValidationError

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
PENDING = OUT / "pending"
MISS = OUT / "miss"
API = "http://127.0.0.1:6119/game"

RACE = {"Terr": "T", "Prot": "P", "Zerg": "Z"}
SAY_GAP = 15          # seconds between pushed says
REPEAT_GAP = 45       # seconds before the same say may repeat
BANK_REPEAT_GAP = 25  # ... but a bank call repeats after 25 s while the bank stays at BANK_REPEAT_FROM or more
BANK_REPEAT_FROM = 600
TICK = 0.5            # seconds per loop: game clock + one screen read (a read takes ~3 ms)
CONFIRM = 2           # consecutive reads (so 1 s at TICK 0.5) before a condition counts
BEHIND_BY = 3         # supply below plan before "behind"
GRACE = 10            # seconds after a step's time before it counts as late
PANEL_STALE = 5.0     # seconds without an accepted read before the page/brain stop seeing the old panel

# Resource panel as fractions of the screen (measured at 2560x1440, standard UI):
# minerals icon starts ~76%, supply ends before the menu buttons at ~96%.
PANEL = (0.755, 0.008, 0.975, 0.042)   # 564 px: room for 1v1's +43 px shift and 3-digit supply


def mmss(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def now_ms():
    return int(time.time() * 1000)


# ---------- game clock ----------

_bad_game = 0   # SC2 API answers that didn't validate (logged on the first, then every 600th)


def fetch_game() -> GameInfo | None:
    """The client API's /game, validated (models.GAME): None when SC2 isn't answering or the answer is malformed."""
    global _bad_game
    try:
        with urllib.request.urlopen(API, timeout=2) as r:
            raw = r.read()
    except Exception:
        return None
    try:
        return models.GAME.validate_json(raw)
    except ValidationError as e:
        _bad_game += 1
        if _bad_game % 600 == 1:
            print(f"client API: unexpected /game ({models.errors(e)}); treated as no game [{_bad_game}x]", flush=True)
        return None


def enemy_key(players, me):
    """Enemy races as a matchups key (e.g. 'TP'), assuming lobby order 1-2 vs 3-4 in a 2v2."""
    names = [p["name"] for p in players]
    if me not in names:
        return None, []
    i = names.index(me)
    half = len(players) // 2 or 1
    mine = range(0, half) if i < half else range(half, len(players))
    enemies = [p for j, p in enumerate(players) if j not in mine]
    letters = sorted((RACE.get(p["race"], "?") for p in enemies), key="TPZ?".index)
    return "".join(letters), enemies   # 1v1: a single letter (Z, P, T or ? for Random)


def pick_plan(plans, key):
    picks = plans["matchups"].get(key or "")
    return picks[0] if picks else "A"


def family_plan(plans, race, solo, key):
    """The plan for this race and matchup: Protoss PT/PZ/PP (1v1) or PX; Terran 1v1 the Field Manual build for the
    matchup (MMM vs Random); Terran 2v2 the Live Coach pick."""
    if race == "P":   # playing Protoss: PT/PZ/PP by matchup in 1v1, the gate-expand plan PX otherwise
        return "P" + key if solo and "P" + (key or "") in plans["plans"] else "PX"
    if solo:   # 1v1: the Field Manual build for the matchup (MMM vs Random); the 2v2 Live Coach page sits out
        return key if key in plans["plans"] else "M"
    return pick_plan(plans, key)            # what the Live Coach page shows for this matchup


# ---------- sparring-bot games (bot/run.py --coach-test) ----------
# Before a coach-test game the bot writes out/bot_game.json (models.BotGame): its in-game name, race and its build
# as a plan. When Dickie isn't in the game and that player is, the helper follows the bot: its race (plan family and
# the -p calls), its result, its build as plan BOT (written to out/plans_bot.json, which coach-core and /plans load),
# no Live Coach pushes, and the game is recorded in coach.db under the bot's name (games.player).

BOT_GAME = OUT / "bot_game.json"
BOT_PLANS = OUT / "plans_bot.json"
BOT_PLAN = "BOT"
BOT_STALE = 600.0   # seconds: an older announcement was left behind by a run that crashed; ignored
_bad_bot = 0


def read_bot_game(path=BOT_GAME, now=None):
    """The announced bot (models.BotGame) if the file exists, is valid and fresh; else None."""
    global _bad_bot
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    try:
        bot = models.BOT_GAME.validate_json(raw)
    except ValidationError as e:
        _bad_bot += 1
        if _bad_bot % 20 == 1:
            print(f"bot game: bad {Path(path).name} ({models.errors(e)}); ignored", flush=True)
        return None
    if (time.time() if now is None else now) - bot["written_at"] > BOT_STALE:
        return None
    return bot


def follow(players, me, bot):
    """-> (name, bot): whose game to coach. Dickie when he's in it; else the announced bot when it is in the game
    (with its announcement); else Dickie's name with no bot (today's behaviour: nobody found, Terran defaults)."""
    names = [p["name"] for p in players]
    if me in names or not bot or bot["player"] not in names:
        return me, None
    return bot["player"], bot


def write_bot_plan(bot, path=BOT_PLANS):
    """Write the bot's build as plan BOT ({"plans": {"BOT": ...}}, atomic) for coach-core's /start and the page's
    /plans; return the plan (steps sorted by time), or None if the announcement has no plan."""
    p = bot.get("plan")
    if not p:
        return None
    plan = {"name": p["name"], "steps": sorted(p["steps"], key=lambda s: s["sec"])}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps({"plans": {BOT_PLAN: plan}}), encoding="utf-8")
    tmp.replace(path)
    return plan


# ---------- OCR ----------

def panel_box(w, h):
    x0, y0, x1, y1 = PANEL
    return int(w * x0), int(h * y0), int(w * x1), int(h * y1)


# Text spans inside the panel crop (fraction of crop width), icons excluded: minerals, gas, supply.
COLUMNS = ((0.105, 0.39), (0.41, 0.68), (0.722, 0.99))
GLYPHS = HERE / "glyphs.json"   # digit templates, built with --learn from labeled panel screenshots
GRID = 14                       # glyphs are normalised to GRID x GRID
MIN_SCORE = 0.80                # pixel agreement needed to accept a glyph


def _ink(p):
    r, g, b = p[:3]
    return min(r, g, b) > 140 or (r > 170 and g < 90 and b < 90)   # white HUD text, or red (supply blocked)


def segment(img):
    """Split one column crop into glyph bitmaps (left to right), each a GRID*GRID string of 0/1."""
    w, h = img.size
    px = img.load()
    ink = [[_ink(px[x, y]) for x in range(w)] for y in range(h)]
    # Connected components (8-neighbour): each digit of the HUD font is one stroke; "5/" can touch columns.
    seen = [[False] * w for _ in range(h)]
    comps = []
    for sy in range(h):
        for sx in range(w):
            if not ink[sy][sx] or seen[sy][sx]:
                continue
            stack, pts = [(sx, sy)], []
            seen[sy][sx] = True
            while stack:
                x, y = stack.pop()
                pts.append((x, y))
                for nx in (x - 1, x, x + 1):
                    for ny in (y - 1, y, y + 1):
                        if 0 <= nx < w and 0 <= ny < h and ink[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            stack.append((nx, ny))
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            comps.append((min(xs), max(xs) + 1, min(ys), max(ys) + 1, set(pts)))
    wide = [c for c in comps if c[1] - c[0] >= 3 and c[3] - c[2] >= 6]
    if not wide:
        return []
    # Text band = the top/bottom most shapes share (digits all align); bright terrain or units behind
    # the panel make odd-sized blobs that rarely line up with anything.
    def peers(c):
        return sum(abs(o[2] - c[2]) <= 1 and abs(o[3] - c[3]) <= 1 for o in wide)
    _, _, by0, by1, _ = max(wide, key=lambda c: (peers(c), c[3] - c[2]))
    band = by1 - by0
    out = []
    for x0, x1, y0, y1, pts in sorted(comps):
        gh, gw = y1 - y0, x1 - x0
        if (gw < 2 or gw > 1.3 * band or gh > 1.6 * band or gh < 0.6 * band
                or min(y1, by1) - max(y0, by0) < 0.6 * min(gh, band)):
            continue
        scale = GRID / gh
        nw = max(1, min(GRID, round(gw * scale)))
        off = (GRID - nw) // 2
        bits = ["0"] * (GRID * GRID)
        for gy in range(GRID):
            for gx in range(nw):
                if (x0 + min(gw - 1, int(gx / scale)), y0 + int(gy / scale)) in pts:
                    bits[gy * GRID + off + gx] = "1"
        out.append("".join(bits))
    return out


def _columns(img):
    return [img.crop((int(img.width * a), 0, int(img.width * b), img.height)) for a, b in COLUMNS]


_templates = None


def classify(bits):
    global _templates
    if _templates is None:
        _templates = json.loads(GLYPHS.read_text(encoding="utf-8")) if GLYPHS.exists() else {}
    best, score = "?", 0.0
    for ch, samples in _templates.items():
        for s in samples:
            sc = sum(a == b for a, b in zip(bits, s)) / len(s)
            if sc > score:
                best, score = ch, sc
    return best if score >= MIN_SCORE else "?"


def ocr_text(img):
    """Read the panel crop as 'minerals | gas | supply'."""
    return " | ".join("".join(classify(b) for b in segment(c)) for c in _columns(img.convert("RGB")))


def learn(pairs):
    """pairs: [(panel image path, '1015|253|35/46'), ...] -> add templates to glyphs.json."""
    from PIL import Image
    db = json.loads(GLYPHS.read_text(encoding="utf-8")) if GLYPHS.exists() else {}
    for path, label in pairs:
        img = Image.open(path).convert("RGB")
        if img.width > 1000:                         # a full screenshot: cut the panel out first
            img = img.crop(panel_box(img.width, img.height))
        for col, text in zip(_columns(img), label.split("|")):
            gl = segment(col)
            if len(gl) != len(text):
                print(f"skip {path} '{text}': found {len(gl)} glyphs")
                continue
            for ch, bits in zip(text, gl):
                if bits not in db.setdefault(ch, []):
                    db[ch].append(bits)
    GLYPHS.write_text(json.dumps(db), encoding="utf-8")
    print("templates:", {k: len(v) for k, v in sorted(db.items())})


def parse_panel(text):
    """'455 | 40 | 19/23' -> dict(minerals, gas, used, cap); None if it doesn't look like the panel."""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 3 or "?" in text:
        return None
    m, gas = parts[:2]
    if not (m.isdigit() and gas.isdigit()):
        return None
    sup = re.fullmatch(r"(\d{1,3})/(\d{1,3})", parts[2])
    if not sup:
        return None
    used, cap = int(sup.group(1)), int(sup.group(2))
    if used > 200 or not 14 <= cap <= 200:
        return None
    return {"minerals": int(m), "gas": int(gas), "used": used, "cap": cap, "raw": text}


class PanelReader:
    """Grabs the resource panel. DXGI desktop duplication (dxcam) first: GDI capture (mss) returns
    black frames from SC2 about half the time. dxcam returns None when the screen hasn't changed,
    so the last frame is reused then.
    The crop follows the SC2 window's client area (sc2window; bot games run windowed), the fixed full-screen
    panel_box when no window is found. Fullscreen gives the same crop as the fixed region."""

    def __init__(self):
        import sc2window
        self.cam, self.prev = None, None
        try:
            import dxcam
            self.cam = dxcam.create(output_idx=0, output_color="BGRA")   # primary output; BGRA needs no OpenCV
            self.window = sc2window.WindowTracker((self.cam.width, self.cam.height), PANEL)
            self.region = self.window.box()
            print(f"capture: dxcam {self.cam.width}x{self.cam.height}", flush=True)
        except Exception as e:
            import mss
            print(f"capture: mss (dxcam unavailable: {e})", flush=True)
            self.sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
            self.mon = self.sct.monitors[1]  # primary monitor
            self.window = sc2window.WindowTracker((self.mon["width"], self.mon["height"]), PANEL)

    def grab(self):
        from PIL import Image
        if self.cam:
            region = self.window.box()
            if region != self.region:     # the window moved: the cached frame is of the old spot
                self.region, self.prev = region, None
            frame = self.cam.grab(region=self.region)
            if frame is not None:
                self.prev = Image.fromarray(frame[..., 2::-1].copy())   # BGRA -> RGB
            if self.prev is not None:
                return self.prev
            x0, y0, x1, y1 = self.region
            return Image.new("RGB", (x1 - x0, y1 - y0))
        x0, y0, x1, y1 = self.window.box()
        box = {"left": self.mon["left"] + x0, "top": self.mon["top"] + y0, "width": x1 - x0, "height": y1 - y0}
        shot = self.sct.grab(box)
        return Image.frombytes("RGB", shot.size, shot.rgb)

    RUST = ("127.0.0.1", 8766)   # rust/panel-reader serve: same answers as panel_ocr, ~1 ms vs 26 ms (p95 5 vs 207)

    def read(self):
        """Read the panel: the Rust service first (one kept-open connection, raw RGB), panel_ocr in-process if
        the service is down. Both give identical results on all labeled crops."""
        import panel_ocr
        self.last = TRACE.span("cap", self.grab)
        if time.time() >= getattr(self, "rust_retry", 0):
            try:
                return TRACE.span("read", self._read_rust, self.last)
            except Exception as e:
                self.rust_conn, self.rust_retry = None, time.time() + 30
                if getattr(self, "source", None) != "python":
                    print(f"reader: python (rust service unavailable: {type(e).__name__})", flush=True)
                self.source = "python"
        return TRACE.span("read_py", lambda: panel_ocr.parse_panel(panel_ocr.ocr_text(self.last)))

    def _read_rust(self, img) -> PanelRead | None:
        """POST the crop to rust/panel-reader; its answer is validated (models.PANEL_READ, strict): a malformed one
        raises, and read() falls back to the Python reader like for a dead service."""
        from rules_client import OneSendConnection
        if getattr(self, "rust_conn", None) is None:
            self.rust_conn = OneSendConnection(*self.RUST, timeout=0.5)
        self.rust_conn.request("POST", f"/read?w={img.width}&h={img.height}", img.tobytes(),
                               {"Content-Type": "application/octet-stream"})
        resp = self.rust_conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {body[:80]!r}")
        if getattr(self, "source", None) != "rust":
            print("reader: rust service", flush=True)
            self.source = "rust"
        return models.PANEL_READ.validate_json(body, strict=True)

    def save_miss(self, t, why):
        """Keep the crop of a failed read (out/miss/) so it can be labeled and learned with --learn."""
        MISS.mkdir(parents=True, exist_ok=True)
        self.misses = getattr(self, "misses", 0) + 1
        if self.misses % 50 == 1:   # keep the newest ~300 (the latest failures are the ones to label); prune rarely
            old = sorted(MISS.glob("*.png"), key=lambda f: f.stat().st_mtime)
            for f in old[:max(0, len(old) - 250)]:
                f.unlink(missing_ok=True)
        self.last.save(MISS / f"{int(t):04d}-{why}.png")


# ---------- cue engine (rust/coach-core on :8767) ----------

class CueEngine:
    """Client for the Rust cue engine: target cues, "now" catch-up cues, lateness and the half-shift.
    If it's down, the page falls back to its own clock-only cues (state.cue_engine = False)."""
    ADDR = ("127.0.0.1", 8767)

    def __init__(self):
        self.conn, self.retry, self.up = None, 0.0, None
        self.plan, self.active = None, False   # active: coach-core accepted /start for this game's plan

    def _post(self, path: str, body: dict[str, object]) -> bytes:
        from rules_client import OneSendConnection
        if self.conn is None:
            self.conn = OneSendConnection(*self.ADDR, timeout=0.3)
        self.conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        resp = self.conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {data[:80]!r}")
        return data

    def _call(self, path, body, parse):
        """POST and validate (strict: coach-core's serde writes exact types); any failure -> None + retry later."""
        if time.time() < self.retry:
            return None
        try:
            out = parse(self._post(path, body), strict=True)
            if self.up is not True:
                print("cues: rust coach-core", flush=True)
            self.up = True
            return out
        except Exception as e:
            self.conn, self.retry = None, time.time() + 30
            if self.up is not False:
                print(f"cues: page fallback (coach-core unavailable: {type(e).__name__})", flush=True)
            self.up = False
            return None

    def start(self, plan: str) -> bool:
        self.plan = plan
        self.active = self._call("/start", {"plan": plan}, models.CUE_START.validate_json) is not None
        return self.active

    def tick(self, t: float, used: int | None) -> CueTickResponse | None:
        """No ticks until coach-core has taken this game's plan: an older coach-core that doesn't know a plan (BOT)
        answers /start with a 404 but would still tick the previous game's engine. /start is retried with the retry."""
        if not self.active and (self.plan is None or time.time() < self.retry or not self.start(self.plan)):
            return None
        return self._call("/tick", {"t": t, "used": used}, models.CUE_TICK.validate_json)


# ---------- coaching rules ----------

def plan_status(steps, t):
    """(expected supply, the step that set it, next step) at game time t."""
    done = [s for s in steps if s["sec"] <= t - GRACE]
    nxt = next((s for s in steps if s["sec"] > t - GRACE), None)
    return (done[-1]["supply"] if done else None), (done[-1] if done else None), nxt


def plausible(prev, cur, dt):
    """Reject misreads: income can't add more than ~60 minerals / 20 gas a second (plus a margin)."""
    return (cur["minerals"] - prev["minerals"] <= 150 + 60 * dt
            and cur["gas"] - prev["gas"] <= 100 + 20 * dt)


def judge(panel, t, steps, race="T"):
    """Return (kind, text) for the most important problem, or None.
    Protoss calls get their own kinds ("-p"): the recorded clips say "depots" and "SCVs"."""
    m, used, cap = panel["minerals"], panel["used"], panel["cap"]
    p = race == "P"
    if t >= 45 and cap < 200 and used >= cap:   # an on-time 0:17 depot finishes ~0:38; 15/15 before that is normal
        return (("blocked-p", f"Supply blocked {used}/{cap}: pylon now") if p
                else ("blocked", f"Supply blocked {used}/{cap}: depot now"))
    exp, step, _ = plan_status(steps, t)
    behind = exp is not None and t <= steps[-1]["sec"] + 30 and used < exp - BEHIND_BY
    bank = 400 if t < 240 else 600
    if any(("Command Center" in s["action"] or "Nexus" in s["action"]) and -15 <= s["sec"] - t <= 20 for s in steps):
        bank += 400   # saving for a planned CC is not banking
    if t >= 60 and m >= bank:   # behind with money in the bank: spending is the fix, so say that
        return "bank", f"Bank {m // 50 * 50}, spend" + (f": supply {used} vs plan {exp}" if behind else "")
    if behind:
        return (("behind-p" if p else "behind"),
                f"Behind: {used} supply, plan {exp}: {'Probes' if p else 'SCVs'} + {step['action']}")
    return None


def repeat_gap(kind, minerals):
    """Seconds before `kind` may be said again: REPEAT_GAP, or BANK_REPEAT_GAP for a bank call while the bank is
    still BANK_REPEAT_FROM+ (sitting on 800-1600 for minutes got one call every 45 s; it should nag). Same as
    coach-core helper::repeat_gap."""
    return BANK_REPEAT_GAP if kind == "bank" and minerals >= BANK_REPEAT_FROM else REPEAT_GAP


# ---------- pushes ----------

class Pusher:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.dir = OUT / "dry" if dry_run else PENDING   # a dry run beside the live helper must not touch its queue
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = max([int(p.stem) for p in self.dir.glob("*.json")] + [0])
        self.last_say = 0.0
        self.recent = {}

    def push(self, data, clock_at=None):
        data = {"seq": now_ms(), **data}
        if clock_at is not None:  # lets --finalize roll the clock forward to the relay moment
            data["_clock_sec"], data["_clock_wall"] = clock_at, time.time()
        self.n += 1
        path = self.dir / f"{self.n}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        shown = {k: v for k, v in data.items() if not k.startswith("_")}
        print(("DRY " if self.dry_run else "PUSH ") + json.dumps(shown) + f"  file={path}", flush=True)

    def say(self, text, kind, clock, relay=False, gap=REPEAT_GAP):
        """Show (and speak) on the local page at once; relay to the Live Coach page only if asked.
        gap: seconds since the same kind (repeat_gap(); default REPEAT_GAP)."""
        wall = time.time()
        if wall - self.last_say < SAY_GAP or wall - self.recent.get(kind, 0) < gap:
            return False
        self.last_say = self.recent[kind] = wall
        STATE.add_say(text, kind, clock)
        print(f"SAY [{mmss(clock)}] {text}", flush=True)
        if relay:
            self.push({"say": text})
        return True


# ---------- local page (http://127.0.0.1:8765) ----------

class State:
    """What the local page shows; the helper writes it, the page and the coach read it via /state or /events.
    Every write bumps `version` and wakes /events streams, so the page hears about a call at once."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self.data: StateData = {   # the /state JSON (models.StateData; watch.py and brain_run.py validate it)
            "in_game": False, "clock": 0.0, "clock_wall": time.time(), "plan": None, "plan_name": "",
            "coach_plan": None, "cue_engine": False, "cues": [], "cue_seq": 0, "lateness": 0.0, "shift": 0,
            "player": "", "race": "T", "vs": "", "key": "", "panel": None, "plan_supply": None, "next": [], "says": [],
            "seq": 0}


    def _touch(self):
        self.version += 1
        self.changed.notify_all()

    def update(self, **kw):
        with self.lock:
            self.data.update(kw)
            self._touch()

    def add_say(self, text, kind, clock, source="helper", audio=None):
        with self.lock:
            self.data["seq"] += 1
            say = {"id": self.data["seq"], "t": mmss(clock), "text": text, "kind": kind, "from": source,
                   "wall": time.time()}
            if audio:   # a pre-rendered clip in live/voice/clips (the coach's own voice, coach_say.py)
                say["audio"] = audio
            self.data["says"] = (self.data["says"] + [say])[-30:]
            self._touch()
            TRACE.call(say, self.version)
        if REC:
            TRACE.span("db", REC.call, clock, source, kind, text)

    def add_cues(self, events, lateness, shift):
        """Cue events from coach-core ({type: target|now, keys, sec}); the page plays them as clips."""
        with self.lock:
            for ev in events:
                self.data["cue_seq"] += 1
                self.data["cues"] = (self.data["cues"] + [{**ev, "id": self.data["cue_seq"]}])[-20:]
            self.data.update(cue_engine=True, lateness=round(lateness, 1), shift=shift)
            if events:
                self._touch()
                TRACE.cues(self.data["cues"][-len(events):], self.version, self.data["clock"], shift)

    def json(self):
        with self.lock:
            return json.dumps({**self.data, "now": time.time()})

    def wait(self, seen, timeout=2.0):
        """Block until the state is newer than `seen` (or timeout); return (version, json)."""
        with self.lock:
            if self.version == seen:
                self.changed.wait(timeout)
            return self.version, self.json()


STATE = State()
TRACE = looptime.tracer("helper")   # off unless SC2_TIMING_TRACE is set (live/looptime.py)
# CLIP_RE (models.py): what /voice/ serves (and what a say's `audio` may name)
REC = None   # db.Recorder once run() starts; the helper is the database's only writer
WEB_DIST = HERE / "web" / "dist"   # the Angular page's build (live/web: npm run build), served at /next/
WEB_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css",
             ".json": "application/json", ".ico": "image/x-icon", ".svg": "image/svg+xml", ".png": "image/png",
             ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8", ".map": "application/json"}


def web_route(path, root=None):
    """-> (status, bytes, content type, cache-control) for a /next/ path: static files from the Angular build.
    Path-safe (no .., no dotfiles, must stay inside root); unknown types are refused, not guessed (Windows'
    mimetypes registry can say text/plain for .js)."""
    p = path.split("?", 1)[0].split("#", 1)[0]
    rel = p[len("/next/"):] if p.startswith("/next/") else ""
    rel = rel or "index.html"
    parts = rel.split("/")
    ctype = WEB_TYPES.get(Path(rel).suffix.lower())
    text = "text/plain; charset=utf-8"
    if not ctype or any(x in ("", ".", "..") or x.startswith(".") or "\\" in x or ":" in x for x in parts):
        return 404, b"not found", text, "no-store"
    base = Path(root or WEB_DIST).resolve()
    f = (base / rel).resolve()
    if not f.is_relative_to(base) or not f.is_file():
        if rel == "index.html":
            return 404, b"Angular page not built: cd live/web && npm ci && npm run build", text, "no-store"
        return 404, b"not found", text, "no-store"
    # index.html names the hashed bundles: never cache it; the bundles themselves never change
    return 200, f.read_bytes(), ctype, "no-store" if rel == "index.html" else "max-age=31536000, immutable"


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "overlay.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
        elif self.path == "/state":
            self._send(200, STATE.json())
        elif self.path == "/events":   # Server-Sent Events: the page gets each change as it happens
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = -1
            try:
                while True:
                    prev, (seen, body) = seen, STATE.wait(seen)
                    self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    TRACE.sse_sent(prev, seen, id(self) % 1000)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return
        elif self.path == "/plans":
            self._send(200, json.dumps(load_plans()))
        elif self.path == "/changelog":   # release notes for the page's "Release notes" button
            f = HERE / "CHANGELOG.md"
            self._send(200, f.read_text(encoding="utf-8") if f.exists() else "", "text/markdown; charset=utf-8")
        elif self.path.startswith("/voice/"):   # the pilot's pre-rendered clips (live/voice/clips)
            name = self.path[len("/voice/"):]
            f = HERE / "voice" / "clips" / name
            if not re.fullmatch(CLIP_RE, name) or not f.exists():
                return self._send(404, '{"error": "no such clip"}')
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav" if name.endswith(".wav") else "application/json")
            self.send_header("Cache-Control", "max-age=3600" if name.endswith(".wav") else "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith(("/history", "/vendor/")):   # game history dashboard, read-only (history.py)
            import history
            self._send(*history.route(self.path))
        elif self.path == "/next" or self.path.startswith("/next/"):   # the Angular page (live/web), opt-in
            if self.path == "/next":
                self.send_response(301)
                self.send_header("Location", "/next/")
                self.end_headers()
                return
            code, data, ctype, cache = web_route(self.path)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(data)
        elif self.path.split("?", 1)[0] == "/notes":   # the coach's latest pointers ("Coach pointers" panel)
            TRACE.page_report(self.path)   # tracing on: the page's own delays ride along on this poll
            rows = REC.recent_notes(8) if REC else []
            self._send(200, json.dumps(rows))
        else:
            self._send(404, '{"error": "not found"}')

    def do_POST(self):
        """POST /say {"text", "audio"?, "kind"?}: the coach (or anything local) puts a line on the page.
        POST /note {"text", "game_id"?}: a coach note into the database.
        Bodies are validated (models.SayBody / NoteBody): a bad one, or an unknown field, is a 400 that says why."""
        if self.path not in ("/say", "/note", "/botgame"):
            return self._send(404, '{"error": "not found"}')
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"
        if self.path == "/botgame":  # bot\run.py: one game + the bot version (the helper is the DB's one writer)
            try:
                gid = REC.bot_game(BotGameBody.model_validate_json(raw).model_dump())
            except ValidationError as e:
                return self._send(400, json.dumps({"error": models.errors(e)}))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps({"ok": True, "id": gid}))
        if self.path == "/note":
            try:
                note = NoteBody.model_validate_json(raw)
                nid = REC.note(note.text, note.game_id)
            except ValidationError as e:
                return self._send(400, json.dumps({"error": models.errors(e)}))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps({"ok": True, "note_id": nid}))
        try:
            say = SayBody.model_validate_json(raw)
        except ValidationError as e:
            bad_audio = any(err["loc"] == ("audio",) for err in e.errors())
            error = "bad audio name" if bad_audio else 'send {"text": "..."}'
            return self._send(400, json.dumps({"error": error, "detail": models.errors(e)}))
        with STATE.lock:
            clock = STATE.data["clock"]
        STATE.add_say(say.text, say.kind, clock, source="coach", audio=say.audio)
        print(f"COACH {say.text}", flush=True)
        self._send(200, '{"ok": true}')


    def log_message(self, *args):
        pass


def serve(port):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"local page: http://127.0.0.1:{port}/", flush=True)


def finalize(path):
    """Write out/push.json from a pending push: fresh seq, clock rolled forward to now (live API if available)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["seq"] = now_ms()
    if "_clock_sec" in data:
        g = fetch_game()
        if g and g.get("players") and any(p["result"] == "Undecided" for p in g["players"]):
            data["clock"] = mmss(g["displayTime"])
        else:
            data["clock"] = mmss(data["_clock_sec"] + time.time() - data["_clock_wall"])
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    (OUT / "push.json").write_text(json.dumps(data), encoding="utf-8")
    print(json.dumps(data))


# ---------- main loop ----------

def load_plans():
    """plans.json (from the Live Coach page) plus local-only plans in plans_custom.json (e.g. M = MMM)."""
    plans = json.loads((HERE / "plans.json").read_text(encoding="utf-8"))
    # M = MMM; Z/P/T = Field Manual 1v1 builds; BOT = the last announced sparring bot's build (out/, not in git)
    for f in (HERE / "plans_custom.json", HERE / "plans_1v1.json", BOT_PLANS):
        if f.exists():
            try:
                plans["plans"].update(json.loads(f.read_text(encoding="utf-8"))["plans"])
            except (OSError, ValueError, KeyError):
                if f != BOT_PLANS:   # the generated bot plan may be mid-write or stale: skip it, never the real ones
                    raise
    focus = HERE / "focus.json"   # the coach's current fixes, shown on the local page
    plans["focus"] = json.loads(focus.read_text(encoding="utf-8"))["focus"] if focus.exists() else []
    return plans


def run(me, dry_run, seconds=None, port=8765, relay_says=False, force_plan=None):
    stop_at = time.time() + seconds if seconds else None
    plans = load_plans()
    pusher = Pusher(dry_run)
    reader = None
    in_game, last_t, plan, steps, panel, solo, race = False, -1.0, None, [], None, False, "T"
    who, bot = me, None           # the player followed this game (Dickie or an announced sparring bot)
    good, good_at = None, 0.0     # last parsed panel read and when
    shown_at = 0.0                # when /state last got an accepted panel
    hits, last_ocr, last_log = {}, 0.0, 0.0
    last_sample = 0.0
    global REC
    if not dry_run:
        import db
        REC = db.Recorder()   # one database for every player (games.player); migrates an older coach.db
    cue = CueEngine()
    import rules_client
    rules = rules_client.RulesClient("helper")   # SC2_RULES=rust: calls from coach-core, Python judge() as fallback
    serve(port)
    if TRACE.on:
        STATE.update(trace=True)   # the page reports its receipt / audio-start times on its /notes poll
        print(f"trace: on -> {TRACE.path}", flush=True)
    print(f"timing helper up: watching {API} as {me} (read-only){' [dry run]' if dry_run else ''}", flush=True)

    while not stop_at or time.time() < stop_at:
        tick = time.time()
        TRACE.begin_tick()
        g = TRACE.span("api", fetch_game)
        live = bool(g and g.get("players") and not g.get("isReplay")
                    and any(p["result"] == "Undecided" for p in g["players"]))
        t = g["displayTime"] if live else 0.0

        if live and (not in_game or t < last_t - 5):          # game start (or a new game we missed the end of)
            in_game, hits = True, {}
            rules.new_game()
            names = [p["name"] for p in g["players"]]
            who, bot = follow(g["players"], me, read_bot_game() if me not in names else None)
            key, enemies = enemy_key(g["players"], who)
            solo = len(enemies) == 1
            mine = [p for p in g["players"] if p["name"] == who]
            race = RACE.get(mine[0]["race"]) if mine else None
            if bot and not race:   # the API said Random (or nothing): the bot knows its own race
                race = RACE.get(bot["race"][:4].capitalize())
            race = race or "T"
            coach_plan = family_plan(plans, race, solo, key)
            bot_plan = write_bot_plan(bot) if bot else None
            if bot_plan:   # the bot's own build is the step plan (and the "Live Coach" plan: nothing is relayed)
                plans["plans"][BOT_PLAN] = bot_plan
                coach_plan = BOT_PLAN
            plan = BOT_PLAN if bot_plan else force_plan or coach_plan
            steps = plans["plans"][plan]["steps"]
            good, good_at = None, 0.0
            vs = " + ".join(f"{p['name']} ({p['race']})" for p in enemies) or "?"
            name = plans["plans"][plan]["name"]
            following = f" following {who} ({race})" if bot else ""
            print(f"GAME {mmss(t)}{following} vs {vs} -> key {key}, plan {plan} {name}", flush=True)
            if REC:   # a game nobody known is in (no Dickie, no announced bot) is kept out of Dickie's history
                allies = [p for p in g["players"] if p not in enemies and p["name"] != who]
                REC.start_game(player=who if mine else "unknown", me=who, ally=", ".join(p["name"] for p in allies),
                               ally_race=", ".join(p["race"] for p in allies), enemies=vs, matchup=key,
                               plan=plan, coach_plan=coach_plan, result="Undecided")
            STATE.update(plan=plan, plan_name=name, coach_plan=coach_plan, vs=vs, key=key or "?", panel=None, says=[],
                         player=who, race=race)
            versus = f" vs {key}" if key else ""   # no key: opponents unknown
            line = f"Plan {plan}, {name}{versus}."
            if bot:   # the bot plan's name starts with the bot's name ("SixRax: 6-Barracks Marine all-in ...")
                line = f"Following {name}{versus}." if bot_plan else f"Following {who}. {line}"
            STATE.add_say(line, "plan", t)
            if bot:   # a sparring-bot game is never relayed to the Live Coach page: no pushes, no pending files
                pass
            elif solo or race == "P":   # the Live Coach page has no Protoss-player plans
                pass
            elif plan == coach_plan:
                pusher.push({"plan": plan, "clock": mmss(t), "run": True,
                             "say": f"Plan {plan}, {name}{versus}."}, clock_at=t)
            else:  # local-only build (e.g. MMM): the Live Coach page shows the matchup plan alongside it
                pusher.push({"plan": coach_plan, "clock": mmss(t), "run": True,
                             "say": f"{name} this game{versus}. Plan {coach_plan} for reference."}, clock_at=t)
            reader = reader or PanelReader()
            STATE.update(cues=[], lateness=0.0, shift=0, cue_engine=cue.start(plan))
        elif in_game and not live:                            # game over / left
            in_game = False
            mine = [p for p in (g or {}).get("players", []) if p["name"] == who]
            result = mine[0]["result"] if mine else "Unknown"
            length = g["displayTime"] if g and g.get("players") else last_t   # final clock, not the last poll
            print(f"GAME OVER {result} at {mmss(length)}" + (f" ({who})" if bot else ""), flush=True)
            if REC:
                REC.end_game(result, length)
            if not solo and not bot:   # bot games were never on the Live Coach page
                pusher.push({"run": False})
        last_t = t
        if in_game:
            exp, _, _ = plan_status(steps, t)
            upcoming = [s for s in steps if s["sec"] > t - GRACE][:4]
            TRACE.span("page", STATE.update, in_game=True, clock=t, clock_wall=time.time(), plan_supply=exp,
                       next=upcoming)
        else:
            STATE.update(in_game=False)

        if in_game and tick - last_ocr >= TICK - 0.05:
            panel = reader.read()
            # Compare with the read just before (accepted or not): a one-off misread disagrees with its
            # neighbours and is dropped, and one bad read can't lock out all the good ones after it.
            prev, prev_at = good, good_at
            if panel:
                good, good_at = panel, tick
            if panel and prev and not plausible(prev, panel, tick - prev_at):
                print(f"[{mmss(t)}] rejected read {panel['raw']!r} (previous {prev['minerals']}/{prev['gas']})",
                      flush=True)
                reader.save_miss(t, "rejected")
                panel = None
            elif panel is None:
                reader.save_miss(t, "unread")
            # None: flag off or coach-core down -> judge() below. Timed as "rules" either way (Rust or Python).
            rust = TRACE.span("rules", rules.helper, t, panel, plan, race)
            if panel is None:
                hits = {}
                TRACE.seen(None)
                # Reads failing (camera over a bright wall in a bot game): an old panel would keep the brain and
                # watcher judging a frozen frame ("Supply stuck at 16"), so after a few seconds show none.
                if STATE.data["panel"] is not None and tick - shown_at > PANEL_STALE:
                    STATE.update(panel=None)
            else:
                shown_at = tick
                TRACE.span("page", STATE.update, panel={k: v for k, v in panel.items() if k != "raw"})
                if REC and t >= 3 and tick - last_sample >= 5:   # the first frames can still show the last game
                    last_sample = tick
                    TRACE.span("db", REC.sample, t, panel, plan_status(steps, t)[0])
                verdict = TRACE.span("rules", judge, panel, t, steps, race) if rust is None else None
                kind = verdict[0] if verdict else None
                rust_call = rust.get("helper") if rust else None
                # Rust mode only returns the confirmed call: its kind is what this read saw (trace "seen" times)
                TRACE.seen(rust_call["kind"] if rust_call else kind)
                hits = {kind: hits.get(kind, 0) + 1} if kind else {}
                if kind and hits[kind] >= CONFIRM and pusher.say(verdict[1], kind, t, relay_says and not bot,
                                                                 repeat_gap(kind, panel["minerals"])):
                    hits = {}
                if rust_call:   # confirmed + gated in coach-core; same kind and text as judge()
                    pusher.say(rust_call["text"], rust_call["kind"], t, relay_says and not bot,
                               repeat_gap(rust_call["kind"], panel["minerals"]))
            # Cue engine: clock + supply (None when unread) every tick -> target / "now" cues for the page.
            res = TRACE.span("cue", cue.tick, t, panel["used"] if panel else None)
            if res:
                TRACE.span("page", STATE.add_cues, res.get("events", []), res.get("lateness", 0.0), res.get("shift", 0))
            elif STATE.data["cue_engine"]:
                STATE.update(cue_engine=False)
            if tick - last_log >= 10:
                last_log = tick
                exp, _, nxt = plan_status(steps, t)
                res = (f"min {panel['minerals']} gas {panel['gas']} supply {panel['used']}/{panel['cap']}"
                       if panel else "panel unreadable")
                print(f"[{mmss(t)}] plan {plan} | {res} | plan supply {exp} | next "
                      f"{nxt['t'] + ' ' + nxt['action'] if nxt else '-'}", flush=True)

        TRACE.end_tick(t=round(t, 1), in_game=in_game)
        time.sleep(max(0.0, TICK - (time.time() - tick)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--me", default=OWNER,
                    help="your in-game name (to find the enemy team); default: live/player.json")
    ap.add_argument("--dry-run", action="store_true", help="print pushes as DRY lines (no relay expected)")
    ap.add_argument("--seconds", type=float, help="stop after this many seconds (for tests)")
    ap.add_argument("--port", type=int, default=8765, help="local page port (127.0.0.1 only)")
    ap.add_argument("--plan", help="always use this plan (e.g. M for MMM) instead of picking from the enemy races")
    ap.add_argument("--relay-says", action="store_true",
                    help="also send behind/bank/blocked says to the Live Coach page (slow: relayed by Claude)")
    ap.add_argument("--finalize", metavar="PENDING_JSON", help="prepare out/push.json for relay and exit")
    ap.add_argument("--learn", nargs=2, action="append", metavar=("IMAGE", "LABEL"),
                    help="add digit templates from a labeled panel/screenshot, e.g. out/panel1.png '1015|253|35/46'")
    a = ap.parse_args()
    if a.learn:
        learn(a.learn)
        sys.exit(0)
    if a.finalize:
        finalize(a.finalize)
        sys.exit(0)
    try:
        run(a.me, a.dry_run, a.seconds, a.port, a.relay_says, a.plan)
    except KeyboardInterrupt:
        pass
