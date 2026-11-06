// Helper state: /plans once, /state + /events (SSE) for every change, the game clock, the 250 ms page tick,
// and the per-update side effects (new call -> voice, engine cues, depot gauge). Ported from overlay.html.
import { Injectable, computed, inject, signal } from '@angular/core';
import { CueScheduler, DepotGauge, GaugeView, mmss } from './logic';
import { TRACE } from './trace';
import { Plans, Say, State } from './types';
import { VoiceService } from './voice.service';

@Injectable({ providedIn: 'root' })
export class LiveService {
  private readonly voice = inject(VoiceService);

  readonly plans = signal<Plans | null>(null);
  readonly state = signal<State | null>(null);
  private offset = 0;
  private needPlans = true;
  private missingPlan: string | null = null;

  /** Status chip; a page error overrides it. */
  readonly status = signal<{ text: string; cls: string }>({ text: 'connecting', cls: 'status' });
  readonly pageError = signal<string | null>(null);

  /** Bumped every 250 ms (clock, build lists, checkpoints, fallback cues). */
  readonly tick = signal(0);
  readonly clockText = signal('-:--');
  /** Bumped when a game starts: the build lists follow NOW again. */
  readonly gameStart = signal(0);
  /** Bumped each time the /events stream (re)connects: the helper may have restarted (its state starts fresh). */
  readonly conn = signal(0);
  /** null = the default reference (see defaultRef). */
  readonly refPlan = signal<string | null>(null);

  // The big call box: the latest call; freshSeq bumps for each new one after the first (pulse + voice).
  readonly lastCall = signal<Say | null>(null);
  readonly freshSeq = signal(0);
  readonly gauge = signal<GaugeView>({ cls: 'room', width: '0', text: '' });

  private lastSay: number | null = null;
  private lastCue: number | null = null;
  private readonly depot = new DepotGauge();
  private readonly cues = new CueScheduler();

  // The plan the Live Coach page shows for this matchup (the helper sends it; older helpers: derive from the matchup).
  readonly matchPlan = computed(() => {
    const s = this.state();
    if (!s) return 'A';
    if (s.coach_plan) return s.coach_plan;
    const picks = this.plans()?.matchups[s.key];
    return picks ? picks[0] : 'A';
  });
  readonly mainPlan = computed(() => this.state()?.plan || this.matchPlan());
  // Reference list: the Live Coach matchup plan, or MMM when you're already playing the matchup plan.
  readonly defaultRef = computed(() => (this.mainPlan() === this.matchPlan() ? 'M' : this.matchPlan()));
  readonly shownRef = computed(() => this.refPlan() || this.defaultRef());
  /** Only after plans have loaded (the old page rendered nothing before that). */
  readonly ready = computed(() => !!this.plans() && !!this.state());

  gameClock(): number | null {
    const s = this.state();
    if (!s || !s.in_game) return null;
    return s.clock + Math.min(1.5, Date.now() / 1000 - this.offset - s.clock_wall);
  }

  start(): void {
    this.listen();
    this.poll();
    setInterval(() => this.everyQuarter(), 250);
  }

  private everyQuarter(): void {
    const c = this.gameClock();
    this.clockText.set(c == null ? '-:--' : mmss(c));
    const plans = this.plans(), s = this.state();
    if (plans && s) { this.tick.update(n => n + 1); this.cueTick(c, plans, s); }
  }

  private cueTick(c: number | null, plans: Plans, s: State): void {
    if (s.cue_engine) return; // the engine is calling cues; this is the fallback
    if (c == null || !s.plan) return;
    const p = plans.plans[s.plan];
    if (!p) return;
    const due = this.cues.due(c, `${s.plan}|${s.vs}`, p.steps);
    if (due.length) this.voice.speakCues(due);
  }

  apply(next: State): void {
    const prev = this.state(), wasIn = prev?.in_game;
    this.offset = Date.now() / 1000 - next.now;
    TRACE.enabled = !!next.trace;
    this.state.set(next);
    // A new game: in_game false -> true, or the clock going back while in game (the helper missed the last end).
    if (next.in_game && (!wasIn || next.clock < prev!.clock - 5)) this.cues.reset();
    if (next.in_game && !wasIn) { this.refPlan.set(null); this.gameStart.update(n => n + 1); }
    if (wasIn && !next.in_game) this.voice.gameOver(); // nothing queued or loading may speak after the game
    if (!this.pageError()) this.status.set({ text: next.in_game ? 'LIVE' : 'IDLE', cls: 'status on' });
    // A plan this page hasn't loaded (a sparring bot's build, plan BOT, written at game start): fetch /plans again,
    // once per plan id. The page keeps rendering meanwhile (every view skips a plan it doesn't have).
    const plans = this.plans();
    if (next.plan && plans && !plans.plans[next.plan] && this.missingPlan !== next.plan) {
      this.missingPlan = next.plan;
      this.needPlans = true;
      void this.poll();
    }
    if (this.plans()) this.render(next);
  }

  private render(s: State): void {
    const p = s.in_game ? s.panel : null; // never show a stale read between games
    this.gauge.set(this.depot.update(p, s.clock));

    const last = s.says[s.says.length - 1];
    if (last && last.id !== this.lastSay) {
      const first = this.lastSay === null;
      this.lastSay = last.id;
      this.lastCall.set(last);
      if (!first) {
        this.freshSeq.update(n => n + 1);
        TRACE.cur = TRACE.recv('s' + last.id); this.voice.speakCall(last); TRACE.cur = null;
      }
    } else if (!last && this.lastSay === null) {
      this.lastSay = 0;
    }
    // Engine cues: play the new ones. Not the backlog on page load or after a reconnect; ids going backwards = a
    // restarted helper (cue_seq starts again at 0): take the new ids from there. None at all once the game is over.
    const cs = s.cues || [], newest = cs.length ? cs[cs.length - 1].id : 0;
    if (this.lastCue === null || newest < this.lastCue || !s.in_game) this.lastCue = newest;
    else for (const ev of cs.filter(e => e.id > this.lastCue!)) {
      TRACE.cur = TRACE.recv('c' + ev.id); this.voice.playCueEvent(ev); TRACE.cur = null;
    }
    this.lastCue = Math.max(this.lastCue, newest);
  }

  private offline(): void { this.status.set({ text: 'helper not running', cls: 'status off' }); }

  private async poll(): Promise<void> {
    try {
      if (this.needPlans || !this.plans()) {
        this.plans.set(await (await fetch('/plans', { cache: 'no-store' })).json());
        this.needPlans = false;
      }
      this.apply(await (await fetch('/state', { cache: 'no-store' })).json());
    } catch { this.offline(); }
  }

  // Live updates: the helper pushes every change (Server-Sent Events); the browser reconnects by itself.
  // After a reconnect, re-fetch plans in case the helper restarted with new ones (the old ones stay on screen meanwhile).
  private listen(): void {
    const es = new EventSource('/events');
    es.onopen = () => {
      this.conn.update(n => n + 1);
      this.lastCue = null; // the helper may have restarted: its cue ids start again
      this.needPlans = true; this.poll();
    };
    es.onmessage = e => { try { this.apply(JSON.parse(e.data)); } catch { /* bad frame */ } };
    es.onerror = () => this.offline();
  }

  // Show script errors on the page (status chip + title) so a silent failure is visible from the couch.
  reportError(msg: string): void {
    this.pageError.set(msg);
    this.status.set({ text: 'page error: ' + msg, cls: 'status off' });
    document.title = `ERR ${msg}`;
  }
}
