"""Shared base for the sparring bots (python-sc2 BotAI, Blizzard's official bot API): steering file, build-order
steps, macro basics (workers, supply ahead, gas via distribute_workers, spending the bank on more production),
attack-move waves and auto-gg. Each bot (sixrax.py, dtrush.py, proxyrax.py) adds its cheese in macro() and, if it
needs one, on_cheese(). The decisions themselves are pure functions in core.py (tested offline)."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from config import BaseConfig
from core import (
    COMMAND_FILE,
    Command,
    CommandReader,
    EnemyMemory,
    Step,
    army_ahead,
    cooled,
    enemy_surrendered,
    enemy_to_beat,
    escape_direction,
    gate_supply,
    gg_due,
    has_lost,
    known_orders,
    local_fight,
    need_supply,
    next_step,
    opening_done,
    reaper_action,
    should_cancel,
    should_recall,
    should_retreat,
    staging_point,
    step_saving,
    supply_margin,
    supply_pending_cap,
    want_production,
    wave_size,
    wave_supply,
)
from loguru import logger
from micro import BioMicro
from sc2.bot_ai import BotAI
from sc2.data import Alliance, Race
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit, UnitOrder
from sc2.units import Units

SUPPLY = {Race.Terran: UnitTypeId.SUPPLYDEPOT, Race.Protoss: UnitTypeId.PYLON}
WORKER = {Race.Terran: UnitTypeId.SCV, Race.Protoss: UnitTypeId.PROBE}
GAS = frozenset({UnitTypeId.REFINERY, UnitTypeId.ASSIMILATOR})
WORKERS = frozenset({UnitTypeId.SCV, UnitTypeId.PROBE, UnitTypeId.DRONE, UnitTypeId.MULE})
ALLY = Alliance.Ally.value
MAXED = 190  # from this supply used a wave no longer waits to be ahead (a maxed army has nothing left to wait for)


def type_of(name: str) -> UnitTypeId:
    """'SupplyDepot' -> UnitTypeId.SUPPLYDEPOT."""
    return UnitTypeId[name.upper()]


def _orders_skipping_unknown(self: Unit) -> list[UnitOrder]:
    """Unit.orders, minus orders whose ability the installed python-sc2 doesn't know. Its game_data.abilities only
    holds ids in its AbilityId enum, and a newer SC2 client can give a unit an order with a newer id (ability 4135 on
    Probes, seen in a DT game): the stock property then raises KeyError, and every step that looks at a Probe's
    orders (distribute_workers, select_build_worker, already_pending) failed."""
    bot = self._bot_object
    return [UnitOrder.from_proto(o, bot) for o in known_orders(self._proto.orders, bot.game_data.abilities)]


_orders = cached_property(_orders_skipping_unknown)
_orders.__set_name__(Unit, "orders")
Unit.orders = _orders  # type: ignore[method-assign,assignment]


class SparringBot(BotAI):
    NAME = "Sparring"
    RACE = Race.Terran
    ARMY: frozenset[UnitTypeId] = frozenset()  # attack in waves (first_wave / min_wave / wave_size)
    RAIDERS: frozenset[UnitTypeId] = frozenset()  # go on their own, hunting workers, once raid_wave exist
    PRODUCTION: frozenset[UnitTypeId] = frozenset()  # counted for the supply margin and the production cap
    raid_wave = 1

    def __init__(self, cfg: BaseConfig, command_file: Path | str = COMMAND_FILE) -> None:
        super().__init__()
        self.cfg = cfg
        self.reader = CommandReader(command_file, cfg.poll_seconds)
        self.all_in = False  # "cheese now": no more workers, everything into the cheese
        self.hold = False  # "stop": army home, no waves
        self.force_attack = False  # "attack now": next army_control sends everything once
        self.sent: set[int] = set()  # tags of wave units already sent
        self.raiding = False
        self.waves = 0
        self.cleared: set[Point2] = set()  # enemy start locations seen empty
        self._target: Point2 | None = None
        self._roam = 0
        self._issued: dict[UnitTypeId, float] = {}
        self._errors: set[str] = set()
        self._lost_since: float | None = None  # auto-gg: since when the bot has looked lost
        self.left = False  # typed gg and left the game
        self.won_by_surrender = False  # left because the enemy typed gg (run.py logs a Victory)
        self.speed = 0.0  # run.py --speed: at most this many game seconds per real second (0 = as fast as it goes)
        self._wall0: float | None = None
        self.enemy_memory = EnemyMemory(cfg.enemy_memory)  # enemy army seen lately (attack only when ahead)
        self.retreating: dict[int, float] = {}  # tag -> when it was pulled back (walks home, ignores threats)
        self._retreat_at: float | None = None
        self._reaper_route_at: dict[int, float] = {}  # Reaper tag -> when its route was last set
        self._reaper_healing: set[int] = set()  # Reapers walking home to regenerate
        self._reaper_wait: dict[int, float] = {}  # Reaper tag -> waits at the hideout until (cfg.reaper_wait)
        self._beaten_by = 0.0  # enemy supply at the front of the last retreat
        self._commit_until = -1.0  # "attack now": no retreat before this
        self._fight_check = -99.0
        self._food: dict[UnitTypeId, float] = {}
        self._said = ("", -99.0)  # (kind, when) of the last army decision logged
        self.retreats = 0  # waves pulled back so far (regroup)
        self.holds = 0  # times a wave the counts allowed was held for not being ahead (one per spell of holding)
        self.micro = BioMicro(self)  # splits / stutter-step / focus fire for Marines + Marauders (cfg.micro)

    # ------------------------------------------------------------------ lifecycle
    async def on_start(self) -> None:
        self.reader.prime()
        self.client.game_step = 2  # only used when not realtime
        if self.cfg.chat:
            await self.chat_send(f"{self.NAME} sparring bot. gl hf")

    async def on_step(self, iteration: int) -> None:
        if self.speed > 0:  # --speed: watchable bot vs AI (Dickie: "slightly less than the fastest")
            import asyncio
            import time
            now = time.monotonic()
            if self._wall0 is None:
                self._wall0 = now - self.time / self.speed
            ahead = self._wall0 + self.time / self.speed - now
            if ahead > 0:
                await asyncio.sleep(ahead)
        for part in (self._steer_step, self._gg_step, self._distribute, self._opening_and_macro, self._army_step):
            if self.left:
                return
            try:
                await part(iteration)
            except Exception as e:  # keep playing: a bug in one part must not end Dickie's game
                key = f"{part.__name__}: {type(e).__name__}: {e}"
                if key not in self._errors:
                    self._errors.add(key)
                    logger.exception(f"[{self.NAME}] {key}")

    async def _steer_step(self, _it: int) -> None:
        cmd = self.reader.poll(self.time)
        if cmd:
            await self.steer(cmd)

    async def _distribute(self, it: int) -> None:
        if it % 8:
            return
        keep = self.not_distributed()
        if not keep:
            await self.distribute_workers()
            return
        # distribute_workers sends every idle worker to mine: hide the ones on another job (proxy builders) from it
        everyone = self.workers
        self.workers = everyone.tags_not_in(keep)
        try:
            await self.distribute_workers()
        finally:
            self.workers = everyone

    def not_distributed(self) -> set[int]:
        """Worker tags distribute_workers must leave alone (bots override: e.g. proxy builders)."""
        return set()

    async def _opening_and_macro(self, _it: int) -> None:
        saving = await self.opening()
        await self.macro(saving)
        if self.time - getattr(self, "_macro_logged", -99.0) >= 30:   # one status line per 30 s, for tuning
            self._macro_logged = self.time
            prod = sorted(self.PRODUCTION, key=str)
            have = ", ".join(f"{t.name} {self.count_of(t)}+{self.already_pending(t):g}" for t in prod)
            done = opening_done(self.cfg.build, self.build_counts())
            logger.info(f"[{self.NAME}] macro {self.time_formatted}: {self.minerals:.0f}m {self.vespene:.0f}g "
                        f"{self.supply_used:.0f}/{self.supply_cap:.0f} | {have} | opening {'done' if done else 'on'}"
                        f" saving {saving}")

    async def _army_step(self, _it: int) -> None:
        self.army_control()
        self.micro.step()  # bio micro (micro.py) after the orders above, so it has the last word
        self.cancel_dying()
        self.reaper_micro()

    def reaper_micro(self) -> None:
        """Raiding Reapers hunt workers with direct orders, kite threats while reloading, and regenerate just outside
        the enemy main, backing off toward their way out, not into the back of the base (core.reaper_action).
        Replaces the attack-move that ended up shooting buildings."""
        reapers = self.units(UnitTypeId.REAPER)
        if not reapers or not self.raiding:
            return
        threats = self.enemy_units.filter(lambda e: e.can_attack_ground and e.type_id not in WORKERS)
        workers = self.enemy_units.filter(lambda e: e.type_id in WORKERS)
        for r in reapers:
            near = threats.closer_than(10, r)
            dist = near.closest_distance_to(r) if near else None
            prey = workers.closer_than(12, r)
            act = reaper_action(r.health_percentage, r.tag in self._reaper_healing, r.weapon_cooldown <= 0, dist,
                                bool(prey), threats=near.closer_than(8, r).amount if near else 0)
            crowd = near.closer_than(8, r).amount >= 2 if near else False
            if act == "kite" and crowd and self.cfg.reaper_wait > 0:
                self._reaper_wait[r.tag] = self.time + self.cfg.reaper_wait
            if act in ("scout", "kite") and not prey and self.time < self._reaper_wait.get(r.tag, -1.0):
                act = "heal"  # wait out of reach at the hideout, don't walk straight back into the crowd
            if act == "heal":
                # back off out of reach and regenerate just outside the enemy main (Dickie: walking all the way home
                # wastes the Reaper; "hide close until he regenerates and can hit again")
                self._reaper_healing.add(r.tag)
                far = threats.closer_than(12, r)
                if far:
                    self.reaper_escape(r, far.closest_to(r), 6)
                elif self.cfg.reaper_hideout and r.distance_to(self.reaper_hideout()) > 4:
                    r.move(self.reaper_hideout())
                elif not r.is_idle:
                    r.stop()
                continue
            self._reaper_healing.discard(r.tag)
            if act == "kite":
                self.reaper_escape(r, near.closest_to(r), 4)
            elif act == "hunt":
                target = prey.closest_to(r)
                if r.order_target != target.tag:
                    r.attack(target)
            elif r.is_idle or r.is_attacking or self.time - self._reaper_route_at.get(r.tag, -99.0) > 3:
                self._reaper_route_at[r.tag] = self.time
                r.move(self.reaper_route(r))

    def reaper_hideout(self) -> Point2:
        """Where a Reaper heals: below the enemy main's cliff on our side, close enough to jump back in."""
        enemy = self.enemy_start_locations[0]
        return enemy.towards(self.start_location, 22)

    def reaper_escape(self, r: Unit, threat: Unit, dist: float) -> None:
        """Back off from `threat` toward the hideout (core.escape_direction), sideways if straight is blocked; no
        walkable step at all (a corner): straight for the hideout, the Reaper jumps the cliff on the way.
        cfg.reaper_hideout off: ba15df0's straight step away from the threat."""
        if not self.cfg.reaper_hideout:
            r.move(r.position.towards(threat.position, -dist))
            return
        pos, hide = (r.position.x, r.position.y), self.reaper_hideout()
        d = escape_direction(pos, (threat.position.x, threat.position.y), (hide.x, hide.y))
        p = self.micro.walkable(pos, d, dist)
        r.move(p if p is not None else hide)

    def reaper_route(self, r: Unit) -> Point2:
        """Into the enemy main over a cliff, not up the ramp (Dickie, demo 26: the Reaper went up the front ramp and got
        smoked). Far out: the main's edge facing our base, which the Reaper reaches with a cliff jump. On the high
        ground: the mineral line behind the workers."""
        enemy = self.enemy_start_locations[0]
        if r.distance_to(enemy) > 18:
            return enemy.towards(self.start_location, 13)  # the cliff edge of the main, toward us
        fields = self.mineral_field.closer_than(12, enemy)
        return fields.center if fields else enemy

    def cancel_dying(self) -> None:
        """Unfinished buildings about to die under attack are cancelled for the 75% refund (core.should_cancel)."""
        if self.cfg.cancel_below <= 0:
            return
        ground = self.enemy_units.filter(lambda e: e.can_attack_ground)
        for b in self.structures.not_ready:
            near = bool(ground) and ground.closest_distance_to(b) < 8
            if should_cancel(b.build_progress, b.health_percentage, near, self.cfg.cancel_below):
                b(AbilityId.CANCEL_BUILDINPROGRESS)
                logger.info(f"[{self.NAME}] cancel {b.type_id.name} at {self.time_formatted}: "
                            f"{b.health_percentage:.0%} health, {b.build_progress:.0%} built")

    async def on_unit_destroyed(self, unit_tag: int) -> None:
        """A dead enemy unit leaves the remembered enemy army at once (not after enemy_memory seconds)."""
        self.enemy_memory.forget(unit_tag)
        self.retreating.pop(unit_tag, None)

    # ------------------------------------------------------------------ steering
    async def steer(self, cmd: Command) -> None:
        logger.info(f"[{self.NAME}] steering at {self.time_formatted}: {cmd.describe()}")
        if self.cfg.chat:
            await self.chat_send(f"[coach] {cmd.describe()}")
        if cmd.gg:
            await self.leave_game("steering gg")
            return
        if cmd.stop:
            self.hold, self.force_attack = True, False
        if cmd.resume:
            self.hold = False
        if cmd.cheese:
            self.hold = False
            self.on_cheese()
        if cmd.pivot:
            self.on_pivot()
        if cmd.attack:
            self.hold, self.force_attack = False, True

    def on_pivot(self) -> None:
        """Steering "pivot": ProxyRax leaves its cheese for macro. The other bots have nothing to pivot to."""
        logger.info(f"[{self.NAME}] pivot: nothing to pivot to for this bot")

    async def leave_game(self, why: str) -> None:
        """Type gg and leave (once). The game then ends for this bot as a Defeat."""
        if self.left:
            return
        self.left = True
        logger.info(f"[{self.NAME}] gg at {self.time_formatted}: {why}")
        await self.chat_send("gg")
        await self.client.leave()

    async def _gg_step(self, _it: int) -> None:
        """Auto-gg (cfg.auto_gg): leave once clearly lost (core.has_lost, held cfg.gg_confirm seconds)."""
        cfg = self.cfg
        chat = [(c.player_id, c.message) for c in (self.state.chat or [])]
        if cfg.accept_gg and enemy_surrendered(chat, self.player_id):
            self.won_by_surrender = True
            logger.info(f"[{self.NAME}] enemy surrendered at {self.time_formatted}: {chat}")
            await self.leave_game("enemy surrendered")
            return
        if not cfg.auto_gg:
            return
        units = self.units.amount
        lost = has_lost(self.time, self.townhalls.amount, units, cfg.gg_time, cfg.gg_min_units, cfg.gg_no_base_units)
        due, self._lost_since = gg_due(lost, self._lost_since, self.time, cfg.gg_confirm)
        if due:
            await self.leave_game(f"lost ({self.townhalls.amount} townhalls, {units} units)")

    def on_cheese(self) -> None:
        """Default "cheese now": stop making workers and attack with everything. Bots extend this."""
        self.all_in = True
        self.force_attack = True

    # ------------------------------------------------------------------ build order
    def count_of(self, t: UnitTypeId) -> int:
        """Done + in progress + a worker on the way."""
        return int(self.structures(t).ready.amount + self.already_pending(t))

    def build_counts(self) -> dict[str, int]:
        return {s.item: self.count_of(type_of(s.item)) for s in self.cfg.build}

    def build_supply(self) -> float:
        """Supply as the build-order gates see it (core.gate_supply): the real supply while it can still rise; once
        worker production has stopped (all-in, or the worker cap reached with no army training) the gates open."""
        workers = self.workers.amount + self.already_pending(WORKER[self.race])
        training = any(self.already_pending(t) for t in self.ARMY | self.RAIDERS)
        return gate_supply(self.supply_used, self.all_in, workers, self.cfg.worker_cap, training)

    def _ok_to_issue(self, t: UnitTypeId, gap: float = 1.5) -> bool:
        """Avoid double orders while the last one hasn't shown up in the observation yet."""
        return self.time - self._issued.get(t, -99.0) >= gap

    async def opening(self) -> bool:
        """Run the next build-order step. Returns True while a due step waits for money (don't spend it)."""
        step = next_step(self.cfg.build, self.build_supply(), self.build_counts(), self.time)
        if step is None:
            return False
        t = type_of(step.item)
        if self.tech_requirement_progress(t) < 1 or not self._ok_to_issue(t):
            return False
        spends = self.cfg.place_fail_spends
        if not self.can_afford(t):
            # waiting on gas alone holds nothing: the minerals keep buying units and workers until they fall below the
            # step's own cost (demo 14: 505 minerals and an idle Barracks at 2:30-3:00, waiting on the Factory's gas)
            return step_saving(False, self.minerals < self.calculate_cost(t).minerals, False, spends)
        return step_saving(True, False, await self.place(t, step), spends)

    async def place(self, t: UnitTypeId, step: Step | None = None) -> bool:
        """Start building t. Returns True once the order is given."""
        if t in GAS:
            taken = self.gas_buildings
            for th in self.townhalls.ready:
                for g in self.vespene_geyser.closer_than(10, th):
                    if not taken.closer_than(1, g) and await self.build(t, near=g):
                        self._issued[t] = self.time
                        return True
            return False
        ok = await self.build(t, near=self.build_spot(t), build_worker=self.builder(t), placement_step=2)
        if ok:
            self._issued[t] = self.time
        return ok

    def build_spot(self, t: UnitTypeId) -> Point2:
        center = self.game_info.map_center
        if self.race == Race.Protoss and t != UnitTypeId.PYLON:
            pylons = self.structures(UnitTypeId.PYLON).ready
            if pylons:
                return pylons.closest_to(self.start_location).position.towards(center, 2)
        if t in SUPPLY.values():
            return self.start_location.towards(center, 7)
        return self.start_location.towards(center, 11)

    def builder(self, t: UnitTypeId) -> Unit | None:
        return None  # python-sc2 picks the closest gathering worker

    # ------------------------------------------------------------------ macro basics
    async def macro(self, saving: bool) -> None:
        raise NotImplementedError

    def orbital_and_mules(self) -> None:
        """Terran with cfg.orbital: an idle Command Center becomes an Orbital once a Barracks is ready, and every
        Orbital calls down a MULE on the fullest mineral field near it at 50 energy."""
        if self.race != Race.Terran or not self.cfg.orbital:
            return
        if self.structures(UnitTypeId.BARRACKS).ready and self.can_afford(UnitTypeId.ORBITALCOMMAND):
            for cc in self.townhalls(UnitTypeId.COMMANDCENTER).ready.idle:
                cc(AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND)
                break
        for oc in self.townhalls(UnitTypeId.ORBITALCOMMAND).ready:
            if oc.energy >= 50:
                fields = self.mineral_field.closer_than(10, oc)
                if fields:
                    oc(AbilityId.CALLDOWNMULE_CALLDOWNMULE, max(fields, key=lambda f: f.mineral_contents))

    def train_workers(self, reserve: bool = False) -> None:
        """reserve: a needed supply building waits for money; no workers until it's placed."""
        if reserve:
            return
        self.orbital_and_mules()   # before SCVs: a Command Center training an SCV can't become an Orbital
        if self.all_in:
            return
        w = WORKER[self.race]
        for th in self.townhalls.ready.idle:
            if self.workers.amount + self.already_pending(w) < self.cfg.worker_cap and self.can_afford(w):
                th.train(w)

    def production_count(self) -> int:
        return self.structures.filter(lambda s: s.type_id in self.PRODUCTION and s.is_ready).amount

    async def auto_supply(self, saving: bool = False) -> bool:
        """After the opening's first supply building: keep supply ahead of production (core.supply_margin; config
        supply_*). Returns True while a needed supply building waits for money: the caller then holds units and
        workers so they don't eat it (the coach saw 31/31, 47/47, 55/55, 63/63 blocks with 6-rax). saving: a due
        build-order step waits for money; it goes first (the cheese timings), so nothing is placed here."""
        st = SUPPLY[self.race]
        if self.supply_cap <= 15 or saving:
            return False
        cfg = self.cfg
        prod = self.production_count()
        margin = supply_margin(prod, self.townhalls.ready.amount, cfg.supply_base, cfg.supply_per_prod,
                               cfg.supply_per_townhall)
        cap = supply_pending_cap(prod, cfg.supply_max_pending)
        if not need_supply(self.supply_left, self.supply_cap, self.already_pending(st), margin, cap):
            return False
        if not self._ok_to_issue(st):
            return False
        if not self.can_afford(st):
            return True
        await self.place(st)
        return False

    async def spend_bank(self, t: UnitTypeId) -> None:
        """More production from a bank (core.want_production; config spend_bank / prod_cap / spend_time)."""
        cfg = self.cfg
        done = opening_done(cfg.build, self.build_counts())
        if not want_production(self.minerals, self.time, done, self.count_of(t), self.already_pending(t),
                               cfg.prod_cap, cfg.spend_bank, cfg.spend_time):
            return
        if self.tech_requirement_progress(t) >= 1 and self.can_afford(t) and self._ok_to_issue(t):
            await self.place(t)

    async def keep_count(self, t: UnitTypeId, n: int) -> None:
        """Build more of t (one at a time) until n exist."""
        if self.count_of(t) >= n or self.tech_requirement_progress(t) < 1:
            return
        if self.can_afford(t) and self._ok_to_issue(t):
            await self.place(t)

    # ------------------------------------------------------------------ army
    def rally_point(self) -> Point2:
        try:
            return self.main_base_ramp.top_center
        except Exception:
            return self.start_location.towards(self.game_info.map_center, 8)

    def stage_point(self) -> Point2:
        """Where the waiting army gathers (core.staging_point; cfg.stage_forward). rally_point() stays the real
        home: "stop" pulls everything back there."""
        target = self._target if self._target is not None else self.attack_target()
        return Point2(staging_point(self.rally_point(), target, forward=self.cfg.stage_forward))

    def ally_structures(self) -> Units:
        return self.all_units.filter(lambda u: u.alliance == ALLY and u.is_structure)

    def enemy_starts(self) -> list[Point2]:
        """Possible enemy mains, nearest first; in 2v2 the ally's main (seen through shared vision) is left out."""
        allies = self.ally_structures()
        locs = [p for p in self.enemy_start_locations if not allies or allies.closest_distance_to(p) > 15]
        return sorted(locs, key=lambda p: p.distance_to(self.start_location))

    def attack_target(self) -> Point2:
        if self.enemy_structures:
            return self.enemy_structures.closest_to(self.start_location).position
        for loc in self.enemy_starts():
            if loc not in self.cleared:
                return loc
        exps = sorted(self.expansion_locations_list, key=lambda p: -p.distance_to(self.start_location))
        return exps[self._roam % len(exps)] if exps else self.game_info.map_center

    def _update_scouting(self, units: Units) -> None:
        if not units:
            return
        for loc in self.enemy_starts():
            if loc not in self.cleared and units.closest_distance_to(loc) < 7:
                if not self.enemy_structures or self.enemy_structures.closest_distance_to(loc) > 15:
                    self.cleared.add(loc)
        if self._target is not None and not self.enemy_structures and units.closest_distance_to(self._target) < 5:
            self._roam += 1

    def _recall(self, army: Units) -> bool:
        """cfg.recall (MMM): the sent wave comes home to fight when enough enemy is at our buildings
        (core.should_recall); not while an "attack now" is committed. True while such a force is at home (6+ supply,
        recall on): no new wave leaves until it's dealt with."""
        frac = getattr(self.cfg, "recall", 0.0)
        if frac <= 0 or self.all_in or self.time < self._commit_until:
            return False
        threats = self.threats().filter(lambda e: e.type_id not in WORKERS)
        home = self.supply_of(threats) if threats else 0.0
        if not self.sent:
            return home >= 6
        wave = army.tags_in(self.sent)
        theirs = self.supply_of(wave)
        if should_recall(home, theirs, frac):
            logger.info(
                f"[{self.NAME}] recall at {self.time_formatted}: {home:g} enemy supply at home vs wave {theirs:g}"
            )
            self.sent.clear()
            self.retreating.clear()
        return home >= 6

    def threats(self) -> Units:
        mine = self.structures
        if not mine:
            return self.enemy_units.subgroup([])
        near = self.enemy_units.filter(lambda e: mine.closest_distance_to(e) < self.cfg.defend_radius)
        fighters = near.filter(lambda e: e.type_id not in WORKERS)
        return fighters if fighters or near.amount < 3 else near  # 3+ workers = a worker rush: fight them too

    def raid_target(self, u: Unit, target: Point2) -> Point2:
        workers = self.enemy_units.filter(lambda e: e.type_id in WORKERS).closer_than(20, u)
        return workers.closest_to(u).position if workers else target

    def split_army(self) -> tuple[Units, Units]:
        """(wave army, raiders). Bots override (DT: only the first few DTs raid, the rest join the waves)."""
        return self.units.of_type(self.ARMY), self.units.of_type(self.RAIDERS)

    def wave_ready(self, first: bool, at_home: Units) -> bool:
        """Extra gate on a wave the unit counts already allow (MMM: the first push waits for Stim). Default: none."""
        return True

    # ------------------------------------------------------------------ attack only when ahead / regroup when losing
    def food(self, t: UnitTypeId) -> float:
        """A unit type's supply from the game data (Zergling 0.5, Marauder 2; Overlord, Larva, MULE 0)."""
        f = self._food.get(t)
        if f is None:
            try:
                f = float(self.game_data.units[t.value]._proto.food_required)
            except (KeyError, AttributeError):
                f = 1.0  # a type the installed python-sc2 doesn't know: count it as a unit
            self._food[t] = f
        return f

    def supply_of(self, units: Units) -> float:
        return sum(self.food(u.type_id) for u in units)

    def enemy_army(self) -> Units:
        """Visible enemy combat units: no workers, structures or 0-supply units (Overlords, Larva, Broodlings)."""
        return self.enemy_units.filter(
            lambda e: e.type_id not in WORKERS and not e.is_structure and self.food(e.type_id) > 0)

    def _say(self, kind: str, msg: str, every: float = 30.0) -> None:
        """Log an army decision once when it changes (hold / wave / retreat / regroup), then at most every `every` s."""
        last, at = self._said
        if kind != last or self.time - at >= every:
            self._said = (kind, self.time)
            logger.info(f"[{self.NAME}] {msg}")

    def ahead_enough(self, army: Units) -> bool:
        """The ahead gate on a wave the counts already allow (core.army_ahead; cfg.ahead_ratio, enemy_memory), plus no
        wave for retreat_cooldown after a retreat. "cheese now" (all-in) and a maxed army skip it."""
        cfg, now = self.cfg, self.time
        if self.all_in or self.supply_used >= MAXED:
            return True
        if not cooled(self._retreat_at, now, cfg.retreat_cooldown):
            self._say("regroup", f"regrouping at {self.time_formatted}: no wave for {cfg.retreat_cooldown:g}s "
                                 f"after the retreat")
            return False
        theirs = enemy_to_beat(self.enemy_memory.supply(now), self._beaten_by, self._retreat_at, now, cfg.beaten_hold)
        ours = self.supply_of(army)
        if army_ahead(ours, theirs, cfg.ahead_ratio):
            return True
        if self._said[0] != "hold":
            self.holds += 1
        self._say("hold", f"holding wave {self.waves + 1} at {self.time_formatted}: {ours:g} vs enemy {theirs:g} "
                          f"(x{cfg.ahead_ratio:g} needed, {len(self.enemy_memory)} enemy units remembered)")
        return False

    def regroup(self, army: Units, enemy: Units) -> None:
        """Sent units pull back home while the enemy army at their front outweighs them (core.local_fight +
        should_retreat; cfg.retreat_*). They leave `sent` and join the next wave. Not while all-in, within
        force_commit of "attack now", or within retreat_cooldown of the last retreat."""
        cfg, now = self.cfg, self.time
        if (cfg.retreat_ratio <= 0 or not self.sent or not enemy or self.all_in or now < self._commit_until
                or not cooled(self._retreat_at, now, cfg.retreat_cooldown) or now - self._fight_check < 0.5):
            return
        self._fight_check = now
        out = army.tags_in(self.sent)
        ours = [(u.position.x, u.position.y, self.food(u.type_id)) for u in out]
        theirs = [(e.position.x, e.position.y, self.food(e.type_id)) for e in enemy]
        # theirs: the enemy army at the front; ours: the WHOLE sent wave, the same yardstick the wave left on. Judging
        # by the lead units turned every wave around at first contact (demo 21: "army 32 vs enemy 24" left, then
        # "3 vs 12 nearby, 32 units back", eight times; Dickie: Marines running back and forth). A wave now pulls
        # back only once its losses leave it behind.
        _, their = local_fight(ours, theirs, cfg.retreat_radius, cfg.retreat_reach)
        mine = wave_supply(ours)
        if not should_retreat(mine, their, cfg.retreat_ratio):
            return
        home = self.rally_point()
        self._retreat_at = now
        self._beaten_by = their  # the next wave must out-number this (core.enemy_to_beat)
        self.retreats += 1
        self.sent.clear()
        for u in out:
            u.move(home)
            self.retreating[u.tag] = now
        self._say("retreat", f"retreat at {self.time_formatted}: {mine:g} vs {their:g} nearby, {out.amount} units "
                             f"back to regroup", every=0)

    def _still_retreating(self, u: Unit, home: Point2) -> bool:
        """A pulled-back unit walks home, ignoring threats, until it is near home or 30 s have passed."""
        since = self.retreating.get(u.tag)
        if since is None:
            return False
        if self.time - since > 30 or u.distance_to(home) < 10:
            del self.retreating[u.tag]
            return False
        return True

    def army_control(self) -> None:
        army, raiders = self.split_army()
        self._update_scouting(army | raiders)
        target = self.attack_target()
        retarget = target != self._target
        self._target = target
        force, self.force_attack = self.force_attack, False
        home = self.rally_point() if self.hold else self.stage_point()
        enemy = self.enemy_army()
        self.enemy_memory.see(((e.tag, self.food(e.type_id)) for e in enemy), self.time)

        if self.hold:
            self.sent.clear()
            self.retreating.clear()
            self.raiding = False
            for u in army | raiders:
                if u.distance_to(home) > 6 and (u.is_idle or u.order_target != home):
                    u.move(home)
            return

        if not self.raiding and raiders and (raiders.amount >= self.raid_wave or force or self.all_in):
            self.raiding = True
        for u in raiders:
            if self.raiding:
                if u.is_idle or retarget:
                    u.attack(self.raid_target(u, target))
            elif u.is_idle and u.distance_to(home) > 6:
                u.move(home)

        self.sent &= army.tags
        defending = self._recall(army)
        self.regroup(army, enemy)
        at_home = army.tags_not_in(self.sent)
        n = at_home.amount
        first = self.waves == 0
        cfg = self.cfg
        counts = n > 0 and (
            (first and (n >= cfg.first_wave or (self.time >= cfg.attack_time and n >= cfg.min_wave)))
            or (not first and n >= wave_size(cfg, self.time))
        )
        # order matters: wave_ready last, so MMM logs its "first push" only for the push that really leaves
        go = n > 0 and (
            force or (counts and not defending and self.ahead_enough(army) and self.wave_ready(first, at_home))
        )
        if go:
            if force:
                self._commit_until = self.time + cfg.force_commit
            self.sent |= at_home.tags
            self.retreating.clear()
            self.waves += 1
            self._said = ("wave", self.time)
            logger.info(f"[{self.NAME}] wave {self.waves} at {self.time_formatted}: {n} units, army "
                        f"{self.supply_of(army):g} vs enemy {self.enemy_memory.supply(self.time):g} -> {target}")
            for u in at_home:
                u.attack(target)
        else:
            threats = self.threats()
            for u in at_home:
                if self._still_retreating(u, self.rally_point()):
                    continue
                if threats:
                    if u.is_idle or not u.is_attacking:
                        u.attack(threats.closest_to(u).position)
                elif u.is_idle and u.distance_to(home) > 6:
                    u.move(home)
        for u in army.tags_in(self.sent):
            if u.is_idle or retarget:
                u.attack(target)
