# Live pilot voice: plan (2026-10-30, planning agent; not built yet)

**Recommendation: hybrid.** Stitched whole-phrase clips for the helper's own calls (0 ms, 0 GPU), live GPU
render only for the coach's free-form lines, and only if an FPS test passes. Everything cached; browser speech
is the last fallback.

## Facts measured
- OmniVoice loads in ~6 s and uses **1.94 GB VRAM** (2.97 → 5.08 GB with SC2 at the menu). The 33 s first call
  was likely compile warm-up. `/generate` has `num_step` (fewer = faster), `ref_audio`, `stream`;
  `OMNIVOICE_DEVICE` can pin CPU.
- Gap today: helper calls contain numbers ("Bank 1300, spend") but the page plays one fixed clip per kind,
  so the number isn't spoken.

## Options
| | latency | FPS risk | VRAM | quality | effort |
|---|---|---|---|---|---|
| a GPU live, warm | ~0.6 s | spike per line; no GPU priority on Windows | +2 GB | best | low |
| b CPU only | several s (unmeasured) | competes with SC2 CPU | 0 | same | low |
| c stitch phrases | 0 | 0 | 0 | phrase-level fine, word-level choppy | medium |
| d hybrid (pick) | 0 calls / ~0.6 s new lines | only on cache miss | +2 GB | good | medium |
| e other engines | KittenTTS presets don't match; CPU engines need our voice as a reference clip ("cloning" our own designed voice: owner's call) |

Phrase set for (c): "Bank N, spend!" (N in 50s, ~100 clips), "Supply blocked, N of N" (≤200),
"Behind: N supply" + "plan N" + step clip. ~500 clips, ~5 min render.

## Measurement (before enabling live GPU)
SC2 FPS (Ctrl+Alt+F), PresentMon (portable, live/tools/), `typeperf` GPU counters, `nvidia-smi -lms 200`.
Same vs-AI game or replay, 2 min each: container off / warm idle / one line every 10 s.
Pass: avg FPS drop < 5 %, 1 % lows < +5 ms, no new stalls > 50 ms, VRAM < 9 GB, live line starts < 1.2 s (p95).

## Design
`outputs/voice.py` in the helper turns a line into audio: hash-cached clip → stitched phrase → live render
(1.5 s timeout) → none (browser speech). Uncached lines are rendered between games; one dummy generation
before each game for warm-up. Each say carries `audio: {status, files, prio}`; pending renders arrive over SSE.
Page: priority queue (call > coach > cue, stale cues dropped), 25 ms crossfades, pending > 4 s → browser speech.

## Steps
1. Offline bench: num_step 32/16/8 ± ref_audio, plus a listening test. 2. FPS/VRAM test above → go/no-go for GPU live.
3. voice.py with cache + fake service tests. 4. Phrase clips in render_clips.py. 5. Helper audio field + cache route.
6. Page queue/crossfade/pending. 7. Live + between-game caching in vs-AI and custom games.

## Risks / questions for the owner
- WSL2 can't deprioritize the container's GPU work: step 2 decides. Warm-up must happen before the match.
- Use `docker stop -t 30` (a plain stop was killed with exit 137 after an unclean shutdown).
- German flavour lives in spelling: free-form lines need it.
- Questions: is ~1 s acceptable for coach lines? How many free-form lines per game? OK to use our designed
  voice as a reference clip for other engines?
