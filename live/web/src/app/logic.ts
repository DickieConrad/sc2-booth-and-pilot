// Pure page logic, ported line for line from live/overlay.html (constants, thresholds and wording unchanged).
import { Note, Panel, Say, Step } from './types';

export const GRACE = 10; // same as timing.py: a step counts as passed 10s after its time
export const CUE_LEAD = 5;

export const mmss = (s: number): string => {
  s = Math.max(0, Math.floor(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};
export const secs = (t: string): number => {
  const [m, s] = t.split(':').map(Number);
  return m * 60 + s;
};
export const cueText = (a: string): string =>
  a.replace(/\s*\(.*?\)/g, '').replace(/\bx(\d)/g, '$1').replace(/#/g, '').trim();

export const store = {
  get(k: string): string | null {
    try { return localStorage.getItem(k); } catch { return null; }
  },
  set(k: string, v: string): void {
    try { localStorage.setItem(k, v); } catch { /* storage blocked */ }
  },
};

// ---------- clock ----------
// The lane's clock: runs at real speed every frame and glides toward the helper's clock instead of
// jumping (each update lands a little early or late); only a big jump (>3 s: reconnect, new game) snaps.
export class ClockSmoother {
  private smooth: number | null = null;
  private smoothAt = 0;
  step(c: number | null, now: number): number | null {
    if (c == null) { this.smooth = null; return null; }
    const dt = Math.min(0.25, (now - this.smoothAt) / 1000);
    this.smoothAt = now;
    if (this.smooth == null || Math.abs(c - this.smooth) > 3) return (this.smooth = c);
    this.smooth += dt + (c - this.smooth - dt) * Math.min(1, dt * 1.5);
    return this.smooth;
  }
}

// ---------- depot gauge ----------
// Depot gauge (Dickie's #1 leak): room = cap - used, against what ~30 s of production eats
// (about 6 early, 12 from 6:00, 18 from 9:00). Counts each new supply block this game.
export interface GaugeView { cls: string; width: string; text: string; }

export class DepotGauge {
  blocks = 0;
  private wasBlocked = false;
  private gaugeClock = 0;
  update(p: Panel | null, t: number): GaugeView {
    if (!p || t < this.gaugeClock) { this.blocks = 0; this.wasBlocked = false; } // new game (or between games)
    this.gaugeClock = p ? t : 0;
    if (!p || p.cap >= 200) return { cls: 'room', width: p ? '100%' : '0', text: '' };
    const need = t < 360 ? 6 : t < 540 ? 12 : 18, room = p.cap - p.used, blocked = room <= 0;
    if (blocked && !this.wasBlocked) this.blocks++;
    this.wasBlocked = blocked;
    return {
      cls: 'room' + (blocked ? ' bad' : room < need ? ' warn' : ''),
      width: Math.max(4, Math.min(100, (room / (need * 2)) * 100)) + '%',
      text: (blocked ? 'BLOCKED: depot!' : room < need ? `${room} room: depot now` : `${room} room`)
        + (this.blocks ? ` · blocks ${this.blocks}` : ''),
    };
  }
}

// ---------- stats ----------
export function mineralsClass(p: Panel | null, t: number): string {
  return 'stat' + (p && t >= 60 && p.minerals >= (t < 240 ? 400 : 600) ? ' warn' : '');
}
export function supplyClass(p: Panel | null, planSupply: number | null): string {
  return 'stat' + (p && p.cap < 200 && p.used >= p.cap ? ' bad'
    : p && planSupply != null && p.used < planSupply - 3 ? ' warn' : '');
}

// ---------- checkpoints ----------
// Supply targets each minute (graded against your real supply as the clock passes them) and the
// plan's key timings. Grades live in the page: a reload mid-game starts grading from that point.
export const KEY_STEPS = [/^Command Center( \(natural\))?$/, /^Factory$/, /^Starport$/, /^Stim/, /^Command Center #3/,
  /^Barracks ×2/, /move out/i, /^HIT/];

export interface Chip { label: string; value: string | number; sec?: number; want?: number; }

export function supplyAt(steps: Step[], sec: number): number | null | undefined {
  let s: number | null | undefined = null;
  for (const st of steps) if (st.sec <= sec) s = st.supply;
  return s;
}

export function supplyChips(steps: Step[]): Chip[] {
  const out: Chip[] = [];
  const last = steps[steps.length - 1].sec;
  for (let m = 2; m * 60 <= Math.min(last, 600); m++) {
    const want = supplyAt(steps, m * 60);
    if (want == null || (m > 2 && want === supplyAt(steps, (m - 1) * 60))) continue; // no new target that minute
    out.push({ label: `${m}:00`, value: want, sec: m * 60, want });
  }
  // 1v1 Field Manual builds give SCV-count checkpoints instead of supply after 3:00 (shown, not graded).
  for (const st of steps.filter(s => /^Check/.test(s.action))) {
    out.push({ label: st.t, value: st.action.replace(/^Check:\s*/, '').replace(/, stop here/, '') });
  }
  return out;
}

export function keyChips(steps: Step[]): Chip[] {
  const out: Chip[] = [];
  for (const re of KEY_STEPS) {
    const st = steps.find(s => re.test(s.action));
    if (st) out.push({ label: st.t, value: cueText(st.action), sec: st.sec });
  }
  return out;
}

// ---------- step lane ----------
export const LANE_BACK = 10, LANE_AHEAD = 50, NOW_AT = 0.22, TIERS = [26, 74, 122];

export interface LaneItem { kind: 'tick' | 'note' | 'callmark'; sec: number; text: string; sub?: string; top?: number;
  coach?: boolean; title?: string; key: string; }

export function laneItems(steps: Step[], calls: Say[]): LaneItem[] {
  const out: LaneItem[] = [];
  // Ticks every 10s.
  const last = steps[steps.length - 1].sec + 60;
  for (let s = 0; s <= last; s += 10) out.push({ kind: 'tick', sec: s, text: s % 30 === 0 ? mmss(s) : '', key: `k${s}` });
  // Steps; ones close in time stack into tiers so they don't overlap.
  const tierEnd = TIERS.map(() => -1e9);
  steps.forEach((st, i) => {
    let tier = tierEnd.findIndex(end => st.sec >= end);
    if (tier < 0) tier = tierEnd.indexOf(Math.min(...tierEnd));
    tierEnd[tier] = st.sec + 8; // a note is ~8 seconds wide on the lane
    out.push({ kind: 'note', sec: st.sec, text: cueText(st.action), sub: st.supply != null ? `${st.t} · ${st.supply}` : st.t,
      top: TIERS[tier], key: `n${i}` });
  });
  for (const c of calls) {
    out.push({ kind: 'callmark', sec: secs(c.t), text: c.text, title: `${c.t} ${c.text}`, coach: c.from === 'coach',
      key: `c${c.id}` });
  }
  return out;
}

/** Where a lane item sits and whether it is drawn, for lane width w at clock c (null = no game). */
export function laneX(sec: number, c: number | null, w: number): { x: number; visible: boolean } {
  const pps = (w * (1 - NOW_AT)) / LANE_AHEAD, now = c == null ? 0 : c;
  return { x: w * NOW_AT + (sec - now) * pps, visible: sec >= now - LANE_BACK - 10 && sec <= now + LANE_AHEAD + 5 };
}

// ---------- step cues (fallback when the Rust engine is not calling them) ----------
export class CueScheduler {
  private cued = new Set<number>();
  private cueKey = '';
  /** A new game: every step gets its cue again (a rematch has the same plan and names). */
  reset(): void { this.cued = new Set(); }
  /** Steps whose 5-second cue is due now (each step once per game and plan|matchup). */
  due(c: number, key: string, steps: Step[]): string[] {
    if (key !== this.cueKey) { this.cueKey = key; this.cued = new Set(); }
    const due: string[] = [];
    for (const st of steps) {
      const at = st.sec - CUE_LEAD;
      if (this.cued.has(st.sec) || c < at || /^Check/.test(st.action)) continue; // checkpoints aren't actions
      this.cued.add(st.sec);
      if (c - at <= 3) due.push(cueText(st.action)); // skip steps already past (joined late, clock jump)
    }
    return due;
  }
}

// ---------- build lists ----------
export interface Row { kind: 'step' | 'call'; sec: number; t: string; supply?: number | null; action?: string;
  note?: string; call?: Say; }

export function timelineRows(steps: Step[], calls: Say[]): Row[] {
  const rows: Row[] = steps.map(st => ({ ...st, kind: 'step' as const }));
  for (const c of calls) rows.push({ sec: secs(c.t), t: c.t, kind: 'call', call: c });
  rows.sort((a, b) => a.sec - b.sec || Number(a.kind === 'call') - Number(b.kind === 'call'));
  return rows;
}

/**
 * scrollTop that puts a row a third of the way down its list. Measured from the list's own top (both rects are
 * viewport rects, so where the list sits on the page cancels out). The old code used row.offsetTop, which counts
 * from the nearest positioned ancestor (the page body), so the lists jumped by the height of everything above them.
 */
export function followScrollTop(rowTop: number, listTop: number, listClientTop: number, scrollTop: number,
  viewHeight: number): number {
  return Math.max(0, rowTop - listTop - listClientTop + scrollTop - viewHeight / 3);
}

// ---------- compact mode: next steps ----------
export interface NextStep { sec: number; t: string; supply?: number | null; text: string; due: boolean; }

/** The next n steps still to do (same "passed" rule as the lists); before a game: the plan's first n. */
export function nextSteps(steps: Step[], c: number | null, n = 3): NextStep[] {
  return steps.filter(st => c == null || st.sec > c - GRACE).slice(0, n)
    .map(st => ({ sec: st.sec, t: st.t, supply: st.supply, text: cueText(st.action), due: c != null && st.sec - c <= 5 }));
}

// ---------- coach pointers ----------
export interface Pointer { id: number; label: string; result: string; who: '' | 'coach' | 'dickie'; text: string; }

// Tooling notes (reader, helper, duplicate rows) live in the DB for the coach, not on the player's page.
export const pointerFilter = (r: Note) => !/^(Reader|Switched the helper|DB live|Duplicate of game)/.test(r.text);

export function pointer(r: Note): Pointer {
  const opp = r.enemies ? r.enemies.replace(/ \((\w+)\)/g, '') : '';
  const label = r.game_id ? `#${r.game_id} ${r.result === 'Victory' ? 'W' : r.result === 'Defeat' ? 'L' : '?'} vs ${opp}` : 'session';
  // Live conversation lines: only the speaker's name is colored (Dickie blue, Coach purple).
  const m = r.text.match(/^(Owner callout|Coach): (.*)$/s);
  return { id: r.id, label, result: r.result === 'Victory' ? 'win' : r.result === 'Defeat' ? 'loss' : 'x',
    who: m ? (m[1] === 'Coach' ? 'coach' : 'dickie') : '', text: m ? m[2] : r.text };
}

// ---------- release notes ----------
// live/CHANGELOG.md: "## " lines are versions, "- " lines are changes. The top version labels the button.
export interface NoteSection { title: string; items: string[]; }
export function parseChangelog(md: string): { version: string | null; blocks: NoteSection[] } {
  const blocks: NoteSection[] = [];
  let cur: NoteSection | null = null;
  for (const line of md.split(/\r?\n/)) {
    if (line.startsWith('## ')) { cur = { title: line.slice(3), items: [] }; blocks.push(cur); }
    else if (line.startsWith('- ')) {
      if (!cur) { cur = { title: '', items: [] }; blocks.push(cur); }
      cur.items.push(line.slice(2));
    }
  }
  const top = md.match(/^## (v[\d.]+)/m);
  return { version: top ? top[1] : null, blocks };
}
