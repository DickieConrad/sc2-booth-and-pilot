# SC2 Coach — live helper architecture

Status: 2026-10-30. Describes what runs today and where it is heading. Everything here only **reads** the
game (client API + screen); nothing ever sends input to StarCraft II.

## Today

One Python process on Windows (`live/timing.py`, ~650 lines) does everything:

```
  SC2 client API (:6119) ─┐                         ┌─> local page (overlay.html, SSE /events)
  screen (dxcam + OCR) ───┼─> loop every 0.5 s ─────┼─> SQLite (coach.db)
                          │   plan + rules (judge)  ├─> PUSH lines → Claude → Live Coach artifact (2v2)
  Claude (/say, /note) ───┘                         └─> live.log (Claude's watch feed)
```

What works well: one process, no services to babysit; the page gets each change in ~1 ms (SSE);
screen reads ~3 ms with 0 wrong numbers in tests; every game lands in the DB.

What hurts as the coach grows: `timing.py` mixes sensing, rules, HTTP, DB and relay; rules are hard-coded
(`judge`); plans live in four JSON files with slightly different shapes; voice is the browser's.

## Target shape (same single process, split into parts)

```
live/
  sources/        read-only sensors, each emits events
    game.py         client API → GameStarted / Tick(t) / GameEnded(result)
    panel.py        dxcam capture + reader (panel_ocr, or the Rust service if it wins) → PanelRead
    (later) map.py      loading-screen map name → MapSeen
    (later) replay.py   newest replay after a game (sc2reader, owner-approved folder) → ReplayParsed
  coach/
    plans.py        one plan model for all sources (Live Coach 2v2, Field Manual 1v1, custom)
    rules.py        small rule objects: SupplyBlock, Bank, Behind, CCSaving... each → Call | None
    benchmarks.py   (later) personal targets from DB history ("your median supply at 3:00 is 23")
  outputs/
    page.py         HTTP + SSE server (/state /events /plans /notes /say /note)
    voice.py        (next) pre-rendered clip library + fallback to browser speech
    relay.py        Live Coach PUSH queue (2v2 only)
    store.py        SQLite recorder (single writer)
  bus.py          tiny in-process event bus: sources publish, coach + outputs subscribe
  main.py         wiring + config (config.toml: player name, tick, thresholds, ports)
```

Rules for the refactor:
- Behaviour-preserving first: same page, same DB schema, same log lines Claude watches.
- Every rule gets a unit test fed with recorded samples from `coach.db` (we have real games to replay).
- Sources never import outputs; outputs never call sources. Everything goes through events.

## Voice (in progress)

VoiceStudio runs in Docker (`live/voice/up.sh`, container `sc2coach-voice`, GPU, loopback only).
It is used **offline**: an original "former Valkyrie pilot" voice is designed once, then every step cue,
standard call and number phrase is rendered to `live/voice/clips/` with a manifest; the page plays them
through a cockpit-radio filter. No cloning of real or game voices.
Coach lines get a second, **live** voice (owner approved GPU in games): `live/voice/coach_say.py "..."` renders
with `studio.COACH_VOICE`/`COACH_SEED` (calm, low, male, British; ~1 s warm), caches `clips/coach-<slug>-<hash>.wav`
and POSTs `/say {text, audio}`; the page plays it as a call through a clean chain (no radio). Render failed or
over 3 s -> text-only say, browser speech with a male system voice. Run `coach_say.py --warm` after a container start.

## Mic (push-to-talk, `live/mic.py`)

Hold the thumb button, speak, release: faster-whisper (GPU) transcribes locally (~0.3 s), then three lanes in order
on the mic's worker thread (the key and audio threads never wait on them):

```
  transcript ─> fast lane (voice/fastlane.json keywords) ── hit ──> cached coach clip → /say, FAST line
                  │ miss / long, in a game (/state clock)
                  └─> intent lane (live/intents.py, Jev, <= 0.6 s) ── say ──> "Paused." / counters.json line → /say
                        │ pass (enemy_shows / behind / ask_build): note tagged " [intent: x 0.93]"
                        └─> slow path (unchanged): MISS line + "Owner callout: ..." note → the coach answers
```

- **Intent lane**: one TypeSafe `system_one` request with two questions: a `Choice` over 7 intents (chatter first:
  Jev leans to the first option, so doubt falls to the safe slow path) and a `Noul` "talking to the coach about this
  game?" (`talk`). The state carries a one-line context ("in a game, push-to-talk") plus the line; without it real
  callouts scored talk ~0.3. Model pinned (`intents.MODEL`, jev-1.14.0); `RetryPolicy(max_retries=0, timeout=0.6)`
  (a total budget) plus a 0.65 s future timeout, so the worker never blocks longer; any error, timeout or missing
  SDK/key → `None` → slow path. 300 calls per mic run at most.
- **`decide()`** (pure): no game → slow; talk < 0.3 → slow (chatter), < 0.7 → slow (unsure); confidence < 0.7 →
  slow; pause/resume only with their keyword (pause|stop|hold / resume|go again|unpause|continue); counter_unit only
  for a unit in `counters.json` (24 units); enemy_shows/behind/ask_build → pass; chatter → slow.
  Pause/resume are local only ("Paused." / "Back on." on the page); the 2v2 Live Coach clock stays the coach's to drive.
- **Offline score** (`tools/eval_intents.py`, 134 lines, 2026-10-30, jev-1.14.0): Jev 96% (36/36 game callouts),
  p50 109 ms, p95 259 ms; the lane as shipped acts on 33/36 game callouts, 32 right, 1 chatter line tagged (a pass,
  not a spoken answer). Live probe: p50 ~97 ms, no cold-start penalty after 20 s idle.
- **Clips**: `voice/render_counters.py` pre-renders every counters/pause/resume line (VoiceStudio up); a missing clip
  falls back to text-only `/say` (browser speech), never a render in the mic path.
- **mic.log** lines (the coach's monitor greps `^(HEARD|FAST|MISS|INTENT)`):
  `INTENT [m:ss] <intent> <conf> <action> talk=<p> | <text>`, action `say[:Unit]`, `pass` or `slow:<why>`.
- Off: `SC2_INTENTS=0` before `mic.ps1`. `mic.ps1 --wav f.wav` also prints the lane's verdict (nothing posted).

## Cue engine (Rust, live 2026-10-30)

`rust/coach-core` (port 8767) owns step-cue scheduling, the owner's "hybrid":
- **Target cues** at the plan's clock time (−5 s): the targets never move.
- **"Now" catch-up cues** before 3:00: if your supply hadn't reached the step's trigger 3 s after its time,
  she calls "<step>, now!" the moment you get there.
- **Half-shift** after 3:00: lateness is measured from your supply against the plan's supply curve
  (EMA, frozen when the plan has no more supply targets), and later cues move by half of it, capped at 15 s.
The helper posts `/start` at game start and `/tick {t, used}` every 0.5 s; events go to the page over SSE.
Fallback: the page's own clock-only cues. 18 unit tests plus 3 replay tests over 10 recorded games.
Plans come from `live/plans.json`, `plans_custom.json`, `plans_1v1.json` and, last, the generated
`live/out/plans_bot.json` (plan `BOT`, see "Players"); `/start` reloads them, a bad bot file is skipped. The helper
only ticks once `/start` accepted the game's plan (an older coach-core answers 404 for `BOT`; it would otherwise
keep ticking the previous game's engine); `/start` is retried every 30 s, the page's cues cover meanwhile.

## Players: Dickie and the sparring bots (2026-10-30)

One `coach.db` for every game; `games.player` says whose it is: the owner's in-game name (`live/player.json`, the default and every game
from before the column) or a sparring bot's in-game name (`SixRax`, `DtRush`, `ProxyRax`). Light multi-tenant: no
per-player tables or files.

```
  bot/run.py --coach-test ──> live/out/bot_game.json {player, race, bot, written_at, plan: {name, steps}}
                                  │ (deleted after the game; ignored when older than 10 min)
  helper, game start: Dickie in the players? ── yes ──> follow Dickie (unchanged)
                                  │ no: a fresh bot_game.json whose player is in the game?
                                  ├─ yes ──> follow the bot: its race (plan family, -p calls), its result,
                                  │          its build as plan BOT (-> live/out/plans_bot.json -> coach-core /start,
                                  │          the page's /plans), no Live Coach pushes / pending files / relayed says,
                                  │          recorded with player = the bot's name
                                  └─ no ───> today's behaviour (Dickie's name, Terran defaults), player "unknown"
```

- **The bot's plan.** `bot/core.py coach_steps` turns the bot's `build` (supply-gated `Step`s) into plan steps
  `{sec, t, supply, action, note}`. coach-core needs a `sec` per step (target cues, lateness), so each step gets an
  estimate from its supply (the plans' standard times: 14 → 0:18, 16 → 0:40, 19 → 1:25, 20 → 1:40, then ~10 s per
  supply), its `at` gate, and at least 15 s after the step before. The supply is real, so the "now" catch-up cues
  (before 3:00) and the helper's behind / bank rules key on what the bot actually does. Chosen over clock-only cues
  (no plan at all) and over an inline plan in `/start` (the rules route `/rules/tick` also needs the plan by id).
- **History / review.** `/history` lists Dickie's games by default with a Player dropdown for the bots;
  `/history/review/latest` is Dickie's latest game (`?player=SixRax` for a bot's); per-game routes take any id.
  The Angular review popup keeps calling `latest`, so a bot game never shows up as "your last game".
- **Migration.** `db.connect()` (the helper's Recorder, the import tool) adds the column (`ALTER TABLE`, default
  the owner's name) and an index; read-only paths (`history.py`, the `db.py` CLI) never write and treat a file without
  the column as all Dickie's. `tools/import_test_games.py` moves the old `coach-test.db` games in once (backup
  first, `--dry-run`, ids remapped, player from the game's players / notes).
- `watch.py` prints the followed player: `WATCH start: plan BOT vs A.I. 1 (Medium) (Terr), player DtRush`.
- The booth brain follows the same player's race (`/state` `race`); see Call rules below.

## Call rules (Rust, opt-in: `SC2_RULES=rust`)

`coach-core` also holds the call rules (`rust/coach-core/src/rules/`), ported 1:1 with the same thresholds,
cooldowns and text:
- **helper** (`rules/helper.rs` = `timing.py` `plan_status` / `judge` + the CONFIRM streak + `Pusher.say`'s gaps):
  supply blocked (from 0:45), bank (400 / 600 from 4:00, +400 while saving for a planned CC/Nexus; adds "supply X
  vs plan Y" when also behind), behind plan (3+ supply under the plan step 10 s old). A problem must hold for 2
  reads in a row; then 15 s since any helper say and 45 s since the same kind. Kinds are an enum
  (`blocked`, `blocked-p`, `bank`, `behind`, `behind-p`).
- **booth brain** (`rules/brain.rs` = `brain.py` `describe` + `RulesBrain` + `brain_run.Runner.feed`): facts from
  the panel history, then one call from a fixed set (`CallKind`: lost_cc, lost_depot, depot(s), cc, stuck, orbital,
  gas_idle, bank(_big), gas(_early), turret / bunker / stall reminders), with the global 20 s gap (lost CC / depot
  skip it), per-kind cooldowns, the spend-family cooldown (90 s, or 1.5x worse from 30 s), the losing-fight hold,
  rotating wording, page text (numbers) vs clip text (none). Python's `round()` (half to even) is matched.
  Race-aware: the helper puts the followed player's race in `/state` (`race`: T/P/Z), brain_run passes it on
  (`brain.race` in `/rules/tick`; missing = T); Protoss / Zerg get pylon / overlord, Probes / Drones, Gateways /
  Hatcheries wording (`RACE_KINDS` / `CallKind::wording`), and never hear cc, orbital, turret, bunker, stall (nor
  lost_cc for Zerg); parity: `tools/rules_parity.py --synthetic` (fixture `rules_parity_synth.json`).

Route: `POST /rules/tick` on :8767 with `{game, t, sample: {minerals, gas, used, cap} | null, new_game?,
helper?: {plan, race, wall}, brain?: {matchup, external: [kind | null, ...]}}` -> `{game, fresh, helper: {kind,
text} | null, brain: {kind, text, clip_text, confidence} | null}`. State is kept per game key (the helper and the
brain runner each use their own key; a new key, `new_game`, or the clock going back 5+ s starts fresh; the 8 most
recent games are kept). The helper still filters implausible reads in Python and sends `null` for them.

Python side (`live/rules_client.py`): off by default. With `SC2_RULES=rust` in the environment, `timing.py` and
`brain_run.py` ask coach-core each tick; if it doesn't answer they use the Python rules (retry after 30 s), like the
panel reader. The Python rules stay the reference. Parity: `tools/rules_parity.py --db <coach.db>` replays every
recorded game through both and diffs the calls (27 games: 188 helper + 279 brain calls identical at the recorded
cadence, 237 + 307 upsampled to 0.5 s, also through HTTP); `cargo test` replays the exported fixture
(`tests/fixtures/rules_parity.json`). Per tick: Rust ~1.4 µs (p50) vs Python ~26 µs at the recorded cadence,
~2.5 µs vs ~91 µs at 0.5 s (Python's `describe` rescans the history); ~0.18 ms through HTTP.

## Boundary models (pydantic, `live/models.py`)

Every JSON that crosses a process or HTTP boundary has a declared shape. Inside a process the data stays a plain dict
typed as a TypedDict (mypy checks it, no run-time cost); at the boundary a pydantic v2 `TypeAdapter.validate_json`
parses and checks it in one pass (pydantic-core, Rust), or a `BaseModel` for request bodies.

| boundary | shape (models.py) | mode | extra fields | on a bad payload |
|---|---|---|---|---|
| SC2 client API `GET :6119/game` (`timing.fetch_game`) | `GameInfo` | lax | ignored | treated as no game; logged (1st, then every 600th) |
| panel-reader `POST :8766/read` | `PanelRead \| None` | strict | ignored | Python reader, retry in 30 s (like a dead service) |
| coach-core `POST :8767/start`, `/tick` (`CueEngine`) | `CueStartResponse`, `CueTickResponse` | strict | ignored | page's own cues, retry in 30 s |
| coach-core `POST :8767/rules/tick` (`rules_client`) | `RulesTickRequest` -> `RulesTickResponse` (`HelperCall`, `BrainCall`) | strict | ignored | Python rules, retry in 30 s; logged |
| helper `GET :8765/state` read by `brain_run.py`, `watch.py` | `StateData` (only `in_game`, `clock` required) | lax | ignored | brain: logged, next poll; watch: stderr line, never the feed |
| helper `POST :8765/say` | `SayBody` (text stripped to 200, audio = a clip `.wav`, kind) | lax, numbers -> str | **forbidden** | 400 `{"error", "detail"}` |
| helper `POST :8765/note` | `NoteBody` (text, game_id) | lax | **forbidden** | 400 `{"error"}` |
| helper `GET /history/review/<id>` | `Review` (serialized with `REVIEW.dump_json`) | - | - | (output; the tests validate it strictly) |
| `live/out/bot_game.json` from `bot/run.py --coach-test` (`timing.read_bot_game`) | `BotGame` | lax | ignored | not followed (Dickie's defaults); logged |

Policy: what our own services send may grow (a newer coach-core adds a field), so unknown keys are dropped; what
clients write into the helper is forbidden, so a misspelled field is a 400 that names it. Strict (exact JSON types)
for the Rust services, whose serde output is exact; lax for the SC2 API and `/state`. The call kinds are `Literal`s
(`HelperKind`, `BrainKind`): a kind coach-core knows and Python doesn't is rejected, i.e. the Python rules take over
(`test_models.py` checks `brain.KINDS` and `timing.judge` against them). Requests the helper sends
(`RulesTickRequest`, the cue bodies) are typed statically and written with `json.dumps`. Not validated on purpose:
the trace lines (`looptime`, internal), plans*.json (`tools/checks.py` checks them in CI), and `coach_say.py`'s
`/say` body (it stays stdlib-only for its start-up time; the helper validates what it receives).

Cost (`tools/bench_models.py`, Windows python 3.12 = the helper's interpreter, p50): a helper tick's four answers
(game, panel, cues, rules) take 5.5 us through `validate_json` vs 8.9 us through the `json.loads` they replaced
(pydantic-core parses faster than the json module); the brain's full `/state` poll (30 says, 20 cues) 31.6 vs
30.2 us. Well under the 0.1 ms budget. One-time: `import models` (pydantic from `live/.pkgs` on the share) ~1.6 s
at process start.

## Language choices

- **Python** stays the core: the latency budget is dominated by tick/confirm timing, not compute.
- **Rust** (`rust/panel-reader/`): port of the reader, identical results on all 159 labeled + 480 stress
  crops, ~1 ms per read over localhost vs Python's 26 ms (p95 ~5 vs 207 ms). **Adopted** 2026-10-30 as a
  microservice (port 8766) with automatic Python fallback. Template learning stays in Python (glyphs2.json);
  restart the service after relearning.
- **TypeScript**: if the page outgrows one HTML file (components, charts from `coach.db`), move it to a
  small TS build that emits static files into `live/`.

## Latency budget (event on screen → you hear it)

Measured 2026-10-30 (`perf/measure-loop`), offline: nothing live was started, stopped or called. **M** = measured
on this machine (Windows Python 3.12 = the helper's interpreter, unless noted), **E** = estimate or older number.
Scripts: throwaway copies of the Rust services on spare ports in WSL, the 159 labeled crops, `coach.db` (30 games,
2509 samples) and the clip WAVs, all read-only.

### Where the time goes (one helper call: blocked / bank / behind)

| step | time | how known |
|---|---|---|
| wait for the next capture | 0–500 ms (TICK) | design |
| client API `fetch_game` (urllib, new connection) | transport 0.9 ms p50, 13 ms p95; SC2's own reply time unknown | M (loopback test server) / E |
| capture (dxcam) | ~3 ms | E (earlier test) |
| panel read, Rust service in WSL | 0.97 ms p50, 4.4 ms p95, 47 ms max | M (Windows → WSL) |
| panel read, Python fallback | 18 ms p50, 141 ms p95, 202 ms max | M |
| rules: `judge` + `plan_status` | 0.011 ms p50, 0.023 ms p95 | M (2509 samples) |
| booth brain `RulesBrain.decide` | 0.017 ms p50, 0.032 ms p95 | M (2509 samples, WSL) |
| **cue engine `POST /tick` (coach-core in WSL)** | **43 ms p50, 47 ms p95, every tick** | M |
| same, headers + body in one `send()` | 0.26 ms p50, 0.38 ms p95 | M |
| DB write (`INSERT` + commit on the `\\wsl.localhost` share) | 8.3 ms p50, 11.6 ms p95 (local disk: 2.2 ms) | M |
| `State.update` / JSON for one SSE write | 0.001 ms / 0.04 ms p50, 0.09 ms p95 | M |
| confirm (CONFIRM = 2 reads) | +500 ms | design |
| SSE to the page | ~1 ms | E (earlier test) |
| clip fetch + decode | first play only, then cached | trace will tell |
| **voice queue (one voice at a time)** | 8% of cues arrive while a clip plays; those wait **1.1 s p50, 3.0 s p95** | simulated: coach-core replay of 10 games + real clip lengths |
| multi-step cue ("Barracks, Refinery") | 2nd step starts 1.6 s after the 1st (p50) | simulated |
| clip lengths | pilot cues 2.0 s p50 (3.3 max), "now" 1.75 s, coach lines 3.2 s p50, 5.3 s p95 | M (WAV headers) |
| coach line render (`coach_say.py`) | ~1 s warm, 3 s budget | E (no render log exists yet; the trace records it) |
| mic → text (GPU whisper) | 0.29 s p50, 0.17–0.51 s | M (mic logs) |

Tick health from the existing `live.log`: all 1,248 in-game 10-second status lines are 10–11 game seconds apart,
so no tick stalled for about a second or more in ~3.5 hours of logged games (M; finer stalls need the trace).

### What it means

- **Python compute is not the problem.** Rules, brain and state work add up to < 0.1 ms per tick. Porting them to
  Rust saves under a millisecond; it is worth doing for structure and tests, not for speed.
- **The biggest fixed cost per tick is a socket quirk, not a language:** `http.client` sends the headers and a small body
  in two writes, and the Windows → WSL localhost relay holds the second until a delayed ACK (~40 ms). The 80 KB panel
  crop doesn't hit it; the small `/tick` JSON does, every tick. TCP_NODELAY on the client did not help (M); one
  write per request did (43 → 0.26 ms).
- **The 2–3 s "late" calls come from the voice, not the loop:** a cue that lands while another clip plays waits for it
  (clips are ~2 s, coach lines ~3 s), and a two-step cue says its second step ~1.6 s later. Coach lines also pay the
  render (~1–3 s) before they reach the page. Detection itself is 0.5–1.0 s by design (sampling wait + confirm).
- Calls held back by `SAY_GAP` (15 s) / `REPEAT_GAP` (45 s) fire when the gap opens: late by design, not by load.

### Recommendations (ordered by milliseconds saved per effort)

1. **One write per POST to the Rust services** (`CueEngine._post`, also `PanelReader._read_rust`): headers + body in one
   `send()`. −43 ms on every tick, a few lines of Python. Running coach-core natively on Windows would also avoid the relay.
2. **Voice queue policy on the page:** drop or merge a target cue that has waited more than ~1 s (the list shows it),
   let a "now" cue cut in front of target cues, say multi-step cues as one shorter clip, trim clip silence. This is
   where the seconds are.
3. **Shorter tick: 0.5 → 0.2 s** with confirmation counted in time, not reads (e.g. 2 reads ≥ 0.2 s apart). Detection
   drops from 0.5–1.0 s to 0.2–0.4 s. After (1) a tick's work is ~5 ms (capture ~3 ms, API ~1 ms, read ~1 ms), so 0.2 s
   leaves plenty of room.
4. **DB writes off the tick thread** (a queue + writer thread in `db.Recorder`): 8–12 ms on the share, and `REC.call`
   runs on the tick thread right when a call fires.
5. **Coach lines short, pre-rendered where possible:** a 3 s clip blocks every cue behind it. Keep `--warm` after a
   container start; the trace's `say` records give the real render times.
6. **Keep-alive for the client API** (`fetch_game` opens a new connection each tick): p95 13 ms → 0.4 ms on a loopback test.
7. **Rules into Rust**: for one tested engine, not speed (see above). Rust screen capture: only if the trace shows
   capture is slow.

### Measuring it live (opt-in)

Set `SC2_TIMING_TRACE=1` in the shell before starting the helper (`$env:SC2_TIMING_TRACE = "1"` then `live\run.ps1`),
and before `coach_say.py` / `brain_run.py` if you want them too. Each process appends JSON lines to
`live/out/looptrace-<proc>.log`, written from a background thread so the tick never waits on the disk. The helper adds
`trace: true` to `/state`, and the page (old and `/next/`) sends its receipt / audio-start times with its next `/notes`
poll: no extra requests. Then `python live/looptime.py` prints p50/p95/max per tick stage (api, cap, read, rules,
cue, db, page), tick interval / jitter / overruns, per-call confirm → page → audio, engine-cue lateness and queue wait,
end to end ("first read that showed it → text on the page" and "→ audio start"), coach_say render vs cache,
and the slowest ticks. Unset, nothing is recorded and nothing changes.

## Development

All from WSL at the repo root (details in `CONTRIBUTING.md`). Tools are project-local: `bash tools/dev-setup.sh`
puts ruff in `.tools/py`, mypy + pydantic in the `.tools/typing` venv and rustfmt + clippy in `.rust/`.
- **`bash ci.sh`**: lint (ruff, **mypy** per `mypy.ini`: all of `live/` + `tools/`, strict for the core modules, see
  `CONTRIBUTING.md` "Typing"; cargo fmt + clippy `-D warnings` for both crates, `node --check` of the page script,
  plans/focus JSON schema: every step has `sec`/`t`/`action`, `t` == `sec`, in order) -> `cargo test` in both crates
  -> `ast.parse` of every `.py` + every `test_*.py` (in the `.tools/typing` python, which has pydantic). PASS/FAIL table
  with timings, non-zero exit on failure; ~3 s warm. A missing `.tools/typing` is a FAIL with the install command.
  `ruff format --check` is advisory only (the code keeps its dense hand-aligned style). Never touches the running helper.
- **`bash ci.sh --fast`**: the pre-commit subset incl. mypy (no compiling, ~1 s); hook in `tools/hooks/pre-commit`.
- **Commits**: Conventional Commits (`feat:`, `fix:`, `perf:`, `refactor:`, `docs:`, `chore:`, `test:`), subject in plain
  words, optional `Impact: <measured effect>` trailer.
- **Releases**: `python3 tools/release.py notes` previews; `bump` prepends `## vX.Y.Z · date` to this folder's
  `CHANGELOG.md` (the page's Release notes; top heading = running version) and prints the commit + `git tag` commands.
  Default bump: minor if any `feat`, else patch.

## Next steps (in order)

1. Voice clips + page player (Valkyrie pilot).
2. Refactor into the target shape, behaviour-preserving, with rule tests from recorded games.
3. Decide on the Rust reader from the benchmark.
4. Map name from the loading screen; replay parsing after games (with the owner's replay folder).
5. Personal benchmarks from DB history drive the checkpoints instead of fixed plan numbers.
