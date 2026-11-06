# sc2-booth-and-pilot

Practice kit for StarCraft II, in two parts:

- **Pilot**: sparring bots that play a set build (a 6-Barracks all-in, a DT rush, a proxy Reaper/Marine opening, a
  2-base MMM game, a Skytoss game) so you can practise against it in a local game.
- **Booth**: a live coach that watches your game and calls out supply blocks, falling behind your build order and
  banked minerals, on a web page and by voice.

**Not for ranked.** The coach is meant for games against the built-in AI and custom games. It only reads: the game's
local client API (`http://localhost:6119/game`, for the clock and players) and the screen (a crop of the resource
panel). It never sends keystrokes, mouse input or any other command to StarCraft II, and it never reads game memory.
The sparring bots are separate. They control only their own player, through Blizzard's bot API, in local games that
python-sc2 starts. They never touch Battle.net.

## What you get

- Five sparring bots (see [Bots](#bots)), playable 1v1, or 2v2 with a built-in AI ally. Each bot can also play the
  built-in AI, which is how you test one.
- A build-order step lane that follows the game clock, with plans you can pick from.
- Live calls for supply blocks, being behind the plan and banking, on a local page and by voice.
- A history and review page for every game, stored in a local SQLite database.
- Optional push-to-talk: hold a key, say a callout ("proxy", "oracle"), and the coach logs it and can answer.

## Before you start

**Required**

- Windows 10 or 11 with StarCraft II installed.
- WSL 2 (the scripts were written for a Debian distro). You clone and set up in WSL. You run the games and the
  coach from Windows PowerShell, on the repo's `\\wsl.localhost\<distro>\...` path.
- [uv](https://docs.astral.sh/uv/), `curl` and `tar` inside WSL. `live/setup.sh` uses them to put a Windows
  Python 3.12 (`live/.python`) and the Windows packages (`live/.pkgs`) inside the repo. Nothing is installed
  system-wide.
- Blizzard map files (`.SC2Map`). They are not in this repo. See [Get a map](#get-a-map).

**Optional**

- Push-to-talk (`live/mic.py`) uses faster-whisper. An NVIDIA GPU makes it faster, but it is not required: if
  no GPU is usable it runs on the CPU.
- Docker with GPU support, for the coach's own voice (`live/voice/up.sh`). Without it the page speaks with your
  browser's built-in speech.
- Node.js 20.19 or newer, for the Angular version of the page (`live/web`, served at `/next/`).
- Rust, for two small helper services (`rust/coach-core`, `rust/panel-reader`). The coach works without them.
- A TypeSafe API key in `live/.env.typesafe` (git-ignored), for the optional "Jev" intent lane and rules brain.

### Get a map

The bots need `.SC2Map` files. Put them in **`bot/maps/`** (the folder is git-ignored). `run.py` sends the map to
StarCraft II as bytes, so you do not copy anything into the game folder.

- **1v1:** download Blizzard's API map packs from <https://github.com/Blizzard/s2client-proto#map-packs>. The zip
  password is `iagreetotheeula`, which accepts Blizzard's AI and Machine Learning License. `Melee.zip` has
  `Simple64` and `Simple96`, small maps that are quick to test on. The `Ladder*` packs have real ladder maps.
  Extract the `.SC2Map` files into `bot/maps/`.
- **2v2:** the packs only have 1v1 maps. Open a 4-player melee map in the StarCraft II Editor (File > Open >
  Battle.net tab) and use *Save As* into `bot/maps/`. Some ladder maps are locked; use any unlocked 4-player map.

`--map` takes a file path or a map name. A name is looked up in `bot/maps/` first (case does not matter), then in
the StarCraft II install's `Maps` folder. If you pass no `--map` and `bot/maps/` has exactly one map, that one is
used.

## Quick start: play a sparring bot

1. **Clone the repo in WSL.** Keep it in your WSL home. Windows git refuses to work on this share.

   ```bash
   # WSL
   cd ~
   git clone https://github.com/DickieConrad/sc2-booth-and-pilot.git
   cd sc2-booth-and-pilot
   ```

2. **Set up Python.** This downloads Windows Python 3.12 and the coach's packages into `live/`. The packages
   include some large GPU libraries for push-to-talk, so it takes a while.

   ```bash
   # WSL, from the repo root
   bash live/setup.sh
   ```

3. **Install the bot library.** Open PowerShell and go to the repo on the WSL share. Replace `<distro>` with your
   distro name (for example `Debian`; `wsl -l` lists them) and `<you>` with your WSL user name.

   ```powershell
   # PowerShell
   cd \\wsl.localhost\<distro>\home\<you>\sc2-booth-and-pilot
   live\.python\python.exe -m pip install --target bot\.pkgs burnysc2
   ```

4. **Get a map.** Follow [Get a map](#get-a-map). For a first try, put `Simple64.SC2Map` in `bot/maps/`.

5. **Check the setup without starting the game.** `--dry-run` prints the seats and map and starts nothing.

   ```powershell
   # PowerShell, from the repo root
   live\.python\python.exe bot\run.py --bot sixrax --vs easy --map Simple64 --dry-run
   ```

6. **Watch one bot play the built-in AI.** Close the normal StarCraft II client first. This starts a game with the
   bot on one side and an easy AI on the other, and stops it at 10:00 if nobody has won.

   ```powershell
   # PowerShell, from the repo root
   live\.python\python.exe bot\run.py --bot sixrax --vs easy --map Simple64 --time-limit 10:00
   ```

7. **Play against the bot yourself.** `--vs human` makes you the opponent, in real time.

   ```powershell
   # PowerShell, from the repo root
   live\.python\python.exe bot\run.py --bot sixrax --vs human --map Simple64
   ```

   More examples:

   ```powershell
   # PowerShell, from the repo root
   live\.python\python.exe bot\run.py --bot dt --vs human --map "Equilibrium LE"
   live\.python\python.exe bot\run.py --bot sixrax --bot2 dt --vs human --2v2 --map "<2v2 map>"
   live\.python\python.exe bot\run.py --list     # every bot and every setting you can change with --set
   live\.python\python.exe bot\run.py --help     # all options
   ```

**What to expect.** python-sc2 starts its own StarCraft II windows (offline, no Battle.net). A 1v1 against a bot opens
two windows: yours, and a small one for the bot. A 2v2 opens three. Play in yours and leave the small ones open;
closing one ends that bot. The game starts with the default (Standard) hotkeys. Ctrl+C in the terminal closes the
windows python-sc2 started.

**Steer a bot mid-game.** From WSL, while a game runs: `python3 bot/steer.py cheese` (also `attack`, `stop`,
`resume`, `gg`). See [bot/README.md](bot/README.md) for what each does.

For every option, the 2v2 setup and how the bots decide things, read [bot/README.md](bot/README.md).

## Quick start: the live coach

The coach follows a game you play against the built-in AI or in a custom game, or a bot-vs-AI game started with
`--coach-test`.

1. **Finish steps 1 and 2 above** (clone and `bash live/setup.sh`).

2. **Tell it your in-game name.** The coach uses it to know which team is yours.

   ```bash
   # WSL, from the repo root
   cp live/player.example.json live/player.json
   # then edit live/player.json so it reads {"name": "YourInGameName"}
   ```

   You can set the `SC2_PLAYER` environment variable instead. It wins over the file.

3. **Start the coach.**

   ```powershell
   # PowerShell, from the repo root
   powershell -ExecutionPolicy Bypass -File live\run.ps1
   ```

   It prints `local page: http://127.0.0.1:8765/`.

4. **Open the dashboard** at <http://127.0.0.1:8765/> in your browser. Put it on a second screen if you have one.
   Game history is at <http://127.0.0.1:8765/history>.

5. **Start a game** against the built-in AI or a custom game.

**What you see and hear.** The page follows the game clock and shows a step lane for the build order. It reads your
minerals, gas and supply from the resource panel on the screen, so keep StarCraft II visible on your primary
monitor. When you fall behind the plan, hit a supply block or bank minerals, it shows a call and speaks it. The
voice uses your browser's speech unless you set up the Docker voice. Each game is saved and shows up on the history
page.

**Stop it.** Press Ctrl+C in the PowerShell window running `run.ps1`.

### Optional: push-to-talk callouts

Hold a key, say what you saw, and the coach logs it. This needs the model weights, which are downloaded once, and
a microphone. Start with the weights:

```bash
# WSL, from the repo root
bash live/fetch_models.sh medium.en
```

Then, in a second PowerShell window:

```powershell
# PowerShell, from the repo root
powershell -ExecutionPolicy Bypass -File live\mic.ps1
```

The default key is the mouse Forward button (`mouse5`). Pick another with `--key` (for example `--key f13`, `--key
mouse4` or a letter). `--device cpu` forces the CPU, and `--model small.en` picks a smaller, faster model (fetch it
first with `bash live/fetch_models.sh small.en`).

### Optional: the whole stack with one command

`live\start.ps1` starts the helper, the rules brain and the mic in minimized windows, and `live\stop.ps1` stops
them. It also starts the two Rust services, so build those first:

```bash
# WSL, from the repo root
source rust/env.sh
curl --proto '=https' -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path --default-toolchain none
(cd rust/coach-core && cargo build --release)
(cd rust/panel-reader && cargo build --release)
mkdir -p live/out        # the start script writes its logs and PID file here
```

```powershell
# PowerShell, from the repo root
powershell -ExecutionPolicy Bypass -File live\start.ps1 -NoClaude   # -NoClaude: don't open a Claude Code session
powershell -ExecutionPolicy Bypass -File live\stop.ps1              # stops only what start.ps1 started, by PID
```

`start.ps1` puts the services on ports 8765 (helper), 8766 (panel reader) and 8767 (coach-core). Add `-NoMic` to
skip the mic. It skips any service whose port is already in use and never restarts one.

## Bots

Run `bot\run.py --list` to see every setting of every bot. Bots only control their own player.

| `--bot` | Race | What it does | Good for practising |
|---|---|---|---|
| `sixrax` | Terran | 6-Barracks Marine all-in. 16 SCVs, no gas, six Barracks by about 2:30, Marines nonstop. First wave at 16 Marines, then a wave every 8. | Holding an early Marine flood. |
| `dt` | Protoss | Dark Templar rush. Gateway, Cyber Core, Twilight, Dark Shrine, then Gateways making DTs that hunt your workers. | Getting detection up in time. |
| `proxy` | Terran | Proxy 2-Barracks Reaper/Marine near your natural, in waves. If the cheese stalls it switches to a 2-base bio game. | Scouting a proxy and holding it. |
| `mmm` | Terran | 2-base Marine/Marauder/Medivac macro game: wall, Orbital, Stim, Combat Shield, waves of 16. It scans cloaked attackers and builds turrets once cloak shows. | TvT macro practice, and a real game for testing the coach. |
| `skytoss` | Protoss | 2-base Protoss macro game into Void Rays, then Carriers. Waves of 30+ supply. | TvP against a Carrier build. |

`sixrax`, `dt` and `proxy` are cheeses. `mmm` and `skytoss` are normal macro games. All bots attack in waves, and
the macro bots only attack when they think they are ahead.

## Troubleshooting

**StarCraft II is not found.** python-sc2 looks for the game in `Documents\StarCraft II\ExecuteInfo.txt`, which the
game writes when it runs, and then in `C:\Program Files (x86)\StarCraft II`. If yours lives elsewhere, set the
`SC2PATH` environment variable to the install folder.

**Map not found.** Check that the `.SC2Map` file is in `bot/maps/` and that `--map` matches its file name. Names
that are not in `bot/maps/` are looked up in the game's `Maps` folder instead. `--dry-run` shows which one it
picked. If a map sent from `bot/maps/` is refused on your client, copy it into `C:\Program Files (x86)\StarCraft
II\Maps\` (create `Maps` if it is missing) and pass just its name.

**The 2v2 teams look wrong.** The API has no team field, so the map's default lobby layout decides. At the start,
your ally's base should show as allied (green). If it does not, try another 4-player melee map.

**`start.ps1` says it must run from the repo on the WSL share.** Run it from PowerShell in
`\\wsl.localhost\<distro>\home\<you>\sc2-booth-and-pilot`, not from a copy on a Windows drive.

**Windows git says "dubious ownership".** Use git from WSL. Windows git refuses this repo on the WSL share.

**A port is in use.** The coach page uses 8765. Start it on another port with
`powershell -ExecutionPolicy Bypass -File live\run.ps1 --port 8770` and open that port instead. `start.ps1` skips a
service whose port is already listening, so it can look like it started when something else holds the port.

**The mic is not heard or does not start.** If the log says `no weights at ...`, run `bash live/fetch_models.sh
medium.en` in WSL (or the model you chose with `--model`). If it says the GPU is not used, it is running on the
CPU, which still works but is slower. Check that you are holding the right key (`mouse5` by default; change it with
`--key`).

## Project layout

```
bot/            sparring bots (python-sc2): run.py launcher, one file per bot, micro modules, bot/README.md
live/           the live coach: helper (timing.py), page (overlay.html), history page, database, screen reader,
                push-to-talk (mic.py), rules brain, plans, ARCHITECTURE.md and CHANGELOG.md
live/voice/     the coach's voice: VoiceStudio container script and clip rendering
live/web/       Angular version of the page (optional)
rust/           coach-core (cue engine) and panel-reader (panel digit reader), as localhost services
tools/          checks, release notes, evals and test fixtures
docs/           ROADMAP.md
ci.sh           local checks: lint, types, Rust tests, Python tests, web build
```

How the coach works inside is in [live/ARCHITECTURE.md](live/ARCHITECTURE.md). Plans are in
[docs/ROADMAP.md](docs/ROADMAP.md).

## Contributing

Work from WSL. Run `bash tools/dev-setup.sh` once, then `bash ci.sh` before you send a change. Commit messages follow
Conventional Commits. [CONTRIBUTING.md](CONTRIBUTING.md) has the details.

## Third-party code

- `live/vendor/htmx.min.js`: [htmx](https://htmx.org) 2.0.4, Zero-Clause BSD licence.
- `live/vendor/alpine.min.js`: [Alpine.js](https://alpinejs.dev) 3.14.8, MIT licence.
- python-sc2 / burnysc2 (MIT), the Python packages in `live/.pkgs` and the npm packages are installed by the setup
  steps and are not included here.
- `rust/panel-reader/tests/fixtures/*.png` are small crops of the StarCraft II resource panel, used as test input.

## License

MIT. See [LICENSE](LICENSE).

StarCraft II is a trademark of Blizzard Entertainment. This project is not affiliated with or endorsed by Blizzard.
