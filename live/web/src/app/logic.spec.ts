import {
  ClockSmoother, CueScheduler, DepotGauge, followScrollTop, keyChips, laneItems, laneX, mineralsClass, mmss, parseChangelog, pointer,
  nextSteps, pointerFilter, supplyChips, supplyClass, timelineRows, TIERS,
} from './logic';
import { Say, Step } from './types';

const panel = (used: number, cap: number) => ({ minerals: 0, gas: 0, used, cap });
const st = (sec: number, action: string, supply?: number): Step => ({ sec, t: mmss(sec), action, supply });

describe('depot gauge', () => {
  it('needs 6 room before 6:00, 12 before 9:00, 18 after', () => {
    const g = new DepotGauge();
    expect(g.update(panel(20, 26), 100)).toEqual({ cls: 'room', width: '50%', text: '6 room' });
    expect(g.update(panel(21, 26), 110).cls).toBe('room warn');
    expect(g.update(panel(21, 26), 110).text).toBe('5 room: depot now');
    expect(g.update(panel(60, 72), 360).cls).toBe('room'); // 12 room at 6:00: exactly enough
    expect(g.update(panel(61, 72), 361).cls).toBe('room warn');
    expect(g.update(panel(100, 117), 540).cls).toBe('room warn'); // 17 < 18 from 9:00
    expect(g.update(panel(100, 118), 541).cls).toBe('room');
  });

  it('counts each new block once and resets on a new game', () => {
    const g = new DepotGauge();
    expect(g.update(panel(23, 23), 100)).toEqual({ cls: 'room bad', width: '4%', text: 'BLOCKED: depot! · blocks 1' });
    expect(g.update(panel(23, 23), 101).text).toBe('BLOCKED: depot! · blocks 1'); // same block
    expect(g.update(panel(23, 31), 102).text).toBe('8 room · blocks 1');
    expect(g.update(panel(31, 31), 103).text).toBe('BLOCKED: depot! · blocks 2');
    expect(g.update(panel(14, 15), 10).text).toBe('1 room: depot now'); // clock went back: new game
  });

  it('is full at 200 cap and empty between games', () => {
    const g = new DepotGauge();
    expect(g.update(panel(200, 200), 900)).toEqual({ cls: 'room', width: '100%', text: '' });
    expect(g.update(null, 0)).toEqual({ cls: 'room', width: '0', text: '' });
  });
});

describe('stat tiles', () => {
  it('flags a bank (400 before 4:00, 600 after) and supply trouble', () => {
    expect(mineralsClass({ ...panel(0, 0), minerals: 400 }, 59)).toBe('stat');
    expect(mineralsClass({ ...panel(0, 0), minerals: 400 }, 60)).toBe('stat warn');
    expect(mineralsClass({ ...panel(0, 0), minerals: 599 }, 240)).toBe('stat');
    expect(supplyClass(panel(31, 31), null)).toBe('stat bad');
    expect(supplyClass(panel(200, 200), null)).toBe('stat');
    expect(supplyClass(panel(26, 40), 30)).toBe('stat warn');
    expect(supplyClass(panel(27, 40), 30)).toBe('stat');
  });
});

describe('clock smoothing', () => {
  it('snaps on the first read and on jumps over 3 s, glides otherwise', () => {
    const s = new ClockSmoother();
    expect(s.step(null, 0)).toBeNull();
    expect(s.step(100, 1000)).toBe(100);
    // 100 ms later the helper says 100.3: real speed (+0.1) plus 15% of the remaining gap
    expect(s.step(100.3, 1100)).toBeCloseTo(100.1 + 0.2 * 0.15, 6);
    expect(s.step(110, 1200)).toBe(110); // >3 s off: snap
    // a long pause between frames counts as at most 0.25 s
    expect(s.step(110, 6200)).toBeCloseTo(110.25 + (110 - 110 - 0.25) * 0.375, 6);
    expect(s.step(null, 6300)).toBeNull();
    expect(s.step(50, 6400)).toBe(50); // after a game: fresh start
  });
});

describe('fallback step cues', () => {
  it('cues each step 5 s early, once, skipping checks and steps long past', () => {
    const c = new CueScheduler(), steps = [st(18, 'Supply Depot'), st(41, 'Barracks (wall)'), st(45, 'Check: 20 SCVs'),
      st(60, 'Command Center #2')];
    expect(c.due(12, 'A|x', steps)).toEqual([]);
    expect(c.due(13, 'A|x', steps)).toEqual(['Supply Depot']);
    expect(c.due(14, 'A|x', steps)).toEqual([]);
    expect(c.due(36, 'A|x', steps)).toEqual(['Barracks']);
    expect(c.due(60, 'A|x', steps)).toEqual([]); // 55 + 3 < 60: joined late, skip
    expect(c.due(13, 'A|y', steps)).toEqual(['Supply Depot']); // new game: fresh
    expect(c.due(13, 'A|y', steps)).toEqual([]);
    c.reset(); // rematch: same plan, same names
    expect(c.due(13, 'A|y', steps)).toEqual(['Supply Depot']);
  });
});

describe('checkpoints', () => {
  const steps = [st(18, 'Supply Depot', 14), st(100, 'Command Center (natural)', 19), st(130, 'Factory', 22),
    st(170, 'Starport', 22), st(250, 'Stimpack', 40), st(400, 'Check: 44 SCVs, stop here'), st(420, 'MOVE OUT', 70)];
  it('makes a supply chip per minute with a new target', () => {
    expect(supplyChips(steps).map(c => [c.label, c.value])).toEqual([
      ['2:00', 19], ['3:00', 22], ['5:00', 40], ['7:00', 70], ['6:40', '44 SCVs']]);
  });
  it('lists the key timings in KEY_STEPS order', () => {
    expect(keyChips(steps).map(c => [c.label, c.value])).toEqual([
      ['1:40', 'Command Center'], ['2:10', 'Factory'], ['2:50', 'Starport'], ['4:10', 'Stimpack'], ['7:00', 'MOVE OUT']]);
  });
});

describe('step lane', () => {
  it('stacks close steps into tiers and places NOW at 22%', () => {
    const items = laneItems([st(10, 'A'), st(12, 'B'), st(14, 'C'), st(16, 'D'), st(30, 'E')], [
      { id: 1, t: '0:20', text: 'Depot', kind: 'blocked', from: 'helper' } as Say]);
    expect(items.filter(i => i.kind === 'note').map(i => i.top)).toEqual([TIERS[0], TIERS[1], TIERS[2], TIERS[0], TIERS[0]]);
    expect(items.filter(i => i.kind === 'tick').map(i => i.text)).toEqual(['0:00', '', '', '0:30', '', '', '1:00', '', '', '1:30']);
    expect(items.find(i => i.kind === 'callmark')).toMatchObject({ sec: 20, title: '0:20 Depot', coach: false });
    expect(laneX(100, 100, 1000)).toEqual({ x: 220, visible: true });
    expect(laneX(150, 100, 1000).x).toBeCloseTo(1000, 6);
    expect(laneX(156, 100, 1000).visible).toBe(false);
    expect(laneX(79, 100, 1000).visible).toBe(false);
  });
});

describe('build list', () => {
  it('puts calls after steps at the same second', () => {
    const rows = timelineRows([st(20, 'A'), st(40, 'B')], [{ id: 3, t: '0:20', text: 'x', kind: 'k', from: 'coach' }]);
    expect(rows.map(r => r.kind)).toEqual(['step', 'call', 'step']);
  });
});

describe('build list follow scroll', () => {
  it('measures the row from the list, not the page', () => {
    // List starts 700px down the page (top bar, hud, lane, pointers, checkpoints above it), 600px tall.
    // The current row is 300px into the list content: it should land 200px (a third) from the list top.
    expect(followScrollTop(700 + 300, 700, 0, 0, 600)).toBe(100);
    // Same row with the list already scrolled 250px: its viewport top moved up by 250, the target is unchanged.
    expect(followScrollTop(700 + 300 - 250, 700, 0, 250, 600)).toBe(100);
    // Where the list sits on the page doesn't matter (the old offsetTop math added the 700px).
    expect(followScrollTop(40 + 300, 40, 0, 0, 600)).toBe(100);
    expect(followScrollTop(700 + 50, 700, 0, 0, 600)).toBe(0); // near the top: clamp, don't scroll negative
    expect(followScrollTop(700 + 301, 700, 1, 0, 600)).toBe(100); // list border is not content
  });
});

describe('next steps (compact mode)', () => {
  const steps = [st(0, 'SCV x2 (constant)', 12), st(17, 'Supply Depot', 14), st(40, 'Barracks', 16), st(60, 'Refinery', 16),
    st(90, 'Reaper', 19)];
  it('shows the next three not yet passed; a step stays until 10 s after its time', () => {
    expect(nextSteps(steps, 20).map(s => s.text)).toEqual(['Supply Depot', 'Barracks', 'Refinery']);
    expect(nextSteps(steps, 27).map(s => s.text)).toEqual(['Barracks', 'Refinery', 'Reaper']);
    expect(nextSteps(steps, 36)[0]).toEqual({ sec: 40, t: '0:40', supply: 16, text: 'Barracks', due: true });
    expect(nextSteps(steps, 30)[0].due).toBe(false);
    expect(nextSteps(steps, 95).map(s => s.text)).toEqual(['Reaper']);
    expect(nextSteps(steps, 200)).toEqual([]);
  });
  it('before a game: the plan opening, nothing due', () => {
    expect(nextSteps(steps, null, 2)).toEqual([
      { sec: 0, t: '0:00', supply: 12, text: 'SCV 2', due: false },
      { sec: 17, t: '0:17', supply: 14, text: 'Supply Depot', due: false },
    ]);
  });
});

describe('pointers', () => {
  it('labels games and colors only the speaker', () => {
    const r = pointer({ id: 1, text: 'Coach: Nice hit.', game_id: 28, result: 'Victory', enemies: 'FoeOne (Prot) + FoeTwo (Prot)' });
    expect(r).toEqual({ id: 1, label: '#28 W vs FoeOne + FoeTwo', result: 'win', who: 'coach', text: 'Nice hit.' });
    expect(pointer({ id: 2, text: 'Owner callout: oracle' })).toMatchObject({ label: 'session', who: 'dickie', text: 'oracle' });
    expect(pointer({ id: 3, text: 'Depots', game_id: 2, result: 'Defeat', enemies: null }).label).toBe('#2 L vs ');
    expect(pointerFilter({ id: 4, text: 'Reader: 0 misses' })).toBe(false);
  });
});

describe('release notes', () => {
  it('reads versions and changes; the top version labels the button', () => {
    const { version, blocks } = parseChangelog('# Changelog\n\n## v1.2.0 · 2026-10-30\n- one\n- two\n\n## v1.1.0\r\n- old\r\n');
    expect(version).toBe('v1.2.0');
    expect(blocks).toEqual([{ title: 'v1.2.0 · 2026-10-30', items: ['one', 'two'] }, { title: 'v1.1.0', items: ['old'] }]);
  });
});
