// Step lane: steps slide right-to-left into the NOW line, driven by the smoothed clock every animation frame.
// Angular renders the notes / ticks / call marks when the plan or the calls change; the frame loop only moves them.
import { Component, DestroyRef, ElementRef, afterNextRender, computed, inject, viewChild } from '@angular/core';
import { ClockSmoother, GRACE, LaneItem, laneItems, laneX } from '../logic';
import { LiveService } from '../live.service';

@Component({
  selector: 'section[appLane]',
  host: { class: 'lanebox', 'aria-label': 'Step lane' },
  template: `
    <div class="lane" #box><div class="hitline"></div>
      @for (it of lane()?.items; track it.key) {
        @switch (it.kind) {
          @case ('tick') { <div class="tick" style="display:none" [attr.data-sec]="it.sec">@if (it.text) { <span>{{ it.text }}</span> }</div> }
          @case ('note') { <div class="note" style="display:none" [attr.data-sec]="it.sec" [style.top.px]="it.top">{{ it.text }}<small>{{ it.sub }}</small></div> }
          @default { <div class="callmark" style="display:none" [class.coach]="it.coach" [attr.data-sec]="it.sec" [title]="it.title">{{ it.text }}</div> }
        }
      }
    </div>
  `,
})
export class Lane {
  private readonly live = inject(LiveService);
  private readonly box = viewChild.required<ElementRef<HTMLElement>>('box');
  private readonly smoother = new ClockSmoother();

  protected readonly lane = computed<{ sig: string; items: LaneItem[] } | null>(() => {
    if (!this.live.ready()) return null;
    const id = this.live.mainPlan(), p = this.live.plans()!.plans[id];
    if (!p) return null;
    const calls = this.live.state()!.says || [];
    return { sig: [id, calls.map(c => c.id).join(',')].join('|'), items: laneItems(p.steps, calls) };
  }, { equal: (a, b) => a?.sig === b?.sig });

  constructor() {
    let raf = 0;
    const frame = () => {
      if (this.live.ready()) this.laneTick(this.smoother.step(this.live.gameClock(), performance.now()));
      raf = requestAnimationFrame(frame);
    };
    afterNextRender(() => frame());
    inject(DestroyRef).onDestroy(() => cancelAnimationFrame(raf));
  }

  private laneTick(c: number | null): void {
    if (!this.lane()) return;
    const lane = this.box().nativeElement, w = lane.clientWidth;
    for (const n of Array.from(lane.querySelectorAll<HTMLElement>('[data-sec]'))) {
      const sec = Number(n.dataset['sec']), { x, visible } = laneX(sec, c, w);
      n.style.display = visible ? '' : 'none';
      if (!visible) continue;
      n.style.transform = `translate3d(${x.toFixed(1)}px,0,0)`;
      if (n.classList.contains('note')) {
        n.classList.toggle('due', c != null && sec - c <= 5 && sec - c > -GRACE);
        n.classList.toggle('done', c != null && sec <= c - GRACE);
      }
    }
  }
}
