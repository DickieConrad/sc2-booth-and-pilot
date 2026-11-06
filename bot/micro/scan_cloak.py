"""Scanner Sweep on an undetected cloaked attacker (DT, Banshee, Lurker) with our army next to it (cfg.scan_cloak)."""
from __future__ import annotations

from core import scan_spot
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from .context import FightCtx
from .helpers import _xy

NOT_ARMY = frozenset({U.SCV, U.MULE})
SCAN_MEMORY = 30.0  # seconds a cast stays in ctx.scans


class ScanCloak:
    name = "scan_cloak"

    def step(self, ctx: FightCtx) -> set[int]:
        bot, now = ctx.bot, ctx.now
        ctx.scans[:] = [s for s in ctx.scans if now - s[2] < SCAN_MEMORY]
        ocs = bot.structures(U.ORBITALCOMMAND).ready.filter(lambda o: o.energy >= 50)
        if not ocs:
            return set()
        cloaked = [_xy(e) for e in bot.enemy_units if e.is_cloaked and not e.is_revealed and e.can_attack]
        if not cloaked:
            return set()
        army = [_xy(u) for u in bot.units if u.can_attack and u.type_id not in NOT_ARMY]
        spot = scan_spot(cloaked, army, ctx.scans, now, ctx.cfg.scan_army)
        if spot is None:
            return set()
        oc = max(ocs, key=lambda o: o.energy)
        oc(AbilityId.SCANNERSWEEP_SCAN, Point2(spot))
        ctx.scans.append((spot[0], spot[1], now))
        from loguru import logger  # here, not at the top: test_micro runs without loguru

        logger.info(f"[scan] {bot.time_formatted}: Scanner Sweep at ({spot[0]:.0f}, {spot[1]:.0f}) on "
                    f"{len(cloaked)} cloaked, {oc.energy:.0f} energy")
        return {oc.tag}
