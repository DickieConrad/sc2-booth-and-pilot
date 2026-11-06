"""Marine split windows and immediate fight fallback, in original Marine order."""
from __future__ import annotations

from core import split_active, split_direction
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.unit import Unit
from sc2.units import Units

from .context import FightCtx
from .fight import fight
from .helpers import BANES, SPLIT_MATES, _xy, walkable


def splitters(ctx: FightCtx, banes: Units) -> list[tuple[Unit, Unit]]:
    """(Marine, closest Baneling) for the Marines inside their split window."""
    cfg, now = ctx.cfg, ctx.now
    if not banes or cfg.split_radius <= 0:
        ctx.split_since.clear()
        return []
    out = []
    for u in ctx.fighting.of_type(U.MARINE).tags_not_in(ctx.claimed):
        bane = banes.closest_to(u)
        if bane.distance_to(u) > cfg.split_radius:
            ctx.split_since.pop(u.tag, None)
            continue
        since = ctx.split_since.setdefault(u.tag, now)
        if split_active(since, now, cfg.split_window):
            out.append((u, bane))
    return out


def split(ctx: FightCtx, u: Unit, bane: Unit) -> bool:
    """Shoot a Baneling in range with the weapon ready, else run (core.split_direction). False: no walkable
    spot, fight instead."""
    cfg, enemies, spots = ctx.cfg, ctx.enemies, ctx.spots
    if u.weapon_cooldown == 0:
        banes = enemies.of_type(U.BANELING).closer_than(u.ground_range + 3, u).filter(u.target_in_range)
        if banes:
            u.attack(banes.closest_to(u))
            return True
    pos = _xy(u)
    lim = SPLIT_MATES * SPLIT_MATES
    mates = [s for s in spots if 0 < (s[0] - pos[0]) ** 2 + (s[1] - pos[1]) ** 2 <= lim]
    p = walkable(ctx.bot, pos, split_direction(pos, _xy(bane), mates, cfg.split_spread), cfg.split_step)
    if p is None:
        return False
    u.move(p)
    ctx.stepped.add(u.tag)
    return True


class Splits:
    name = "splits"

    def step(self, ctx: FightCtx) -> set[int]:
        splitting = splitters(ctx, ctx.enemies.of_type(BANES))
        ctx.spots = [_xy(u) for u, _ in splitting]
        ctx.split_tags = {u.tag for u, _ in splitting}
        ordered = set()
        for u, bane in splitting:
            # Keep fallback here: deferring it changes global order and full_cd observations.
            if split(ctx, u, bane) or fight(ctx, u):
                ordered.add(u.tag)
        return ordered
