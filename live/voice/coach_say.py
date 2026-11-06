"""Speak one coach line on the local page in the coach's own voice, rendered live on the GPU (VoiceStudio).

Renders the text with studio.COACH_VOICE / COACH_SEED, caches it as clips/coach-<slug>-<hash>.wav (served by the
helper's /voice/ route; a repeated line is instant), then POSTs /say {"text", "audio"} to the helper. If the render
fails or takes longer than BUDGET seconds, it POSTs the text alone and the page uses browser speech instead.

Usage (from Windows, project root):
    live\\.python\\python.exe live\\voice\\coach_say.py "Bunker at the natural now."
    live\\.python\\python.exe live\\voice\\coach_say.py --warm      # first render after a container start is slow
"""
import argparse
import hashlib
import json
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import looptime  # noqa: E402
import studio  # noqa: E402

CLIPS = HERE / "clips"
BUDGET = 3.0   # seconds: past this the line goes out as browser speech (a late line is worse than a plain voice)


def clip_name(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "line"
    return f"coach-{slug}-{hashlib.md5(text.encode()).hexdigest()[:8]}.wav"


def render(text):
    """Render with the coach's voice; return wav bytes (raises on failure or a non-wav reply)."""
    audio, ext, _ = studio.generate(text, {"instruct": studio.COACH_VOICE, "seed": studio.COACH_SEED,
                                           "speed": studio.COACH_SPEED, "effect_preset": "raw"})
    if ext != ".wav" or audio[:4] != b"RIFF":
        raise RuntimeError(f"expected wav, got {ext} ({len(audio)} bytes)")
    return audio


def render_within(text, budget):
    """Render in a worker thread; return (wav bytes or None, error or None). The worker is abandoned past budget."""
    box = {}

    def work():
        try:
            box["audio"] = render(text)
        except Exception as e:
            box["error"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(budget)
    if th.is_alive():
        return None, f"render over {budget:g}s"
    return box.get("audio"), box.get("error")


def post_say(port: int, text: str, audio: str | None = None, kind: str | None = None) -> int:
    """kind (optional) labels the call in the DB (default "coach"); the booth brain sends "brain-<kind>"."""
    body = {"text": text, **({"audio": audio} if audio else {}), **({"kind": kind} if kind else {})}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/say", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status


def post_note(port: int, text: str) -> int:
    """The line also goes in the DB notes, so it shows in the page's Coach pointers next to the owner's callouts."""
    body = json.dumps({"text": f"Coach: {text}"}).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/note", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", help="the line to say")
    ap.add_argument("--warm", action="store_true", help="render one throwaway line (no post) to wake the model")
    ap.add_argument("--port", type=int, default=8765, help="the helper's local page port")
    ap.add_argument("--budget", type=float, default=BUDGET, help="seconds to wait for a render before falling back")
    ap.add_argument("--no-note", action="store_true", help="don't add the line to the DB notes / Coach pointers")
    a = ap.parse_args()

    t0 = time.time()
    if a.warm:
        try:
            audio = render("Coach online. Calm hands, sharp eyes.")
            print(f"warm: ok, {len(audio)} bytes in {time.time() - t0:.1f}s")
        except Exception as e:
            print(f"warm: FAILED in {time.time() - t0:.1f}s: {e}")
            sys.exit(1)
        return
    text = (a.text or "").strip()[:200]
    if not text:
        ap.error("give the line to say (or --warm)")

    name = clip_name(text)
    path = CLIPS / name
    if path.exists():
        how, err = "cached", None
    else:
        audio, err = render_within(text, a.budget)
        if audio:
            CLIPS.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")   # not servable until complete (the /voice/ route only serves .wav)
            tmp.write_bytes(audio)
            tmp.replace(path)
            how = "rendered"
        else:
            how = None
    took = time.time() - t0
    trace = looptime.tracer("say", buffered=False)   # SC2_TIMING_TRACE=1: render/cache + POST times
    try:
        tp = time.perf_counter()
        status = post_say(a.port, text, name if how else None)
        trace.emit({"k": "say", "w": round(t0, 3), "how": how or "fallback", "prep": round(took * 1000, 1),
                    "post": round((time.perf_counter() - tp) * 1000, 1), "text": text[:60]})
        if not a.no_note:
            try:
                post_note(a.port, text)
            except Exception as e:
                print(f"note: failed ({e})")
    except Exception as e:
        print(f"say: POST FAILED ({e}); voice {how or 'fallback'} in {took:.2f}s")
        sys.exit(1)
    if how:
        print(f"say: coach voice ({how}) {name} in {took:.2f}s, post {status}")
    else:
        print(f"say: browser fallback ({err}) after {took:.2f}s, post {status}")


if __name__ == "__main__":
    main()
