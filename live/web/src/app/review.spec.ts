import { GameEndWatch, Review, ReviewService, checkpointView, gameEnded, resultClass, spark } from './parts/review';

describe('review: game end', () => {
  it('fires only on the in-game -> out-of-game edge', () => {
    expect(gameEnded(true, false)).toBe(true);
    expect(gameEnded(undefined, false)).toBe(false); // page load between games
    expect(gameEnded(false, false)).toBe(false);
    expect(gameEnded(false, true)).toBe(false); // game start
    expect(gameEnded(true, true)).toBe(false);
    expect(gameEnded(true, undefined)).toBe(true); // state gone
  });
});

describe('review: game end watch', () => {
  const ig = (clock: number) => ({ in_game: true, clock });
  const out = (clock: number) => ({ in_game: false, clock });

  it('opens on a game end seen on the same connection', () => {
    const w = new GameEndWatch();
    expect(w.see(1, ig(300))).toBe(false);
    expect(w.see(1, out(300))).toBe(true);
    expect(w.see(1, out(300))).toBe(false); // once
  });

  it('does not open when the helper restarts mid-game (reconnect, fresh in_game false)', () => {
    const w = new GameEndWatch();
    w.see(1, ig(300));
    expect(w.see(2, out(0))).toBe(false); // the new helper has not seen the game yet
    expect(w.see(2, ig(305))).toBe(false);
    expect(w.see(2, out(600))).toBe(true); // the real end, later
  });

  it('does not open after a game of a minute or less, nor on page load between games', () => {
    const w = new GameEndWatch();
    expect(w.see(1, out(0))).toBe(false);
    w.see(1, ig(45));
    expect(w.see(1, out(45))).toBe(false);
  });
});

describe('review: views', () => {
  it('colors checkpoints like the helper: 3+ behind is bad', () => {
    expect(checkpointView({ t: 360, used: 51, plan: 60, diff: -9 })).toEqual({ label: '6:00', value: '51/60', cls: 'bad' });
    expect(checkpointView({ t: 180, used: 21, plan: 22, diff: -1 }).cls).toBe('warn');
    expect(checkpointView({ t: 480, used: 78, plan: 70, diff: 8 }).cls).toBe('ok');
    expect(checkpointView({ t: 480, used: 78, plan: null, diff: null })).toEqual({ label: '8:00', value: '78', cls: 'ok' });
  });

  it('maps results to classes', () => {
    expect(resultClass('Victory')).toBe('win');
    expect(resultClass('Defeat')).toBe('loss');
    expect(resultClass(null)).toBe('other');
  });
});

describe('review: sparkline', () => {
  it('scales used, plan and cap into the viewBox and lifts the pen on missing reads', () => {
    const s = spark([[0, 12, null, 15], [300, 50, 60, 62], [450, null, null, null], [600, 100, 70, 110]], 600, 90)!;
    expect(s.ymax).toBe(100);
    expect(s.used).toBe('M0,79.2 L300,45 M600,0');
    expect(s.plan).toBe('M300,36 M600,27'); // the missing read at 7:30 breaks it too
    expect(s.cap).toBe('M0,76.5 L300,34.2 M600,0'); // cap above the scale is clamped to the top
    expect(s.marks).toEqual([{ x: 180, label: '3:00' }, { x: 360, label: '6:00' }, { x: 480, label: '8:00' }]);
  });

  it('only marks checkpoints inside the game and needs two reads', () => {
    expect(spark([[0, 12, 12, 15], [200, 30, 30, 39]])!.marks.map(m => m.label)).toEqual(['3:00']);
    expect(spark([[5, 12, 12, 15]])).toBeNull();
    expect(spark([])).toBeNull();
  });
});

describe('review service', () => {
  const review = { id: 30, result: 'Victory', top_fix: { key: 'production' } } as unknown as Review;
  afterEach(() => vi.unstubAllGlobals());

  it('fetches the latest review and opens', async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => review });
    vi.stubGlobal('fetch', fetch);
    const svc = new ReviewService();
    await svc.show();
    expect(fetch).toHaveBeenCalledWith('/history/review/latest', { cache: 'no-store' });
    expect(svc.open()).toBe(true);
    expect(svc.review()).toBe(review);
    expect(svc.error()).toBeNull();
    expect(svc.loading()).toBe(false);
  });

  it('shows the server error instead of an older review', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({ error: 'no game latest' }) }));
    const svc = new ReviewService();
    svc.review.set(review);
    await svc.show();
    expect(svc.error()).toBe('no game latest');
    expect(svc.review()).toBeNull();
    expect(svc.open()).toBe(true);
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('helper down')));
    await svc.show();
    expect(svc.error()).toBe('helper down');
  });
});
