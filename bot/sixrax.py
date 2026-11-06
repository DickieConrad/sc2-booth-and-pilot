"""SixRaxBot: the 6-Barracks Marine all-in (Terran). 16 SCVs, no gas, depot -> six Barracks by ~2:30, Marines
nonstop, first wave of 16 (or 8+ at 3:45), then a new wave every 8 Marines. Timings: config.SixRaxConfig.
"Cheese now" = stop SCVs and attack with every Marine right away."""

from __future__ import annotations

from config import SixRaxConfig
from sc2.data import Race
from sc2.ids.unit_typeid import UnitTypeId as U
from sparring import SparringBot


class SixRaxBot(SparringBot):
    NAME = "SixRax"
    RACE = Race.Terran
    ARMY = frozenset({U.MARINE})
    PRODUCTION = frozenset({U.BARRACKS})

    def __init__(self, cfg: SixRaxConfig | None = None, **kw) -> None:
        super().__init__(cfg or SixRaxConfig(), **kw)

    async def macro(self, saving: bool) -> None:
        reserve = await self.auto_supply(saving)
        self.train_workers(reserve)
        if not saving and not reserve:
            # replace lost Barracks once the opening has placed them all; then more from a bank (up to prod_cap)
            if self.time > 150:
                await self.keep_count(U.BARRACKS, self.cfg.rax_count)
            await self.spend_bank(U.BARRACKS)
            for rax in self.structures(U.BARRACKS).ready.idle:
                if self.can_afford(U.MARINE):
                    rax.train(U.MARINE)
