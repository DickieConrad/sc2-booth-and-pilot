# Live dashboard (Angular)

Angular port of `live/overlay.html` (Angular 21: standalone components, signals, zoneless; esbuild + Vite dev server).
Same layout, CSS, constants and wording as the old page. The helper serves the build at
`http://127.0.0.1:8765/next/`; `/` stays the old page until the owner switches.

All commands from WSL in this folder (Node 20.19+; everything is local to `node_modules/`):

```
npm ci              # once (or after package-lock.json changes)
npm test            # Vitest unit tests (pure logic + voice queue), ~4 s
npm run build       # -> dist/ (base href /next/); the helper serves it at /next/ (needs a helper restart once)
npm start           # ng serve on http://127.0.0.1:4300, helper routes proxied (proxy.conf.mjs)
```

Dev proxy: `/state /events /plans /notes /changelog /voice /history /vendor /say /note` go to `SC2_HELPER`
(default `http://127.0.0.1:8765`). The helper runs on Windows and binds 127.0.0.1, which WSL in NAT mode cannot
reach, so from WSL either point `SC2_HELPER` at a stand-in (`SC2_HELPER=http://127.0.0.1:4301 npm start`) or use
WSL mirrored networking. Careful: the page can POST nothing itself, but `/say` and `/note` are proxied, so anything
you send through the dev server reaches the real helper (speakers, database).

`bash ci.sh` (full run, not `--fast`) runs the tests and a production build into a temp dir; both are skipped
when `node_modules/` is missing.

## Layout

```
src/app/
  types.ts           helper JSON shapes (State, Plans, Say, CueEvent, Note, Manifest)
  logic.ts           pure logic: clock smoothing, depot gauge, checkpoints, lane layout, fallback cues, pointers, changelog
  voice.service.ts   the one voice queue: clips (radio / clean chains, VOLUME) + browser speech; Voice / cues / booth toggles
  live.service.ts    /plans, /state, /events (SSE), game clock, 250 ms tick, per-update side effects
  view-mode.ts       compact in-game mode: top-bar button / `c` key / `?compact=1`, saved in localStorage
  parts/             top bar, call box + feed, stats + depot gauge, step lane (rAF), pointers, checkpoints, build lists,
                     next steps (compact mode)
src/styles.css       the old page's CSS, verbatim (keep in step with overlay.html while both exist)
src/compact.css      compact mode only (Angular page): hides parts with CSS, so the lane, lists and voice keep running
```
