// Top bar: clock, plan + matchup, voice / step cues / booth toggles, release notes, Last game review, History link,
// status chip.
import { Component, ElementRef, computed, inject, signal, viewChild } from '@angular/core';
import { NoteSection, parseChangelog } from '../logic';
import { LiveService } from '../live.service';
import { VoiceService } from '../voice.service';
import { ReviewService } from './review';
import { ViewMode } from '../view-mode';

@Component({
  selector: 'div[appTopBar]',
  template: `
    <dialog class="relnotes" #rel><button (click)="rel.close()" style="float:right">Close</button>
      <div>@for (b of notes(); track $index) {
        @if (b.title) { <h3>{{ b.title }}</h3> }
        @if (b.items.length) { <ul>@for (i of b.items; track $index) { <li>{{ i }}</li> }</ul> }
      }</div></dialog>
    <div class="clock">{{ live.clockText() }}</div>
    <div>
      <div class="plan">@if (plan(); as p) { <b>{{ p.label }}</b>{{ p.name }} } @else { Waiting for a game }</div>
      <div class="vs">{{ vs() }}</div>
    </div>
    <button class="keep" [attr.aria-pressed]="voice.voice()" [class.arm]="voice.arming()" (click)="voice.clickVoice()"
      title="Browsers need one click before a page may speak">{{ voice.voiceLabel() }}</button>
    <button class="keep" [attr.aria-pressed]="mode.compact()" (click)="mode.toggle()"
      title="Compact in-game view: call, clock, tiles, next steps (key: C)">Compact</button>
    <button [attr.aria-pressed]="voice.cueOn()" (click)="voice.setCues(!voice.cueOn())"
      title="Speak your next build step 5 seconds before it is due">Step cues</button>
    <button [attr.aria-pressed]="voice.boothOn()" (click)="voice.setBooth(!voice.boothOn())"
      title="The coach's own voice (Claude in the booth); his lines still show on the page">Booth</button>
    <button (click)="loadNotes(true)" title="What changed, newest first">{{ version() ? version() + ' notes' : 'Release notes' }}</button>
    <button (click)="reviews.show()" title="Review of the last game: the one fix, supply vs plan, findings">Last game</button>
    <a href="/history" target="_blank" rel="noopener" title="Previous games: results, supply, calls, notes (new tab)"
      style="font-weight:700;border:1px solid var(--line);color:var(--fg);padding:6px 12px;text-decoration:none">History</a>
    <span [class]="live.status().cls">{{ live.status().text }}</span>
  `,
})
export class TopBar {
  protected readonly live = inject(LiveService);
  protected readonly voice = inject(VoiceService);
  protected readonly reviews = inject(ReviewService);
  protected readonly mode = inject(ViewMode);
  private readonly rel = viewChild.required<ElementRef<HTMLDialogElement>>('rel');
  protected readonly notes = signal<NoteSection[]>([]);
  protected readonly version = signal<string | null>(null);

  protected readonly plan = computed(() => {
    const s = this.live.state();
    if (!this.live.ready() || !s?.plan) return null;
    return { label: s.plan === 'M' ? 'MMM' : `Plan ${s.plan}`, name: s.plan === 'M' ? '' : s.plan_name };
  });
  protected readonly vs = computed(() => {
    const s = this.live.state();
    if (!this.live.ready() || !s?.plan) return '';
    if (s.in_game && s.cue_engine && (s.lateness ?? 0) >= 1) {
      return `vs ${s.vs} · late +${Math.round(s.lateness!)}s` + (s.shift ? ` · cues +${s.shift}s` : '');
    }
    return (s.in_game ? 'vs ' : 'Last game: vs ') + s.vs;
  });

  constructor() { this.loadNotes(false); }

  // Release notes (live/CHANGELOG.md): "## " lines are versions, "- " lines are changes. The top version labels the button.
  async loadNotes(open: boolean): Promise<void> {
    try {
      const md = await (await fetch('/changelog', { cache: 'no-store' })).text();
      const { version, blocks } = parseChangelog(md);
      this.notes.set(blocks);
      if (version) this.version.set(version);
      if (open) this.rel().nativeElement.showModal();
    } catch { /* helper down: keep the button */ }
  }
}
