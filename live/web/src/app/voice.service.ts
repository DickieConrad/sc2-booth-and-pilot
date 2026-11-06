// The voice: pre-rendered clips (the pilot through a cockpit-radio filter, the coach through a clean chain) and
// browser speech share ONE queue. Calls interrupt everything; step cues wait their turn. Ported from overlay.html.
import { Injectable, computed, signal } from '@angular/core';
import { CUE_LEAD, store } from './logic';
import { TRACE } from './trace';
import { CueEvent, Manifest, Say } from './types';

// Radio strength, set by ear by the owner: started at 1.0, then "dial down the static", then "another 25%".
export const RADIO = { hp: 280, lp: 3800, mid: 3.75, grit: 2.25, hiss: 0.003 };
// Master volume for every voice (clips and browser speech), set by ear: "turn it down like twenty percent".
export const VOLUME = 0.8;

/** One playing clip: stop() cuts it off, done resolves when it ends (or is stopped). */
export interface Playing { stop(): void; done: Promise<void>; }

function radioChain(ctx: AudioContext): AudioNode { // thin band, mild grit, compression, light hiss
  const hp = ctx.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = RADIO.hp;
  const lp = ctx.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = RADIO.lp;
  const mid = ctx.createBiquadFilter(); mid.type = 'peaking'; mid.frequency.value = 1800; mid.gain.value = RADIO.mid;
  const shaper = ctx.createWaveShaper(); const k = RADIO.grit, n = 1024, curve = new Float32Array(n);
  for (let i = 0; i < n; i++) { const x = (i * 2) / n - 1; curve[i] = ((1 + k) * x) / (1 + k * Math.abs(x)); }
  shaper.curve = curve;
  const comp = ctx.createDynamicsCompressor(); comp.threshold.value = -24; comp.ratio.value = 8;
  const out = ctx.createGain(); out.gain.value = 0.85 * VOLUME;
  hp.connect(lp).connect(mid).connect(shaper).connect(comp).connect(out).connect(ctx.destination);
  return hp;
}

// The coach's own voice (coach_say.py renders it live): clean, no radio, just rumble cut and a light compressor.
function cleanChain(ctx: AudioContext): AudioNode {
  const hp = ctx.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = 70;
  const comp = ctx.createDynamicsCompressor(); comp.threshold.value = -18; comp.ratio.value = 2.5;
  const out = ctx.createGain(); out.gain.value = VOLUME;
  hp.connect(comp).connect(out).connect(ctx.destination);
  return hp;
}

/** Decoded live coach lines kept (most recently played); pilot and brain clips are a fixed set and stay. */
export const COACH_CACHE = 64;

@Injectable({ providedIn: 'root' })
export class VoiceService {
  clips: Manifest | null = null;
  private actx: AudioContext | null = null;
  private playing: Playing | null = null;
  private clipQueue: string[][] = [];
  private clipBuf = new Map<string, Promise<AudioBuffer>>();
  private coachBuf = new Map<string, Promise<AudioBuffer>>(); // LRU: Map keeps insertion order, oldest first
  // What the voice is doing, from the moment a sequence starts (before its first clip has loaded) until it ends.
  private busy: 'cue' | 'call' | null = null;
  // Bumped whenever the voice is cut off (a call, hush, game over); a sequence checks it after every await.
  private gen = 0;
  // Bumped at game over: step cues waiting to retry in browser speech give up.
  private cueEpoch = 0;
  private cueUtter: SpeechSynthesisUtterance | null = null; // browser speech saying a step cue (not a call)
  private coachTts: SpeechSynthesisVoice | null | undefined;

  readonly voice = signal(store.get('voice') === '1');
  // Remembered "Voice on" after a reload: browsers keep a page silent until it has been clicked once.
  private readonly armNeeded = signal(this.voice());
  private armed = false;
  readonly voiceLabel = computed(() => (this.armNeeded() ? 'Arm Voice' : this.voice() ? 'Voice on' : 'Voice off'));
  readonly arming = this.armNeeded.asReadonly();
  readonly cueOn = signal(store.get('cues') !== '0');
  readonly boothOn = signal(store.get('booth') !== '0');

  loadManifest(): void {
    fetch('/voice/manifest.json', { cache: 'no-store' }).then(r => (r.ok ? r.json() : null))
      .then(m => { this.clips = m; }).catch(() => {});
  }

  private setVoice(on: boolean): void {
    this.voice.set(on); store.set('voice', on ? '1' : '0');
    this.armNeeded.set(false);
  }
  setCues(on: boolean): void { this.cueOn.set(on); store.set('cues', on ? '1' : '0'); }
  setBooth(on: boolean): void { this.boothOn.set(on); store.set('booth', on ? '1' : '0'); }

  // The click also unlocks audio playback (browsers block sound until the page is clicked once).
  clickVoice(): void {
    if (this.voice() && !this.armed) { this.armed = true; this.setVoice(true); } // first click after a reload only arms the sound
    else { this.setVoice(!this.voice()); this.armed = this.voice(); }
    if (!this.voice()) return;
    this.actx = this.actx || this.newContext(); this.actx?.resume();
    if (this.clips && this.clips.calls['plan']) this.playClips([this.clips.calls['plan']], true); else this.speak('Voice on');
  }

  // ---------- audio plumbing (overridden in tests) ----------
  protected newContext(): AudioContext | null {
    return typeof AudioContext === 'undefined' ? null : new AudioContext();
  }
  protected tts(): SpeechSynthesis | null {
    return typeof window !== 'undefined' && 'speechSynthesis' in window ? window.speechSynthesis : null;
  }
  protected loadClip(file: string): Promise<AudioBuffer> {
    const ctx = this.actx!;
    return fetch('/voice/' + file).then(r => r.arrayBuffer()).then(b => ctx.decodeAudioData(b));
  }
  // Decoded clips, loaded once. Live coach lines (coach=true) are each new text: keep only the last COACH_CACHE.
  private clip(file: string, coach: boolean): Promise<AudioBuffer> {
    const cache = coach ? this.coachBuf : this.clipBuf;
    const hit = cache.get(file);
    if (hit) { if (coach) { cache.delete(file); cache.set(file, hit); } return hit; }
    const p: Promise<AudioBuffer> = this.loadClip(file)
      .catch(e => { if (cache.get(file) === p) cache.delete(file); throw e; }); // a missing clip may appear later
    cache.set(file, p);
    if (coach && cache.size > COACH_CACHE) cache.delete(cache.keys().next().value!);
    return p;
  }
  protected startClip(buf: AudioBuffer, clean: boolean): Playing {
    const actx = this.actx!;
    const src = actx.createBufferSource(); src.buffer = buf;
    const input = clean ? cleanChain(actx) : radioChain(actx);
    src.connect(input);
    const noise = actx.createBufferSource(), nb = actx.createBuffer(1, Math.ceil(actx.sampleRate * (buf.duration + 0.15)), actx.sampleRate);
    const d = nb.getChannelData(0); for (let i = 0; i < d.length && !clean; i++) d[i] = (Math.random() * 2 - 1) * RADIO.hiss;
    noise.buffer = nb; if (!clean) noise.connect(input);
    const done = new Promise<void>(res => { src.onended = () => res(); });
    noise.start(); src.start();
    return { stop: () => src.stop(), done };
  }

  // ---------- the shared queue ----------
  // One voice at a time: the pilot's clips and browser speech never talk over each other.
  private ttsBusy(): boolean { const s = this.tts(); return !!s && (s.speaking || s.pending); }
  private stopPlaying(): void {
    if (this.playing) { const p = this.playing; this.playing = null; try { p.stop(); } catch { /* already ended */ } }
  }
  hush(): void { // a call cuts off everything: clip (playing or still loading), queued cues, browser speech
    this.gen++; this.busy = null; this.cueUtter = null;
    this.clipQueue = [];
    this.stopPlaying();
    this.tts()?.cancel();
  }
  /** The game ended: no step cue plays from here on (queued, loading, playing or waiting to retry). A call finishes. */
  gameOver(): void {
    this.clipQueue = [];
    this.cueEpoch++;
    if (this.busy === 'cue') { this.gen++; this.busy = null; this.stopPlaying(); }
    if (this.cueUtter) { this.cueUtter = null; this.tts()?.cancel(); }
  }
  private nextClip(): void {
    const next = !this.busy && !this.ttsBusy() && this.clipQueue.shift();
    if (next) this.playClips(next, false).catch(() => { /* a cue clip that won't load: the list shows the step */ });
  }
  // Remembered "Voice on" after a reload stays silent until the click: the browser would keep the audio suspended
  // and everything said meanwhile would pile up and play back to back, minutes late, once armed.
  private canTalk(): boolean { return this.voice() && !this.armNeeded(); }

  // Play clip files in order. A call (interrupt=true) stops whatever is playing; cues queue behind.
  // clean=true: the coach's voice (cleanChain, no hiss) instead of the pilot's radio. coach=true: a live coach line
  // (bounded cache). Busy from the first line on, so a second cue in the same frame queues instead of overlapping.
  async playClips(files: string[], interrupt: boolean, clean = false, coach = false): Promise<void> {
    const tagged = files as string[] & { tag?: string };
    const tag = TRACE.take() || tagged.tag; // latency trace (off unless the helper traces)
    this.actx = this.actx || this.newContext();
    if (interrupt) this.hush();
    else if (this.actx?.state === 'suspended') return; // not armed: a cue would only play late
    else if (this.busy || this.ttsBusy()) { if (tag) tagged.tag = tag; this.clipQueue.push(files); return; }
    const gen = this.gen;
    this.busy = interrupt ? 'call' : 'cue';
    try {
      for (const f of files) {
        const t0 = Date.now();
        let buf: AudioBuffer;
        try { buf = await this.clip(f, coach); } catch (e) { if (gen !== this.gen) return; throw e; }
        if (gen !== this.gen) return; // cut off while it loaded
        if (tag && f === files[0]) TRACE.finish(tag, 'clip', Date.now() - t0);
        const p = this.startClip(buf, clean);
        this.playing = p;
        await p.done;
        if (gen !== this.gen) return; // interrupted by a call
        this.playing = null;
      }
    } finally {
      if (gen === this.gen) { this.playing = null; this.busy = null; this.nextClip(); } // also after a failed load
    }
  }

  // A helper call of a known kind, or step cues that all have clips -> the pilot; anything else -> browser speech.
  // A coach line with a rendered clip -> the coach's clean voice (falls back to browser speech if the clip won't load).
  speakCall(say: Say): void {
    if (!this.canTalk() || (say.from === 'coach' && !this.boothOn())) return;
    if (say.from === 'coach' && say.audio) {
      const live = !say.kind.startsWith('brain-'); // brain lines are a fixed, pre-rendered set: keep them cached
      this.playClips([say.audio], true, true, live).catch(() => this.speak(say.text, undefined, true));
      return;
    }
    const f = this.clips && say.from !== 'coach' && this.clips.calls[say.kind];
    if (f) this.playClips([f], true).catch(() => { /* clip won't load */ }); else this.speak(say.text, undefined, say.from === 'coach');
  }

  speakCues(steps: string[]): void {
    if (!this.canTalk() || !this.cueOn()) return;
    const files = this.clips ? steps.map(s => this.clips!.cues[s]) : [];
    if (files.length && files.every(Boolean)) this.playClips(files, false).catch(() => { /* missing clip: the list shows it */ });
    else this.speak(`${steps.join(', then ')}, ${CUE_LEAD} seconds`, 2);
  }

  // Cue events from the Rust engine (coach-core): "target" = the usual 5-second cue, "now" = catch-up when late.
  playCueEvent(ev: CueEvent): void {
    if (ev.type === 'target') return this.speakCues(ev.keys);
    if (!this.canTalk() || !this.cueOn()) return;
    const now = this.clips?.now;
    const files = now ? ev.keys.map(k => now[k]) : [];
    if (files.length && files.every(Boolean)) this.playClips(files, false).catch(() => { /* missing clip */ });
    else this.speak(`${ev.keys.join(', then ')}, now!`, 2);
  }

  // The coach's fallback browser voice: a male system voice if there is one (English GB first), else the default.
  private coachVoice(): SpeechSynthesisVoice | null {
    if (this.coachTts !== undefined) return this.coachTts;
    const vs = this.tts()!.getVoices().filter(v => /^en/i.test(v.lang));
    if (!vs.length) return null; // voices not loaded yet: try again next line
    const male = /\b(Ryan|George|Thomas|Guy|David|Mark|Christopher|Eric|Andrew|Brian|Davis|Roger|Steffan|James|Daniel|Male)\b/i;
    this.coachTts = vs.find(v => male.test(v.name) && /GB/i.test(v.lang)) || vs.find(v => male.test(v.name)) || null;
    return this.coachTts;
  }

  // Calls interrupt anything; step cues wait their turn and never cut off a call.
  speak(text: string, cue?: number, coach?: boolean): void {
    const s = this.tts();
    const tag = TRACE.take();
    if (!this.canTalk() || !s) return;
    if (cue && (this.ttsBusy() || this.busy)) {
      const epoch = this.cueEpoch;
      setTimeout(() => {
        if (epoch !== this.cueEpoch) return; // the game ended meanwhile
        TRACE.cur = tag; this.speak(text, cue - 1);
      }, 1500);
      return;
    }
    if (cue === 0) return; // still busy after the retries: drop the cue, the list shows it anyway
    if (cue) s.cancel(); else this.hush();
    const u = new SpeechSynthesisUtterance(text.replace(/(\d+):(\d\d)/g, '$1 $2'));
    u.rate = 1.15; u.volume = VOLUME;
    this.cueUtter = cue ? u : null;
    if (coach && this.coachVoice()) { u.voice = this.coachTts!; u.rate = 1.05; u.pitch = 0.9; }
    u.onend = u.onerror = () => {
      if (this.cueUtter === u) this.cueUtter = null;
      setTimeout(() => this.nextClip(), 50); // cue clips that waited for this line
    };
    if (tag) u.onstart = () => TRACE.finish(tag, 'tts');
    s.speak(u);
  }
}
