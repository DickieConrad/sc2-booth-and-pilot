# sc2-booth-and-pilot

A personal StarCraft II practice kit for a Terran player, in two parts:

- **Pilot: sparring bots** (`bot/`). Python bots built on Blizzard's official StarCraft II API (through
  [python-sc2 / burnysc2](https://github.com/BurnySc2/python-sc2)) that play the builds you want to practise against
  in local games: a 6-Barracks Marine all-in, a DT rush, a proxy 2-Barracks Reaper/Marine, a 2-base MMM macro game
  and a 2-base Skytoss into Carriers. You play them 1v1, or 2v2 with a built-in AI ally; they can also play the
  built-in AI for tests.
- **Booth: a live coach** (`live/`). A local helper and dashboard for **vs-AI and custom games**. It follows the
  game clock, shows a build-order step lane for the plan picked from the enemy races, reads your minerals, gas and
  supply from the screen, and calls out supply blocks, falling behind the plan and banking, on a local web page and
  by voice. Every game is recorded in a local SQLite database with a history and review page. Two Rust crates
  (`rust/coach-core`, the cue and call-rule engine, and `rust/panel-reader`, an experimental port of the
  resource-panel reader) run as localhost services next to it.

## Read-only: it never sends input

The live coach only **reads**: the game's local client API (`http://localhost:6119/game`, clock and players) and the
screen (a capture of the resource panel, read by template matching). It never sends keystrokes, mouse input or any
other command to StarCraft II, and never reads game memory. Its outputs are a local web page, spoken lines and the
local database.

The sparring bots are separate: they control only their own player, through Blizzard's bot API, in local games that
python-sc2 starts. They do not touch Battle.net.

## Requirements

- Windows 10/11 with StarCraft II installed, and **WSL 2** (the scripts were written for a Debian distro). The repo
  lives in your WSL home; the Windows side runs it from the `\\wsl.localhost\<distro>\...` share.
- [uv](https://docs.astral.sh/uv/) in WSL. `live/setup.sh` uses it to put a Windows CPython 3.12 (`live/.python`)
  and the Windows packages (`live/.pkgs`) inside the repo; nothing is installed system-wide.
- A Rust toolchain for the two crates (pinned to 1.99.1 by `rust-toolchain.toml`; `rust/env.sh` keeps rustup and
  cargo in the repo's `.rust/`).
- Optional:
  - an NVIDIA GPU for push-to-talk transcription (`live/mic.py`, faster-whisper; falls back to the CPU);
  - Docker with GPU support for the coach's own voice (`live/voice/up.sh`, VoiceStudio on 127.0.0.1:3900); without
    it the page uses the browser's speech;
  - Node.js / npm for the Angular version of the page (`live/web`, served at `/next/` once built);
  - a TypeSafe API key (`live/.env.typesafe`, git-ignored) for the optional "Jev" intent lane and rules brain.

## Quick start

From WSL, at the repo root:

```bash
bash live/setup.sh                                      # Windows Python + packages into live/
cp live/player.example.json live/player.json            # then put your in-game name in it
bash live/fetch_models.sh                               # optional: faster-whisper weights for the mic

# Rust crates (toolchain inside .rust/)
source rust/env.sh
curl --proto '=https' -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path --default-toolchain none
(cd rust/coach-core && cargo build --release)
(cd rust/panel-reader && cargo build --release)
```

Your in-game name tells the helper which team is yours. It is read from `live/player.json` (git-ignored), or from
the `SC2_PLAYER` environment variable, which wins.

Then on Windows, from the repo root on the WSL share:

```powershell
powershell -ExecutionPolicy Bypass -File live\run.ps1          # the helper alone: page at http://127.0.0.1:8765/
powershell -ExecutionPolicy Bypass -File live\start.ps1 -NoClaude   # the whole stack, each piece minimized
powershell -ExecutionPolicy Bypass -File live\stop.ps1         # stop what start.ps1 started (by PID)
```

`start.ps1` starts coach-core (:8767) and panel-reader (:8766) in WSL, the helper (:8765), the rules brain and the
mic on Windows. Without `-NoClaude` it also opens a Claude Code session in that window. Game history is at
`http://127.0.0.1:8765/history`.

### Sparring bots

```powershell
live\.python\python.exe -m pip install --target bot\.pkgs burnysc2         # once
live\.python\python.exe bot\run.py --bot sixrax --vs human --map Simple64   # you vs the 6-rax bot
live\.python\python.exe bot\run.py --list                                    # every bot and setting
```

See `bot/README.md` for every bot, the 2v2 setup and the options.

## Maps are not included

Blizzard's maps are not part of this repo (`bot/maps/` is git-ignored). Get them from Blizzard:

- 1v1: the API map packs at <https://github.com/Blizzard/s2client-proto#map-packs> (the zip password accepts
  Blizzard's AI and Machine Learning License). Extract the `.SC2Map` files into `bot/maps/`.
- 2v2: open a 4-player melee map in the StarCraft II Editor and *Save As* into `bot/maps/`.

`bot/README.md` has the details and a fallback.

## Folder layout

```
bot/            sparring bots (python-sc2): bot code, micro modules, run.py launcher, bot/README.md
live/           the live coach: helper (timing.py), page (overlay.html), history (history.py/.html), database (db.py),
                screen reader (panel_ocr.py), push-to-talk (mic.py), rules brain (brain.py), plans (plans*.json),
                ARCHITECTURE.md and CHANGELOG.md
live/voice/     coach voice: VoiceStudio container script, clip rendering, fast-lane lines
live/web/       Angular version of the page (optional)
live/vendor/    vendored htmx and Alpine.js for the history page
rust/           coach-core (cue and call-rule engine, localhost service) and panel-reader (panel digit reader)
tools/          checks and CI helpers, release notes, evals, one-time import tool, test fixtures
docs/           ROADMAP.md
ci.sh           local CI: ruff, mypy, Rust tests, Python tests, web build
```

## Tests

```bash
bash tools/dev-setup.sh   # once: ruff, mypy + pydantic and rustfmt/clippy inside the repo
bash ci.sh                # lint, types, Rust tests, Python tests, web tests and build
```

The Python tests (`python3 tools/checks.py tests`) don't need StarCraft II; they need pydantic (the
`.tools/typing` venv from `tools/dev-setup.sh`). The bot tests cover the parts that don't import python-sc2.

## Third-party code

- `live/vendor/htmx.min.js`: [htmx](https://htmx.org) 2.0.4, Zero-Clause BSD licence.
- `live/vendor/alpine.min.js`: [Alpine.js](https://alpinejs.dev) 3.14.8, MIT licence.
- python-sc2 / burnysc2 (MIT), the Python wheels in `live/.pkgs` and the npm packages are installed by the setup
  steps, not included here.
- `rust/panel-reader/tests/fixtures/*.png` are small crops of the StarCraft II resource panel, used as test input.

StarCraft II is a trademark of Blizzard Entertainment. This project is not affiliated with or endorsed by Blizzard.

## Licence

MIT, see [LICENSE](LICENSE).
