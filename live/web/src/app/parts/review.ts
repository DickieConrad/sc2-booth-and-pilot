// Post-game review: opens by itself when a game ends (in_game true -> false), fetches /history/review/latest
// (live/history.py) and shows result + length, the one fix big, the findings, and a supply-vs-plan sparkline.
// Esc or Close dismisses it; the top bar's "Last game" button reopens it. Read-only: one GET, nothing posted.
import { Component, ElementRef, Injectable, effect, inject, signal, untracked, viewChild } from '@angular/core';
import { LiveService } from '../live.service';
import { mmss } from '../logic';

export interface Finding { kind: string; t: number; text: string; }
export interface Fix { key: string; title: string; action: string; why: string; also: { key: string; title: string; action: string } | null; }
export interface Checkpoint { t: number; used: number; plan: number | null; diff: number | null; }
export interface Review {
  id: number;
  result: string | null;
  length: number | null;
  length_text: string;
  plan: string | null;
  enemies: string | null;
  checkpoints: Checkpoint[];
  findings: Finding[];
  top_fix: Fix;
  series: [number, number | null, number | null, number | null][]; // t, used, plan supply, cap
}

/** True only on the in-game -> out-of-game edge (not on page load, not between games). */
export const gameEnded = (prev: boolean | undefined, next: boolean | undefined): boolean => prev === true && !next;

/** Shorter "games" (a quick leave, a test) get no review popup. */
export const REVIEW_MIN_CLOCK = 60;

/**
 * Game-end detector for the review popup. A restarted helper reports in_game false until it sees the game again,
 * which looks like a game end: so only an end seen on the same /events connection counts (a reconnect forgets the
 * last state), and only after more than a minute of game clock.
 */
export class GameEndWatch {
  private conn: number | null = null;
  private prev: { in_game: boolean; clock: number } | null = null;
  see(conn: number, s: { in_game: boolean; clock: number } | null | undefined): boolean {
    if (conn !== this.conn) { this.conn = conn; this.prev = null; }
    const ended = !!this.prev && this.prev.clock > REVIEW_MIN_CLOCK && gameEnded(this.prev.in_game, s?.in_game);
    this.prev = s ? { in_game: s.in_game, clock: s.clock } : null;
    return ended;
  }
}

export const resultClass = (r: string | null): string => (r === 'Victory' ? 'win' : r === 'Defeat' ? 'loss' : 'other');

/** "51/60" with a class: ok on/above plan, warn 1-2 behind, bad 3+ behind (the helper's "behind" threshold). */
export function checkpointView(c: Checkpoint): { label: string; value: string; cls: string } {
  const cls = c.diff == null || c.diff >= 0 ? 'ok' : c.diff > -3 ? 'warn' : 'bad';
  return { label: mmss(c.t), value: c.plan == null ? `${c.used}` : `${c.used}/${c.plan}`, cls };
}

export interface Spark { used: string; plan: string; cap: string; marks: { x: number; label: string }[]; ymax: number; }

/** SVG paths (viewBox w x h) for used, plan and cap over game time; a null value lifts the pen. */
export function spark(series: Review['series'], w = 600, h = 90): Spark | null {
  if (series.length < 2) return null;
  const tmax = Math.max(60, series[series.length - 1][0]);
  const top = Math.max(20, ...series.flatMap(([, u, p]) => [u ?? 0, p ?? 0]));
  const ymax = Math.ceil(top / 20) * 20;
  const x = (t: number) => +(w * t / tmax).toFixed(1);
  const y = (v: number) => +(h - h * Math.min(v, ymax) / ymax).toFixed(1);
  const line = (k: 1 | 2 | 3) => {
    let d = '', pen = false;
    for (const s of series) {
      const v = s[k];
      if (v == null) { pen = false; continue; }
      d += `${d ? ' ' : ''}${pen ? 'L' : 'M'}${x(s[0])},${y(v)}`;
      pen = true;
    }
    return d;
  };
  const marks = [180, 360, 480].filter(t => t < tmax).map(t => ({ x: x(t), label: mmss(t) }));
  return { used: line(1), plan: line(2), cap: line(3), marks, ymax };
}

/** Shared open/close state, so the top bar's "Last game" button can reopen the review. */
@Injectable({ providedIn: 'root' })
export class ReviewService {
  readonly review = signal<Review | null>(null);
  readonly error = signal<string | null>(null);
  readonly loading = signal(false);
  readonly open = signal(false);

  async show(): Promise<void> {
    this.open.set(true);
    this.loading.set(true);
    try {
      const r = await fetch('/history/review/latest', { cache: 'no-store' });
      const body = await r.json();
      if (!r.ok) throw new Error(body?.error || `HTTP ${r.status}`);
      this.review.set(body as Review);
      this.error.set(null);
    } catch (e) {
      this.review.set(null); // never show an older game's review as if it were the last one
      this.error.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loading.set(false);
    }
  }
}

@Component({
  selector: 'app-review',
  styles: `
    dialog{background:var(--panel);color:var(--fg);border:1px solid var(--line);border-top:3px solid var(--ok);
      width:min(760px,calc(100vw - 32px));max-height:88vh;padding:14px 18px}
    dialog::backdrop{background:rgba(0,0,0,.65)}
    header{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
    h1{margin:0;font-size:1.3rem}
    .win{color:var(--ok)} .loss{color:var(--alert)} .other{color:var(--muted)}
    .vs{color:var(--muted);flex:1}
    .fix{margin:12px 0;padding:12px 14px;background:#0a111a;border-left:4px solid var(--ok)}
    .fix p{margin:4px 0 0;font-size:1.7rem;font-weight:700;color:#fff;line-height:1.15}
    .fix .why,.fix .also{font-size:.9rem;color:var(--muted);font-weight:400;margin-top:6px}
    .fix .also b{color:var(--fg)}
    .cps{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
    .cp{font-family:var(--mono);font-size:.9rem;padding:3px 9px;border:1px solid var(--line)}
    .cp small{color:var(--muted);margin-right:6px}
    .cp.ok{border-color:var(--ok)} .cp.warn{border-color:var(--alert)} .cp.bad{border-color:var(--bad);color:var(--bad)}
    svg{display:block;width:100%;height:90px;background:#0a111a}
    svg path{fill:none;stroke-width:2;vector-effect:non-scaling-stroke}
    .u{stroke:var(--terran)} .p{stroke:var(--coach);stroke-dasharray:5 4} .c{stroke:var(--muted);stroke-width:1;opacity:.5}
    .m{stroke:var(--line);stroke-width:1;vector-effect:non-scaling-stroke}
    .key{display:flex;gap:14px;font-size:.78rem;color:var(--muted);margin:4px 0 0}
    .key i{display:inline-block;width:14px;height:2px;margin-right:5px;vertical-align:middle}
    ul{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:3px;font-size:.92rem}
    li{display:grid;grid-template-columns:76px minmax(0,1fr);gap:8px}
    li span{font-family:var(--mono);font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);padding-top:2px}
    li.block span,li.fight span,li.buildings span{color:var(--alert)}
    .msg{color:var(--muted)}
  `,
  template: `
    <dialog #dlg (close)="reviews.open.set(false)" aria-label="Post-game review">
      @if (reviews.review(); as r) {
        <header>
          <h1>#{{ r.id }} <span [class]="cls(r.result)">{{ r.result || '?' }}</span> {{ r.length_text }}</h1>
          <span class="vs">Plan {{ r.plan || '?' }} vs {{ r.enemies || '?' }}</span>
          <button (click)="dlg.close()" autofocus>Close</button>
        </header>
        <div class="fix"><h2>The one fix · {{ r.top_fix.title }}</h2>
          <p>{{ r.top_fix.action }}</p>
          <div class="why">{{ r.top_fix.why }}</div>
          @if (r.top_fix.also; as a) { <div class="also">Also: <b>{{ a.action }}</b></div> }
        </div>
        @if (r.checkpoints.length) {
          <div class="cps">@for (c of r.checkpoints; track c.t) {
            @let v = cp(c);
            <span [class]="'cp ' + v.cls" [title]="'Supply / plan at ' + v.label"><small>{{ v.label }}</small>{{ v.value }}</span>
          }</div>
        }
        @if (sparkOf(r); as s) {
          <svg viewBox="0 0 600 90" preserveAspectRatio="none" role="img"
            [attr.aria-label]="'Supply used vs plan, 0 to ' + s.ymax">
            @for (m of s.marks; track m.x) { <line class="m" [attr.x1]="m.x" [attr.x2]="m.x" y1="0" y2="90"><title>{{ m.label }}</title></line> }
            <path class="c" [attr.d]="s.cap" /><path class="p" [attr.d]="s.plan" /><path class="u" [attr.d]="s.used" />
          </svg>
          <div class="key"><span><i style="background:var(--terran)"></i>Supply</span>
            <span><i style="background:var(--coach)"></i>Plan</span><span><i style="background:var(--muted)"></i>Cap</span>
            <span>lines at 3:00 · 6:00 · 8:00 · top {{ s.ymax }}</span></div>
        }
        <ul>@for (f of r.findings; track $index) { <li [class]="f.kind"><span>{{ f.kind }}</span>{{ f.text }}</li> }
          @empty { <li><span>-</span>Nothing to flag.</li> }</ul>
      } @else {
        <header><h1>Last game</h1><span class="vs"></span><button (click)="dlg.close()">Close</button></header>
        <p class="msg">{{ reviews.loading() ? 'Loading the review...' : 'No review: ' + (reviews.error() || 'no games yet') }}</p>
      }
    </dialog>
  `,
})
export class ReviewPanel {
  protected readonly reviews = inject(ReviewService);
  private readonly live = inject(LiveService);
  private readonly dlg = viewChild.required<ElementRef<HTMLDialogElement>>('dlg');
  private readonly ends = new GameEndWatch();
  protected readonly cls = resultClass;
  protected readonly cp = checkpointView;
  private sparkFor: Review | null = null;
  private sparkCache: Spark | null = null;

  constructor() {
    // Game over -> fetch and open (once per game end; not on a helper restart, see GameEndWatch).
    effect(() => {
      if (this.ends.see(this.live.conn(), this.live.state())) untracked(() => this.reviews.show());
    });
    effect(() => {
      const d = this.dlg().nativeElement;
      if (this.reviews.open() && !d.open) d.showModal();
      else if (!this.reviews.open() && d.open) d.close();
    });
  }

  protected sparkOf(r: Review): Spark | null {
    if (r !== this.sparkFor) { this.sparkFor = r; this.sparkCache = spark(r.series); }
    return this.sparkCache;
  }
}
