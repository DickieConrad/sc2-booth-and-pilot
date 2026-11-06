// One build list: "Your build" (the plan you're playing, calls inline) or "Reference" (plan tabs). Marks passed /
// current / due steps every 250 ms and keeps the current step in view until you scroll it yourself.
import {
  Component, ElementRef, Injector, afterNextRender, afterRenderEffect, booleanAttribute, computed, effect, inject,
  input, signal, untracked, viewChild,
} from '@angular/core';
import { GRACE, Row, followScrollTop, timelineRows } from '../logic';
import { LiveService } from '../live.service';
import { ViewMode } from '../view-mode';
import { Plan } from '../types';

@Component({
  selector: 'section[appTimeline]',
  host: { class: 'tl', '[class.main]': '!reference()', '[class.ref]': 'reference()',
    '[attr.aria-label]': 'reference() ? "Live Coach plan" : "Your build"' },
  template: `
    <div class="tlhead"><h2>{{ reference() ? 'Reference' : 'Your build' }}</h2>
      @if (reference()) {
        <div class="tabs" role="group" aria-label="Plan">@for (t of tabs(); track t.id) {
          <button [attr.aria-pressed]="t.pressed" [class.match]="t.match" [title]="t.title" (click)="pick(t.id)">{{ t.id }}</button>
        }</div>
      }
      <span class="grow"></span><button class="followbtn" [hidden]="follow()" (click)="backToNow()">Back to NOW</button></div>
    <div class="tlsub">@if (view(); as v) { <b>{{ v.id }} · {{ v.plan.name }}</b> — {{ v.plan.title }} · hit {{ v.plan.hit }} }</div>
    <ul class="tlist" #list>
      @for (r of view()?.rows; track r.kind + r.sec + (r.call?.id ?? r.action)) {
        @if (r.call; as c) {
          <li class="callrow" [class.coach]="c.from === 'coach'"><span class="tm">{{ r.t }}</span>
            <span class="sp">{{ c.from === 'coach' ? 'coach' : c.kind }}</span><span class="ac">{{ c.text }}</span></li>
        } @else {
          <li [attr.data-sec]="r.sec"><span class="tm">{{ r.t }}</span><span class="sp">{{ r.supply ?? '' }}</span>
            <span class="ac">{{ r.action }}</span>@if (r.note) { <span class="nt">{{ r.note }}</span> }</li>
        }
      }
    </ul>
  `,
})
export class Timeline {
  readonly reference = input(false, { transform: booleanAttribute });
  private readonly live = inject(LiveService);
  private readonly injector = inject(Injector);
  private readonly list = viewChild.required<ElementRef<HTMLUListElement>>('list');
  protected readonly follow = signal(true);

  protected readonly view = computed<{ sig: string; id: string; plan: Plan; rows: Row[] } | null>(() => {
    if (!this.live.ready()) return null;
    const id = this.reference() ? this.live.shownRef() : this.live.mainPlan();
    const plan = this.live.plans()!.plans[id];
    if (!plan) return null;
    const calls = this.reference() ? [] : this.live.state()!.says;
    return { sig: [id, calls.map(c => c.id).join(',')].join('|'), id, plan, rows: timelineRows(plan.steps, calls) };
  }, { equal: (a, b) => a?.sig === b?.sig });

  protected readonly tabs = computed(() => {
    const plans = this.live.plans();
    if (!plans) return [];
    const shown = this.live.shownRef(), match = this.live.matchPlan();
    return Object.keys(plans.plans).map(id => ({
      id, pressed: id === shown, match: id === match,
      title: plans.plans[id].name + (id === match ? ' (Live Coach pick for this matchup)' : ''),
    }));
  });

  constructor() {
    afterRenderEffect(() => { this.view(); untracked(() => this.mark(true)); }); // re-rendered: jump to NOW
    effect(() => { this.live.tick(); untracked(() => this.mark(false)); });
    let started = 0;
    effect(() => {
      const n = this.live.gameStart();
      if (n === started) return;
      started = n;
      untracked(() => { this.follow.set(true); if (this.reference()) this.jumpAfterRender(); });
    });
    // Hidden in compact mode (no layout, so no scrolling): back to NOW when the full view returns.
    const mode = inject(ViewMode);
    effect(() => { if (!mode.compact()) untracked(() => this.jumpAfterRender()); });
    afterNextRender(() => {
      for (const ev of ['wheel', 'touchstart', 'mousedown', 'keydown']) {
        this.list().nativeElement.addEventListener(ev, () => {
          if (this.live.state()?.in_game) this.follow.set(false);
        }, { passive: true });
      }
    });
  }

  protected backToNow(): void { this.follow.set(true); this.mark(true); }

  protected pick(id: string): void {
    this.live.refPlan.set(id === this.live.defaultRef() ? null : id);
    this.jumpAfterRender();
  }

  private jumpAfterRender(): void { afterNextRender(() => this.mark(true), { injector: this.injector }); }

  // Mark passed / current / due steps and keep the current step in view.
  private mark(jump: boolean): void {
    const list = this.list().nativeElement;
    const c = this.live.gameClock();
    const items = Array.from(list.querySelectorAll<HTMLElement>('li[data-sec]'));
    let current: HTMLElement | null = null;
    for (const li of items) {
      const sec = Number(li.dataset['sec']);
      const passed = c != null && sec <= c - GRACE;
      li.classList.toggle('passed', passed);
      li.classList.toggle('due', c != null && !passed && sec - c <= 5);
      li.classList.remove('now');
      if (c != null && !passed && !current) current = li;
    }
    if (!current) return;
    current.classList.add('now');
    if (this.follow() && (jump || current.dataset['shown'] !== '1')) {
      items.forEach(li => delete li.dataset['shown']); current.dataset['shown'] = '1';
      const top = followScrollTop(current.getBoundingClientRect().top, list.getBoundingClientRect().top, list.clientTop,
        list.scrollTop, list.clientHeight);
      list.scrollTo({ top, behavior: jump ? 'auto' : 'smooth' });
    }
  }
}
