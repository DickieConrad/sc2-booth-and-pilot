"""MmmBot: a standard 2-base Terran MMM macro game (not a cheese), so the coach sees a real MMM game. Reaper expand:
depot + Barracks walling the main ramp, gas, Reaper, Orbital, Command Center at the natural (~1:45), Factory,
2nd gas, Barracks Reactor, Starport; then Barracks 2-5 (Tech Labs then Reactors, core.barracks_addon), 4 gas,
Engineering Bay. Stim -> Combat Shield -> Concussive from the Tech Labs, +1 attack from the Engineering Bay. Every
Command Center becomes an Orbital and calls down MULEs; SCVs up to 44; supply ahead; the bank buys more Barracks.
Marines, Marauders (core.bio_unit) and Medivacs (core.want_medivac). The first push waits for Stim and ~40 army
supply (core.push_ready), ~6:30-6:45, then waves. Wall depots lower once built and rise while enemies are at them.
Timings: config.MmmConfig. "Cheese now" = stop SCVs and attack with everything (the default). The bio pieces
(expansion, grid, add-ons, research, unit mix, Stim) live in bio.BioBot, shared with ProxyRax's pivot to macro."""

from __future__ import annotations

from bio import ADDONS, DEPOTS, GRID, BioBot
from config import MmmConfig
from core import (
    army_supply,
    depot_action,
    main_cc_spots,
    mule_ok,
    push_ready,
    scan_keep,
    third_due,
    third_fly_action,
    third_morph,
    third_stage_point,
    wall_closed,
    wall_passing,
)
from loguru import logger
from sc2.data import Race
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units

# Enemy types that mean cloak is coming or here (watch_cloak); any cloaked enemy that attacks counts too
CLOAK_SIGNS = frozenset({U.DARKSHRINE, U.DARKTEMPLAR, U.LURKERMP, U.LURKERMPBURROWED, U.LURKERDENMP})

NAMES = {
    U.MARINE: "Marine",
    U.MARAUDER: "Marauder",
    U.MEDIVAC: "Medivac",
    U.REAPER: "Reaper",
    U.SIEGETANK: "Siege Tank",
    U.SIEGETANKSIEGED: "Siege Tank",
    U.VIKINGFIGHTER: "Viking",
    U.VIKINGASSAULT: "Viking",
}
TANKS = frozenset({U.SIEGETANK, U.SIEGETANKSIEGED})


class MmmBot(BioBot):
    NAME = "MMM"
    RACE = Race.Terran
    ARMY = frozenset({U.MARINE, U.MARAUDER, U.MEDIVAC, U.SIEGETANK, U.SIEGETANKSIEGED, U.VIKINGFIGHTER})
    RAIDERS = frozenset({U.REAPER})
    PRODUCTION = frozenset({U.BARRACKS, U.STARPORT})
    UNITS = frozenset({U.REAPER, U.MARINE, U.MARAUDER, U.MEDIVAC})

    def __init__(self, cfg: MmmConfig | None = None, **kw) -> None:
        super().__init__(cfg or MmmConfig(), **kw)
        self._tank_quiet: dict[int, float] = {}  # sieged tank tag -> last time enemy ground was in range
        self._status_at = -99.0
        self._pushed = False
        self._wall_open_until = -99.0  # wall_hold: game time the wall stays open to after the last passer
        self._cloak_seen: float | None = None  # game time we first saw cloak (CLOAK_SIGNS or a cloaked attacker)
        self._turret_try: dict[int, float] = {}  # townhall tag -> last Missile Turret order for its mineral line

    async def on_start(self) -> None:
        await super().on_start()
        self.setup_grid()
        if self.cfg.wall:
            try:
                ramp = self.main_base_ramp
                self._corners = [(c.x, c.y) for c in ramp.corner_depots]
                self._wall_rax = ramp.barracks_correct_placement or ramp.barracks_in_middle
            except Exception as e:  # odd ramps: no wall, depots behind the minerals
                logger.info(f"[{self.NAME}] no ramp wall: {e}")
        logger.info(f"[{self.NAME}] wall depots {self._corners}, wall Barracks {self._wall_rax}")

    # ------------------------------------------------------------------ build-order hooks
    _gas_off = False  # gas SCVs moved to minerals while the gas bank is too big (cfg.gas_off / cfg.gas_on)
    _third: Point2 | None = None  # the third base's spot, once it's wanted
    _third_at = -999.0  # game time of the last third CC attempt (cfg.third_retry)
    _third_home: Point2 | None = None  # cfg.third_fly: where in the main the third CC is being built
    _third_tag: int | None = None  # ... and that CC once it exists (the tag survives lift-off and landing)
    _third_state = ""  # last third_fly_action, logged on change
    _bunker_built = False  # set once the one Bunker is placed: a salvaged or killed Bunker isn't rebuilt

    def count_of(self, t: U) -> int:
        if t in self.UNITS:  # a Reaper that died is not rebuilt
            return int(self.units_created[t] + self.already_pending(t))
        return super().count_of(t)  # depots, Command Centers, Orbitals: BioBot

    async def place(self, t: U, step=None) -> bool:
        if t in self.UNITS:
            ok = self.train_from_barracks(t)
        elif t == U.ORBITALCOMMAND:
            ok = self.morph_orbital()
        elif t == U.COMMANDCENTER:
            ok = await self.expand()
        elif t in ADDONS:
            ok = self.build_addon(t)
        elif t == U.SUPPLYDEPOT:
            ok = await self.place_depot()
        elif t in GRID:
            ok = await self.place_production(t)
        else:
            return await super().place(t, step)
        if ok:
            self._issued[t] = self.time
        return ok

    def train_from_barracks(self, t: U) -> bool:
        rax = self.structures(U.BARRACKS).ready.idle
        if not rax or not self.can_afford(t):
            return False
        rax.first.train(t)
        return True

    def not_distributed(self) -> set[int]:
        return {self._expander} if self._expander is not None else set()

    # ------------------------------------------------------------------ macro
    async def macro(self, saving: bool) -> None:
        await self.send_expander()
        await self.fly_third()
        self.depots()
        await self.bunker_step()
        self.watch_cloak()
        self.mules()
        if not saving:
            self.morph_orbital()  # the natural (and a rebuilt CC) as soon as it's done
        reserve = await self.auto_supply(saving)
        self.train_workers(reserve)
        await self.cloak_turrets()  # before the saving return: a DT doesn't wait for the bank
        self.status(saving)
        if saving or reserve:
            return
        if await self.third_base():
            return
        self.addons()
        self.research_step()
        await self.spend_bank(U.BARRACKS)
        if not await self.gas_sink():
            self.produce()

    async def _distribute(self, it: int) -> None:
        """Gas off above cfg.gas_off banked, on again under cfg.gas_on: 1,000 gas sat unspent at 11:20 on two bases
        while minerals ran short (Dickie, test 32)."""
        cfg = self.cfg
        was = self._gas_off
        if cfg.gas_off > 0:
            self._gas_off = self.vespene > cfg.gas_off or (was and self.vespene > cfg.gas_on)
        if self._gas_off != was:
            logger.info(f"[{self.NAME}] gas {'off' if self._gas_off else 'on'} at {self.time_formatted}: "
                        f"{self.vespene} gas, {self.minerals} minerals")
        if self._gas_off and not it % 8:
            refs = self.gas_buildings.tags
            fields = self.mineral_field
            for w in self.workers.filter(lambda w: w.order_target in refs or w.is_carrying_vespene):
                if fields and self.townhalls.ready:
                    w.gather(fields.closest_to(self.townhalls.ready.closest_to(w)))
        if not self._gas_off:
            await super()._distribute(it)
            return
        gas = self.gas_buildings  # hidden from distribute_workers so it doesn't send SCVs back to the Refineries
        self.gas_buildings = gas.subgroup([])
        try:
            await super()._distribute(it)
        finally:
            self.gas_buildings = gas

    async def third_base(self) -> bool:
        """A third Command Center at cfg.third_scvs SCVs or cfg.third_time: two bases saturate at ~44 SCVs and every
        SCV after that adds nothing (Dickie, test 32: "we probably want to be on a third expo at this point").
        Skipped while enemy ground units are at the spot. True while it waits for its 400: units hold so the Barracks
        don't spend it first."""
        cfg = self.cfg
        if self.count_of(U.COMMANDCENTER) >= 3 or self.count_of(U.COMMANDCENTER) < 2:
            return False
        if not third_due(self.workers.amount, self.time, self.supply_used, cfg.third_scvs, cfg.third_time,
                         cfg.third_supply):
            return False
        if self._third is None:
            self._third = await self.get_next_expansion()
        if self._third is None:
            return False
        # built in the main (third_fly), only a base under attack stops it; at the spot, enemies there do too
        if self.threats() or (not cfg.third_fly and self._third_contested()):
            return False
        if self.time - self._third_at < cfg.third_retry:
            return False
        if not self.can_afford(U.COMMANDCENTER):
            return True
        spot = await self._third_main_spot() if cfg.third_fly else None
        where = "in the main, to fly out"
        if spot is None:  # no third_fly, or no room in the main: built at the spot as before
            if self._third_contested():
                return False
            spot = await self.find_placement(U.COMMANDCENTER, self._third, max_distance=10, random_alternative=False,
                                             placement_step=1)
            where = "at the spot"
            if spot is None:
                return False
        ok = await self.build(U.COMMANDCENTER, near=spot, max_distance=0)
        if ok:
            # tracked either way: one built at the spot is "landed" at once and still gets its PF (game 1, 2026-11-04:
            # the fallback CC sat as a plain CC all game, no Orbital, no PF)
            if cfg.third_fly:  # baseline (third_fly=false): morph_orbital takes it as before
                self._third_home, self._third_tag = spot, None
            self._third_at = self.time
            logger.info(f"[{self.NAME}] third Command Center {where} at {self.time_formatted} "
                        f"({self.workers.amount} SCVs)")
        return ok

    def _third_contested(self) -> bool:
        return self._third is not None and bool(
            self.enemy_units.filter(lambda e: not e.is_flying).closer_than(15, self._third)
        )

    async def _third_main_spot(self) -> Point2 | None:
        """A Command Center spot in the main (core.main_cc_spots): off the mineral line and the production grid's
        first cfg.third_grid_keep spots, on the main's height, that SC2 accepts; nearest the townhall first."""
        fields = self.mineral_field.closer_than(12, self.start_location)
        if not fields:
            return None
        start = self.start_location
        cands = main_cc_spots((start.x, start.y), (fields.center.x, fields.center.y),
                              [(p.x, p.y) for p in self._spots], self.cfg.third_grid_keep)
        z = self.get_terrain_z_height(start)
        level = [Point2(p) for p in cands if abs(self.get_terrain_z_height(Point2(p)) - z) < 0.5]
        # every 2nd offset, the whole main: the nearest 60 were all depots and production (game 2, 2026-11-04)
        pts = [p for p in level if (p.x - start.x) % 2 == 0 and (p.y - start.y) % 2 == 0][:200]
        ok = await self.can_place(U.COMMANDCENTER, pts) if pts else []
        spot = next((p for p, good in zip(pts, ok) if good), None)
        logger.info(f"[{self.NAME}] third main spot: {len(cands)} candidates, {len(level)} on the main, "
                    f"{sum(ok)} of {len(pts)} placeable -> {spot}")
        return spot

    def third_cc(self) -> Unit | None:
        """The flown third (cfg.third_fly): found by its building spot in the main, then followed by tag."""
        if self._third_tag is not None:
            cc = self.townhalls.find_by_tag(self._third_tag)
            if cc is None:  # destroyed: the next attempt builds a new one
                self._third_tag = self._third_home = None
            return cc
        if self._third_home is None:
            return None
        near = self.structures(U.COMMANDCENTER).closer_than(1.5, self._third_home)
        cc = near.first if near else None
        if cc is not None:
            self._third_tag = cc.tag
        return cc

    async def fly_third(self) -> None:
        """cfg.third_fly: lift the finished third CC, fly it to the third, land; hover back over the main while
        enemy ground is at the spot; once landed, a Planetary Fortress (cfg.third_pf) or an Orbital."""
        cc = self.third_cc() if self.cfg.third_fly else None
        if cc is None or self._third is None:
            return
        flying = cc.type_id == U.COMMANDCENTERFLYING
        at_spot = cc.distance_to(self._third) < 3
        busy = bool(cc.orders) and not flying
        act = third_fly_action(at_spot, flying, cc.is_ready, busy, self._third_contested())
        if act != self._third_state:
            logger.info(f"[{self.NAME}] third CC: {act} at {self.time_formatted}")
            self._third_state = act
        if act == "lift":
            cc(AbilityId.LIFT_COMMANDCENTER)
        elif act == "hover" and cc.distance_to(self.start_location) > 6:
            cc.move(self.start_location)
        elif act == "land" and not cc.orders:
            spot = await self.find_placement(U.COMMANDCENTER, self._third, max_distance=4, placement_step=1)
            if spot is not None:
                cc(AbilityId.LAND_COMMANDCENTER, spot)
                self._third = spot
        elif act == "landed" and cc.type_id == U.COMMANDCENTER and cc.is_ready and cc.is_idle:
            race = self.enemy_race.name if self.enemy_race else ""
            if third_morph(race, self.cfg.third_pf, bool(self.structures(U.ENGINEERINGBAY).ready)) == "pf":
                if self.can_afford(U.PLANETARYFORTRESS):
                    cc.build(U.PLANETARYFORTRESS)
                    logger.info(f"[{self.NAME}] third: Planetary Fortress at {self.time_formatted}")
                    self._third_tag = self._third_home = None  # morphing: done
                # else: held (no SCVs, no Orbital) until the 150/150 is there
            else:
                self._third_tag = self._third_home = None  # morph_orbital takes it like any CC

    def third_held(self, cc: Unit) -> bool:
        """The flown third before it has landed: no SCVs and no Orbital morph (it has to lift)."""
        return self.cfg.third_fly and self._third_tag is not None and cc.tag == self._third_tag

    def morph_orbital(self) -> bool:
        if not self.structures(U.BARRACKS).ready:
            return False
        for cc in self.structures(U.COMMANDCENTER).ready.idle:
            if self.third_held(cc) or (self._third_home is not None and cc.distance_to(self._third_home) < 1.5):
                continue
            if self._pf_pending(cc):
                continue
            if self.can_afford(U.ORBITALCOMMAND):
                cc.build(U.ORBITALCOMMAND)
                return True
        return False

    def _pf_pending(self, cc: Unit) -> bool:
        """A landed third waiting for its PF money: don't let morph_orbital take it."""
        if not self.cfg.third_fly or self._third is None or cc.distance_to(self._third) >= 3:
            return False
        race = self.enemy_race.name if self.enemy_race else ""
        return third_morph(race, self.cfg.third_pf, bool(self.structures(U.ENGINEERINGBAY).ready)) == "pf"

    def stage_point(self) -> Point2:
        """cfg.third_stage: while the third is wanted (being built, flying or landed), the waiting army gathers
        between the natural and the third."""
        if (self.cfg.third_stage > 0 and self._third is not None and self._natural is not None
                and self.count_of(U.COMMANDCENTER) >= 3):
            return Point2(third_stage_point(self._natural, self._third, self.cfg.third_stage))
        return super().stage_point()

    async def gas_sink(self) -> bool:
        """A gas bank (cfg.gas_bank, from cfg.spend_time) adds Starports up to cfg.starports: one Starport can't turn
        2-base gas into Medivacs (demo 14: 560 gas at 7:30); a heavy bank (cfg.gas_heavy) from cfg.armory_time adds
        an Armory for +2. Returns True while one waits for its minerals: units hold so the Barracks don't spend them
        first (demo 15: 484 gas, 30 minerals, no Starport)."""
        cfg = self.cfg
        wanted = [
            (U.STARPORT, cfg.starports, cfg.gas_bank, cfg.spend_time),
            (U.ARMORY, 1, cfg.gas_heavy, cfg.armory_time),
            # a Factory whose Tech Lab spot is blocked gets a replacement on the grid (room for its add-on)
            (U.FACTORY, 1 + len(self._no_addon & self.structures(U.FACTORY).tags), cfg.tank_gas, cfg.spend_time),
        ]
        for t, cap, gas, after in wanted:
            if self.vespene < gas or self.time < after or self.count_of(t) >= cap or not self._ok_to_issue(t):
                continue
            if not self.can_afford(t):
                return True
            if await self.place_production(t):
                self._issued[t] = self.time
            return False
        return False

    def factory_step(self) -> None:
        """Gas into Siege Tanks (cfg.tanks, from cfg.tank_gas once the opening is done): the opening's Factory gets a
        Tech Lab, then makes tanks."""
        cfg = self.cfg
        tanks = int(self.units.of_type(TANKS).amount + self.already_pending(U.SIEGETANK))
        if tanks >= cfg.tanks or self.vespene < cfg.tank_gas or self.time < cfg.spend_time:
            return
        for fac in self.structures(U.FACTORY).ready.idle:
            if not fac.has_add_on:
                since = self._addon_since.get(fac.tag)
                if since is not None and self.time - since > 20:
                    if fac.tag not in self._no_addon:  # demo 16: no Tech Lab and no tank all game, never logged
                        self._no_addon.add(fac.tag)
                        logger.info(f"[{self.NAME}] Factory Tech Lab blocked at {self.time_formatted}: a new Factory")
                elif fac.tag not in self._no_addon and self.can_afford(U.FACTORYTECHLAB):
                    if since is None:
                        logger.info(f"[{self.NAME}] Factory Tech Lab ordered at {self.time_formatted}")
                    fac.build(U.FACTORYTECHLAB)
                    self._addon_since.setdefault(fac.tag, self.time)
            elif fac.has_techlab and self.can_afford(U.SIEGETANK):
                fac.train(U.SIEGETANK)
                logger.info(f"[{self.NAME}] Siege Tank at {self.time_formatted} ({tanks + 1} of {cfg.tanks})")
                tanks += 1

    def train_workers(self, reserve: bool = False) -> None:
        """SCVs to the cap from Orbitals; a plain Command Center morphs first (once there is a Barracks)."""
        if self.all_in or reserve:
            return
        rax = bool(self.structures(U.BARRACKS).ready)
        for th in self.townhalls.ready.idle:
            if th.type_id == U.COMMANDCENTER and rax or self.third_held(th) or th.is_flying:
                continue
            # third_time=0 (no third, --preset baseline): the cap stays worker_cap whatever the CC count
            third = (self.cfg.third_time > 0 or self.cfg.third_supply > 0) and self.count_of(U.COMMANDCENTER) >= 3
            cap = self.cfg.worker_cap_third if third else self.cfg.worker_cap
            if self.workers.amount + self.already_pending(U.SCV) < cap and self.can_afford(U.SCV):
                th.train(U.SCV)

    def mules(self) -> None:
        if not self.cfg.mules:
            return
        ocs = list(self.structures(U.ORBITALCOMMAND).ready)
        energies = [oc.energy for oc in ocs]
        cfg = self.cfg
        keep = scan_keep(cfg.scan_cloak, self._cloak_seen is not None, self.enemy_race == Race.Protoss, self.time,
                         cfg.scan_reserve_time)
        for i, oc in enumerate(ocs):
            if not mule_ok(energies, i, keep):  # keep 0: every Orbital at 50+ energy, as before
                continue
            fields = Units([], self)
            for th in self.townhalls.ready:
                fields.extend(self.mineral_field.closer_than(10, th))
            if fields:
                best = max(fields, key=lambda m: m.mineral_contents)
                oc(AbilityId.CALLDOWNMULE_CALLDOWNMULE, best)
                energies[i] -= 50

    def watch_cloak(self) -> None:
        """Note the first sign of cloak: a Dark Shrine, DT, Lurker or Lurker Den, or any cloaked enemy that attacks
        (a cloaked Banshee or Ghost). From then on MULEs keep a scan's energy and the mineral lines get turrets."""
        cfg = self.cfg
        if self._cloak_seen is not None or not (cfg.scan_cloak or cfg.cloak_turrets):
            return
        sign = (self.enemy_units.filter(lambda e: e.type_id in CLOAK_SIGNS or e.is_cloaked and e.can_attack)
                or self.enemy_structures.of_type(CLOAK_SIGNS))
        if sign:
            self._cloak_seen = self.time
            logger.info(f"[{self.NAME}] cloak seen at {self.time_formatted}: {sign.first.type_id.name}")

    async def cloak_turrets(self) -> None:
        """cfg.cloak_turrets, once cloak is seen: a Missile Turret (detection 11) in each mining base's mineral line,
        once the Engineering Bay is ready. A base waits 20 s between orders (the SCV walks there first)."""
        if not self.cfg.cloak_turrets or self._cloak_seen is None or not self.structures(U.ENGINEERINGBAY).ready:
            return
        turrets = self.structures(U.MISSILETURRET)
        for th in self.townhalls.ready:
            fields = self.mineral_field.closer_than(10, th)
            if not fields or turrets.closer_than(9, th) or self.time - self._turret_try.get(th.tag, -99.0) < 20:
                continue
            if not self.can_afford(U.MISSILETURRET):
                return
            self._turret_try[th.tag] = self.time
            if await self.build(U.MISSILETURRET, near=fields.center.towards(th.position, 2.5), max_distance=5,
                                placement_step=1):
                logger.info(f"[{self.NAME}] Missile Turret for the mineral line at {th.position.rounded} "
                            f"({self.time_formatted})")

    def depots(self) -> None:
        """Depots lowered so the army walks out; raised while enemy ground units are at them (core.depot_action)."""
        enemies = self.enemy_units.filter(lambda e: not e.is_flying)
        depots = self.structures(DEPOTS).ready
        near_any = bool(enemies) and any(enemies.closest_distance_to(d) < self.cfg.wall_radius for d in depots)
        army = self.army_supply_of(self.units.of_type(self.ARMY))
        # an SCV at the wall goes through whatever it's doing: a mining or idle one left outside was shut out
        # (Dickie, test 32: "an SCV sitting outside the base idling because it can't get back in"); cfg.wall_scv off:
        # ba15df0's rule, only units with orders that aren't gathering
        if self.cfg.wall_scv:
            movers = self.units.filter(lambda u: not u.is_flying and (bool(u.orders) or u.type_id == U.SCV))
        else:
            movers = self.units.filter(lambda u: not u.is_flying and bool(u.orders) and not u.is_gathering)
        passing = bool(movers) and any(movers.closest_distance_to(d) < self.cfg.wall_pass for d in depots)
        passing, self._wall_open_until = wall_passing(passing, self.time, self._wall_open_until, self.cfg.wall_hold)
        shut = wall_closed(near_any, self.enemy_race == Race.Zerg, army, self.cfg.wall_safe_supply, passing)
        for d in depots:
            act = depot_action(d.type_id == U.SUPPLYDEPOTLOWERED, shut)
            if act == "lower":
                d(AbilityId.MORPH_SUPPLYDEPOT_LOWER)
            elif act == "raise":
                d(AbilityId.MORPH_SUPPLYDEPOT_RAISE)

    async def bunker_step(self) -> None:
        """cfg.bunker (optional, Dickie: "it should just be optional"): one Bunker toward the enemy in front of the
        natural once its Command Center is placed; Marines nearby fill it, cfg.bunker_repairers SCVs repair it while
        enemies are at it, and after cfg.bunker_until with no enemy near it's emptied and salvaged (75 back)."""
        cfg = self.cfg
        if not cfg.bunker or self._natural is None:
            return
        bunkers = self.structures(U.BUNKER)
        if not bunkers:
            if (self.time < cfg.bunker_until and not self._bunker_built and self.count_of(U.COMMANDCENTER) >= 2
                    and self.structures(U.BARRACKS).ready and self.can_afford(U.BUNKER)
                    and not self.already_pending(U.BUNKER)):
                spot = self._natural.towards(self.enemy_start_locations[0], 6)
                if await self.build(U.BUNKER, near=spot, max_distance=4):
                    self._bunker_built = True
                    logger.info(f"[{self.NAME}] Bunker at the natural at {self.time_formatted}")
            return
        b = bunkers.first
        if not b.is_ready:
            return
        enemies = self.enemy_units.filter(lambda e: not e.is_flying and e.can_attack_ground).closer_than(12, b)
        if self.time > cfg.bunker_until and not enemies:
            if b.has_cargo:
                b(AbilityId.UNLOADALL_BUNKER)
            else:
                b(AbilityId.EFFECT_SALVAGE)
            return
        if b.cargo_left > 0:
            for m in self.units(U.MARINE).closer_than(20, b).sorted_by_distance_to(b).take(b.cargo_left):
                if m.tag not in self.retreating and m.order_target != b.tag:
                    m.smart(b)
        if enemies and b.health < b.health_max:
            fixing = self.workers.filter(lambda w: w.is_repairing).amount
            for w in self.workers.closer_than(15, b).filter(lambda w: not w.is_repairing).take(
                    max(0, cfg.bunker_repairers - fixing)):
                w.repair(b)

    def produce(self) -> None:
        if self.count_of(U.REAPER) < 1:  # the Barracks' first unit is the Reaper (demo 15: a Marine went first)
            return
        if self.count_of(U.REAPER) < self.cfg.reapers:  # --set reapers=2: the next Barracks unit is another Reaper
            self.train_from_barracks(U.REAPER)
            return
        self.factory_step()
        self.produce_bio()

    # ------------------------------------------------------------------ army
    def army_supply_of(self, units: Units) -> int:
        counts: dict[str, int] = {}
        for u in units:
            name = NAMES.get(u.type_id, u.type_id.name)
            counts[name] = counts.get(name, 0) + 1
        return army_supply(counts)

    def wave_ready(self, first: bool, at_home: Units) -> bool:
        cfg = self.cfg
        stim = UpgradeId.STIMPACK in self.state.upgrades
        supply = self.army_supply_of(at_home)
        ok = push_ready(first, supply, stim, self.time, cfg.push_supply, cfg.attack_time, cfg.stim_wait)
        if ok and first and not self._pushed:
            self._pushed = True
            logger.info(
                f"[{self.NAME}] first push at {self.time_formatted}: {at_home.amount} units, {supply} army "
                f"supply, stim {stim}, bank {self.minerals:.0f}m {self.vespene:.0f}g, "
                f"{self.workers.amount} SCVs, supply {self.supply_used:.0f}/{self.supply_cap:.0f}"
            )
        return ok

    def army_control(self) -> None:
        super().army_control()
        self.use_stim()
        self.siege_tanks()

    def siege_tanks(self) -> None:
        """Tanks siege with enemy ground within cfg.siege_range and unsiege after cfg.unsiege_after s without any, so
        they follow the waves."""
        cfg = self.cfg
        ground = self.enemy_units.filter(lambda e: not e.is_flying and e.can_be_attacked)
        for tank in self.units(U.SIEGETANK):
            if ground and ground.closest_distance_to(tank) <= cfg.siege_range:
                tank(AbilityId.SIEGEMODE_SIEGEMODE)
                self._tank_quiet[tank.tag] = self.time
        for tank in self.units(U.SIEGETANKSIEGED):
            if ground and ground.closest_distance_to(tank) <= cfg.siege_range + 1:
                self._tank_quiet[tank.tag] = self.time
            elif self.time - self._tank_quiet.get(tank.tag, 0.0) > cfg.unsiege_after:
                tank(AbilityId.UNSIEGE_UNSIEGE)

    # ------------------------------------------------------------------ log (for tuning)
    async def on_building_construction_started(self, unit: Unit) -> None:
        logger.info(f"[{self.NAME}] build {unit.type_id.name} at {self.time_formatted} ({self.supply_used:.0f} supply)")

    async def on_upgrade_complete(self, upgrade: UpgradeId) -> None:
        logger.info(f"[{self.NAME}] upgrade done {upgrade.name} at {self.time_formatted}")

    async def on_unit_type_changed(self, unit: Unit, previous_type: U) -> None:
        if unit.type_id == U.ORBITALCOMMAND:
            logger.info(f"[{self.NAME}] Orbital Command done at {self.time_formatted}")

    def status(self, saving: bool) -> None:
        if self.time - self._status_at < 30:
            return
        self._status_at = self.time
        army = self.units.of_type(self.ARMY)
        logger.info(
            f"[{self.NAME}] status {self.time_formatted}: {self.minerals:.0f}m {self.vespene:.0f}g "
            f"supply {self.supply_used:.0f}/{self.supply_cap:.0f}, {self.workers.amount} SCVs, "
            f"army {army.amount} units / {self.army_supply_of(army)} supply, "
            f"{self.townhalls.amount} bases, rax {self.structures(U.BARRACKS).amount}, saving {saving}"
        )
