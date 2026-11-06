// Compact in-game mode: only the call, clock + plan, the three tiles with their gauges, and the next steps,
// big enough to read at a glance from the second screen. Toggled with the top-bar button or the `c` key,
// remembered in localStorage; `?compact=1` / `?compact=0` in the URL wins on load (not saved).
// Layout only: every part (and the voice, which lives in LiveService / VoiceService) keeps running.
import { Injectable, signal } from '@angular/core';
import { store } from './logic';

export const COMPACT_KEY = 'compact';

/** The mode on load: the URL param if it says 1/0, else the saved choice, else full. */
export function initialCompact(search: string, saved: string | null): boolean {
  const q = new URLSearchParams(search).get('compact');
  if (q === '1' || q === 'true') return true;
  if (q === '0' || q === 'false') return false;
  return saved === '1';
}

/** Only a bare `c` toggles, and never while typing in a field. */
export function isCompactKey(e: Pick<KeyboardEvent, 'key' | 'ctrlKey' | 'metaKey' | 'altKey' | 'repeat' | 'target'>): boolean {
  if (e.key !== 'c' && e.key !== 'C') return false;
  if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return false;
  const t = e.target as HTMLElement | null;
  if (!t || typeof t !== 'object') return true;
  if (t.isContentEditable) return false;
  return !/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName ?? '');
}

@Injectable({ providedIn: 'root' })
export class ViewMode {
  readonly compact = signal(initialCompact(typeof location === 'undefined' ? '' : location.search, store.get(COMPACT_KEY)));

  setCompact(on: boolean): void { this.compact.set(on); store.set(COMPACT_KEY, on ? '1' : '0'); }
  toggle(): void { this.setCompact(!this.compact()); }
}
