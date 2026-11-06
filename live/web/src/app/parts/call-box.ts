// The latest call (big, pulses when new) and the six before it, newest first.
import { Component, ElementRef, computed, effect, inject, untracked } from '@angular/core';
import { LiveService } from '../live.service';

@Component({
  selector: 'section[appCallBox]',
  host: { class: 'call', 'aria-live': 'polite', '[class.coach]': 'last()?.from === "coach"' },
  template: `
    <p>{{ last()?.text ?? 'No calls yet.' }}</p>
    <small>{{ meta() }}</small>
    <ol class="feed" aria-label="Earlier calls">@for (c of feed(); track c.id) {
      <li [class.coach]="c.from === 'coach'"><span>{{ c.t }}</span><span>{{ c.text }}</span></li>
    }</ol>
  `,
})
export class CallBox {
  private readonly live = inject(LiveService);
  private readonly el = inject<ElementRef<HTMLElement>>(ElementRef);
  protected readonly last = this.live.lastCall;
  protected readonly meta = computed(() => {
    const l = this.last();
    return l ? `${l.t} · ${l.from === 'coach' ? 'coach' : l.kind}` : '';
  });
  // Feed: the calls before the latest one, newest first.
  protected readonly feed = computed(() => (this.live.ready() ? this.live.state()!.says.slice(-7, -1).reverse() : []));

  private seenSeq = 0;

  constructor() {
    // Each new call resets the box; a call that arrived live (not the one shown on page load) pulses again.
    effect(() => {
      const seq = this.live.freshSeq();
      this.last();
      untracked(() => {
        const n = this.el.nativeElement;
        n.classList.remove('fresh');
        if (seq !== this.seenSeq) { this.seenSeq = seq; void n.offsetWidth; n.classList.add('fresh'); }
      });
    });
  }
}
