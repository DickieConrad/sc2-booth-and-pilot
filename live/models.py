"""Typed shapes of the JSON that crosses a process or HTTP boundary, and the pydantic v2 validators for them.

Inside a process the data stays plain dicts typed as TypedDicts (checked by mypy, zero cost at run time). At the
boundary, a TypeAdapter (hot path: JSON parsed and checked in one pass by pydantic-core) or a BaseModel (request
bodies) validates what comes in; a bad payload raises pydantic.ValidationError, which every caller catches and logs
or answers with a 400, so a tick never crashes on it.

  boundary                                         shape                   validated by        extra fields
  SC2 client API  GET :6119/game                   GameInfo                GAME (lax)          ignored
  panel-reader    POST :8766/read -> panel|null    PanelRead               PANEL_READ (strict) ignored
  coach-core      POST :8767/start, /tick (cues)   CueStart, CueTick       CUE_* (strict)      ignored
  coach-core      POST :8767/rules/tick            RulesTickRequest ->     RULES_TICK (strict) ignored
                                                   RulesTickResponse
  helper          GET :8765/state, /events         StateData               STATE (lax)         ignored
  helper          POST :8765/say, /note            SayBody, NoteBody       BaseModel           FORBIDDEN (400)
  helper          GET /history/review/<id>         Review                  REVIEW (serialize)  -
  bot/run.py      live/out/bot_game.json           BotGame                 BOT_GAME (lax)      ignored
  booth brain     its calls (Python or Rust)       BrainCall               in RULES_TICK       ignored

Extra-fields policy: what our own services send may grow (a newer coach-core or helper adds a field), so unknown
keys are ignored there; what clients write INTO the helper is forbidden, so a misspelled field is a 400 that names
it instead of being dropped silently. Strict = exact JSON types (no "12" for 12), used for the Rust services
(serde writes exact types); lax for the SC2 API and /state (an int clock where a float is declared is fine).
"""
import importlib.util
import re
import sys
from pathlib import Path
from typing import Annotated, Literal

if importlib.util.find_spec("pydantic") is None and sys.platform == "win32":
    # The Windows python started without PYTHONPATH (brain_run.py, watch.py by hand): the wheels are in live/.pkgs
    sys.path.append(str(Path(__file__).resolve().parent / ".pkgs"))

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator  # noqa: E402
from typing_extensions import NotRequired, TypedDict  # noqa: E402  (pydantic needs this TypedDict on Python < 3.12)

Count = Annotated[int, Field(ge=0)]
CLIP_RE = r"[a-z0-9-]+\.wav|manifest\.json"   # what the helper's /voice/ serves (and what a say's `audio` may name)

# ---------- panel (minerals / gas / supply) ----------


class Panel(TypedDict):
    """One resource-panel read: what the rules, the DB and /state use."""
    minerals: Count
    gas: Count
    used: Count
    cap: Count


class PanelRead(Panel):
    """A reader's answer (panel_ocr.parse_panel, rust/panel-reader /read): the panel plus the raw text it read."""
    raw: str


class Sample(TypedDict):
    """A panel read at a game time: the booth brain's history entry."""
    t: float
    minerals: int
    gas: int
    used: int
    cap: int


# ---------- SC2 client API (GET http://127.0.0.1:6119/game) ----------


class Player(TypedDict):
    name: str
    race: str      # "Terr" | "Prot" | "Zerg" | "random"
    result: str    # "Undecided" while the game runs, then "Victory" / "Defeat" / "Tie"


class GameInfo(TypedDict):
    displayTime: float
    isReplay: NotRequired[bool]
    players: NotRequired[list[Player]]


# ---------- coach-core cue engine (POST :8767/start, /tick) ----------

CueType = Literal["target", "now"]


class CueEvent(TypedDict):
    type: CueType
    keys: list[str]
    sec: int


class CueTickResponse(TypedDict):
    events: list[CueEvent]
    lateness: float
    shift: int


class CueStartResponse(TypedDict):
    ok: bool
    steps: int


# ---------- coach-core call rules (POST :8767/rules/tick) ----------

HelperKind = Literal["blocked", "blocked-p", "bank", "behind", "behind-p"]
BrainKind = Literal["lost_cc", "lost_depot", "depot", "depots", "cc", "stuck", "orbital", "gas_idle", "bank_big",
                    "bank", "gas_early", "gas", "turret", "bunker", "stall"]


class HelperIn(TypedDict):
    plan: str
    race: str
    wall: float


class BrainIn(TypedDict):
    matchup: str
    external: list[str | None]   # kinds others said since the last poll; None = the coach
    race: NotRequired[str]       # the followed player's race ("T" | "P" | "Z"); absent (an older client) = "T"


class RulesTickRequest(TypedDict):
    game: str
    t: float
    sample: Panel | None
    new_game: NotRequired[bool]
    helper: NotRequired[HelperIn]
    brain: NotRequired[BrainIn]


class HelperCall(TypedDict):
    kind: HelperKind
    text: str


class BrainCall(TypedDict):
    """One booth-brain call: page text (with numbers) and the clip text (none, so it is pre-rendered)."""
    kind: BrainKind
    text: str
    clip_text: str
    confidence: Annotated[float, Field(ge=0, le=1)]


class RulesTickResponse(TypedDict):
    game: str
    fresh: bool
    helper: HelperCall | None
    brain: BrainCall | None


# ---------- the helper's page state (GET :8765/state, /events) ----------


class Step(TypedDict):
    """A plan step (live/plans*.json; tools/checks.py checks the files)."""
    sec: int
    t: str
    action: str
    supply: NotRequired[int | None]
    note: NotRequired[str]


class BotPlan(TypedDict):
    name: str
    steps: Annotated[list[Step], Field(min_length=1)]


class BotGame(TypedDict):
    """live/out/bot_game.json, written by bot/run.py --coach-test before a bot game: the player the helper follows
    when Dickie isn't in the game, and that bot's build as the step plan (bot/core.py bot_game)."""
    player: Annotated[str, Field(min_length=1)]
    race: str          # "terran" | "protoss" | "zerg"
    bot: str           # bot key (sixrax, dt, proxy)
    written_at: float  # unix time; older than timing.BOT_STALE = left behind by a crashed run, ignored
    plan: NotRequired[BotPlan | None]


Say = TypedDict("Say", {"id": int, "t": str, "text": str, "kind": str, "from": str, "wall": float,
                        "audio": NotRequired[str]})


class Cue(CueEvent):
    id: int


class StateData(TypedDict):
    """The helper writes every key (timing.State); readers need only in_game + clock, so a reader restarted against
    an older or newer helper keeps working. Every key that is present is type-checked."""
    in_game: bool
    clock: float
    clock_wall: NotRequired[float]
    player: NotRequired[str]   # whose game the helper follows: Dickie, or the announced bot (bot_game.json)
    race: NotRequired[str]     # that player's race, "T" | "P" | "Z" (absent from an older helper: the brain uses "T")
    plan: NotRequired[str | None]
    plan_name: NotRequired[str]
    coach_plan: NotRequired[str | None]
    cue_engine: NotRequired[bool]
    cues: NotRequired[list[Cue]]
    cue_seq: NotRequired[int]
    lateness: NotRequired[float]
    shift: NotRequired[int]
    vs: NotRequired[str]
    key: NotRequired[str]
    panel: NotRequired[Panel | None]
    plan_supply: NotRequired[int | None]
    next: NotRequired[list[Step]]
    says: NotRequired[list[Say]]
    seq: NotRequired[int]
    trace: NotRequired[bool]
    now: NotRequired[float]   # added when serialized


class NoteRow(TypedDict):
    """GET /notes: the latest coach notes with their game's label (db.Recorder.recent_notes)."""
    id: int
    text: str
    created_at: float
    game_id: int | None
    result: str | None
    enemies: str | None
    length_sec: float | None


# ---------- request bodies the helper accepts (POST :8765/say, /note) ----------


class SayBody(BaseModel):
    """POST /say: a line on the page (the coach, coach_say.py, the booth brain, mic.py)."""
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)
    text: str
    audio: str | None = None   # a clip in live/voice/clips, served by /voice/
    kind: str = "coach"

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        return v.strip()[:200]

    @field_validator("audio")
    @classmethod
    def _audio(cls, v: str | None) -> str | None:
        if not v:
            return None
        if not (re.fullmatch(CLIP_RE, v) and v.endswith(".wav")):
            raise ValueError("bad audio name")
        return v


class BotGameBody(BaseModel):
    """POST /botgame: one bot\\run.py game with the bot version that played it (bot/out/games.jsonl row)."""
    model_config = ConfigDict(extra="forbid")
    at: str
    bot: str
    vs: str
    opp_race: str = ""
    map: str = ""
    result: str
    game_time: str = ""
    commit: str = ""
    code: str = ""
    overrides: list[str] = []
    coach_test: bool = False
    replay: str = ""


class NoteBody(BaseModel):
    """POST /note: a coach note into the database (to the current/latest game unless game_id is given)."""
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)
    text: str
    game_id: int | None = None

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        return v.strip()


# ---------- post-game review (GET /history/review/<id>) ----------


class Checkpoint(TypedDict):
    t: int
    used: int
    plan: int | None
    diff: int | None


class Window(TypedDict):
    start: float
    end: float


class BehindWindow(Window):
    min: int
    max: int


class BlockWindow(Window):
    used: int
    cap: int


class PileWindow(Window):
    """Bank (minerals) and gas windows."""
    low: int
    peak: int


class DropEvent(Window):
    """A lost fight (supply) or lost buildings (cap)."""
    before: int
    after: int


class IdleWindow(Window):
    supply: int
    room: int


class Finding(TypedDict):
    kind: str
    t: float
    text: str


class Also(TypedDict):
    key: str
    title: str
    action: str


class Fix(TypedDict):
    key: str
    title: str
    action: str
    why: str
    also: Also | None
    scores: dict[str, float]


class Findings(TypedDict):
    checkpoints: list[Checkpoint]
    behind: list[BehindWindow]
    blocks: list[BlockWindow]
    bank: list[PileWindow]
    gas: list[PileWindow]
    fights: list[DropEvent]
    buildings: list[DropEvent]
    idle: list[IdleWindow]
    bank_peak: int | None
    gas_peak: int | None


class Review(Findings):
    id: int
    player: NotRequired[str | None]   # games.player (the owner, or a bot's name); absent on a DB before the column
    result: str | None
    length: float | None
    length_text: str
    plan: str | None
    coach_plan: str | None
    enemies: str | None
    matchup: str | None
    started_at: float
    findings: list[Finding]
    top_fix: Fix
    calls: dict[str, int]
    hand_review: str | None
    series: list[list[float | None]]


# ---------- the adapters (build once at import; validate_json parses + checks in one pass) ----------

GAME: TypeAdapter[GameInfo] = TypeAdapter(GameInfo)
PANEL_READ: TypeAdapter[PanelRead | None] = TypeAdapter(PanelRead | None)
CUE_START: TypeAdapter[CueStartResponse] = TypeAdapter(CueStartResponse)
CUE_TICK: TypeAdapter[CueTickResponse] = TypeAdapter(CueTickResponse)
RULES_TICK: TypeAdapter[RulesTickResponse] = TypeAdapter(RulesTickResponse)
STATE: TypeAdapter[StateData] = TypeAdapter(StateData)
BOT_GAME: TypeAdapter[BotGame] = TypeAdapter(BotGame)
REVIEW: TypeAdapter[Review] = TypeAdapter(Review)


def errors(e: ValidationError, n: int = 3) -> str:
    """A short one-line summary of a ValidationError (for logs and 400 bodies): 'loc: msg; ...'."""
    errs = e.errors()
    parts = [f"{'.'.join(str(x) for x in err['loc']) or 'body'}: {err['msg']}" for err in errs[:n]]
    return "; ".join(parts) + (f" (+{len(errs) - n} more)" if len(errs) > n else "")
