import { GAS_FROM, GasGauge } from './gas';

const panel = (minerals: number, gas: number) => ({ minerals, gas, used: 50, cap: 100 });

describe('gas gauge', () => {
  it('stays neutral before 3:00', () => {
    const g = new GasGauge();
    expect(g.update(panel(50, 900), GAS_FROM - 1)).toEqual({ cls: 'room off', width: '100%', text: '900 gas' });
    expect(g.floats).toBe(0);
  });

  it('is ok with little gas or plenty of minerals', () => {
    const g = new GasGauge();
    expect(g.update(panel(0, 250), 200)).toEqual({ cls: 'room', width: '31%', text: '250 gas' });
    expect(g.update(panel(300, 1200), 201).cls).toBe('room');
    expect(g.update(panel(0, 0), 202).width).toBe('4%');
  });

  it('warns when gas heavy and minerals under 300', () => {
    const g = new GasGauge();
    expect(g.update(panel(299, 251), 200)).toEqual({ cls: 'room warn', width: '31%', text: '251 gas: gas heavy' });
    expect(g.update(panel(150, 400), 201).cls).toBe('room warn'); // 400 is not over 400
    expect(g.update(panel(200, 600), 202).cls).toBe('room warn'); // 200 minerals is not starved
  });

  it('goes bad when floating gas while mineral-starved: 3 SCVs, 6 over 800', () => {
    const g = new GasGauge();
    expect(g.update(panel(199, 401), 300)).toEqual({ cls: 'room bad', width: '50%', text: '3 SCVs to minerals · floats 1' });
    expect(g.update(panel(100, 800), 301).text).toBe('3 SCVs to minerals · floats 1');
    expect(g.update(panel(100, 801), 302).text).toBe('6 SCVs to minerals · floats 1');
  });

  it('counts each new float once and resets on a new game', () => {
    const g = new GasGauge();
    g.update(panel(100, 500), 300);
    g.update(panel(100, 600), 301); // same float
    expect(g.update(panel(400, 600), 302).text).toBe('600 gas · floats 1');
    expect(g.update(panel(50, 900), 303).text).toBe('6 SCVs to minerals · floats 2');
    expect(g.floats).toBe(2);
    expect(g.update(panel(50, 0), 10).text).toBe('0 gas'); // clock went back: new game
    expect(g.floats).toBe(0);
    g.update(panel(100, 500), 300);
    expect(g.update(null, 0)).toEqual({ cls: 'room', width: '0', text: '' }); // between games
    expect(g.floats).toBe(0);
  });
});
