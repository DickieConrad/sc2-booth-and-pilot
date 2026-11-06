"""BioBot: the 2-base Terran bio pieces shared by MmmBot (its whole game) and ProxyRaxBot (after its pivot to macro).
Expansion with a pre-sent SCV, Orbital morph, depots behind the minerals (or the ramp wall), Barracks / Factory /
Starport on the production grid with room for add-ons, Barracks add-ons (core.barracks_addon), Tech Lab research
(Stim -> Combat Shield -> Concussive) and Engineering Bay upgrades, Marines / Marauders (core.bio_unit) and Medivacs
(core.want_medivac), Stim in fights. Nothing here runs on its own: the bot calls these from its macro() / place() /
army_control(). The config must carry the fields read here (techlabs, research, ebay_research, marauder_share,
marauder_share_rich, gas_heavy, medivac_per, medivac_cap, expand_send, stim_hp): MmmConfig, ProxyRaxConfig."""

from __future__ import annotations

from core import (
    barracks_addon,
    behind_minerals,
    bio_unit,
    depot_spots,
    mining_side,
    next_research,
    port_slots,
    production_spots,
    wall_depot,
    want_medivac,
    want_vikings,
)
from loguru import logger
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units
from sparring import SparringBot, type_of

DEPOTS = frozenset({U.SUPPLYDEPOT, U.SUPPLYDEPOTLOWERED})
ADDONS = frozenset({U.BARRACKSREACTOR, U.BARRACKSTECHLAB})
GRID = frozenset({U.BARRACKS, U.FACTORY, U.STARPORT, U.ENGINEERINGBAY})
VIKINGS = frozenset({U.VIKINGFIGHTER, U.VIKINGASSAULT})
BIG_TARGETS = frozenset({U.COLOSSUS, U.CARRIER, U.TEMPEST, U.BROODLORD, U.BATTLECRUISER, U.MOTHERSHIP})
BIG_MEMORY = 90.0  # a big target unseen this long no longer counts for Vikings
STIM = {
    U.MARINE: (AbilityId.EFFECT_STIM_MARINE, BuffId.STIMPACK),
    U.MARAUDER: (AbilityId.EFFECT_STIM_MARAUDER, BuffId.STIMPACKMARAUDER),
}
CLAIM_SECONDS = 25.0  # a spot an SCV is walking to stays reserved this long


def upgrade_of(name: str) -> UpgradeId:
    """'ShieldWall' -> UpgradeId.SHIELDWALL."""
    return UpgradeId[name.upper()]


class BioBot(SparringBot):
    def __init__(self, cfg, **kw) -> None:
        super().__init__(cfg, **kw)
        self._corners: list[tuple[float, float]] = []
        self._wall_rax: Point2 | None = None
        self._minerals: Point2 | None = None
        self._spots: list[Point2] = []
        self._natural: Point2 | None = None
        self._expander: int | None = None  # the SCV walking to the natural before the Command Center is affordable
        self._claims: dict[tuple[float, float], float] = {}  # build spots an SCV is on its way to
        self._addon_since: dict[int, float] = {}  # Barracks tag -> when its add-on was first ordered
        self._no_addon: set[int] = set()  # Barracks whose add-on spot turned out blocked: Marines only
        self._researching: dict[str, float] = {}  # upgrade -> when ordered (until the observation shows it)
        self._big_seen: dict[int, float] = {}  # enemy Colossus/Carrier/... tag -> last seen (Vikings answer them)
        self._lab_ordered: dict[int, float] = {}  # Tech Lab / Engineering Bay tag -> when it was last given research
        self._stimmed: dict[int, float] = {}

    def setup_grid(self) -> None:
        """The production grid in the main (core.production_spots), away from its mineral line."""
        mf = self.mineral_field.closer_than(12, self.start_location)
        self._minerals = mf.center if mf else self.start_location
        start = (self.start_location.x, self.start_location.y)
        self._spots = [Point2(p) for p in production_spots(start, (self._minerals.x, self._minerals.y))]

    # ------------------------------------------------------------------ counting
    def count_of(self, t: U) -> int:
        if t == U.SUPPLYDEPOT:  # lowered depots are another unit type
            return int(self.structures(DEPOTS).ready.amount + self.already_pending(t))
        if t == U.COMMANDCENTER:  # Orbitals still count as the Command Centers of the build
            return int(self.townhalls.ready.amount + self.already_pending(t))
        if t == U.ORBITALCOMMAND:  # a morph under way counts: the step held the bank for 25 s (demo 15: CC at 2:10)
            return int(self.structures(U.ORBITALCOMMAND).amount + self.orbitals_morphing())
        return super().count_of(t)

    def orbitals_morphing(self) -> int:
        return sum(
            1
            for cc in self.structures(U.COMMANDCENTER)
            if any(o.ability.id == AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND for o in cc.orders)
        )

    # ------------------------------------------------------------------ townhalls
    def morph_orbital(self) -> bool:
        """An idle finished Command Center becomes an Orbital Command."""
        if not self.structures(U.BARRACKS).ready:
            return False
        for cc in self.structures(U.COMMANDCENTER).ready.idle:
            if self.can_afford(U.ORBITALCOMMAND):
                cc.build(U.ORBITALCOMMAND)  # the morph; build() takes its 150 off this step's minerals
                return True
        return False

    async def expand(self) -> bool:
        if self._natural is None:
            self._natural = await self.get_next_expansion()
        if self._natural is None:
            return False
        scv = self.workers.find_by_tag(self._expander) if self._expander else None
        ok = await self.build(
            U.COMMANDCENTER, near=self._natural, max_distance=10, random_alternative=False, placement_step=1,
            build_worker=scv,
        )
        if ok:
            self._expander = None
        return ok

    async def send_expander(self) -> None:
        """Before the natural's Command Center is affordable, an SCV walks there (cfg.expand_send) so the CC starts
        the moment the 400 is in."""
        if self._expander is not None or self.count_of(U.COMMANDCENTER) >= 2 or self.minerals < self.cfg.expand_send:
            return
        if not self.count_of(U.ORBITALCOMMAND):
            return
        if self._natural is None:
            self._natural = await self.get_next_expansion()
        miners = self.workers.gathering
        if self._natural is None or not miners:
            return
        scv = miners.closest_to(self._natural)
        scv.move(self._natural)
        self._expander = scv.tag

    # ------------------------------------------------------------------ placement
    def addon_barracks(self) -> Units:
        """Barracks that may start an add-on now, in the order they get one."""
        return self.structures(U.BARRACKS).ready.idle

    def build_addon(self, t: U) -> bool:
        for rax in self.addon_barracks():
            if not rax.has_add_on and rax.tag not in self._no_addon and self.can_afford(t):
                rax.build(t)
                self._addon_since.setdefault(rax.tag, self.time)
                return True
        return False

    def _claimed(self) -> list[tuple[float, float]]:
        self._claims = {p: t for p, t in self._claims.items() if self.time - t < CLAIM_SECONDS}
        return list(self._claims)

    async def place_depot(self) -> bool:
        """Ramp-wall corners first (core.wall_depot), then behind the main's mineral line (core.behind_minerals)."""
        taken = [(d.position.x, d.position.y) for d in self.structures(DEPOTS)] + self._claimed()
        spot = wall_depot(self._corners, taken)
        while spot is not None:
            p = Point2(spot)
            if await self.can_place_single(U.SUPPLYDEPOT, p):
                if await self.build(U.SUPPLYDEPOT, near=p, max_distance=0):
                    self._claims[spot] = self.time
                    return True
                return False
            self._corners = [c for c in self._corners if c != spot]  # blocked for good (a building or rocks)
            spot = wall_depot(self._corners, taken)
        th = self.townhalls.ready.closest_to(self.start_location) if self.townhalls.ready else None
        if th is not None:
            mf = self.mineral_field.closer_than(12, th)
            if mf:
                behind = Point2(behind_minerals((th.position.x, th.position.y), (mf.center.x, mf.center.y)))
                if getattr(self.cfg, "depot_clear", False):
                    return await self._place_clear_depot(th, mf.center, behind, taken)
                pos = await self.find_placement(U.SUPPLYDEPOT, behind, max_distance=8, random_alternative=False)
                if pos is not None and pos.distance_to(th) > th.distance_to(mf.center) + 1:
                    return await self.build(U.SUPPLYDEPOT, near=pos, max_distance=0)
        return await SparringBot.place(self, U.SUPPLYDEPOT)

    async def _place_clear_depot(self, th: Unit, minerals: Point2, behind: Point2, taken: list) -> bool:
        """cfg.depot_clear: the first core.depot_spots spot SC2 accepts (40 tried per step), nothing reserved left
        -> the old fallback."""
        prod = [(s.position.x, s.position.y) for s in self.structures.of_type(GRID - {U.ENGINEERINGBAY})]
        used = [Point2(p) for p in prod]
        grid = [(p.x, p.y) for p in self._spots if all(p.distance_to(u) > 1 for u in used)]
        spots = depot_spots(
            (th.position.x, th.position.y), (minerals.x, minerals.y), (behind.x, behind.y), prod, grid
        )
        spots = [Point2(p) for p in spots if all(Point2(p).distance_to(Point2(c)) > 1.5 for c in taken)][:40]
        if spots:
            for p, ok in zip(spots, await self.can_place(U.SUPPLYDEPOT, spots)):
                if ok and await self.build(U.SUPPLYDEPOT, near=p, max_distance=0):
                    self._claims[(p.x, p.y)] = self.time
                    return True
        return await SparringBot.place(self, U.SUPPLYDEPOT)

    async def place_production(self, t: U) -> bool:
        """The first Barracks walls the ramp; the rest go on the production grid (core.production_spots), each with
        room for its add-on, away from the mineral line."""
        if t == U.BARRACKS and self._wall_rax is not None and self.count_of(U.BARRACKS) == 0:
            if await self.build(t, near=self._wall_rax, max_distance=0):
                return True
        claimed = self._claimed()
        free = [p for p in self._spots if all(p.distance_to(Point2(c)) > 2 for c in claimed)]
        if not free:
            return await self._place_off_grid(t)
        ok = await self.can_place(t, free)
        addon = await self.can_place(U.SUPPLYDEPOT, [p.offset((2.5, -0.5)) for p in free])
        for p, a, b in zip(free, ok, addon):
            if a and (b or t == U.ENGINEERINGBAY) and await self.build(t, near=p, max_distance=0):
                self._claims[(p.x, p.y)] = self.time
                return True
        return await self._place_off_grid(t)

    async def _place_off_grid(self, t: U) -> bool:
        """No grid spot: cfg.prod_clear searches out from the usual spot (room for the add-on) and skips any spot on a
        townhall's mineral-line side (core.mining_side); nothing found -> try again next step. Off: the old
        spiral search."""
        if not getattr(self.cfg, "prod_clear", False):
            return await SparringBot.place(self, t)
        near = self.build_spot(t)
        fields = [(th, self.mineral_field.closer_than(12, th)) for th in self.townhalls]
        lines = [((th.position.x, th.position.y), (mf.center.x, mf.center.y)) for th, mf in fields if mf]
        addon = t in (U.BARRACKS, U.FACTORY, U.STARPORT)
        for reach in (8, 14, 20, 26):
            p = await self.find_placement(t, near, max_distance=reach, random_alternative=True, placement_step=2,
                                          addon_place=addon)
            if p is None or any(mining_side((p.x, p.y), h, m) for h, m in lines):
                continue
            if await self.build(t, near=p, max_distance=0):
                self._issued[t] = self.time
                return True
        return False

    # ------------------------------------------------------------------ add-ons, research, units
    def addon_gate(self) -> bool:
        """Barracks add-ons may start (MMM: once the opening's Reactor is placed; that Barracks makes the Reaper and
        the first Marines)."""
        return self.count_of(U.BARRACKSREACTOR) >= 1

    def addons(self) -> None:
        """Every Barracks gets an add-on (core.barracks_addon) once addon_gate() opens. One whose add-on never
        appears (spot blocked) makes Marines without one."""
        kind = barracks_addon(
            self.count_of(U.BARRACKSREACTOR),
            self.count_of(U.BARRACKSTECHLAB),
            self.cfg.techlabs,
            self.addon_gate(),
        )
        for rax in self.structures(U.BARRACKS).ready:
            since = self._addon_since.get(rax.tag)
            if since is not None and not rax.has_add_on and rax.is_idle and self.time - since > 20:
                self._no_addon.add(rax.tag)
        if kind is not None and self._ok_to_issue(U.BARRACKSTECHLAB) and self._ok_to_issue(U.BARRACKSREACTOR):
            t = U.BARRACKSTECHLAB if kind == "TechLab" else U.BARRACKSREACTOR
            if self.build_addon(t):
                self._issued[t] = self.time

    def wants_addon(self, rax: Unit) -> bool:
        return not rax.has_add_on and rax.tag not in self._no_addon and self.addon_gate()

    def research_step(self) -> None:
        cfg = self.cfg
        have = set()
        for name in (*cfg.research, *cfg.ebay_research):
            up = upgrade_of(name)
            recent = self.time - self._researching.get(name, -99.0) < 3
            if up in self.state.upgrades or self.already_pending_upgrade(up) > 0 or recent:
                have.add(name)
        for order, lab in ((cfg.research, U.BARRACKSTECHLAB), (cfg.ebay_research, U.ENGINEERINGBAY)):
            name = next_research(order, have)
            # a lab given an order a moment ago still reads idle: research() picked it again and the second order
            # replaced the first (demo 22: Combat Shield at 7:42 overwritten by Concussive at 7:43 on the same lab)
            free = self.structures(lab).ready.idle.filter(lambda b: self.time - self._lab_ordered.get(b.tag, -99.0) > 3)
            if name is None or not free:
                continue
            up = upgrade_of(name)
            if not self.can_afford(up):
                continue
            building = free.first
            building.research(up)
            self._lab_ordered[building.tag] = self.time
            self._researching[name] = self.time
            logger.info(f"[{self.NAME}] research {name} at {self.time_formatted}")

    def produce_bio(self) -> None:
        """Medivacs from idle Starports (core.want_medivac), then every Barracks fills its slots (two with a Reactor)
        with Marines / Marauders (core.bio_unit)."""
        marines = int(self.units(U.MARINE).amount + self.already_pending(U.MARINE))
        marauders = int(self.units(U.MARAUDER).amount + self.already_pending(U.MARAUDER))
        # Medivacs first: the Barracks spend every mineral, and a Starport served last never got one (demo 13: no
        # Medivac by 5:00 with 150 gas banked)
        medivacs = int(self.units(U.MEDIVAC).amount + self.already_pending(U.MEDIVAC))
        for e in self.enemy_units.of_type(BIG_TARGETS):
            self._big_seen[e.tag] = self.time
        self._big_seen = {t: s for t, s in self._big_seen.items() if self.time - s < BIG_MEMORY}
        vikings = int(self.units.of_type(VIKINGS).amount + self.already_pending(U.VIKINGFIGHTER))
        for port in self.structures(U.STARPORT).ready:
            # cfg.starport_reactor (MMM): the Reactor first, unless Vikings are needed now; a blocked spot (no
            # add-on 20 s after the order) makes units without one
            wants = getattr(self.cfg, "starport_reactor", False) and port.tag not in self._no_addon
            if wants and not port.has_add_on and port.is_idle:
                since = self._addon_since.get(port.tag)
                if since is not None and self.time - since > 20:
                    self._no_addon.add(port.tag)
                    logger.info(f"[{self.NAME}] Starport Reactor blocked at {self.time_formatted}: units without")
                    wants = False
                elif not want_vikings(len(self._big_seen), vikings, self.cfg.vikings_per, self.cfg.viking_cap):
                    if self.can_afford(U.STARPORTREACTOR):
                        port.build(U.STARPORTREACTOR)
                        self._addon_since.setdefault(port.tag, self.time)
                        logger.info(f"[{self.NAME}] Starport Reactor at {self.time_formatted}")
                    continue
                else:
                    wants = False  # Vikings now: no time for the Reactor
            if len(port.orders) >= port_slots(port.has_reactor, port.has_add_on, wants):
                continue
            if want_vikings(len(self._big_seen), vikings, self.cfg.vikings_per, self.cfg.viking_cap):
                if self.can_afford(U.VIKINGFIGHTER):
                    port.train(U.VIKINGFIGHTER)
                    vikings += 1
                continue  # the Viking's gas first: no Medivac from this Starport meanwhile
            if want_medivac(
                medivacs, marines + marauders, self.cfg.medivac_per, self.cfg.medivac_cap
            ) and self.can_afford(U.MEDIVAC):
                port.train(U.MEDIVAC)
                medivacs += 1
        for rax in self.structures(U.BARRACKS).ready:
            slots = 2 if rax.has_reactor else 1
            if len(rax.orders) >= slots or (rax.is_idle and self.wants_addon(rax)):
                continue
            if rax.has_add_on and not (rax.has_reactor or rax.has_techlab):
                continue
            for _ in range(slots - len(rax.orders)):
                share = self.cfg.marauder_share_rich if self.vespene >= self.cfg.gas_heavy else self.cfg.marauder_share
                t = type_of(bio_unit(rax.has_techlab, marines, marauders, share))
                if not self.can_afford(t):
                    t = U.MARINE
                    if not self.can_afford(t):
                        return
                rax.train(t, queue=bool(rax.orders))
                marines += t == U.MARINE
                marauders += t == U.MARAUDER

    def use_stim(self) -> None:
        if UpgradeId.STIMPACK not in self.state.upgrades:
            return
        enemies = self.enemy_units.filter(lambda e: e.can_be_attacked)
        if not enemies:
            return
        for u in self.units(set(STIM)).tags_in(self.sent):
            ability, buff = STIM[u.type_id]
            if u.has_buff(buff) or u.health_percentage < self.cfg.stim_hp:
                continue
            if self.time - self._stimmed.get(u.tag, -99.0) < 12 or enemies.closest_distance_to(u) > 7:
                continue
            u(ability)
            self._stimmed[u.tag] = self.time
