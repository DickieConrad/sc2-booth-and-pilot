"""Render the coach's clip library in the pilot's voice (studio.py: VOICE tags + SEED 8086).

Writes live/voice/clips/<slug>.wav and live/voice/clips/manifest.json:
  {"cues": {"<step text as the page speaks it>": "file.wav"}, "calls": {"<kind>": "file.wav"}}
Clips are recorded raw; the page adds the cockpit-radio filter at playback.
Run with the VoiceStudio container up and no game running (it uses the GPU):
  live\\.python\\python.exe live\\voice\\render_clips.py
"""
import hashlib
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import studio  # noqa: E402
import timing  # noqa: E402  (load_plans: every plan the page can show)

CLIPS = HERE / "clips"

CALLS = {
    "blocked": "Supply blocked, Commander! Depots, now!",
    "bank": "Ze bank is piling up! Spend it, schnell!",
    "behind": "You are falling behind! SCVs und production, ja?",
    "plan": "Plan locked in. Let's fly, Commander!",
}


def cue_text(action):
    """Same shortening as the page's cueText()."""
    a = re.sub(r"\s*\(.*?\)", "", action)
    a = re.sub(r"\bx(\d)", r"\1", a).replace("#", "")
    return a.strip()


def bot_plans():
    """Every sparring bot's build as the plan the helper follows in --coach-test (plan BOT changes per game), so the
    pilot has a clip for each of their steps too."""
    bot = HERE.parent.parent / "bot"
    sys.path.insert(0, str(bot))
    import core  # type: ignore[import-not-found]  # noqa: E402  (bot/core.py: coach_steps)
    from config import CONFIGS  # type: ignore[import-not-found]  # noqa: E402
    return [{"steps": core.coach_steps(cfg().build)} for cfg in CONFIGS.values()]


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] + "-" + hashlib.md5(text.encode()).hexdigest()[:6]


def main():
    CLIPS.mkdir(exist_ok=True)
    manifest = {"voice": studio.VOICE, "seed": studio.SEED, "cues": {}, "now": {}, "calls": {}}
    jobs = [("calls", kind, line) for kind, line in CALLS.items()]
    seen, seen_now = set(), set()
    for plan in [*timing.load_plans()["plans"].values(), *bot_plans()]:
        for st in plan["steps"]:
            if st["action"].startswith("Check"):
                continue
            short = cue_text(st["action"])
            if short not in seen:
                seen.add(short)
                jobs.append(("cues", short, f"{short}, five seconds!"))
            # Catch-up cue (coach-core "now" events): early steps that carry a supply trigger.
            if st["sec"] < 180 and st.get("supply") is not None and short not in seen_now:
                seen_now.add(short)
                jobs.append(("now", short, f"{short}, now!"))
    t0 = time.time()
    for i, (group, key, line) in enumerate(jobs, 1):
        name = slug(line) + ".wav"
        path = CLIPS / name
        if not path.exists():
            audio, ext, _ = studio.generate(line, {"effect_preset": "raw"})
            path.write_bytes(audio)
        manifest[group][key] = name
        if i % 20 == 0:
            print(f"{i}/{len(jobs)} clips ({time.time() - t0:.0f}s)", flush=True)
    (CLIPS / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"done: {len(manifest['cues'])} cues, {len(manifest['now'])} now-cues, {len(manifest['calls'])} calls "
          f"in {time.time() - t0:.0f}s -> {CLIPS}")


if __name__ == "__main__":
    main()
