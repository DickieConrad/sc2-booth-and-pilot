import { Manifest } from './types';
import { Playing, VoiceService } from './voice.service';

/**
 * Audio replaced by fakes: each started clip waits until the test ends (or stops) it. Clip loads are async like the
 * real fetch + decode (they finish on a later task), and a held file's load waits until the test releases it.
 */
class FakeVoice extends VoiceService {
  log: string[] = [];
  live = new Map<string, () => void>();
  spoken: string[] = [];
  loads: string[] = [];
  held = new Set<string>();
  private gates = new Map<string, () => void>();
  ctxState: AudioContextState = 'running';
  protected override newContext() {
    const v = this;
    return { resume() { v.ctxState = 'running'; return Promise.resolve(); }, get state() { return v.ctxState; } } as unknown as AudioContext;
  }
  protected override tts() { return null; }
  protected override loadClip(file: string) {
    this.loads.push(file);
    return new Promise<AudioBuffer>((res, rej) => {
      const go = () => (file.startsWith('missing') ? rej(new Error('404')) : res({ file } as unknown as AudioBuffer));
      if (this.held.has(file)) this.gates.set(file, go); else setTimeout(go, 0);
    });
  }
  protected override startClip(buf: AudioBuffer, clean: boolean): Playing {
    const file = (buf as unknown as { file: string }).file;
    this.log.push(file + (clean ? ' (clean)' : ''));
    let end!: () => void;
    const done = new Promise<void>(res => { end = res; });
    this.live.set(file, end);
    return { stop: () => { this.log.push('stop ' + file); end(); }, done };
  }
  override speak(text: string, cue?: number, coach?: boolean) { this.spoken.push(`${text}${coach ? ' [coach]' : ''}`); }
  finish(file: string) { this.live.get(file)!(); }
  release(file: string) { this.held.delete(file); this.gates.get(file)?.(); }
}

const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0)); };
const clips: Manifest = { calls: { blocked: 'blocked.wav', plan: 'plan.wav' },
  cues: { Depot: 'depot.wav', Barracks: 'rax.wav', Gas: 'missing-gas.wav' }, now: { Depot: 'depot-now.wav' } };

function make(): FakeVoice {
  const v = new FakeVoice();
  v.voice.set(true); v.cueOn.set(true); v.boothOn.set(true);
  v.clips = clips;
  return v;
}

describe('voice queue', () => {
  it('plays cues in order, one at a time', async () => {
    const v = make();
    v.speakCues(['Depot']);
    await flush();
    v.speakCues(['Barracks']);
    v.playCueEvent({ id: 1, type: 'now', keys: ['Depot'] });
    await flush();
    expect(v.log).toEqual(['depot.wav']);
    v.finish('depot.wav'); await flush();
    expect(v.log).toEqual(['depot.wav', 'rax.wav']);
    v.finish('rax.wav'); await flush();
    expect(v.log).toEqual(['depot.wav', 'rax.wav', 'depot-now.wav']);
  });

  it('a call cuts off the clip and drops the queued cues', async () => {
    const v = make();
    v.speakCues(['Depot']); await flush();
    v.speakCues(['Barracks']);
    v.speakCall({ id: 9, t: '2:00', text: 'Supply blocked', kind: 'blocked', from: 'helper' });
    await flush();
    expect(v.log).toEqual(['depot.wav', 'stop depot.wav', 'blocked.wav']);
    v.finish('blocked.wav'); await flush();
    expect(v.log).toEqual(['depot.wav', 'stop depot.wav', 'blocked.wav']); // Barracks cue was dropped
  });

  it('plays a multi-clip cue back to back', async () => {
    const v = make();
    v.speakCues(['Depot', 'Barracks']); await flush();
    v.finish('depot.wav'); await flush();
    expect(v.log).toEqual(['depot.wav', 'rax.wav']);
  });

  it('routes coach lines: clean clip, browser speech fallback, muted with the booth off', async () => {
    const v = make();
    v.speakCall({ id: 1, t: '1:00', text: 'Depot now', kind: 'coach', from: 'coach', audio: 'coach-depot.wav' });
    await flush();
    expect(v.log).toEqual(['coach-depot.wav (clean)']);
    v.speakCall({ id: 2, t: '1:05', text: 'Scout', kind: 'coach', from: 'coach', audio: 'missing.wav' });
    await flush();
    expect(v.spoken).toEqual(['Scout [coach]']);
    v.speakCall({ id: 3, t: '1:10', text: 'No clip', kind: 'coach', from: 'coach' });
    expect(v.spoken.at(-1)).toBe('No clip [coach]');
    v.boothOn.set(false);
    v.speakCall({ id: 4, t: '1:15', text: 'Muted', kind: 'coach', from: 'coach' });
    expect(v.spoken.at(-1)).toBe('No clip [coach]');
  });

  it('falls back to browser speech when any cue clip is missing, and stays quiet when off', async () => {
    const v = make();
    v.speakCues(['Depot', 'Factory']);
    expect(v.spoken).toEqual(['Depot, then Factory, 5 seconds']);
    v.playCueEvent({ id: 2, type: 'now', keys: ['Barracks'] });
    expect(v.spoken.at(-1)).toBe('Barracks, now!');
    v.speakCall({ id: 5, t: '3:00', text: 'Bank 900', kind: 'bank', from: 'helper' });
    expect(v.spoken.at(-1)).toBe('Bank 900');
    v.cueOn.set(false);
    v.speakCues(['Depot']);
    v.voice.set(false);
    v.speakCall({ id: 6, t: '3:05', text: 'quiet', kind: 'blocked', from: 'helper' });
    await flush();
    expect(v.log).toEqual([]);
    expect(v.spoken.length).toBe(3);
  });
});

describe('voice queue: clips still loading', () => {
  it('two cues in one frame play one after the other, not on top of each other', async () => {
    const v = make();
    v.speakCues(['Depot']);
    v.speakCues(['Barracks']); // same frame: the first clip is still loading
    await flush();
    expect(v.log).toEqual(['depot.wav']);
    v.finish('depot.wav'); await flush();
    expect(v.log).toEqual(['depot.wav', 'rax.wav']);
  });

  it('hush stops a clip that is still loading', async () => {
    const v = make();
    v.held.add('depot.wav');
    v.speakCues(['Depot']); await flush();
    v.hush();
    v.release('depot.wav'); await flush();
    expect(v.log).toEqual([]);
  });

  it('the next clip of a sequence never plays over a call that came in while it loaded', async () => {
    const v = make();
    v.held.add('rax.wav');
    v.speakCues(['Depot', 'Barracks']); await flush();
    v.finish('depot.wav'); await flush(); // rax.wav is loading now
    v.speakCall({ id: 9, t: '2:00', text: 'Supply blocked', kind: 'blocked', from: 'helper' });
    await flush();
    v.release('rax.wav'); await flush();
    expect(v.log).toEqual(['depot.wav', 'blocked.wav']);
    v.finish('blocked.wav'); await flush();
    v.speakCues(['Depot']); await flush(); // and the queue still works afterwards
    expect(v.log.at(-1)).toBe('depot.wav');
  });

  it('a clip that fails to load does not leave the queue stuck', async () => {
    const v = make();
    v.speakCues(['Depot', 'Gas']); await flush(); // Gas: the manifest names a clip the server can't serve
    v.speakCues(['Barracks']);
    v.finish('depot.wav'); await flush();
    expect(v.log).toEqual(['depot.wav', 'rax.wav']);
  });
});

describe('voice: remembered "Voice on" after a reload', () => {
  afterEach(() => localStorage.removeItem('voice'));

  it('stays quiet until armed, then starts fresh instead of replaying a backlog', async () => {
    localStorage.setItem('voice', '1');
    const v = new FakeVoice();
    v.cueOn.set(true); v.boothOn.set(true); v.clips = clips;
    v.ctxState = 'suspended'; // no click yet: the browser keeps the AudioContext suspended
    expect(v.voiceLabel()).toBe('Arm Voice');
    v.speakCues(['Depot']);
    v.playCueEvent({ id: 1, type: 'now', keys: ['Depot'] });
    v.speakCall({ id: 9, t: '2:00', text: 'Supply blocked', kind: 'blocked', from: 'helper' });
    await flush();
    expect(v.log).toEqual([]);
    v.clickVoice(); await flush();
    expect(v.voiceLabel()).toBe('Voice on');
    expect(v.log).toEqual(['plan.wav']);
    v.finish('plan.wav'); await flush();
    expect(v.log).toEqual(['plan.wav']); // nothing from before the click
    v.speakCues(['Barracks']); await flush();
    expect(v.log).toEqual(['plan.wav', 'rax.wav']);
  });
});

describe('voice: clip cache', () => {
  it('keeps pilot and brain clips, but only the last 64 live coach clips', async () => {
    const v = make();
    const coach = (n: number) =>
      v.speakCall({ id: n, t: '1:00', text: `line ${n}`, kind: 'coach', from: 'coach', audio: `coach-${n}.wav` });
    const brain = () =>
      v.speakCall({ id: 0, t: '0:30', text: 'Bank', kind: 'brain-bank', from: 'coach', audio: 'brain-bank.wav' });
    brain(); await flush();
    v.hush(); v.speakCues(['Depot']); await flush();
    v.hush();
    coach(1); await flush();
    coach(1); await flush();
    expect(v.loads.filter(f => f === 'coach-1.wav').length).toBe(1); // cached
    for (let n = 2; n <= 70; n++) coach(n);
    await flush();
    coach(1); await flush();
    expect(v.loads.filter(f => f === 'coach-1.wav').length).toBe(2); // evicted, fetched again
    brain(); await flush();
    v.hush(); v.speakCues(['Depot']); await flush();
    expect(v.loads.filter(f => f === 'brain-bank.wav' || f === 'depot.wav')).toEqual(['brain-bank.wav', 'depot.wav']);
  });
});

describe('voice: game over', () => {
  it('stops step cues (playing and queued) and lets a playing call finish', async () => {
    const v = make();
    v.speakCues(['Depot']); await flush();
    v.speakCues(['Barracks']);
    v.gameOver(); await flush();
    expect(v.log).toEqual(['depot.wav', 'stop depot.wav']);
    v.speakCall({ id: 9, t: '5:57', text: 'Supply blocked', kind: 'blocked', from: 'helper' }); await flush();
    v.speakCues(['Depot']); // queued behind the call
    v.gameOver(); await flush();
    expect(v.log).toEqual(['depot.wav', 'stop depot.wav', 'blocked.wav']); // the call keeps playing
    v.finish('blocked.wav'); await flush();
    expect(v.log).toEqual(['depot.wav', 'stop depot.wav', 'blocked.wav']); // the queued cue was dropped
  });

  it('drops a cue still loading when the game ends', async () => {
    const v = make();
    v.held.add('depot.wav');
    v.speakCues(['Depot']); await flush();
    v.gameOver();
    v.release('depot.wav'); await flush();
    expect(v.log).toEqual([]);
  });
});
