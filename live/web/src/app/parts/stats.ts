// Minerals / gas / supply tiles, the gas balance gauge under gas and the depot gauge under supply.
import { Component, computed, inject } from '@angular/core';
import { GasGauge } from '../gas';
import { mineralsClass, supplyClass } from '../logic';
import { LiveService } from '../live.service';

@Component({
  selector: 'div[appStats]',
  host: { class: 'stats' },
  styles: `
    .room.off i { background: var(--muted); }
    .gastxt { font-size: .75rem; }
  `,
  template: `
    <div [class]="minCls()"><small>Minerals</small><b>{{ p()?.minerals ?? '-' }}</b></div>
    <div class="stat"><small>Gas</small><b>{{ p()?.gas ?? '-' }}</b>
      <div [class]="gas().cls" title="Gas bank: amber = gas heavy, red = floating gas while mineral-starved, move SCVs from gas to minerals"><i [style.width]="gas().width"></i></div>
      <span class="gastxt">{{ gas().text }}</span></div>
    <div [class]="supCls()"><small>Supply</small><b>{{ p() ? p()!.used + '/' + p()!.cap : '-' }}</b><span>{{ planSup() }}</span>
      <div [class]="live.gauge().cls" title="Supply room left: green = safe, amber = build a depot, red = blocked"><i [style.width]="live.gauge().width"></i></div>
      <span id="roomtxt">{{ live.gauge().text }}</span></div>
  `,
})
export class Stats {
  protected readonly live = inject(LiveService);
  private readonly gasGauge = new GasGauge();
  // never show a stale read between games
  protected readonly p = computed(() => {
    const s = this.live.state();
    return this.live.ready() && s!.in_game ? s!.panel : null;
  });
  protected readonly planSup = computed(() => {
    const s = this.live.state();
    return this.p() && s!.plan_supply != null ? `plan ${s!.plan_supply}` : '';
  });
  protected readonly minCls = computed(() => mineralsClass(this.p(), this.live.state()?.clock ?? 0));
  protected readonly supCls = computed(() => supplyClass(this.p(), this.live.state()?.plan_supply ?? null));
  // Runs once per helper update (state is a new object each time), like the depot gauge in LiveService.render.
  protected readonly gas = computed(() => this.gasGauge.update(this.p(), this.live.state()?.clock ?? 0));
}
