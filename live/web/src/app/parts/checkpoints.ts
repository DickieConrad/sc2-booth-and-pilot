// Checkpoints: supply targets each minute (graded against your real supply as the clock passes them), the plan's
// key timings, and the coach's focus list. Grades live in the page: a reload mid-game starts grading from that point.
import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { Chip, keyChips, supplyChips } from '../logic';
import { LiveService } from '../live.service';

interface ChipView extends Chip { cls: string; got: number | null; }

@Component({
  selector: 'section[appCheckpoints]',
  host: { class: 'cps', 'aria-label': 'Checkpoints' },
  template: `
    <div class="cprow"><h2>Supply</h2><div class="chips">@for (ch of supply(); track $index) {
      <span [class]="ch.cls"><span>{{ ch.label }}</span><b>{{ ch.value }}</b>@if (ch.got != null) { <small>you {{ ch.got }}</small> }</span>
    }</div></div>
    <div class="cprow"><h2>Timings</h2><div class="chips">@for (ch of key(); track $index) {
      <span [class]="ch.cls"><span>{{ ch.label }}</span><b>{{ ch.value }}</b></span>
    }</div></div>
    <div class="cprow"><h2>Focus</h2><ul class="focus">@for (f of base()?.focus; track $index) { <li>{{ f }}</li> }</ul></div>
  `,
})
export class Checkpoints {
  private readonly live = inject(LiveService);
  private readonly grades = signal<Record<number, number>>({});
  private readonly clock = signal<number | null>(null);
  private cpGame = '';

  // Built once per plan (like the old page): chips + focus list.
  protected readonly base = computed(() => {
    const plans = this.live.plans();
    const id = this.live.mainPlan(), p = plans?.plans[id];
    if (!p) return null;
    return { id, sup: supplyChips(p.steps), key: keyChips(p.steps), focus: plans!.focus || [] };
  }, { equal: (a, b) => a?.id === b?.id });

  protected readonly supply = computed<ChipView[]>(() => {
    const b = this.base(), g = this.grades(), c = this.clock();
    if (!b) return [];
    let next = false;
    return b.sup.map(ch => {
      if (ch.sec == null) return { ...ch, cls: 'cp', got: null }; // SCV-count checks: shown, not graded
      const got = g[ch.sec] ?? null;
      let cls = 'cp' + (got != null ? (got >= ch.want! - 2 ? ' hit' : ' miss') : '');
      if (c != null && c < ch.sec && !next) { next = true; cls += ' next'; }
      return { ...ch, cls, got };
    });
  });

  protected readonly key = computed<ChipView[]>(() => {
    const b = this.base(), c = this.clock();
    if (!b) return [];
    let next = false;
    return b.key.map(ch => {
      let cls = 'cp' + (c != null && c > ch.sec! + 10 ? ' past' : '');
      if (c != null && c <= ch.sec! + 10 && !next) { next = true; cls += ' next'; }
      return { ...ch, cls, got: null };
    });
  });

  constructor() {
    effect(() => { this.live.tick(); untracked(() => this.grade()); });
  }

  // Grade supply targets as the clock passes them.
  private grade(): void {
    const s = this.live.state(), b = this.base();
    if (!s || !b) return;
    const game = s.in_game ? `${s.plan}|${s.vs}` : '';
    let g = this.grades();
    if (game !== this.cpGame) { this.cpGame = game; if (game) g = {}; }
    const c = this.live.gameClock();
    const next = { ...g };
    for (const ch of b.sup) {
      if (ch.sec != null && c != null && c >= ch.sec && next[ch.sec] == null && s.panel) next[ch.sec] = s.panel.used;
    }
    if (Object.keys(next).length !== Object.keys(this.grades()).length || g !== this.grades()) this.grades.set(next);
    this.clock.set(c);
  }
}
