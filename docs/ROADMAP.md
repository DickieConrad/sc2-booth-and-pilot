# Roadmap

Ideas and planned work for the SC2 coach. Add anything here (Dickie or the coach); when something ships it moves
to `live/CHANGELOG.md`. Rule for every coach item: it only **reads** the game; nothing ever sends input to Dickie's
StarCraft II games. The sparring bot (`bot/`) is the one exception by design: it plays its own local games through
Blizzard's official bot API, never on Battle.net.

Status: **now** = in progress · **next** = agreed, queued · **idea** = not decided yet.

## Now
- **Type safety** — Pydantic models at the JSON boundaries (helper state, samples, `/rules/tick`, `/say`, `/note`,
  review) and a static type checker in CI, strict on the core modules.
- **Rules in Rust, live** — `SC2_RULES=rust` is on for the helper and the brain (identical calls to Python on all
  recorded games, `tools/rules_parity.py`). Left: a few games on it, then make it the default; later drop the Python
  copies (keep `judge`/`RulesBrain` only as the parity reference) and move the read filter (`plausible`) and the plan
  pick into Rust too.
- **Sparring bot** (v0) — `bot/`: python-sc2 (burnysc2, installed in `bot/.pkgs`) bots for local games only:
  `sixrax` (6-Barracks Marine all-in), `dt` (DT rush), `proxy` (proxy 2-rax Reaper/Marine); every timing in
  `bot/config.py` or `--set`. `bot/run.py`: Dickie vs bot in realtime (`--vs human`), bot vs built-in AI, and
  `--2v2` (Dickie + AI ally vs two bots, via `bot/match.py`; team layout to check on the first run). Steerable
  mid-game through `bot/out/command.json` (`bot/steer.py cheese|attack|stop|resume|gg`). Not yet run against a live
  client; needs the map packs in `bot/maps/`. Dickie + his buddy together would need a LAN setup.
- **Jev intent lane for the mic** — `live/intents.py`: callouts that miss the keyword fast lane go to TypeSafe's
  Jev in a game (one request: intent `Choice` + "talking to the coach?" `Noul`, ~0.1 s, 0.6 s cap). Answers
  pause/resume ("Paused." / "Back on.", local only) and "what beats X" from `live/counters.json` (24 units, clips
  via `live/voice/render_counters.py`); "they're doing Z" / "behind on Y" / "what next" are tagged for the coach.
  Offline: acts on 33/36 game callouts, 32 right. `SC2_INTENTS=0` turns it off. Left: a few games on it, then
  canned replies or Live Coach reaction cards for enemy_shows/behind, and wiring pause/resume to the 2v2 clock.
- **Bot games as a player** — `bot/run.py --coach-test` announces the bot (`live/out/bot_game.json`: name, race,
  its build as plan `BOT`); the helper follows it (race-right calls, the bot's own build cues, its result, no Live
  Coach pushes) and records it in `coach.db` with `games.player` = the bot's name. `/history` has a Player dropdown
  (Dickie by default), `/history/review/latest?player=`. Go-live: import the old `coach-test.db` games once
  (`tools/import_test_games.py`), restart the helper and coach-core. Left: the booth brain's calls by race.
- **Live latency check** — one traced session (`SC2_TIMING_TRACE=1` + `live/looptime.py`) to confirm the offline
  numbers in `live/ARCHITECTURE.md` ("Latency budget").

## Next
- **Voice queue policy** — drop/merge stale target cues, "now" cues cut in front, multi-step cues as one clip, trimmed
  silence: the measured source of the 1–3 s late calls (queued cues wait 1.1 s p50, 3.0 s p95).
- **Shorter tick** — 0.5 s → ~0.2 s, confirmation counted in time not reads; detection 0.5–1.0 s → 0.2–0.4 s. A tick's
  work is ~5 ms now that the cue POST is one write.
- **DB writes off the tick thread** — 8–12 ms per insert on the `\\wsl.localhost` share.
- **Switch `/` to the Angular page** once Dickie is happy with `/next/` (then `src/styles.css` stops being a copy).
- **Helper loop into Rust** — the next step after the rules: the tick loop and its shared state (page server
  thread + game loop share STATE/REC today) move into a Rust service, where the compiler rules out
  unsynchronised shared data; Python keeps only screen capture (dxcam) and the mic. Today's races were in the
  page's TypeScript voice queue, process management and logic, not the language, so plan it, don't rush it.
- **Refactor `timing.py`** into `sources/`, `coach/`, `outputs/` (see `live/ARCHITECTURE.md`), behaviour-preserving,
  one worktree per piece, CI green at every step.
- **One voice module** shared by `coach_say.py`, the mic fast lane and the brain runner.
- **`config.toml`** for ports, thresholds, cooldowns, player name (a settings panel on the page).
- **Fast-lane learning loop** — review `MISS` lines between sessions and add replies (curated, no ML training).
- **Protoss clips** — record the pilot's "pylon" / "Probes" calls and PT/PZ/PP step cues so Protoss games
  don't fall back to browser speech.

## Ideas
- **2v2: two MMM bots vs two Insane AIs** (Dickie, 2026-10-31) -- `run.py --2v2` seats it already, but each bot
  attacks and retreats alone while the AIs gang up. First: shared targets (attack together, wait for the partner's
  wave) and helping defend the partner's base. MMM alone beats Insane Protoss and Zerg 1v1.
- **Coach asks when it can't see** (Dickie's idea) — for facts the screen doesn't show (production buildings,
  upgrades, tech), the coach asks a short yes/no or number question at a calm moment ("Two Factories yet?"),
  Dickie answers on push-to-talk, and the answer is stored for the game so calls use it ("3rd Barracks" only
  when there are 2). Answers parsed by the intent lane (Jev Noul for yes/no, Choice for counts); at most one
  question per ~90 s, never in a fight; a missing answer changes nothing.
- **Jev: sort MISS lines between games** (Choice + "new intent?") — suggests fast-lane keywords/synonyms for
  review; the learning loop for the mic. Cheap, no live risk.
- **Jev: guess the enemy strategy from early scouting callouts** (Choice over the Live Coach reaction cards, act
  at confidence >= 0.8: write `callout` once per phase). Eval on per-game callout sequences + replays.
- **Jev: draft the post-game one fix** (Choice over the main mistake + Score per habit across games, numbers
  turned into words locally first: Jev is weak at maths). Claude accepts or overrules the draft.
- **Jev: tilt check** (Noul/Score on the last few callouts) -> calmer, shorter lines; tone only, never strategy.
- **Jev notes** (docs): weak at numbers/maths and long irrelevant state, biased to the first Choice option;
  RetryPolicy timeout is a total budget (default 30 s); pin `jev-1.14.0`; one request can carry several questions.
- **Zerg sparring bots, in Rust, with a Jev commander** — the third race for `bot/` (Terran: 6-rax, proxy;
  Protoss: DT rush): 12-pool ling flood, roach/ravager all-in, ling/bane bust, written with `rust-sc2` (check it
  is maintained and its unit data matches the current patch first) as a head-to-head with the Python bots.
  Commander after TypeSafe's jev-plays-doom: the bot describes its state as text (army, enemy seen, bases,
  phase) and Jev picks a strategy button (attack / defend / expand / tech switch / harass / all-in) every few
  seconds via Choice, with rule vs intent criteria compared; a small typed Rust client for `POST /v1/systemone`
  (reqwest + serde). Same steering file and `--coach-test` as the Python bots. Local games only.
- **Coach as commander** — the coach (an LLM) steers the sparring bot's strategy every 10-30 s through its
  command file ("switch to Marauders + tanks", "attack the third") while the scripted bot does the fast
  micro/macro. Possibly built with Pydantic AI (typed tool calls). Local games only.
- **Replay import** — after each game parse the newest replay (sc2reader) into coach.db: exact build times per
  building, compared with the calls and the plan in the review screen. Needs Dickie's replay folder path.
- **Redis event-bus experiment** — prototype helper/brain/mic/Rust hand-offs over Redis pub/sub, benchmark it
  against the current HTTP/SSE hand-offs with the loop-timing trace; adopt only if the numbers win.
- **Perf gate in CI** — benchmark rule evaluation, panel read and the page voice queue; fail CI on a regression
  beyond a set budget.
- **History page in the Angular app** (`/next/history`): one frontend instead of three page styles.
- **Specific bank calls** — "Bank 1200: 3 Barracks" instead of "Bank 1200, spend" after 6:00.
- **TvZ scouting cue** at ~2:00: "Scout: third hatch? Gases?"; scan reminder at 3:30 if nothing seen.
- **Focus list items** — "Tanks seen: drop his main, don't attack"; "5:30 turret in each mineral line".
- **Tab guard** — a second open coach page mutes itself ("another tab is speaking").
- **Rust screen capture** — only if measurements say capture matters; needs a Windows Rust toolchain (owner OK).
- **Map name** from the loading screen.
- **Personal benchmarks** from DB history drive the checkpoints instead of fixed plan numbers.
- **3:30 drill** — vs-AI practice: stop at 4:00, target 26 supply at 3:00 with the CC down by 1:45.
- **Nuke / ghost OCR** for late-game alerts.
- **Live pilot voice for numbers** (`live/voice/LIVE_PLAN.md`): stitched phrases so "Bank 1300" is spoken.
- **Refresh the VoiceStudio key** (local container password) if ever wanted.
