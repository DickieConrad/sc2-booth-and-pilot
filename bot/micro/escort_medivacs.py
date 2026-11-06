"""Escort medivacs, preserving the original order policy."""
from __future__ import annotations

from sc2.ids.unit_typeid import UnitTypeId as U

from .context import FightCtx
from .helpers import BIO


class EscortMedivacs:
    name = "escort_medivacs"

    def step(self, ctx: FightCtx) -> set[int]:
        """Every step, fight or not: a Medivac more than cfg.medivac_leash from its closest bio unit heal-moves to it;
        with no bio left it flies home. The wave's attack-move sent Medivacs ahead alone, faster than the Marines,
        and they died first (Dickie, demo 25: "the medivac didn't follow with the army and flew in alone")."""
        bot = ctx.bot
        ordered: set[int] = set()
        leash = bot.cfg.medivac_leash
        medivacs = bot.units(U.MEDIVAC).tags_not_in(bot.retreating)
        if leash <= 0 or not medivacs:
            return ordered
        bio = bot.units.of_type(BIO)
        home = bot.rally_point()
        for m in medivacs.tags_not_in(ctx.claimed):
            if not bio:
                if m.distance_to(home) > 6:
                    m.move(home)
                    ordered.add(m.tag)
                continue
            # a Medivac sent with a wave follows that wave's bio, not the fresh Marines at home: it stayed escorting
            # the units at the gate while its wave fought without heals (Dickie, test 32). cfg.medivac_wave off:
            # ba15df0, the closest bio unit wherever it is
            if not bot.cfg.medivac_wave:
                mate = bio.closest_to(m)
                if m.distance_to(mate) > leash:
                    m.attack(mate.position)
                    ordered.add(m.tag)
                continue
            wave = bio.tags_in(bot.sent) if m.tag in bot.sent else bio.tags_not_in(bot.sent)
            if not wave and m.tag in bot.sent:  # its wave is dead: fly home, don't stay to die (Dickie, test 32)
                if m.distance_to(home) > 6:
                    m.move(home)
                    ordered.add(m.tag)
                continue
            mate = (wave or bio).closest_to(m)
            if m.distance_to(mate) > leash:
                m.attack(mate.position)  # attack-move = heal-move for a Medivac
                ordered.add(m.tag)
        return ordered
