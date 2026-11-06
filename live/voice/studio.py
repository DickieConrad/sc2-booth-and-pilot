"""Talk to the local VoiceStudio container (live/voice/up.sh): generate one line in the coach's voice.

The voice is an original character designed from a text description (no cloning):
VOICE below + a fixed SEED keep it the same voice across clips.

Usage: python studio.py "Supply depot, five seconds." out.ogg     (run from Windows: live\\.python\\python.exe)
"""
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:3900"
KEY = (HERE / ".env").read_text(encoding="utf-8").split("=", 1)[1].strip()

# OmniVoice only accepts a fixed tag vocabulary (gender, age, pitch, a few accents, whisper), so the owner's brief
# below is mapped to the closest tags; the German flavour goes into the scripted lines ("zose", "ja") and the
# cockpit-radio sound is applied by the page's audio filter at playback.
VOICE = "female, young adult, moderate pitch, russian accent"
SPEED = 1.15

# The owner's character brief (2026-10-30): an original character, not a clone.
BRIEF = ("Adult woman, early 30s, mid-range alto voice. Confident, crisp and slightly smug, like a showboating "
         "fighter pilot on comms. Heavy theatrical German accent with a noticeable Russian/Slavic edge: rolled or "
         "tapped R's, W pronounced as V, TH pronounced as Z or D, hard clipped consonants, short vowels. Delivery is "
         "fast, punchy and military, with short barked phrases and a playful upward flourish at the end of lines. "
         "Campy and charismatic rather than serious. Sounds like she is speaking over a cockpit radio: slightly thin, "
         "compressed, a little gritty. Late-1990s video game voice-over energy.")
SEED = 8086   # the owner's pick from the audition (2026-10-30)

# The coach's own voice (coach_say.py, rendered live for free-form coach lines): calm, low, male, clean (no radio
# filter on the page). Also an original voice from tags + seed, not a clone; clearly apart from the pilot above.
COACH_VOICE = "male, middle-aged, low pitch, british accent"
COACH_SEED = 1979
COACH_SPEED = 1.1


def _request(method, path, body=None, headers=None, timeout=300):
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers={"Authorization": f"Bearer {KEY}", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get_content_type()


def _multipart(fields):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode("utf-8"), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def generate(text, extra=None):
    """Return (audio bytes, extension, response json) for one line."""
    fields = {"text": text, "instruct": VOICE, "seed": SEED, "speed": SPEED, "language": "en", **(extra or {})}
    body, headers = _multipart(fields)
    raw, ctype = _request("POST", "/generate", body, headers)
    if ctype.startswith("audio/"):
        return raw, ".wav" if "wav" in ctype else ".ogg", {}
    info = json.loads(raw)
    audio_id = info.get("audio_id") or info.get("id")
    url = info.get("audio_url") or info.get("url") or (f"/audio/{audio_id}.ogg" if audio_id else None)
    if not url:
        raise RuntimeError(f"no audio in response: {str(info)[:300]}")
    audio, _ = _request("GET", url if url.startswith("/") else "/" + url.split("/", 3)[-1])
    return audio, ".ogg", info


if __name__ == "__main__":
    t0 = time.time()
    audio, ext, info = generate(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "out" / f"test{ext}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(audio)
    print(f"{len(audio)} bytes -> {out} in {time.time() - t0:.1f}s; keys: {sorted(info)[:12]}")
