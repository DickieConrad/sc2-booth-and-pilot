"""Leash medivacs, preserving the original order policy."""
from __future__ import annotations

from sc2.ids.unit_typeid import UnitTypeId as U

from .context import FightCtx
from .helpers import LEASH_REACH


class LeashMedivacs:
    name = "leash_medivacs"

    def step(self, ctx: FightCtx) -> set[int]:
        bot = ctx.bot
        ordered: set[int] = set()
        fighting = ctx.fighting
        leash = bot.cfg.medivac_leash
        if leash <= 0 or not fighting:
            return ordered
        # Deliberately may override escort: the original final heal-move wins.
        for m in bot.units(U.MEDIVAC):
            if m.tag in bot.retreating:
                continue
            d = fighting.closest_distance_to(m)
            if leash < d <= LEASH_REACH:
                m.attack(fighting.closest_to(m).position)
                ordered.add(m.tag)
        return ordered
