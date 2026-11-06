"""DtRushBot: Dark Templar rush (Protoss). Pylon, Gateway, 2 gas, Cyber Core -> Twilight Council -> Dark Shrine,
three Gateways. Two Stalkers hold home; once the Shrine is done every Gateway makes DTs whenever it can afford one
(no Warp Gate research, so the Gateways stay Gateways and train DTs directly, chrono-boosted). The first two DTs go
together, then each new hunter goes alone, hunting workers; dt_hunters DTs hunt, later DTs join the army.
After the build order the bank buys more Gateways (up to prod_cap) and Zealots / Stalkers / Adepts
(core.gateway_unit), and the army (Gateway units + extra DTs) attacks in waves like the Terran bots.
Timings: config.DtRushConfig. "Cheese now" = stop Probes, skip the Stalkers, every DT hunts the moment it is out."""

from __future__ import annotations

from config import DtRushConfig
from core import gateway_unit
from sc2.data import Race
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.units import Units
from sparring import SparringBot, type_of

CHRONO = AbilityId.EFFECT_CHRONOBOOSTENERGYCOST


class DtRushBot(SparringBot):
    NAME = "DtRush"
    RACE = Race.Protoss
    ARMY = frozenset({U.ZEALOT, U.STALKER, U.ADEPT})
    RAIDERS = frozenset({U.DARKTEMPLAR})
    PRODUCTION = frozenset({U.GATEWAY})

    def __init__(self, cfg: DtRushConfig | None = None, **kw) -> None:
        super().__init__(cfg or DtRushConfig(), **kw)
        self.raid_wave = self.cfg.dt_wave
        self.hunters: set[int] = set()  # tags of the DTs that hunt workers on their own
        self._turn = 0  # Zealot / Stalker / Adept rotation

    def on_cheese(self) -> None:
        self.all_in = True
        self.raid_wave = 1
        if self.units(U.DARKTEMPLAR):
            self.raiding = True

    def split_army(self) -> tuple[Units, Units]:
        """The first dt_hunters DTs hunt (all of them after "cheese now"); later DTs fight with the wave army."""
        dts = self.units(U.DARKTEMPLAR)
        self.hunters &= dts.tags
        cap = len(dts) if self.all_in else self.cfg.dt_hunters
        for dt in dts.tags_not_in(self.hunters | self.sent):
            if len(self.hunters) >= cap:
                break
            self.hunters.add(dt.tag)
        return self.units.of_type(self.ARMY) | dts.tags_not_in(self.hunters), dts.tags_in(self.hunters)

    async def macro(self, saving: bool) -> None:
        reserve = await self.auto_supply(saving)
        self.train_workers(reserve)
        self.chrono()
        if not saving and not reserve and not self.all_in:
            await self.spend_bank(U.GATEWAY)
        shrine = bool(self.structures(U.DARKSHRINE).ready)
        core_ready = bool(self.structures(U.CYBERNETICSCORE).ready)
        stalkers = self.units(U.STALKER).amount + self.already_pending(U.STALKER)
        dts = self.units(U.DARKTEMPLAR).amount + self.already_pending(U.DARKTEMPLAR)
        # train() takes the cost off self.minerals / self.vespene at once, so each Gateway sees what is left
        for gw in self.structures(U.GATEWAY).ready.idle:
            if not shrine and (saving or reserve or self.all_in):
                continue  # the build order (and the Shrine) first
            if shrine and reserve and not self.can_afford(U.DARKTEMPLAR):
                continue  # a pylon waits for money; only a DT may jump it
            if not shrine and core_ready and stalkers < self.cfg.defenders:
                if self.can_afford(U.STALKER):
                    gw.train(U.STALKER)
                    stalkers += 1
                continue
            pick = gateway_unit(self.minerals, self.vespene, shrine, core_ready, int(dts), self.cfg.dt_hunters,
                                self.cfg.spend_bank, self._turn)
            if pick is None:
                break
            t = type_of(pick)
            if not self.can_afford(t):
                break
            gw.train(t)
            if t == U.DARKTEMPLAR:
                dts += 1
            elif shrine:
                self._turn += 1

    def chrono(self) -> None:
        if not self.cfg.chrono:
            return
        dt_time = bool(self.structures(U.DARKSHRINE).ready)
        for nx in self.townhalls.ready:
            if nx.energy < 50:
                continue
            busy = self.structures(U.GATEWAY).ready if dt_time else self.townhalls.ready
            busy = busy.filter(lambda s: not s.is_idle and not s.has_buff(BuffId.CHRONOBOOSTENERGYCOST))
            if busy:
                nx(CHRONO, busy.first)
                return
