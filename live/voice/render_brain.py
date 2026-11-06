"""Render every booth-brain phrasing (brain.KINDS clip texts, no numbers) into the coach clip cache.

Same voice and file names as coach_say.py, so brain_run.py finds them with clip_name(). Nothing is posted.
Lines already cached are skipped (--force re-renders). Needs the VoiceStudio container (sc2coach-voice, :3900) up.
    live\\.python\\python.exe live\\voice\\render_brain.py [--force]
"""
import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brain  # noqa: E402
from coach_say import CLIPS, clip_name, render  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-render lines that are already cached")
    a = ap.parse_args()
    lines = brain.all_clip_texts()
    CLIPS.mkdir(parents=True, exist_ok=True)
    t0, done, cached, failed = time.time(), 0, 0, 0
    for line in lines:
        path = CLIPS / clip_name(line)
        if path.exists() and not a.force:
            cached += 1
            continue
        t = time.time()
        try:
            audio = render(line)
        except Exception as e:
            failed += 1
            print(f"FAIL  {line}  ({e})", flush=True)
            continue
        tmp = path.with_suffix(".part")   # the helper's /voice/ route only serves complete .wav files
        tmp.write_bytes(audio)
        tmp.replace(path)
        done += 1
        print(f"ok    {time.time() - t:4.1f}s  {path.name}", flush=True)
    print(f"brain: {len(lines)} lines, {done} rendered, {cached} cached, {failed} failed, "
          f"{time.time() - t0:.1f}s total", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
