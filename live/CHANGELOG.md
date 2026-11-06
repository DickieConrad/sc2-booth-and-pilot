# Release notes

Newest first. The page's "Release notes" button shows this file; the top version is the one running.

## v1.6.0 · 2026-10-31
- MMM --set reapers=2; the Zerg wall opens for our own units. Impact: the natural goes down on time vs Zerg; a 2-Reaper opening for demos.
- MMM third base, gas-off, optional Bunker; Medivacs keep to their wave. Impact: MMM keeps its economy past 12:00 and its Medivacs in the fights.
- Services survive the shell that started them; restart.ps1; medium.en mic; bot_games table. Impact: a restart from any session stays up, and the mic mishears less.
- Fix: untrack a worktree symlink that replaced bot/.pkgs; a more cautious Reaper. Impact: a fresh clone or worktree no longer clobbers bot/.pkgs.
- Fix: drop a stale DIFFICULTIES that rejected --vs cheatinsane. Impact: MMM can play the Cheater AIs again; it beat Insane Zerg on Thunderbird.
- Fix: cancel unfinished buildings by expected health, not raw health. Impact: no more lost Reactors and depots to a passing Zergling.
- Fix: MMM third CC waits 60 s between tries and never while attacked; Reaper waits out a crowd; games log their version. Impact: the bot keeps its SCVs when the third dies, and every bot game says which version played it.

## v1.5.0 · 2026-10-31 · Bots that pivot, micro and beat Insane
- The MMM bot beat every built-in AI level, up to Cheater 3 (Insane) Protoss, the hardest there is.
- MarineKing-style micro: Marines split from Banelings, Marines and Marauders stutter-step vs melee, focus fire, and Medivacs stay with the bio instead of flying in alone.
- The proxy bot pivots to a 2-base bio game when its cheese stalls, gets Stim, Shield and +1 first, and won vs Hard Protoss after 0-5.
- Waves only attack when ahead; a losing wave pulls back as a whole, and the next one waits until it out-numbers the army that beat it.
- The ramp wall closes as one piece (and stays shut vs Zerg until there's an army); dying unfinished buildings are cancelled for the refund.
- Vikings when Colossus or big air show up; Siege Tanks, an Armory and +2 spend the gas.
- The Reaper hunts workers, kites, regenerates nearby and cliff-jumps into the main instead of using the ramp.
- New Skytoss bot: a 2-base Protoss air game into Carriers, for TvP practice.
- Every bot game saves its replay; unflagged replays roll off after 14 days, wins vs Elite or cheating AIs are kept.
- The bot runner offers the cheating AI levels (Cheater 1-3).
- Fix: Combat Shield could be overwritten by Concussive on the same Tech Lab.

## v1.4.0 · 2026-10-31 · Sparring bots that macro and think
- New MMM sparring bot: a standard 2-base Terran macro game (Reaper expand, ramp wall, Orbitals and MULEs, Stim, Medivacs, Siege Tanks, +2). It won 6 of 6 tonight: Hard Zerg, Protoss and Terran (twice), plus Very Hard Zerg in 9:00.
- The bots attack only when they're ahead of the enemy army they've seen, and a wave that is losing pulls back to join the next one, instead of feeding units one at a time.
- The bots spend their bank, keep supply ahead, and stop holding minerals while a build step waits on gas.
- The proxy bot gets an Orbital with MULEs, and its later waves gather at home and grow to 10.
- The coach speaks Protoss and Zerg: Pylons/Overlords, Probes/Drones, Nexus/Hatchery, and no Terran-only calls in a Protoss or Zerg game.
- Fix: a high bank is called again every 25 s while it stays at 600 or more, until it's spent.
- Fix: no "Supply stuck: queue SCVs" for 30 s after a lost fight.
- Fix: the mic's quick answers only fire on enemy reports, not on Dickie's own questions ("Should I drop?" no longer gets "Drop. Pull SCVs").
- Fix: the coach keeps reading minerals, gas and supply over pale map walls, drops a frozen reading after 5 s, and follows the SC2 window when it isn't fullscreen.
- Fix: the in-game feed no longer crashes on an unusual character in a plan step or player name.
- Under the hood: roadmap notes (helper loop into Rust; the coach asking short questions about what it can't see).

## v1.3.0 · 2026-10-30 · Sparring & Senses
- Sparring bots (bot/): play against a 6-Barracks Marine rush, a DT rush or a proxy 2-Barracks, locally through Blizzard's bot API; steer them mid-game with bot/steer.py (cheese, attack, stop, gg).
- --coach-test: a bot plays the built-in AI fullscreen while the whole coach follows it; the coach now follows the bot's own build and race, not plan A.
- Bot games are their own player: History and the post-game review show your games by default, with a Player filter for the bots.
- Mic intent lane: in a game, "how do I counter Thors?" (24 units) and "pause" / "resume" get an instant spoken answer; scouting reports and "behind on..." callouts are tagged for the coach.
- No more step cues after a game ends, no overlapping cues, and no pile of old cues after you click Arm Voice.
- The review popup no longer opens on a helper restart mid-game; one misread no longer counts as lost buildings.
- Fewer false alarms: no supply-block call for the normal 15/15 wait at the start, and a fight under the same cap doesn't repeat the block.
- The brain's Stall and Turret reminders now work when the opponents' races are unknown.
- Fix: the plan line no longer says "vs None".
- Under the hood: typed JSON at every boundary (Pydantic) and mypy in CI; a stale connection to the Rust engine reconnects instead of dropping to Python for 30 s; full code review of the day's work.

## v1.2.0 · 2026-10-30 · Brain, Rust & Review
- The booth brain is live: depot, bank, gas and lost-building calls come instantly from pre-recorded clips instead of waiting for the coach.
- The brain no longer nags: bank and gas calls repeat only after 90 s or when the pile is 1.5x bigger, rotate their wording, and stay quiet while you're losing a fight.
- Calls are now decided by the Rust engine (coach-core), with the Python rules as a fallback; same calls on all 27 recorded games.
- About 43 ms less work on every helper tick (one network write per request to the Rust services).
- Depot gauge under Supply: shows room left, warns before you're blocked and counts blocks this game.
- Gas gauge under Gas (new page): turns red when gas floats while minerals are low and says how many SCVs to move.
- History page (History button or /history): every game with result, supply vs plan, blocks, bank, calls and notes.
- New page at /next/ (Angular): same dashboard, plus a post-game review that opens at Game Over with the one fix that matters ("Last game" reopens it).
- Compact mode on the new page (Compact button or the C key): just the call, clock, gauges and the next three steps, in big type.
- Fix: the build lists no longer scroll past the current step.
- The mic understands unit names better (Hellions, DTs, Starport, Terran...).
- Coach voice is 20% quieter; Dickie and Coach names are colored in the pointers.
- Under the hood: the coach is woken only for game events (blocks, lost fights, floats, idle production) plus a minute heartbeat; opt-in latency tracing (SC2_TIMING_TRACE=1).

## v1.1.0 · 2026-10-30 · Booth & Pilot
- The coach has his own voice ("the booth"): calm, British, rendered live on the GPU, no radio filter. The pilot keeps the timing calls.
- Push-to-talk: hold the mouse's Forward thumb button, speak, release. Transcribed locally; the coach answers on the page.
- New Booth button mutes only the coach's voice. Voice is the master switch; Step cues is the pilot's build steps.
- Coach pointers show the conversation too ("Dickie: ...", "Coach: ..."), refreshed every 3 s; game labels read "#22 L vs FoeOne".
- The coach watches a 30-second snapshot during games and makes short calls.
- Protoss: the helper detects when you play Protoss, uses the PX gate-expand plan (timings unverified) and calls "pylon" / "Probes".
- One voice at a time: clips and browser speech never talk over each other.
- Smoother timeline: the step lane redraws every frame and glides to the game clock instead of jumping.
- Fix: the first 0:00 sample no longer carries numbers over from the previous game.
- Fast lane: key words in a callout (roaches, drop, tanks, oracle...) get an instant pre-recorded reply in the coach's voice; misses are logged so new replies can be added between sessions.
- Faster mic: speech-to-text runs on the GPU (1.4 s -> 0.2-0.3 s per callout), CPU fallback.
- Protoss 1v1 plans PT / PZ / PP for patch 5.0.17, researched with sources; PX stays as the fallback.
- Release notes button on the page; versioning, linting and a local CI script (ci.sh).

## v1.0.0 · 2026-10-30 · First release
- Live timing helper: reads the SC2 client API and the resource panel (read-only), picks the plan from the matchup, makes short calls.
- Local page: clock, step lane, checkpoints, build lists, call feed, coach pointers.
- Game database (coach.db) with samples, calls and post-game notes.
- Plans: 2v2 Live Coach plans A-F, MMM, Terran Field Manual 1v1 (vs Z/P/T).
- Rust panel reader (~1 ms per read) and Rust cue engine (target cues, "now" catch-up cues, half-shift).
- The pilot's voice: pre-rendered clips through a cockpit-radio filter, Arm Voice after a reload.
