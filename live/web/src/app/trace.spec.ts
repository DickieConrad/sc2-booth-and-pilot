import { LoopTrace } from './trace';

describe('latency trace', () => {
  it('records nothing while the helper is not tracing', () => {
    const t = new LoopTrace();
    expect(t.recv('s1', 1000)).toBeNull();
    t.finish('s1', 'clip', 0, 1100);
    expect(t.report(1200)).toBe('');
  });

  it('times receipt -> audio start and reports once', () => {
    const t = new LoopTrace();
    t.enabled = true;
    t.cur = t.recv('s7', 1000);
    expect(t.take()).toBe('s7');
    expect(t.take()).toBeNull();
    t.finish('s7', 'clip', 30, 1250);
    expect(JSON.parse(t.report(1300))).toEqual([{ key: 's7', recv: 1000, how: 'clip', start: 1250, dec: 30, wait: 220 }]);
    expect(t.report(1400)).toBe('');
  });

  it('reports calls never spoken within 15 s as how "none"', () => {
    const t = new LoopTrace();
    t.enabled = true;
    t.recv('c3', 1000);
    expect(t.report(2000)).toBe('');
    expect(JSON.parse(t.report(17000))).toEqual([{ key: 'c3', recv: 1000, how: 'none' }]);
  });
});
