"""Escort vikings, preserving the original order policy."""
from __future__ import annotations

from core import viking_pull_back
from sc2.ids.unit_typeid import UnitTypeId as U

from .context import FightCtx
from .helpers import BIO


class EscortVikings:
    name = "escort_vikings"

    def step(self, ctx: FightCtx) -> set[int]:
        """Vikings keep to their wave's bio (core.viking_pull_back): the wave's attack-move flew them in ahead of the
        Marines and they died first (Dickie, 2026-10-31: "they dive in first"). With no bio left they fly home."""
        bot = ctx.bot
        ordered: set[int] = set()
        leash = getattr(bot.cfg, "viking_leash", 0)
        vikings = bot.units(U.VIKINGFIGHTER).tags_not_in(bot.retreating)
        if leash <= 0 or not vikings:
            return ordered
        bio = bot.units.of_type(BIO)
        home = bot.rally_point()
        air = bot.enemy_units.filter(lambda e: e.is_flying)
        for v in vikings.tags_not_in(ctx.claimed):
            wave = bio.tags_in(bot.sent) if v.tag in bot.sent else bio.tags_not_in(bot.sent)
            mates = wave or bio
            if not mates:
                if v.distance_to(home) > 6:
                    v.move(home)
                    ordered.add(v.tag)
                continue
            mate = mates.closest_to(v)
            cover = bool(air) and air.closest_distance_to(mate) <= 9
            if viking_pull_back(v.distance_to(mate), leash, cover):
                v.move(mate.position.towards(home, 2))  # move, not attack-move: an attack-move dives again
                ordered.add(v.tag)
        return ordered
