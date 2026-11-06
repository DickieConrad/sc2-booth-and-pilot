// The live dashboard (Angular port of live/overlay.html). Same layout, same ids where the old CSS needs them.
import { Component, ErrorHandler, Injectable, inject } from '@angular/core';
import { LiveService } from './live.service';
import { VoiceService } from './voice.service';
import { CallBox } from './parts/call-box';
import { Checkpoints } from './parts/checkpoints';
import { Lane } from './parts/lane';
import { Pointers } from './parts/pointers';
import { ReviewPanel } from './parts/review';
import { Stats } from './parts/stats';
import { Timeline } from './parts/timeline';
import { TopBar } from './parts/top-bar';
import { NextSteps } from './parts/next-steps';
import { ViewMode, isCompactKey } from './view-mode';

/** Angular errors land on the status chip too (window errors are caught in App). */
@Injectable()
export class PageErrorHandler implements ErrorHandler {
  private readonly live = inject(LiveService);
  handleError(error: unknown): void {
    console.error(error);
    this.live.reportError(error instanceof Error ? error.message : String(error));
  }
}

@Component({
  selector: 'app-root',
  imports: [TopBar, CallBox, Stats, Lane, Pointers, Checkpoints, Timeline, NextSteps, ReviewPanel],
  host: { '(document:keydown)': 'onKey($event)' },
  template: `
    <div class="wrap" [class.compact]="mode.compact()">
      <div class="top" appTopBar></div>
      <div class="hud">
        <section appCallBox></section>
        <div appStats></div>
      </div>
      @if (mode.compact()) { <section appNextSteps></section> }
      <section appLane></section>
      <section appPointers></section>
      <section appCheckpoints></section>
      <div class="tls">
        <section appTimeline></section>
        <section appTimeline reference></section>
      </div>
    </div>
    <app-review />
  `,
})
export class App {
  protected readonly mode = inject(ViewMode);

  // Compact mode only hides parts with CSS: the lane, the lists and the voice keep running underneath.
  protected onKey(e: KeyboardEvent): void {
    if (isCompactKey(e)) { e.preventDefault(); this.mode.toggle(); }
  }

  constructor() {
    const live = inject(LiveService);
    // Show script errors on the page (status chip + title) so a silent failure is visible from the couch.
    window.addEventListener('error', e => live.reportError(`${e.message} (line ${e.lineno})`));
    inject(VoiceService).loadManifest();
    live.start();
  }
}
