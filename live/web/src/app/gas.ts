// Gas balance gauge (Dickie's #1 leak in the last games: banking 400-1600 gas while mineral-starved).
// Rule we coach: gas > 400 and minerals low -> move 3-6 SCVs from gas to minerals. Visual only (no voice).
// Shaped like DepotGauge in logic.ts: per-update, counts each new "bad" stretch this game, resets on a new game.
import { Panel } from './types';
import { GaugeView } from './logic';

export const GAS_FROM = 180; // before 3:00 the opener decides gas: neutral, no advice
export const GAS_HEAVY = 250; // gas above this ...
export const MIN_LOW = 300; // ... with minerals below this: warn ("gas heavy")
export const GAS_FLOAT = 400; // gas above this ...
export const MIN_STARVED = 200; // ... with minerals below this: bad -> 3 SCVs to minerals
export const GAS_FLOAT_BIG = 800; // gas above this when bad: 6 SCVs to minerals
export const GAS_BAR_FULL = 800; // the bar fills toward this bank

export class GasGauge {
  floats = 0;
  private wasBad = false;
  private gaugeClock = 0;
  update(p: Panel | null, t: number): GaugeView {
    if (!p || t < this.gaugeClock) { this.floats = 0; this.wasBad = false; } // new game (or between games)
    this.gaugeClock = p ? t : 0;
    if (!p) return { cls: 'room', width: '0', text: '' };
    const width = Math.max(4, Math.min(100, Math.round((p.gas / GAS_BAR_FULL) * 100))) + '%';
    if (t < GAS_FROM) { this.wasBad = false; return { cls: 'room off', width, text: `${p.gas} gas` }; }
    const bad = p.gas > GAS_FLOAT && p.minerals < MIN_STARVED;
    const warn = !bad && p.gas > GAS_HEAVY && p.minerals < MIN_LOW;
    if (bad && !this.wasBad) this.floats++;
    this.wasBad = bad;
    return {
      cls: 'room' + (bad ? ' bad' : warn ? ' warn' : ''),
      width,
      text: (bad ? `${p.gas > GAS_FLOAT_BIG ? 6 : 3} SCVs to minerals` : warn ? `${p.gas} gas: gas heavy` : `${p.gas} gas`)
        + (this.floats ? ` · floats ${this.floats}` : ''),
    };
  }
}
