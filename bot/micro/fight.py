"""Stutter-step and focus fire; also used inline when a split has no walkable spot."""
from __future__ import annotations

from core import focus_target, should_kite, unit_vector
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.unit import Unit

from .context import FightCtx
from .helpers import _xy, is_melee, walkable


def fight(ctx: FightCtx, u: Unit) -> bool:
    cfg, enemies = ctx.cfg, ctx.enemies
    near = enemies.closer_than(cfg.micro_radius, u)
    if not near:
        return False
    cd = u.weapon_cooldown
    full = max(ctx.full_cd.get(u.type_id, 0.0), cd)
    ctx.full_cd[u.type_id] = full
    if cfg.kite_range > 0 and cd > 0:
        melee = near.filter(is_melee)
        if melee:
            m = melee.closest_to(u)
            gap = u.distance_to(m) - u.radius - m.radius
            if should_kite(cd, full, gap, cfg.kite_range, cfg.kite_frac):
                p = walkable(ctx.bot, _xy(u), unit_vector(_xy(m), _xy(u)), cfg.kite_step)
                if p is not None:
                    u.move(p)
                    ctx.stepped.add(u.tag)
                    return True
    in_range = near.filter(u.target_in_range)
    cands = [(e.tag, e.type_id == U.BANELING, e.can_attack, e.health + e.shield, u.distance_to(e))
             for e in in_range]
    target = u.order_target if u.is_attacking else None
    pick = focus_target(cands, target if isinstance(target, int) else None)
    ordered = False
    if pick is not None:
        u.attack(in_range.find_by_tag(pick))
        ordered = True
    elif not cands and u.tag in ctx.stepped:  # the step back is over and nothing is in range: back in
        u.attack(near.closest_to(u).position)
        ordered = True
    ctx.stepped.discard(u.tag)
    return ordered


class Fight:
    name = "fight"

    def step(self, ctx: FightCtx) -> set[int]:
        ordered = set()
        for u in ctx.fighting:
            # Failed splits already fought inline, even if that produced no order.
            if u.tag not in ctx.claimed and u.tag not in ctx.split_tags:
                if fight(ctx, u):
                    ordered.add(u.tag)
        return ordered
