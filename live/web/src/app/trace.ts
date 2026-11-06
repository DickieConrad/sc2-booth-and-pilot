// Latency trace: only when the helper runs with SC2_TIMING_TRACE=1 (state.trace). Same records as overlay.html:
// per spoken call / cue, when the page got it (recv), when its audio started (start), clip fetch+decode (dec) and
// the rest (wait: queue behind other speech). Sent along with the next /notes poll; summary: live/looptime.py.

export interface TraceRec { key: string; recv: number; start?: number; dec?: number; wait?: number; how?: string; }

export class LoopTrace {
  enabled = false;
  /** The tag of the call being handed to the voice right now (consumed synchronously by playClips / speak). */
  cur: string | null = null;
  private open: Record<string, TraceRec> = {};
  private done: TraceRec[] = [];

  recv(key: string, now = Date.now()): string | null {
    if (!this.enabled) return null;
    this.open[key] = { key, recv: now };
    return key;
  }

  take(): string | null { const t = this.cur; this.cur = null; return t; }

  finish(tag: string | null | undefined, how: string, dec = 0, now = Date.now()): void {
    const r = tag ? this.open[tag] : undefined;
    if (!r) return;
    delete this.open[tag!];
    r.how = how; r.start = now; r.dec = dec; r.wait = now - r.recv - dec;
    this.done.push(r);
    console.debug('trace', r);
  }

  /** Done records + anything never spoken within 15 s (voice off, booth muted, dropped cue); '' when none. */
  report(now = Date.now()): string {
    for (const k of Object.keys(this.open)) {
      if (now - this.open[k].recv > 15000) { this.done.push({ ...this.open[k], how: 'none' }); delete this.open[k]; }
    }
    return this.done.length ? JSON.stringify(this.done.splice(0, 40)) : '';
  }
}

export const TRACE = new LoopTrace();
