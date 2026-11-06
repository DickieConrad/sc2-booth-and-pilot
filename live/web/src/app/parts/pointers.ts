// Coach pointers: post-game notes from the database, newest first; refreshed every 3 s (review notes, plus
// callouts and coach lines during games). Only the speaker's name is colored (Dickie blue, Coach purple).
import { Component, DestroyRef, inject, signal } from '@angular/core';
import { Pointer, pointer, pointerFilter } from '../logic';
import { TRACE } from '../trace';
import { Note } from '../types';

@Component({
  selector: 'section[appPointers]',
  host: { class: 'pointers', 'aria-label': 'Coach pointers' },
  template: `
    <h2>Coach pointers</h2>
    <ol>
      @for (r of rows(); track r.id) {
        <li><span [class]="'g ' + r.result">{{ r.label }}</span><span class="t">@if (r.who) {
          <b [class]="'who ' + r.who">{{ r.who === 'coach' ? 'Coach: ' : 'Dickie: ' }}</b>}{{ r.text }}</span></li>
      } @empty {
        <li class="idle"><span>No pointers yet.</span></li>
      }
    </ol>
  `,
})
export class Pointers {
  protected readonly rows = signal<Pointer[]>([]);
  private sig = '';

  constructor() {
    this.poll();
    const id = setInterval(() => this.poll(), 3000); // callouts and coach lines land here mid-game
    inject(DestroyRef).onDestroy(() => clearInterval(id));
  }

  private async poll(): Promise<void> {
    try {
      const rep = TRACE.report(); // tracing on: the page's delays ride along on this poll (no extra request)
      const url = rep ? '/notes?trace=' + encodeURIComponent(rep) : '/notes';
      const rows = ((await (await fetch(url, { cache: 'no-store' })).json()) as Note[]).filter(pointerFilter);
      const sig = rows.map(r => r.id).join(',');
      if (sig === this.sig) return;
      this.sig = sig;
      this.rows.set(rows.map(pointer));
    } catch { /* helper down: keep what is shown */ }
  }
}
