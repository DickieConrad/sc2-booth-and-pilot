// Compact mode: the next three steps of the plan you're playing, big. Due (within 5 s) glows like the lane.
import { Component, computed, inject } from '@angular/core';
import { nextSteps } from '../logic';
import { LiveService } from '../live.service';

@Component({
  selector: 'section[appNextSteps]',
  host: { class: 'nextsteps', 'aria-label': 'Next steps' },
  template: `
    <h2>Next</h2>
    <ol>@for (s of steps(); track s.sec + s.text) {
      <li [class.due]="s.due"><span class="tm">{{ s.t }}</span><span class="sp">{{ s.supply ?? '' }}</span><span class="ac">{{ s.text }}</span></li>
    } @empty { <li class="idle">Waiting for a game</li> }</ol>
  `,
})
export class NextSteps {
  private readonly live = inject(LiveService);
  protected readonly steps = computed(() => {
    this.live.tick(); // re-evaluate every 250 ms with the clock
    if (!this.live.ready()) return [];
    const p = this.live.plans()!.plans[this.live.mainPlan()];
    return p ? nextSteps(p.steps, this.live.gameClock()) : [];
  });
}
