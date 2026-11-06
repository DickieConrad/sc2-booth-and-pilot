import { TestBed } from '@angular/core/testing';
import { LiveService } from './live.service';
import { CueEvent, Plans, State } from './types';
import { VoiceService } from './voice.service';

const plans: Plans = { matchups: { TP: ['A'] }, plans: { A: { name: 'Steel', title: 'A', hit: '6:00',
  steps: [{ sec: 20, t: '0:20', supply: 14, action: 'Supply Depot' }] } } };

function setup() {
  const voice = { playCueEvent: vi.fn(), speakCall: vi.fn(), speakCues: vi.fn(), gameOver: vi.fn() };
  TestBed.configureTestingModule({ providers: [{ provide: VoiceService, useValue: voice }] });
  const live = TestBed.inject(LiveService);
  live.plans.set(plans);
  const state = (over: Partial<State> = {}): State => {
    const now = Date.now() / 1000;
    return { in_game: true, clock: 100, clock_wall: now, now, plan: 'A', plan_name: 'Steel', vs: 'Bob (Prot)', key: 'TP',
      panel: null, plan_supply: null, says: [], cues: [], cue_engine: true, ...over };
  };
  const quarter = () => (live as unknown as { everyQuarter(): void }).everyQuarter();
  const played = () => voice.playCueEvent.mock.calls.map(([ev]) => (ev as CueEvent).id);
  return { live, voice, state, quarter, played };
}
const cue = (id: number): CueEvent => ({ id, type: 'target', keys: ['Supply Depot'] });

describe('live: engine cues', () => {
  it('skips the backlog on load, then plays each new cue once', () => {
    const { live, state, played } = setup();
    live.apply(state({ cues: [cue(4), cue(5)] }));
    live.apply(state({ cues: [cue(4), cue(5), cue(6)] }));
    live.apply(state({ cues: [cue(4), cue(5), cue(6)] }));
    expect(played()).toEqual([6]);
  });

  it('follows a helper restart: cue ids start again from 1', () => {
    const { live, state, played } = setup();
    live.apply(state({ cues: [cue(40)] }));
    live.apply(state({ cues: [cue(40), cue(41)] }));
    live.apply(state({ in_game: false, cues: [] })); // fresh helper: cue_seq back to 0
    live.apply(state({ cues: [cue(1)] }));
    live.apply(state({ cues: [cue(1), cue(2)] }));
    expect(played()).toEqual([41, 1, 2]);
  });
});

describe('live: a plan the page has not loaded', () => {
  it('fetches /plans again once for a new plan id (a sparring bot build), then has it', async () => {
    const bot = { ...plans, plans: { ...plans.plans, BOT: { name: 'SixRax', title: 'BOT', hit: '', steps: [] } } };
    const { live, state } = setup();
    const fetchMock = vi.fn(async (url: string) => ({
      json: async () => (url === '/plans' ? bot : state({ plan: 'BOT', player: 'SixRax' })),
    }));
    vi.stubGlobal('fetch', fetchMock);
    try {
      live.apply(state({ plan: 'BOT', player: 'SixRax' }));
      live.apply(state({ plan: 'BOT', player: 'SixRax' })); // still loading: no second fetch
      await vi.waitFor(() => expect(live.plans()?.plans['BOT']).toBeTruthy());
      expect(fetchMock.mock.calls.filter(([u]) => u === '/plans')).toHaveLength(1);
      live.apply(state({ plan: 'A' })); // a known plan: nothing fetched
      expect(fetchMock.mock.calls.filter(([u]) => u === '/plans')).toHaveLength(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('live: game over', () => {
  it('flushes the voice and plays no cue that arrives with (or after) the game end', () => {
    const { live, voice, state, played } = setup();
    live.apply(state({ cues: [cue(7)] }));
    live.apply(state({ clock: 355, cues: [cue(7), cue(8)] }));
    expect(voice.gameOver).not.toHaveBeenCalled();
    live.apply(state({ in_game: false, clock: 357, cues: [cue(7), cue(8), cue(9)] })); // HIT cue in the last frame
    expect(voice.gameOver).toHaveBeenCalledTimes(1);
    expect(played()).toEqual([8]);
    live.apply(state({ in_game: false, clock: 357, cues: [cue(7), cue(8), cue(9)] }));
    expect(voice.gameOver).toHaveBeenCalledTimes(1); // once per game end
  });
});

describe('live: fallback cues (no engine)', () => {
  it('cues each step again in a rematch against the same opponents', () => {
    const { live, voice, state, quarter } = setup();
    live.apply(state({ cue_engine: false, clock: 16 }));
    quarter();
    expect(voice.speakCues).toHaveBeenCalledTimes(1);
    live.apply(state({ cue_engine: false, clock: 17 }));
    quarter();
    expect(voice.speakCues).toHaveBeenCalledTimes(1); // once per game
    live.apply(state({ cue_engine: false, in_game: false, clock: 0 }));
    live.apply(state({ cue_engine: false, clock: 16 })); // rematch: same plan, same names
    quarter();
    expect(voice.speakCues).toHaveBeenCalledTimes(2);
  });

  it('treats the clock going back to the start as a new game', () => {
    const { live, voice, state, quarter } = setup();
    live.apply(state({ cue_engine: false, clock: 16 }));
    quarter();
    live.apply(state({ cue_engine: false, clock: 300 }));
    live.apply(state({ cue_engine: false, clock: 16 })); // the helper missed the end: the next game is already on
    quarter();
    expect(voice.speakCues).toHaveBeenCalledTimes(2);
  });

  it('stays quiet after the game ends', () => {
    const { live, voice, state, quarter } = setup();
    live.apply(state({ cue_engine: false, clock: 10 }));
    quarter();
    live.apply(state({ cue_engine: false, in_game: false, clock: 16 }));
    quarter();
    expect(voice.speakCues).not.toHaveBeenCalled();
  });
});
