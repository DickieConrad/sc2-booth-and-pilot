"""SkytossBot: a standard 2-base Protoss macro game into air (not a cheese), so Dickie (Terran) practises TvP against
Carriers. Pylon, Gateway, gas, Nexus at the natural (~1:30), Cybernetics Core, 2nd gas, a Pylon at the natural;
an Adept and a Stalker hold early (core.gateway_pick); Stargate (~2:45-3:00) and Void Rays (Phoenixes once enemy air
shows; core.stargate_unit), 4 gas, two Shield Batteries at the natural, 2nd Stargate, Fleet Beacon (~5:30-6:00),
3rd Stargate, then Carriers (they build their Interceptors themselves: autocast). +1/+2 air weapons and armor from
the Cybernetics Core; Chrono Boost on the Stargates, then Probes, then the Core (core.chrono_pick); Probes to 44.
The first attack waits for the fleet (core.fleet_ready: 6 Carriers or ~80 army supply, or 10:00), then waves with
the shared attack-when-ahead / regroup-when-losing logic. Gateway units only defend (and Zealots from a big bank).
A placement that keeps failing asks for a new Pylon and stops holding the bank (core.placement_stuck).
Timings: config.SkytossConfig. "Cheese now" = stop Probes and attack with everything (the default)."""

from __future__ import annotations

from config import SkytossConfig
from core import (
    army_ahead,
    chrono_pick,
    fleet_ready,
    gateway_pick,
    next_research,
    opening_done,
    placement_stuck,
    production_spots,
    stargate_unit,
)
from loguru import logger
from sc2.data import Race
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units
from sparring import GAS, SparringBot, type_of

CHRONO = AbilityId.EFFECT_CHRONOBOOSTENERGYCOST
AIR = frozenset({U.VOIDRAY, U.PHOENIX, U.CARRIER})
GROUND = frozenset({U.ZEALOT, U.STALKER, U.ADEPT})
# flying enemy units that don't call for Phoenixes (detectors, Overlords)
NOT_AIR_THREAT = frozenset({U.OBSERVER, U.OBSERVERSIEGEMODE, U.OVERSEER, U.OVERSEERSIEGEMODE, U.OVERLORD,
                            U.OVERLORDTRANSPORT})
POWER = 6.0  # a building centre this close to a ready Pylon is powered (the field reaches 6.5)
PYLON_GAP = 7.0  # main-base Pylons keep this far apart, so the buildings around them have room
NATURAL_RADIUS = 12.0  # a Pylon / Nexus this close to the natural belongs to the natural


def upgrade_of(name: str) -> UpgradeId:
    """'ProtossAirWeaponsLevel1' -> UpgradeId.PROTOSSAIRWEAPONSLEVEL1."""
    return UpgradeId[name.upper()]


class SkytossBot(SparringBot):
    NAME = "Skytoss"
    RACE = Race.Protoss
    ARMY = AIR | GROUND
    PRODUCTION = frozenset({U.GATEWAY, U.STARGATE})

    def __init__(self, cfg: SkytossConfig | None = None, **kw) -> None:
        super().__init__(cfg or SkytossConfig(), **kw)
        self._natural: Point2 | None = None
        self._expander: int | None = None  # the Probe walking to the natural before the Nexus is affordable
        self._spots: list[Point2] = []  # main-base Pylon spots (core.production_spots), nearest first
        self._bad_spots: set[Point2] = set()
        self._fail_since: dict[U, float] = {}  # structure -> since when its placement keeps failing
        self._retry_at: dict[U, float] = {}
        self._fail_logged: dict[U, float] = {}
        self._room_pylon = False  # a powered building found no room: the next Pylon goes up even without need
        self._researching: dict[str, float] = {}
        self._enemy_air = False
        self._status_at = -99.0
        self._pushed = False

    async def on_start(self) -> None:
        await super().on_start()
        mf = self.mineral_field.closer_than(12, self.start_location)
        minerals = mf.center if mf else self.start_location
        start = (self.start_location.x, self.start_location.y)
        height = self.get_terrain_z_height(self.start_location)
        # a Pylon is 2x2: its centre sits on whole numbers, the Nexus's (and the grid's) on .5
        spots = [Point2((x + 0.5, y + 0.5)) for x, y in production_spots(start, (minerals.x, minerals.y), reach=2)]
        # Pylon spots on the main's plateau only (the grid reaches over the cliff on small mains)
        self._spots = [p for p in spots if self.in_map(p) and abs(self.get_terrain_z_height(p) - height) < 1.0]
        try:
            self._natural = await self.get_next_expansion()
        except Exception as e:  # odd maps: the Nexus step finds one later
            logger.info(f"[{self.NAME}] no natural yet: {e}")
        logger.info(f"[{self.NAME}] natural {self._natural}, {len(self._spots)} Pylon spots in the main")

    def in_map(self, p: Point2) -> bool:
        a = self.game_info.playable_area
        return a.x <= p.x < a.x + a.width and a.y <= p.y < a.y + a.height and self.in_placement_grid(p)

    # ------------------------------------------------------------------ places
    def natural_nexus(self) -> Unit | None:
        if self._natural is None:
            return None
        near = self.townhalls.closer_than(NATURAL_RADIUS / 2, self._natural)
        return near.first if near else None

    def main_nexus(self) -> Unit | None:
        near = self.townhalls.closer_than(NATURAL_RADIUS / 2, self.start_location)
        return near.first if near else None

    # ------------------------------------------------------------------ build-order hooks
    async def place(self, t: U, step=None) -> bool:
        """Start building t. A failed placement is retried every second and logged every 20 s; after
        cfg.place_give_up seconds of failing it no longer holds the bank (returns True) and, for a powered building,
        the next Pylon goes up for room."""
        if self.time < self._retry_at.get(t, -1.0):
            return placement_stuck(self._fail_since.get(t), self.time, self.cfg.place_give_up)
        if t in GAS:
            ok = await super().place(t, step)
        elif t == U.NEXUS:
            ok = await self.expand()
        elif t == U.PYLON:
            ok = await self.place_pylon()
        elif t == U.SHIELDBATTERY:
            ok = await self.place_battery()
        else:
            ok = await self.place_powered(t)
        if ok:
            self._issued[t] = self.time
            self._fail_since.pop(t, None)
            return True
        return self.failed(t)

    def failed(self, t: U) -> bool:
        since = self._fail_since.setdefault(t, self.time)
        self._retry_at[t] = self.time + 1.0
        if t not in GAS and t not in (U.PYLON, U.NEXUS) and self.time - since >= 3:
            self._room_pylon = True
        if self.time - self._fail_logged.get(t, -99.0) >= 20:
            self._fail_logged[t] = self.time
            logger.info(f"[{self.NAME}] can't place {t.name} at {self.time_formatted} (failing for "
                        f"{self.time - since:.0f}s, {self.minerals:.0f}m {self.vespene:.0f}g)")
        return placement_stuck(since, self.time, self.cfg.place_give_up)

    async def expand(self) -> bool:
        target = self._natural
        if target is None or self.townhalls.closer_than(3, target):
            target = await self.get_next_expansion()
        if target is None:
            return False
        probe = self.workers.find_by_tag(self._expander) if self._expander else None
        ok = await self.build(
            U.NEXUS, near=target, max_distance=10, random_alternative=False, placement_step=1, build_worker=probe
        )
        if ok:
            self._expander = None
        return ok

    async def send_expander(self) -> None:
        """Before the natural's Nexus is affordable, a Probe walks there (cfg.expand_send) so the Nexus starts the
        moment the 400 is in."""
        if self._expander is not None and not self.workers.find_by_tag(self._expander):
            self._expander = None  # it died on the way
        if (self._expander is not None or self._natural is None or self.count_of(U.NEXUS) >= 2
                or self.minerals < self.cfg.expand_send or not self.count_of(U.GATEWAY)):
            return
        miners = self.workers.gathering
        if not miners:
            return
        probe = miners.closest_to(self._natural)
        probe.move(self._natural)
        self._expander = probe.tag

    def not_distributed(self) -> set[int]:
        return {self._expander} if self._expander is not None else set()

    async def place_pylon(self) -> bool:
        """The natural gets one Pylon (in front of its Nexus, away from the minerals) for the Shield Batteries; the
        rest go on the main's grid (core.production_spots), PYLON_GAP apart."""
        pylons = self.structures(U.PYLON)
        nat = self.natural_nexus()
        if nat is not None and not pylons.closer_than(NATURAL_RADIUS, nat):
            mf = self.mineral_field.closer_than(10, nat)
            spot = nat.position.towards(mf.center, -5) if mf else nat.position.towards(self.game_info.map_center, 5)
            if await self.build(U.PYLON, near=spot, max_distance=5, random_alternative=False, placement_step=1):
                return True
        free = [p for p in self._spots
                if p not in self._bad_spots and (not pylons or pylons.closest_distance_to(p) >= PYLON_GAP)]
        if free:
            ok = await self.can_place(U.PYLON, free)
            for p, good in zip(free, ok):
                if not good:
                    self._bad_spots.add(p)
                    continue
                if await self.build(U.PYLON, near=p, max_distance=0):
                    return True
        return await super().place(U.PYLON)  # fallback: near the main Nexus, toward the map centre

    async def place_powered(self, t: U) -> bool:
        """Gateway, Core, Stargate, Fleet Beacon...: next to a ready Pylon in the main (the natural's Pylon is kept for
        the Shield Batteries), within its power field."""
        pylons = self.structures(U.PYLON).ready
        nat = self.natural_nexus()
        main = pylons.filter(lambda p: nat is None or p.distance_to(nat) > NATURAL_RADIUS) or pylons
        for py in main.sorted_by_distance_to(self.start_location):
            pos = await self.find_placement(t, py.position, max_distance=7, random_alternative=False,
                                            placement_step=1)
            if pos is not None and pos.distance_to(py) <= POWER:
                if await self.build(t, near=pos, max_distance=0):
                    return True
        return False

    async def place_battery(self) -> bool:
        """Shield Batteries between the natural's Pylon and its Nexus (the main's if there is no natural)."""
        anchor = self.natural_nexus() or self.main_nexus()
        if anchor is None:
            return False
        pylons = self.structures(U.PYLON).ready.closer_than(NATURAL_RADIUS, anchor)
        if not pylons:
            self._room_pylon = True  # place_pylon puts the natural's Pylon first
            return False
        py = pylons.closest_to(anchor)
        pos = await self.find_placement(U.SHIELDBATTERY, py.position.towards(anchor.position, 2), max_distance=5,
                                        random_alternative=False, placement_step=1)
        if pos is None or pos.distance_to(py) > POWER:
            return False
        return await self.build(U.SHIELDBATTERY, near=pos, max_distance=0)

    async def room_pylon(self) -> None:
        """One extra Pylon when a powered building had no room (or the natural has no Pylon for its Batteries)."""
        if not self._room_pylon or self.already_pending(U.PYLON) or not self._ok_to_issue(U.PYLON):
            return
        if self.can_afford(U.PYLON) and await self.place_pylon():
            self._issued[U.PYLON] = self.time
            self._room_pylon = False
            logger.info(f"[{self.NAME}] Pylon for room at {self.time_formatted}")

    # ------------------------------------------------------------------ macro
    async def macro(self, saving: bool) -> None:
        await self.send_expander()
        self.watch_air()
        reserve = await self.auto_supply(saving)
        self.train_workers(reserve)
        self.chrono()
        self.status(saving)
        await self.room_pylon()  # even while a step saves: that step can't be placed without it
        if self.all_in:
            self.produce()  # "cheese now": everything into units
            return
        if saving or reserve:
            self.gateways(defence_only=True)  # a base under attack still gets its Stalkers
            return
        self.research_step()
        if await self.gas_sink():
            return
        self.produce()

    def watch_air(self) -> None:
        """Once enemy air (not Overlords / Observers) has been seen, the Stargates add a couple of Phoenixes."""
        if self._enemy_air:
            return
        air = self.enemy_army().filter(lambda e: e.is_flying and e.type_id not in NOT_AIR_THREAT)
        if air:
            self._enemy_air = True
            logger.info(f"[{self.NAME}] enemy air seen at {self.time_formatted}: {air.first.type_id.name}")

    async def gas_sink(self) -> bool:
        """A gas bank (cfg.gas_bank, from cfg.spend_time, after the build order) adds Stargates up to cfg.prod_cap.
        Returns True while one waits for its minerals: units hold so they don't spend them first."""
        cfg = self.cfg
        if (self.vespene < cfg.gas_bank or self.time < cfg.spend_time or self.count_of(U.STARGATE) >= cfg.prod_cap
                or self.already_pending(U.STARGATE) or not self._ok_to_issue(U.STARGATE)):
            return False
        if not opening_done(cfg.build, self.build_counts()):
            return False
        if not self.can_afford(U.STARGATE):
            return True
        if await self.place_powered(U.STARGATE):
            self._issued[U.STARGATE] = self.time
            logger.info(f"[{self.NAME}] extra Stargate from {self.vespene:.0f} gas at {self.time_formatted}")
        else:
            self._room_pylon = True
        return False

    def chrono(self) -> None:
        """Every Nexus with 50 energy boosts one busy structure, by cfg.chrono_order (core.chrono_pick)."""
        if not self.cfg.chrono:
            return
        boosted: set[int] = set()
        for nx in self.townhalls.ready:
            if nx.energy < 50:
                continue
            busy: dict[str, Units] = {}
            for kind in self.cfg.chrono_order:
                t = U.NEXUS if kind == "Nexus" else type_of(kind)
                busy[kind] = self.structures(t).ready.filter(
                    lambda s: not s.is_idle and s.tag not in boosted and not s.has_buff(BuffId.CHRONOBOOSTENERGYCOST)
                )
            kind = chrono_pick({k: v.amount for k, v in busy.items()}, self.cfg.chrono_order)
            if kind is None:
                return
            target = busy[kind].first
            nx(CHRONO, target)
            boosted.add(target.tag)

    def research_step(self) -> None:
        """Air weapons / armor at the Cybernetics Core (cfg.air_research), once the first Stargate is placed (the
        Stargate comes first). Level 2 waits for the Fleet Beacon (python-sc2's research() checks it)."""
        cfg = self.cfg
        if not self.structures(U.STARGATE) or not self.structures(U.CYBERNETICSCORE).ready.idle:
            return
        have = set()
        for name in cfg.air_research:
            up = upgrade_of(name)
            recent = self.time - self._researching.get(name, -99.0) < 3
            if up in self.state.upgrades or self.already_pending_upgrade(up) > 0 or recent:
                have.add(name)
        name = next_research(cfg.air_research, have)
        if name is not None and self.research(upgrade_of(name)):
            self._researching[name] = self.time
            logger.info(f"[{self.NAME}] research {name} at {self.time_formatted}")

    def have(self, t: U) -> int:
        return int(self.units(t).amount + self.already_pending(t))

    def produce(self) -> None:
        """Stargates first (the gas is for them; a Gateway loop served first starved MMM's Starports), then the
        Gateways. A Stargate unit waiting for money holds its cost: the Gateways only get what is left."""
        cfg = self.cfg
        beacon = bool(self.structures(U.FLEETBEACON).ready)
        carriers, rays, phoenixes = self.have(U.CARRIER), self.have(U.VOIDRAY), self.have(U.PHOENIX)
        for sg in self.structures(U.STARGATE).ready.idle.filter(lambda s: s.is_powered):
            name = stargate_unit(beacon, carriers, rays, phoenixes, self._enemy_air, cfg.void_rays, cfg.phoenixes,
                                 cfg.carriers)
            if name is None:
                break
            t = type_of(name)
            if not self.can_afford(t):
                cost = self.calculate_cost(t)
                self.gateways(reserve=(cost.minerals, cost.vespene))
                return
            sg.train(t)
            carriers += t == U.CARRIER
            rays += t == U.VOIDRAY
            phoenixes += t == U.PHOENIX
        self.gateways()

    def threatened(self) -> bool:
        """Enemy army near our buildings (4+ supply) that the units at home don't clearly beat (x1.2)."""
        threats = self.threats()
        theirs = self.supply_of(threats)
        if theirs < 4:
            return False
        home = self.units.of_type(self.ARMY).tags_not_in(self.sent)
        return not army_ahead(self.supply_of(home), theirs, 1.2)

    def gateways(self, defence_only: bool = False, reserve: tuple[float, float] = (0, 0)) -> None:
        """Gateway units (core.gateway_pick): the defenders, Stalkers/Zealots while threatened, Zealots from a big
        mineral bank. reserve: minerals/gas a waiting Stargate unit holds."""
        cfg = self.cfg
        threatened = self.threatened()
        if defence_only and not threatened:
            return
        made = sum(self.units_created[t] + self.already_pending(t) for t in GROUND)
        core_ready = bool(self.structures(U.CYBERNETICSCORE).ready)
        minerals, gas = self.minerals - reserve[0], self.vespene - reserve[1]
        for gw in self.structures(U.GATEWAY).ready.idle.filter(lambda s: s.is_powered):
            name = gateway_pick(int(made), cfg.defenders, threatened, core_ready, minerals, gas, cfg.zealot_bank)
            if name is None:
                return
            t = type_of(name)
            cost = self.calculate_cost(t)
            if not self.can_afford(t) or minerals < cost.minerals or gas < cost.vespene:
                return
            gw.train(t)
            minerals, gas = minerals - cost.minerals, gas - cost.vespene
            made += 1

    # ------------------------------------------------------------------ army
    def wave_ready(self, first: bool, at_home: Units) -> bool:
        cfg = self.cfg
        carriers = at_home(U.CARRIER).amount
        supply = self.supply_of(at_home)
        ok = fleet_ready(first, carriers, supply, self.time, cfg.push_carriers, cfg.push_supply, cfg.attack_time,
                         cfg.wave_supply)
        if ok and first and not self._pushed:
            self._pushed = True
            logger.info(
                f"[{self.NAME}] first attack at {self.time_formatted}: {at_home.amount} units ({carriers} Carriers, "
                f"{at_home(U.VOIDRAY).amount} Void Rays), {supply:g} army supply, bank {self.minerals:.0f}m "
                f"{self.vespene:.0f}g, {self.workers.amount} Probes, supply {self.supply_used:.0f}/"
                f"{self.supply_cap:.0f}"
            )
        return ok

    # ------------------------------------------------------------------ log (for tuning)
    async def on_building_construction_started(self, unit: Unit) -> None:
        logger.info(f"[{self.NAME}] build {unit.type_id.name} at {self.time_formatted} ({self.supply_used:.0f} supply)")

    async def on_building_construction_complete(self, unit: Unit) -> None:
        if unit.type_id in (U.NEXUS, U.CYBERNETICSCORE, U.STARGATE, U.FLEETBEACON):
            logger.info(f"[{self.NAME}] done {unit.type_id.name} at {self.time_formatted}")

    async def on_upgrade_complete(self, upgrade: UpgradeId) -> None:
        logger.info(f"[{self.NAME}] upgrade done {upgrade.name} at {self.time_formatted}")

    async def on_unit_created(self, unit: Unit) -> None:
        if unit.type_id in self.ARMY:
            n = self.units_created[unit.type_id]
            logger.info(f"[{self.NAME}] unit {unit.type_id.name} #{n} at {self.time_formatted} "
                        f"({self.supply_used:.0f}/{self.supply_cap:.0f} supply)")

    def status(self, saving: bool) -> None:
        if self.time - self._status_at < 30:
            return
        self._status_at = self.time
        army = self.units.of_type(self.ARMY)
        ground = army.of_type(GROUND).amount
        logger.info(
            f"[{self.NAME}] status {self.time_formatted}: {self.minerals:.0f}m {self.vespene:.0f}g "
            f"supply {self.supply_used:.0f}/{self.supply_cap:.0f}, {self.workers.amount} Probes, "
            f"army {army.amount} units / {self.supply_of(army):g} supply ({army(U.CARRIER).amount} Carriers, "
            f"{army(U.VOIDRAY).amount} Void Rays, {army(U.PHOENIX).amount} Phoenixes, {ground} Gateway units), "
            f"{self.townhalls.amount} bases, Stargates {self.structures(U.STARGATE).amount}, Fleet Beacon "
            f"{bool(self.structures(U.FLEETBEACON).ready)}, enemy air {self._enemy_air}, saving {saving}"
        )
