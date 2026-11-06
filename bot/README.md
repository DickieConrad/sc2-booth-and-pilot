# Sparring bot

Bots that play the cheeses Dickie needs to practise against, in **local games only**, through Blizzard's official
StarCraft II bot API (via [python-sc2 / burnysc2](https://github.com/BurnySc2/python-sc2)). Dickie plays them live
in realtime, 1v1 or 2v2 with an AI ally. Nothing here touches Battle.net, reads game memory or sends input to
Dickie's own games; the bots only control their own player through the API. Separate from the coach (`live/`),
which stays read-only.

| `--bot` | Race | What it does |
|---|---|---|
| `sixrax` | Terran | **6-Barracks Marine all-in** (the one that beat Dickie). 16 SCVs, no gas, depot then six Barracks by ~2:30, Marines nonstop. First wave at 16 Marines (or 8+ at 3:45), then a wave every 8. Up to 8 Barracks from a bank. |
| `dt` | Protoss | **DT rush.** Pylon, Gateway, 2 gas, Cyber Core, Twilight, Dark Shrine, 3 Gateways. Two Stalkers hold home; once the Shrine is done every Gateway makes a DT whenever it can (chrono-boosted). The first 3 DTs hunt workers (the first 2 together, then alone); later DTs join the army. With spare money: more Gateways (up to 6) and Zealots / Stalkers / Adepts; the army attacks in waves (10 units, or 6+ at 5:30, then every 6). |
| `proxy` | Terran | **Proxy 2-Rax Reaper/Marine.** Depot at home; two SCVs leave at 0:16 and build two Barracks near the enemy natural (toward the map centre). One Reaper (hunts workers), then Marines in waves of 4 (or 2+ at 2:30). The army (home Barracks' Marines too) stages at the proxy while a proxy Barracks stands, so waves leave from near the enemy natural. One more Barracks at home from 4:00; with a bank, more from 2:30 (up to 5 in all). **Pivots to macro** once the cheese has stalled (see below). |

| `mmm` | Terran | **2-base MMM macro** (not a cheese: a real Terran game for the coach). Ramp wall (2 depots + Barracks; depots lowered, raised while enemies are at them), gas, Reaper, Orbital, CC at the natural ~1:45, Factory, 2nd gas, Barracks Reactor, Starport; Barracks 2-5 (2 Tech Labs, then Reactors), 4 gas, Engineering Bay. Stim -> Combat Shield -> Concussive, +1 attack; MULEs; 44 SCVs. Marines, Marauders, Medivacs; first push waits for Stim and 40 army supply (or 6:45), then waves of 16 (24 from 10:00). The bank adds Barracks (up to 8) from 5:00. |
| `skytoss` | Protoss | **2-base Skytoss into Carriers** (not a cheese: TvP practice vs Carriers). Pylon, Gateway, gas, Nexus at the natural ~1:30, Cyber Core, 2nd gas, a Pylon at the natural; an Adept + a Stalker defend; Stargate ~2:45-3:00, Void Rays (up to 5; 2 Phoenixes once enemy air shows), 4 gas, 2 Shield Batteries at the natural, 2nd Stargate, Fleet Beacon ~5:30-6:00, 3rd Stargate, then Carriers (Interceptors by autocast). +1/+2 air weapons and armor from the Core; Chrono on Stargates > Probes > Core; 44 Probes. First attack with 6 Carriers or 80 army supply at home (or 10:00), then waves of 30+ supply. A gas bank adds Stargates (up to 5) from 7:00; Zealots only from an 800 mineral bank or while the base is attacked. A placement that keeps failing adds a Pylon and stops holding the bank after 20 s. |

All of them: macro basics (workers to a cap, supply ahead of need, workers spread over minerals and gas), the
cheese, then attack-move waves at the closest known enemy building, else the enemy main(s), else roaming the
bases. Units at home fight anything that comes within 25 of their buildings.

Around the cheese (the cheese timings themselves don't change; a build-order step that waits for money always goes
first):

- **Supply ahead:** free supply is kept at `supply_base` + `supply_per_prod` x production + `supply_per_townhall`
  x townhalls (2 + 2 + 2 by default: 16 for six Barracks), counting depots/pylons under way as free; one under way
  at a time plus one per 3 production buildings (`supply_max_pending` at most). While a needed one can't be
  afforded, units and workers wait so they don't eat its money.
- **Saving for a step** (`core.step_saving`): a due build-order step holds the bank ("saving": no units, no auto
  depots) only while its minerals are short. One we can afford but couldn't place doesn't (`place_fail_spends`, on;
  Skytoss off, it has `place_give_up`): vs Insane Zerg on Acropolis (2026-10-31) the lost natural's Command Center
  kept failing to place and the bot sat at 33/31 supply with 1,312 minerals from 4:00 to 5:30+.
- **Spending the bank:** once the build order is placed, `spend_bank` (400) minerals or more add one production
  building at a time (two once the bank is double), up to `prod_cap`, not before `spend_time`; idle production
  queues units every step.
- **Staging:** the waiting army gathers `stage_forward` (0..1) of the way from home toward the target (0 = home,
  the default for 6-rax and DT, where the waiting army defends); waves leave from there. ProxyRax stages at the
  proxy while a Barracks stands within `proxy_alive_radius` of it, else at 0.4. "stop" still pulls everyone home.
- **Attack only when ahead:** the bot remembers the enemy army it has seen (each combat unit by tag with its supply
  from the game data; workers, Overlords and structures don't count) for `enemy_memory` (75 s) after it was last
  seen, or until it dies. A wave the unit counts allow leaves only when the army on the attack (at home + still out)
  has at least `ahead_ratio` x that supply (MMM 1.3, ProxyRax 1.2, 6-rax and DT 1.0); nothing seen yet = go (a
  cheese's first wave keeps its timer). Logged once per change: `holding wave 3 at 5:10: 14 vs enemy 22 (x1.3
  needed, ...)`. "attack" / "cheese" steering, all-in and a maxed army (190 supply) skip it.
- **Regroup when losing:** sent units whose front (the ones within `retreat_radius` 12 of enemy army, and everything
  of both sides within 12 of their centre) is outweighed (ours < `retreat_ratio` x theirs: 0.7, 6-rax and DT 0.6)
  walk back to the rally point, ignoring threats until near it (30 s at most), and join the next wave. Then no wave
  and no retreat for `retreat_cooldown` (15 s). Not while all-in, nor for `force_commit` (30 s) after "attack".
  `--set ahead_ratio=0` / `--set retreat_ratio=0` turns either off.
- **Bio micro** (`micro`, on; `micro/`, every step, Marines and Marauders within `micro_radius` 10 of an enemy,
  at most `micro_cap` 60): **splits**: a Marine with a Baneling (or cocoon) within `split_radius` 8 runs
  `split_step` 3 away from it and from the Marines next to it for `split_window` 1.5 s, fights 3 s, repeats while
  Banelings are near; with its weapon ready it shoots a Baneling in range instead. **Stutter-step**: a melee enemy
  (Zergling, Zealot, Baneling, Ultralisk, Broodling, ...) within `kite_range` 3.5 (edge to edge) = step `kite_step`
  2 back while more than `kite_frac` 0.35 of the reload is left, then shoot; ranged units are not kited. Steps go
  straight back, else turned 45/90 degrees (walkable ground only). **Focus fire**: Banelings, then the lowest HP
  among units that fight back, then the rest; a target in range is kept. Medivacs near the fight stay within
  `medivac_leash` 6 of the bio. Retreating units and "stop" are left alone. `--set micro=false` = plain a-move.

Micro modules live in `bot/micro/`. `BioMicro` builds one `FightCtx` per enabled step and runs the registry
in this fixed order: `escort_medivacs`, `escort_vikings`, `splits`, `fight`, `leash_medivacs`. Escorts run even
without enemies. Existing settings still control selection and behavior; there is no new setting or baseline
change. Splits retain their immediate fight fallback before the next splitter. The final Medivac leash may
issue another order to an escorted Medivac, preserving the original last-order-wins behavior.

To add a module, create one file with a class implementing `MicroModule`: a `name` and
`step(ctx: FightCtx) -> set[int]`. Read the observation and shared state from `ctx`, skip `ctx.claimed` tags,
and return only tags actually ordered; the controller adds them to the claimed set. Register its factory at
the intended priority in `micro/registry.py` (`MODULES`) and gate selection in `make_micro(cfg)` if needed.
Keep pure decisions/geometry in `core.py` and shared pathing/classifications in `micro/helpers.py`. Add order
trace scenarios in `test_micro.py`; its frozen characterization traces cover the original behavior without SC2.
`from micro import BioMicro` and the `BioMicro.walkable()` helper used by Reapers remain available.

- **Scan on cloak** (MMM; `scan_cloak`, on; `micro/scan_cloak.py`): an Orbital with 50+ energy scans an undetected
  cloaked enemy that attacks (DT, cloaked Banshee or Ghost, Lurker) once `scan_army` (2) of our army units are within
  10 of it, aimed at the one with the most cloaked units around it; no second scan within 12 of one cast in the last
  4 s. So a scan is there when it's needed, MULEs leave 50 energy across the Orbitals vs Protoss from
  `scan_reserve_time` (4:00) and vs anyone once cloak is seen (a Dark Shrine, DT, Lurker or Lurker Den, or any
  cloaked attacker). **Turrets** (`cloak_turrets`, on): once cloak is seen and the Engineering Bay is ready, a Missile
  Turret in each mining base's mineral line. `--preset baseline` turns both off (MULEs at 50 energy, as ba15df0).
- **Auto-gg** (`auto_gg`, on): no townhall and fewer than `gg_no_base_units` (5) units, or from `gg_time` (6:00)
  fewer than `gg_min_units` (6) army + workers, held for `gg_confirm` (5 s): the bot types gg and leaves, so Dickie
  isn't hunting buildings. `--set auto_gg=false` turns it off.
- **Accept surrender** (`accept_gg`, on): the enemy types gg (the built-in AI does when it gives up): the bot leaves
  and `run.py` logs the game as a Victory, so a batch never waits on the surrender dialog. Bookkeeping only, so
  not in `--preset baseline`; `--set accept_gg=false` turns it off.

### ProxyRax pivot to macro

The cheese (proxy, Reaper, Marine waves) stays as above until `pivot_time` (4:00). From then on, at the first sign
it has stalled (`core.pivot_reasons`), the bot pivots once to a 2-base bio game: the bank stayed at `pivot_bank`
(500)+ for `pivot_bank_hold` (20 s), the proxy Barracks are gone, `pivot_retreats` (1) waves were pulled back, or
`pivot_holds` (1) waves were held for not being ahead (0 = that sign off). Logged as `pivot to macro at 04:00: bank
885, 3 retreats, 2 held waves, proxy alive, 16 SCVs, 5 Barracks (bank, retreats, waves held)`. "pivot" steering
does it at once (also during "cheese now", which otherwise blocks it); `--set pivot_time=0` turns it off.

After the pivot (`core.pivot_config`; the pieces are MMM's, `bio.BioBot`): Command Center at the natural (SCV sent
ahead; units wait up to 45 s for its 400), Orbitals + MULEs, SCVs to `pivot_workers` (40), gas to `pivot_gas` (3,
two per base), Factory -> Starport (Medivacs, one per 8 bio, up to 6), Tech Labs on 2 home Barracks (Stim -> Combat
Shield -> Concussive, Marauders up to 35% of the bio), Reactors on the rest, Barracks up to `pivot_rax` (8) from a
`pivot_bank` bank, depots behind the minerals and supply for Reactor Barracks, Stim in fights. Waves of `pivot_wave`
(16) gather at home and keep the ahead / regroup rules. A Barracks keeps making Marines until its add-on is
affordable. Expected (not yet seen live) from a 4:00 pivot with ~800 banked and next to no gas: natural ~4:05
(done ~5:15), 2nd gas ~4:05, Factory ~4:30-4:45, Tech Labs ~4:45, Stim ~5:00 (done ~6:40), Starport ~5:20, first
Medivacs ~6:00-6:30, ~40 SCVs ~6:30-7:00.

## Setup (once, on Windows)

burnysc2 is installed **into this folder only** (`bot/.pkgs`, git-ignored), with the project's Windows Python:

```powershell
# from the repo root on Windows
live\.python\python.exe -m pip install --target bot\.pkgs burnysc2
```

python-sc2 finds StarCraft II through `Documents\StarCraft II\ExecuteInfo.txt` (written by the game) or
`C:\Program Files (x86)\StarCraft II`; set `SC2PATH` if it lives elsewhere.

### Maps

The bots need `.SC2Map` files. Put them in **`bot/maps/`** (git-ignored): `run.py` sends a map from there to
StarCraft II as bytes (`RequestSaveMap` + `LocalMap.map_data`), so nothing has to be copied into the game folder.

- **1v1:** Blizzard's API map packs, <https://github.com/Blizzard/s2client-proto#map-packs> (zip password
  `iagreetotheeula`, which accepts the AI and Machine Learning License). `Melee.zip` has `Simple64`/`Simple96`
  (small, quick tests); the `Ladder*` packs have real ladder maps. Extract the `.SC2Map` files into `bot/maps/`.
- **2v2:** the API packs are 1v1 maps. A 2v2 map (4 start locations) has to come from the game itself: open a 2v2
  map in the StarCraft II Editor (File > Open > Battle.net tab) and *Save As* into `bot/maps/`. Ladder maps are
  sometimes locked against opening; then use any unlocked 4-player melee map.
- **Fallback** if a map sent as bytes is refused on the first run (not verified on a live client yet): copy the
  map into `C:\Program Files (x86)\StarCraft II\Maps\` (create `Maps` if missing; that's where python-sc2 looks by
  default) and pass just its name: `--map Simple64`. Names not found in `bot/maps/` are looked up there.

## Playing against it

Close the normal StarCraft II client first: python-sc2 starts its own SC2 windows (`SC2_x64.exe`, offline, no
Battle.net). **1v1 vs a bot = 2 windows** (yours, 1600x900 at the top-left, and a small one for the bot);
**2v2 = 3 windows** (yours + one per bot). Play in yours; leave the small ones open (closing one ends that bot).
The API client starts with a fresh profile, so hotkeys are the **Standard** layout. Realtime = Faster speed.

```powershell
# from the repo root on Windows
live\.python\python.exe bot\run.py --bot sixrax --vs human --map Simple64        # Dickie vs 6-rax
live\.python\python.exe bot\run.py --bot dt --vs human --map "Equilibrium LE"    # vs DT rush
live\.python\python.exe bot\run.py --bot proxy --vs human --map Simple64         # vs proxy 2-rax
live\.python\python.exe bot\run.py --bot skytoss --vs human --map "Equilibrium LE" # TvP vs Carriers
live\.python\python.exe bot\run.py --bot sixrax --bot2 dt --vs human --2v2 --map "<2v2 map>"
live\.python\python.exe bot\run.py --bot sixrax --vs easy --map Simple64         # bot vs built-in AI (test)
live\.python\python.exe bot\run.py --bot dt --vs medium --map Simple64 --time-limit 10:00   # test, stops at 10:00
live\.python\python.exe bot\run.py --list                                        # bots + every setting
```

Useful options: `--set key=value` (repeatable, any setting from `--list`, e.g. `--set attack_time=3:15
--set first_wave=10`; times take m:ss), `--fullscreen` / `--window 1920x1080`, `--replay C:\path\game.SC2Replay`,
`--realtime` (bot vs AI at real speed; always on vs human), `--time-limit m:ss` (bot vs AI only: python-sc2's
`game_time_limit` stops the game there; it ends as a Tie and prints `Result: Tie (time limit 10:00 reached: stopped,
no winner)`), `--ai-race`, `--ally-race`, `--ally-difficulty`,
`--dry-run` (print seats and map, start nothing), `--coach-test` (bot vs AI for testing the coach: realtime, the
bot's window fullscreen so the coach can read its panel). Timings live in `config.py`.

### `--preset baseline`: the bot that won

`--preset NAME` applies a named list of `--set` values (`config.PRESETS`) before your own `--set` ones, which win.
`baseline` is commit ba15df0, the bot that beat Insane Zerg twice (Thunderbird, Ephemeron): every behaviour change
since is switched off, so changes go back in one at a time (`--preset baseline --set gas_off=800`). Keys a bot
doesn't have are skipped for it. `--dry-run` prints what was applied; the games log (`--games`) records it.

| `--set` (baseline value) | On (default) | Off (= ba15df0) |
|---|---|---|
| `reaper_wait=0` | a Reaper that backed off from 2+ enemies waits 20 s before trying again | no wait |
| `reaper_hideout=false` | backing off heads for the way out under the enemy main's cliff, heals there | straight away from the threat, heals where it stops |
| `viking_leash=0` | a Viking stays within 7 of its wave's bio; chases air only while that air is near the bio, never past 14 | attack-moves with the wave, flies in ahead |
| `medivac_wave=false` | a sent Medivac keeps to its own wave; flies home when that wave is dead | follows the closest bio unit |
| `wall_scv=false` (MMM) | any SCV at the ramp wall opens it, mining or idle | only units with orders that aren't gathering |
| `recall=0` (MMM) | a sent wave comes home when the enemy at our buildings is 6+ supply and half the wave's; no new wave leaves meanwhile | a sent wave never comes back |
| `prod_clear=false` (MMM) | a production building off the grid keeps room for its add-on and never goes on a mineral-line side | python-sc2's spiral search, anywhere |
| `depot_clear=false` (MMM) | depots behind the minerals first, never on an add-on square or a free production-grid spot | behind the minerals, then next to the main (add-on squares included) |
| `starport_reactor=false` (MMM) | each Starport builds a Reactor before its first unit (Vikings needed now skip it) | bare Starports, one unit at a time |
| `wall_hold=0` (MMM) | an opened wall stays open 6 s after the last passer (SCVs were stuck as it flapped) | shuts the step the last passer leaves |
| `gas_off=0` (MMM) | gas SCVs mine minerals above 800 banked gas, back under 300 | never |
| `third_supply=0` (MMM) | the third Command Center also goes down at 100 supply used (even with `third_time=0`) | not at any supply |
| `third_time=0` (MMM) | third Command Center at 40 SCVs or 5:30, SCV cap 60 once it's placed | no third, cap stays 44 |
| `third_fly=false` (MMM) | the third CC is built in the main (off the grid and the mineral line), lifts once done, lands at the third; hovers back over the main while enemy ground is at the spot; landed: Planetary Fortress vs `third_pf` races (`zerg`; needs an Engineering Bay), else Orbital. No room in the main (spots clear of the first `third_grid_keep` (12) production-grid spots) = built at the spot, still made a PF | an SCV builds it at the spot |
| `third_stage=0` (MMM) | while the third is wanted the waiting army gathers `third_stage` (0.5) of the way from the natural to the third | the usual stage point (home) |
| `bunker=false` (MMM) | (default off) a Bunker at the natural | none |
| `place_fail_spends=false` | an affordable step that fails to place doesn't hold the bank | it holds ("saving") |

With `--coach-test` the bot **announces itself** to the coach: before the game `run.py` writes
`live/out/bot_game.json` (atomic) with the first bot's in-game name (`SixRax`, `DtRush`, `ProxyRax`, `MMM`, `Skytoss`; its window is
the fullscreen one the coach reads), race, bot key and its `build` as a coach plan (`core.coach_steps`: one step per
build step, `sec` estimated from the step's supply, see `live/ARCHITECTURE.md` "Players"). The file is deleted
after the game; the helper ignores one older than 10 minutes. The timing helper then follows the bot instead of
looking for Dickie: its race (Protoss calls for DT), its own build as the cue plan (no more "Third Barracks" from
plan A while 6-rax already has six), its result, no Live Coach pushes. The game goes into `live/coach.db` with
`player` = the bot's name, so `/history` (Player dropdown) and `/history/review/latest?player=SixRax` work on bot
games while Dickie's history and review stay his. `run.py` posts nothing to the helper.

Leaving or losing the game ends it; Ctrl+C in the terminal closes the SC2 windows python-sc2 started (only those).

### 2v2 support

- **Supported (built here):** Dickie + built-in AI ally (Protoss, Hard by default) vs two bots (`--bot2` picks the
  second, default the same bot), and two bots vs two built-in AIs (`--vs <difficulty> --2v2`).
  python-sc2's own `run_game` can't do this (it handles one participant + AIs, or two participants and no AI, and
  drops the AIs then), so `bot/match.py` runs it: one SC2 process per human/bot, the game created on the first with
  every seat (AIs included), all joined with one shared port config (SC2's multiplayer API).
- **Unverified on a live client:** which seats end up on which team. The API has no team field: the map's default
  lobby layout decides. Seats are created in the order Dickie, ally, bot, bot (players 1+2 vs 3+4, the usual 2v2 map
  layout). First 2v2 run: check the ally's base shows as allied (green) at the start. If not, the map is laid out
  differently (or as FFA); try another 2v2 map.
- **Not supported: two humans (Dickie + his buddy).** Every API player needs its own SC2 process connected to this
  script on this PC; there is no Battle.net lobby, so the buddy can't join from his own PC. (SC2's API has a LAN
  mode, but it needs SC2 + this repo + Python on his PC and port forwarding; not in v0.) Use the AI ally instead.
- 1v1 vs the bot is python-sc2's standard human-vs-bot setup (two SC2 processes). `--stock` runs 1v1 through
  python-sc2's own `run_game` instead of `bot/match.py`, as a fallback (map must then be in the SC2 Maps folder).

## Steering it mid-game

Every bot reads **`bot/out/command.json`** every 2 game seconds (`poll_seconds`). Write it from anywhere (WSL or
Windows); the bot acts once on each new command and says it in chat (`[coach] cheese`).

```powershell
python3 bot/steer.py cheese     # WSL; or on Windows: live\.python\python.exe bot\steer.py cheese
```

| Command | JSON | Effect |
|---|---|---|
| cheese | `{"cheese": "now"}` | Go all-in now: no more workers; 6-rax/proxy attack with everything, proxy sends its SCVs now; DT skips the Stalkers and sends every DT the moment it's out. |
| attack | `{"attack": "now"}` | Send every army unit now, whatever the wave size. |
| stop | `{"stop": true}` | Pull everything back home and hold; no waves until `attack` or `resume`. |
| resume | `{"resume": true}` | Waves continue on schedule. |
| gg | `{"gg": true}` | The bot types gg and leaves (ends the drill). |
| pivot | `{"pivot": "now"}` | ProxyRax: leave the cheese for the 2-base macro game now (other bots ignore it). |

Rules: values `true`, `"now"`, `"yes"`, `1` all mean on; several actions can be combined. A command fires when the
file's content changes; add `"seq"` (e.g. unix time in ms, as `steer.py` does) to repeat the same command, and a
`seq` lower than the last one is ignored. Whatever is in the file when a game starts is treated as old and never
fires. Write the file atomically (temp file + rename) so a bot never reads half a file; `core.write_command` does.

## Files

| File | What |
|---|---|
| `run.py` | CLI: seats, map, overrides; starts the game. |
| `match.py` | Runner for any seat mix (2v2, maps sent as bytes), built from python-sc2's own pieces. |
| `sparring.py` | `SparringBot` base: steering, build-order steps, workers/supply, waves (only when ahead; regroup when losing), targets (2v2: skips the ally's main). |
| `sixrax.py`, `dtrush.py`, `proxyrax.py`, `mmm.py`, `skytoss.py` | The five bots. |
| `bio.py` | `BioBot`: the 2-base bio pieces (natural, grid, add-ons, research, unit mix, Stim) MMM plays and ProxyRax pivots to. |
| `micro/` | `BioMicro`, shared context/helpers and module registry: Medivac/Viking escorts, splits, fight, Medivac leash. |
| `config.py` | Every timing and size, one dataclass per bot. |
| `core.py` | Pure logic (stdlib): steering parser/reader/writer, build-order steps, overrides, map lookup, seats. |
| `steer.py` | Write a steering command. |
| `test_core.py` | Tests for `core.py` + `config.py`; stdlib only, run by `ci.sh` in WSL (no SC2, no burnysc2). |

The bot code (`sparring.py`, the bots, `match.py`, `run.py` beyond `--dry-run`/`--list`) needs burnysc2 and a
running SC2, so it isn't unit-tested; check it with `--vs easy` games.
