"""Push-to-talk callouts for the coach.

Hold F13 (a mouse side button mapped in G Hub), speak ("roach warren, two gases"), release.
The clip is transcribed locally (faster-whisper: CUDA float16 on the GPU, else CPU int8) and written as one line:
    live/out/mic.log    HEARD [m:ss] <text>      (also printed, and POSTed to the helper's /note)
Fast lane: if the text hits a keyword in voice/fastlane.json, the pre-rendered coach clip for that entry is
POSTed to /say at once (no render, skipped if the clip isn't cached), logged as  FAST [m:ss] <line>  after the
HEARD line, and noted as "Coach: <line>". Render the clips with voice/render_fastlane.py. Enemy counters fire only
on a report of the enemy, never on a question or plan about Dickie's own play ("Should I drop him?", "I need a
banshee"): those go on to the intent lane (fastlane.py, the matcher; test_fastlane.py).
Intent lane (intents.py): in a game, a callout the fast lane missed goes to Jev (one request, <= 0.6 s, after the
transcript, before the slow path) and is logged as  INTENT [m:ss] <intent> <conf> <action> talk=<p> | <text>.
pause/resume ("Paused." / "Back on.") and "what beats <unit>" (counters.json) are answered on the page at once
(clips: voice/render_counters.py); enemy_shows / behind / ask_build are tagged in the note and left to the coach;
anything else takes the slow path unchanged. SC2_INTENTS=0 turns it off.
mic.log lines the coach's monitor greps: ^(HEARD|FAST|MISS|INTENT).

Read-only: the key listener only listens (no suppression, never sends keys or clicks).
Launch on Windows:  powershell -ExecutionPolicy Bypass -File live\\mic.ps1
                    [--key mouse5] [--model medium.en] [--device auto]
Test without a mic: mic.ps1 --wav some.wav  (transcribes the file, prints the latency and fast-lane match, exits)
"""
import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.request
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(HERE / ".models"))   # weights stay in this folder, not the user profile
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# CUDA 12 cuBLAS / cuDNN 9 runtime DLLs from the nvidia-* wheels in .pkgs (setup.sh). They must be findable before
# ctranslate2 is imported: add_dll_directory for Python's loader, PATH for the DLLs that load other DLLs.
CUDA_BIN = [d for d in (HERE / ".pkgs" / "nvidia" / p / "bin" for p in ("cublas", "cudnn", "cuda_nvrtc")) if d.is_dir()]
if sys.platform == "win32":
    for _d in CUDA_BIN:
        os.add_dll_directory(str(_d))
    os.environ["PATH"] = os.pathsep.join([*map(str, CUDA_BIN), os.environ.get("PATH", "")])
CUDA_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll")

import numpy as np  # noqa: E402
from fastlane import fast_match, load_fastlane  # noqa: E402

RATE = 16000
MIN_HOLD = 0.3        # seconds; shorter taps are ignored
MAX_HOLD = 15.0       # seconds; recording stops here even if the key is still held
PREROLL = 0.25        # seconds of audio kept from just before the press (catches a clipped first word)
SILENCE_PEAK = 0.004  # a clip whose loudest sample is below this is treated as silence
LOG = HERE / "out" / "mic.log"
HELPER_PORT = 8765
HELPER = f"http://127.0.0.1:{HELPER_PORT}"

PROMPT = ("StarCraft 2 callouts: roach warren, two gases, baneling nest, hatch, hatchery, pool, spawning pool, "
          "pylon, gateway, stalker, adept, oracle, void ray, cannon, reaper, marine, marauder, bunker, "
          "siege tank, medivac, viking, liberator, widow mine, cyclone, thor, banshee, zergling, roach, "
          "mutalisk, queen, creep, natural, third, main, proxy, all-in, expand, harass, Terran, Hellion, Hellbat, "
          "SCV, depot, Factory, Starport, Armory, Engineering Bay, Ghost, Raven, Overseer, Observer, "
          "Dark Templar, DTs, Dark Shrine, Blink, Immortal, Colossus, Lurker, Ravager, Hydralisk, Infestor.")
BEAM = 5  # beam search: a little GPU time for fewer mishears (1 = greedy, the old setting; Dickie, 2026-10-31)
JUNK = {"",".", "you", "thank you.", "thanks for watching!", "bye."}   # whisper's usual noises on near-silence
# Words whisper still mishears after the prompt; fixed after transcription. Add new ones from HEARD lines.
FIXES = [(re.compile(p, re.I), w) for p, w in [
    (r"\bhell[ -]?(?:ians?|eons?|ions?|yons?)\b", "Hellions"), (r"\bhell ?bats?\b", "Hellbats"),
    (r"\bd\.? ?t\.?s\b|\bdeeties\b", "DTs"), (r"\bdark templars?\b", "Dark Templar"),
    (r"\btar+[ae]n\b|\bterr?ain\b", "Terran"), (r"\bs\.? ?c\.? ?v\.?s\b", "SCVs"),
    (r"\bmedi ?vacs?\b", "Medivacs"), (r"\bmarauders?\b", "Marauders"), (r"\bbane ?lings?\b", "Banelings"),
    (r"\bover ?seers?\b", "Overseers"), (r"\bstar ?ports?\b", "Starport"),
    (r"\bengi(?:neering)? ?bays?\b", "Engineering Bay"),
]]


def fix_words(text):
    for pat, word in FIXES:
        text = pat.sub(word, text)
    return text


def mmss(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def game_clock():
    """'m:ss' from the timing helper, or '-:--' when not in a game / helper down."""
    try:
        with urllib.request.urlopen(HELPER + "/state", timeout=0.5) as r:
            s = json.loads(r.read())
        if not s.get("in_game"):
            return "-:--"
        t = float(s.get("clock", 0))
        if s.get("clock_wall"):   # the helper ticks every 0.5 s; roll forward to now
            t += min(2.0, max(0.0, time.time() - float(s["clock_wall"])))
        return mmss(t)
    except Exception:
        return "-:--"


def post_note(text, tag=""):
    """tag (the intent lane's " [intent: x 0.93]") goes after the text: the page parses "Owner callout: <text>"."""
    try:
        body = json.dumps({"text": f"Owner callout: {text}{tag}"}).encode()
        req = urllib.request.Request(HELPER + "/note", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=1.5).read()
    except Exception as e:
        print(f"mic: /note failed ({e})", flush=True)


def cuda_problem():
    """None if the GPU path looks usable, else why not. Checked up front: a missing cuDNN DLL makes CTranslate2
    abort the whole process mid-transcribe instead of raising, so the fallback can't rely on catching it."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return "no CUDA device"
    except Exception as e:
        return f"ctranslate2: {e}"
    if sys.platform == "win32":
        import ctypes
        for dll in CUDA_DLLS:
            try:
                ctypes.WinDLL(dll)
            except OSError:
                return f"{dll} not found (bash live/setup.sh installs the nvidia-* wheels)"
    return None


def load_model(name, device="auto"):
    """Return (model, device, compute_type). auto: CUDA float16 if it loads and warms up, else CPU int8."""
    from faster_whisper import WhisperModel
    local = HERE / ".models" / f"faster-whisper-{name}"   # filled by fetch_models.sh (HF's own download
    if not (local / "model.bin").exists():                 # can't lock files on the WSL share)
        sys.exit(f"mic: no weights at {local}; from WSL run: bash live/fetch_models.sh {name}")
    tries = []
    if device in ("auto", "cuda"):
        why = cuda_problem()
        if why is None:
            tries.append(("cuda", "float16"))
        elif device == "cuda":
            sys.exit(f"mic: --device cuda, but {why}")
        else:
            print(f"mic: GPU not used ({why}); CPU int8", flush=True)
    if device in ("auto", "cpu"):
        tries.append(("cpu", "int8"))
    for i, (dev, ct) in enumerate(tries):
        try:
            m = WhisperModel(str(local), device=dev, compute_type=ct, cpu_threads=min(8, os.cpu_count() or 4))
            # warm-up; segments are lazy, so consume them or the encoder/decoder never actually run
            list(m.transcribe(np.zeros(RATE, dtype=np.float32), language="en", beam_size=1)[0])
            return m, dev, ct
        except Exception as e:
            if i == len(tries) - 1:
                raise
            print(f"mic: {dev} {ct} failed ({e}); falling back to {tries[i + 1][0]}", flush=True)


def coach_say_mod():
    """voice/coach_say.py (clip_name, CLIPS, post_say, post_note), or None if it can't be imported."""
    try:
        sys.path.insert(0, str(HERE / "voice"))
        import coach_say
        return coach_say
    except Exception as e:
        print(f"mic: fast lane off (coach_say import failed: {e})", flush=True)
        return None


def transcribe(model, audio):
    if audio.size == 0 or float(np.abs(audio).max()) < SILENCE_PEAK:
        return ""
    segs, _ = model.transcribe(audio, language="en", beam_size=BEAM, initial_prompt=PROMPT,
                               condition_on_previous_text=False, without_timestamps=True, vad_filter=False)
    text = " ".join(s.text.strip() for s in segs).strip()
    return "" if text.lower() in JUNK else fix_words(text)


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        sys.exit("mic: --wav needs 16-bit PCM")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if rate != RATE:   # linear resample is plenty for speech
        a = np.interp(np.arange(0, len(a), rate / RATE), np.arange(len(a)), a).astype(np.float32)
    return a


def key_matcher(spec):
    """'f13' / 'f9' / 'a' / 'vk:124' -> function(key) -> bool, plus a label."""
    from pynput import keyboard
    spec = spec.strip().lower()
    if spec.startswith("vk:"):
        vk = int(spec[3:], 0)
    elif hasattr(keyboard.Key, spec):
        vk = getattr(keyboard.Key, spec).value.vk
    elif len(spec) == 1:
        vk = keyboard.KeyCode.from_char(spec).vk
        if vk is None:
            return (lambda k: getattr(k, "char", None) == spec), spec.upper()
    else:
        sys.exit(f"mic: unknown key {spec!r} (try f13, f14, vk:124)")

    def match(k):
        v = getattr(k, "vk", None)
        if v is None and hasattr(k, "value"):
            v = getattr(k.value, "vk", None)
        return v == vk
    return match, spec.upper()


def intent_lane():
    """intents.IntentLane if SC2_INTENTS isn't 0 and the SDK + key are there, else None (reason printed)."""
    try:
        import intents
    except Exception as e:
        print(f"mic: intent lane off (import failed: {type(e).__name__})", flush=True)
        return None
    if not intents.enabled():
        print("mic: intent lane off (SC2_INTENTS=0)", flush=True)
        return None
    lane = intents.IntentLane()
    if not lane.ready:
        print(f"mic: intent lane off ({lane.why})", flush=True)
        return None
    return lane


class PushToTalk:
    def __init__(self, model, out=LOG, note=True, fast=None, lane=None):
        self.model, self.out, self.note = model, out, note
        self.fast = fast           # (rules, max_words, coach_say module) or None
        self.lane = lane           # intents.IntentLane (ready) or None
        self.lock = threading.Lock()
        self.rec = None            # list of chunks while recording
        self.t0 = 0.0
        self.clock = [None]
        self.held = False
        self.pre = []              # pre-roll chunks
        self.jobs = queue.Queue()

    # audio thread
    def on_audio(self, indata, frames, t, status):
        chunk = indata[:, 0].copy()
        with self.lock:
            if self.rec is not None:
                self.rec.append(chunk)
                if time.monotonic() - self.t0 >= MAX_HOLD:
                    self._finish(capped=True)
            else:
                self.pre.append(chunk)
                while sum(len(c) for c in self.pre) > PREROLL * RATE:
                    self.pre.pop(0)

    # key thread (must return fast)
    def press(self):
        with self.lock:
            if self.held:          # key auto-repeat
                return
            self.held = True
            self.rec, self.t0 = list(self.pre), time.monotonic()
            self.clock = holder = [None]
        # the game time at the moment of the callout, not of the transcript (fetched off the key thread)
        threading.Thread(target=lambda: holder.__setitem__(0, game_clock()), daemon=True).start()

    def release(self):
        with self.lock:
            self.held = False
            if self.rec is not None:
                self._finish(capped=False)

    def _finish(self, capped):   # caller holds the lock
        dur, chunks = time.monotonic() - self.t0, self.rec
        self.rec = None
        if dur < MIN_HOLD:
            return
        if capped:
            print(f"mic: {MAX_HOLD:.0f} s cap reached, transcribing", flush=True)
        self.jobs.put((self.clock, np.concatenate(chunks) if chunks else np.zeros(0, np.float32)))

    # worker thread
    def work(self):
        while True:
            clock, audio = self.jobs.get()
            clock = clock[0] or game_clock()
            t = time.perf_counter()
            try:
                text = transcribe(self.model, audio)
            except Exception as e:
                print(f"mic: transcribe failed ({e})", flush=True)
                continue
            if not text.strip():
                # logged so a dead mic shows up: a press that records silence left no trace (2026-10-31, "mic died
                # again": no way to tell a muted headset from a button that isn't seen)
                peak = float(np.abs(audio).max()) if audio.size else 0.0
                print(f"mic: {len(audio) / RATE:.1f} s clip, nothing heard (peak {peak:.4f}; "
                      f"{'silent: headset muted?' if peak < SILENCE_PEAK else 'no words'})", flush=True)
                continue
            line, status = self.fast_reply(text)   # /say goes out before any logging: the voice is what's waited on
            ti = time.perf_counter()
            intent = None if line or status not in ("miss", "long", "off") else self.intent_reply(text, clock)
            ti = time.perf_counter() - ti
            emit(f"HEARD [{clock}] {text}", self.out)
            tag = ""
            if line:
                emit(f"FAST [{clock}] {line}", self.out)
            if intent:
                ilog, act, tag = intent
                emit(ilog, self.out)
                if act.act == "say":
                    line = act.line
            if not line and status == "miss" and len(text.split()) >= 2:
                emit(f"MISS [{clock}] {text}", self.out)   # reviewed between sessions for new fastlane entries
            print(f"mic: {len(audio) / RATE:.1f} s clip in {time.perf_counter() - t:.2f} s"
                  f"{f' (intent {ti * 1000:.0f} ms)' if intent else ''}"
                  f"{'' if status in ('fast', 'miss', 'off') else ' (fast lane: ' + status + ')'}", flush=True)
            if self.note:
                threading.Thread(target=self.notes, args=(text, line, tag), daemon=True).start()

    def intent_reply(self, text, clock):
        """Intent lane (in a game only): classify, decide, and for a `say` POST the line in the background.
        Returns (INTENT log line, Action, note tag) or None when the lane is off / not in a game."""
        if not self.lane or clock == "-:--":   # game_clock() says "-:--" outside a game (or with the helper down)
            return None
        import intents
        v = self.lane.classify(text)
        act = intents.decide(v.intent, v.conf, True, text, v.talk) if v else intents.decide(None, 0.0, True, text)
        if act.act == "say" and act.line:
            threading.Thread(target=self.say_line, args=(act.line,), daemon=True).start()
        tag = intents.note_tag(v.intent, v.conf) if v and act.act in ("say", "pass") else ""
        return intents.log_line(clock, v, act, text), act, tag

    def say_line(self, line):
        """POST an intent-lane line to /say: the cached coach clip if rendered, else the text (browser speech).
        Never renders here (voice/render_counters.py does that between games)."""
        cs = self.fast[2] if self.fast else coach_say_mod()
        try:
            if cs:
                name = cs.clip_name(line)
                have = (cs.CLIPS / name).exists()
                if not have:
                    print(f"mic: no clip for {line!r}, run voice/render_counters.py", flush=True)
                cs.post_say(HELPER_PORT, line, name if have else None)
            else:
                req = urllib.request.Request(HELPER + "/say", data=json.dumps({"text": line}).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=3).read()
        except Exception as e:
            print(f"mic: intent /say failed ({e})", flush=True)

    def fast_reply(self, text):
        """POST the cached coach clip for a fastlane hit. Returns (line or None, 'fast'|'miss'|'off'|reason)."""
        if not self.fast:
            return None, "off"
        rules, max_words, cs = self.fast
        if max_words and len(text.split()) > max_words:
            return None, "long"   # talking to the coach, not a callout: neither a reply nor a MISS
        line = fast_match(rules, text)
        if not line:
            return None, "miss"
        name = cs.clip_name(line)
        if not (cs.CLIPS / name).exists():   # never render in the mic path
            return None, f"no clip for {line!r}, run voice/render_fastlane.py"
        try:
            cs.post_say(HELPER_PORT, line, name)
        except Exception as e:
            return None, f"/say failed ({e})"
        return line, "fast"

    def notes(self, text, line, tag=""):
        post_note(text, tag)                # owner first, then the coach's reply, so the DB reads in order
        cs = (self.fast[2] if self.fast else coach_say_mod()) if line else None
        if line and cs:
            try:
                cs.post_note(HELPER_PORT, line)
            except Exception as e:
                print(f"mic: coach /note failed ({e})", flush=True)


def emit(line, out=LOG):
    print(line, flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", default="mouse5", help="push-to-talk: mouse5 (default, the thumb Forward button), mouse4, "
                    "f13, f14, a letter, or vk:<code>")
    ap.add_argument("--model", default="medium.en", help="faster-whisper model (medium.en, small.en, base.en, ...)")
    ap.add_argument("--no-note", action="store_true", help="don't POST callouts to the helper's /note")
    ap.add_argument("--no-fast", action="store_true", help="no fast-lane replies (voice/fastlane.json)")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"),
                    help="auto (default): GPU (CUDA float16) if usable, else CPU int8")
    ap.add_argument("--wav", help="transcribe this wav file and exit (test mode; nothing is posted or logged)")
    ap.add_argument("--repeat", type=int, default=3, help="--wav: transcribe this many times (latency spread)")
    a = ap.parse_args()

    t = time.perf_counter()
    model, device, compute = load_model(a.model, a.device)
    print(f"mic: model {a.model} on {device} {compute} loaded in {time.perf_counter() - t:.1f} s", flush=True)
    fast = None
    if not a.no_fast:
        rules, max_words = load_fastlane()
        cs = coach_say_mod() if rules else None
        fast = (rules, max_words, cs) if cs else None
    lane = intent_lane()

    if a.wav:
        audio = read_wav(a.wav)
        for _ in range(max(1, a.repeat)):
            t = time.perf_counter()
            text = transcribe(model, audio)
            print(f"mic: {len(audio) / RATE:.1f} s clip in {time.perf_counter() - t:.2f} s -> {text!r}", flush=True)
        if fast:
            line = fast_match(fast[0], text) if not (fast[1] and len(text.split()) > fast[1]) else None
            have = line and (fast[2].CLIPS / fast[2].clip_name(line)).exists()
            print(f"mic: fast lane -> {line!r}{' (clip cached)' if have else ' (no clip)' if line else ''}", flush=True)
        if lane and text.strip():   # as if in a game; nothing is posted
            import intents
            t = time.perf_counter()
            v = lane.classify(text)
            act = intents.decide(v.intent, v.conf, True, text, v.talk) if v else intents.decide(None, 0.0, True, text)
            print(f"mic: intent lane in {(time.perf_counter() - t) * 1000:.0f} ms -> "
                  f"{intents.log_line('0:00', v, act, text)}", flush=True)
        return

    import sounddevice as sd
    from pynput import keyboard, mouse
    # Mouse side buttons: the raw button, or the "browser back/forward" key Logi Options+ may send for it instead.
    MOUSE = {"mouse4": (mouse.Button.x1, 166), "mouse5": (mouse.Button.x2, 167)}
    if a.key in MOUSE:
        button, alt_vk = MOUSE[a.key]
        match, label = key_matcher(f"vk:{alt_vk}")
        label = f"{a.key} (thumb button)"
    else:
        button, (match, label) = None, key_matcher(a.key)
    ptt = PushToTalk(model, note=not a.no_note, fast=fast, lane=lane)
    threading.Thread(target=ptt.work, daemon=True).start()

    raw = []   # set once the raw mouse button is seen: then ignore the duplicate "browser back" key it also sends

    def on_press(k):
        if match(k) and not raw:
            ptt.press()

    def on_release(k):
        if match(k) and not raw:
            ptt.release()

    dev = sd.query_devices(kind="input")["name"]   # MME cuts names at 31 chars; take the full one from another API
    dev = max((d["name"] for d in sd.query_devices() if d["max_input_channels"] and d["name"].startswith(dev)),
              key=len, default=dev)
    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=int(RATE * 0.05),
                        callback=ptt.on_audio):
        listener = keyboard.Listener(on_press=on_press, on_release=on_release, suppress=False)
        listener.start()
        if button is not None:   # read-only like the keyboard hook: never suppresses or sends clicks
            def on_click(x, y, b, pressed):
                if b == button:
                    raw.append(1) if not raw else None
                    (ptt.press if pressed else ptt.release)()
            mouse.Listener(on_click=on_click, suppress=False).start()
        listener.wait()
        print(f"mic: listening on {label} (model {a.model} on {device} {compute}, "
              f"fast lane {len(fast[0]) if fast else 'off'}{' entries' if fast else ''}, "
              f"intent lane {'on' if lane else 'off'}, mic {dev})", flush=True)
        try:
            while listener.is_alive():
                listener.join(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            listener.stop()


if __name__ == "__main__":
    main()
