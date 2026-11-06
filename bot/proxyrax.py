"""ProxyRaxBot: proxy 2-Barracks Reaper/Marine (Terran). Depot at home, two SCVs leave at 0:16 and build two
Barracks near the enemy natural (toward the map centre); one gas at home for the Reaper. The first Barracks makes
a Reaper (hunts workers), then both make Marines that gather at the proxy and attack in waves of 4 (or 2+ at 2:30).
From 4:00 one more Barracks at home (more from a bank, from 2:30). The army stages at the proxy while a proxy
Barracks stands, so waves leave from near the enemy natural. Timings: config.ProxyRaxConfig.
"Cheese now" = send the proxy SCVs now (if not yet), stop SCVs, attack with everything.

Pivot to macro (once, core.pivot_reasons): from pivot_time (4:00) at the first sign the cheese has stalled (a bank
held, the proxy gone, a wave pulled back or held), or on "pivot" steering, the bot plays the 2-base bio game with
MMM's pieces (bio.BioBot): Command Center at the natural (SCV sent ahead), Orbitals and MULEs, SCVs to 40, gas to 3,
Factory -> Starport (Medivacs), Tech Labs on 2 home Barracks (Stim -> Combat Shield -> Concussive, Marauders),
Reactors on the rest, Barracks up to 8 from the bank, waves of 16 from home (core.pivot_config)."""

from __future__ import annotations

from bio import GRID, BioBot
from config import ProxyRaxConfig
from core import Step, gas_target, held_since, pivot_config, pivot_reasons, staging_point, wave_size
from loguru import logger
from sc2.data import Race
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units

BIO = frozenset({U.MARINE, U.MARAUDER, U.MEDIVAC})
NATURAL_WAIT = 45.0  # after the pivot, units and buildings wait this long at most for the natural's 400 minerals


class ProxyRaxBot(BioBot):
    NAME = "ProxyRax"
    RACE = Race.Terran
    ARMY = frozenset({U.MARINE})
    RAIDERS = frozenset({U.REAPER})
    PRODUCTION = frozenset({U.BARRACKS})
    PROXY_RAX = 2

    def __init__(self, cfg: ProxyRaxConfig | None = None, **kw) -> None:
        super().__init__(cfg or ProxyRaxConfig(), **kw)
        self.proxy: Point2 | None = None
        self.proxy_tags: list[int] = []
        self.pivoted = False
        self._pivot_at = 0.0
        self._pivot_asked = False  # "pivot" steering
        self._bank_since: float | None = None  # since when the bank has stayed at pivot_bank+
        self._pivot_log = -99.0

    async def on_start(self) -> None:
        await super().on_start()
        self.proxy = self.proxy_spot()

    def proxy_spot(self) -> Point2:
        starts = self.enemy_starts() or list(self.enemy_start_locations)
        enemy_main = starts[0]
        exps = sorted(self.expansion_locations_list, key=lambda p: p.distance_to(enemy_main))
        natural = exps[1] if len(exps) > 1 else enemy_main
        return natural.towards(self.game_info.map_center, self.cfg.proxy_distance)

    def on_cheese(self) -> None:
        super().on_cheese()
        self.send_proxy_scvs()

    def on_pivot(self) -> None:
        self._pivot_asked = True  # macro() pivots on its next step

    def proxy_alive(self) -> bool:
        """A Barracks (done or building) still stands at the proxy spot."""
        if self.proxy is None:
            return False
        rax = self.structures(U.BARRACKS)
        return bool(rax) and rax.closest_distance_to(self.proxy) <= self.cfg.proxy_alive_radius

    def stage_point(self) -> Point2:
        """The army waits and new Marines (home Barracks too) gather at the proxy while it stands, so waves leave
        from near the enemy natural instead of walking home first (core.staging_point). After the pivot: home."""
        if self.proxy is None or self.pivoted:
            return super().stage_point()
        if wave_size(self.cfg, self.time) > self.cfg.wave_size:   # late waves of 10 gather at home: waiting by the
            return self.rally_point()                              # enemy natural they were picked off (demo 11)
        target = self._target if self._target is not None else self.attack_target()
        spot = staging_point(self.rally_point(), target, self.proxy, self.proxy_alive(), self.cfg.stage_forward)
        return Point2(spot)

    # ------------------------------------------------------------------ proxy SCVs
    def send_proxy_scvs(self) -> None:
        if self.pivoted or self.proxy is None or self.count_of(U.BARRACKS) >= self.PROXY_RAX:
            return
        if not self.all_in and self.time < self.cfg.proxy_scv_time:
            return
        alive = [t for t in self.proxy_tags if self.workers.find_by_tag(t)]
        self.proxy_tags = alive
        # pick distinct SCVs ourselves: select_build_worker returns the same gathering SCV again within one step
        # (its move isn't in the observation yet), so only one SCV ever left
        free = self.workers.tags_not_in(self.proxy_tags).filter(lambda w: w.is_gathering or w.is_idle)
        for w in free.sorted_by_distance_to(self.proxy)[: self.PROXY_RAX - len(self.proxy_tags)]:
            self.proxy_tags.append(w.tag)
            w.move(self.proxy)

    def not_distributed(self) -> set[int]:
        """The proxy SCVs: distribute_workers would send them home to mine while they wait at the proxy. After the
        pivot: the SCV walking to the natural."""
        if self.pivoted:
            return {self._expander} if self._expander is not None else set()
        return set(self.proxy_tags) if self.count_of(U.BARRACKS) < self.PROXY_RAX else set()

    def proxy_builder(self) -> Unit | None:
        for t in self.proxy_tags:
            w = self.workers.find_by_tag(t)
            if w is not None and not w.is_constructing_scv:
                return w
        return None

    def is_proxy(self, t: U) -> bool:
        return not self.pivoted and t == U.BARRACKS and self.count_of(U.BARRACKS) < self.PROXY_RAX

    async def place(self, t: U, step: Step | None = None) -> bool:
        if self.pivoted:
            return await self.place_macro(t, step)
        if not self.is_proxy(t) or self.proxy is None:
            return await super().place(t, step)
        self.send_proxy_scvs()
        w = self.proxy_builder()
        if w is None:
            return False
        if w.distance_to(self.proxy) > 6:  # build only once the spot is in sight
            if w.is_idle or w.is_gathering:
                w.move(self.proxy)
            return False
        ok = await self.build(t, near=self.proxy, build_worker=w, max_distance=12, placement_step=2)
        if ok:
            self._issued[t] = self.time
        return ok

    async def place_macro(self, t: U, step: Step | None = None) -> bool:
        """After the pivot, MMM's placement: the natural, depots behind the minerals, production on the grid."""
        if t == U.COMMANDCENTER:
            ok = await self.expand()
        elif t == U.SUPPLYDEPOT:
            ok = await self.place_depot()
        elif t in GRID:
            ok = await self.place_production(t)
        else:
            return await super().place(t, step)
        if ok:
            self._issued[t] = self.time
        return ok

    # ------------------------------------------------------------------ macro
    async def macro(self, saving: bool) -> None:
        if not self.pivoted:
            self.check_pivot()
        if self.pivoted:
            await self.bio_macro(saving)
            return
        self.send_proxy_scvs()
        reserve = await self.auto_supply(saving)
        self.train_workers(reserve)
        if saving or reserve:
            return
        if self.time >= self.cfg.home_rax_time:
            await self.keep_count(U.BARRACKS, self.PROXY_RAX + self.cfg.home_rax)
        await self.spend_bank(U.BARRACKS)  # home Barracks from a bank, from spend_time (4:00) on
        reapers = self.units_created[U.REAPER] + self.already_pending(U.REAPER)
        for rax in self.structures(U.BARRACKS).ready.idle:
            if reapers < self.cfg.reapers and self.can_afford(U.REAPER):
                rax.train(U.REAPER)
                reapers += 1
            elif self.can_afford(U.MARINE):
                rax.train(U.MARINE)

    # ------------------------------------------------------------------ pivot to macro
    def check_pivot(self) -> None:
        """Pivot once the cheese has stalled (core.pivot_reasons) or on "pivot" steering. Not while all-in ("cheese
        now" commits the bot) unless steering asks for it."""
        cfg = self.cfg
        self._bank_since = held_since(self.minerals >= cfg.pivot_bank, self._bank_since, self.time)
        if self._pivot_asked:
            self.pivot(["steering"])
            return
        if self.all_in:
            return
        held = self.time - self._bank_since if self._bank_since is not None else 0.0
        why = pivot_reasons(self.time, cfg.pivot_time, held, cfg.pivot_bank_hold, self.proxy_alive(), self.retreats,
                            self.holds, cfg.pivot_retreats, cfg.pivot_holds)
        if why:
            self.pivot(why)

    def pivot(self, why: list[str]) -> None:
        logger.info(
            f"[{self.NAME}] pivot to macro at {self.time_formatted}: bank {self.minerals:.0f}, {self.retreats} "
            f"retreats, {self.holds} held waves, proxy {'alive' if self.proxy_alive() else 'dead'}, "
            f"{self.workers.amount} SCVs, {self.structures(U.BARRACKS).amount} Barracks ({', '.join(why)})"
        )
        self.pivoted = True
        self._pivot_at = self.time
        self.all_in = False
        self.cfg = pivot_config(self.cfg)  # SCVs to 40, Barracks to 8, waves of 16 from home
        self.ARMY = self.ARMY | BIO | {U.VIKINGFIGHTER}
        self.PRODUCTION = self.PRODUCTION | {U.STARPORT}
        self.proxy_tags = []  # the proxy SCVs go back to mining
        self.setup_grid()

    async def bio_macro(self, saving: bool) -> None:
        """The 2-base bio game: natural first (everything else waits up to NATURAL_WAIT s for its 400), then gas,
        Factory -> Starport, add-ons, research, Barracks from the bank, Marines / Marauders / Medivacs."""
        cfg = self.cfg
        await self.send_expander()
        reserve = await self.auto_supply(saving)
        self.train_workers(reserve)  # SparringBot: Orbitals + MULEs, then SCVs to cfg.worker_cap
        self.pivot_status()
        if saving or reserve:
            return
        if self.count_of(U.COMMANDCENTER) < 2 and self._ok_to_issue(U.COMMANDCENTER):
            if self.can_afford(U.COMMANDCENTER):
                await self.place(U.COMMANDCENTER)
            elif self.time - self._pivot_at < NATURAL_WAIT:
                return
        await self.keep_count(U.REFINERY, gas_target(self.townhalls.ready.amount, cfg.pivot_gas))
        self.addons()
        self.research_step()  # Stim -> Combat Shield on the Tech Labs, +1 on the Engineering Bay
        await self.keep_count(U.ENGINEERINGBAY, 1)
        stim = UpgradeId.STIMPACK
        if stim in self.state.upgrades or self.already_pending_upgrade(stim) > 0:  # Stim's gas first, then Medivacs
            await self.keep_count(U.FACTORY, 1)
            await self.keep_count(U.STARPORT, 1)
        await self.spend_bank(U.BARRACKS)
        self.produce_bio()

    def addon_gate(self) -> bool:
        return True  # no opening Reactor to wait for: Tech Labs first (core.barracks_addon), then Reactors

    def addon_barracks(self) -> Units:
        """Home Barracks first: the Tech Labs (and their research) stay away from the proxy."""
        return self.structures(U.BARRACKS).ready.idle.sorted_by_distance_to(self.start_location)

    def wants_addon(self, rax: Unit) -> bool:
        """An idle Barracks waits for its add-on only once that add-on is ordered or affordable: at the pivot the
        Barracks have no add-ons and there is next to no gas, and waiting would stop every Marine."""
        if not super().wants_addon(rax):
            return False
        return rax.tag in self._addon_since or self.can_afford(U.BARRACKSREACTOR)  # 50/50: either add-on

    def army_control(self) -> None:
        super().army_control()
        if self.pivoted:
            self.use_stim()

    def pivot_status(self) -> None:
        """One line per 30 s after the pivot, for tuning."""
        if self.time - self._pivot_log < 30:
            return
        self._pivot_log = self.time
        army = self.units.of_type(self.ARMY)
        logger.info(
            f"[{self.NAME}] macro status {self.time_formatted}: {self.workers.amount} SCVs, "
            f"{self.townhalls.amount} bases, {self.gas_buildings.amount} gas, rax {self.structures(U.BARRACKS).amount}"
            f" (tech labs {self.structures(U.BARRACKSTECHLAB).amount}, reactors "
            f"{self.structures(U.BARRACKSREACTOR).amount}), factory {self.structures(U.FACTORY).amount}, starport "
            f"{self.structures(U.STARPORT).amount}, army {army.amount} units / {self.supply_of(army):g} supply"
        )
