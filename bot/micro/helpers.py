"""Shared unit classifications and pathing glue; pure geometry lives in core."""
from __future__ import annotations

from typing import TYPE_CHECKING

from core import step_points
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2
from sc2.unit import Unit

if TYPE_CHECKING:
    from sparring import SparringBot

BIO = frozenset({U.MARINE, U.MARAUDER})
BANES = frozenset({U.BANELING, U.BANELINGCOCOON})
MELEE = frozenset({
    U.ZERGLING, U.BANELING, U.ULTRALISK, U.BROODLING, U.ZEALOT, U.DARKTEMPLAR, U.SCV, U.PROBE, U.DRONE,
})
SPLIT_MATES = 2.5  # splitting Marines this close push each other apart
LEASH_REACH = 20.0  # a Medivac farther than this from the fighting bio isn't part of this fight


def is_melee(e: Unit) -> bool:
    return not e.is_flying and (e.type_id in MELEE or (e.can_attack_ground and 0 < e.ground_range <= 1))


def _xy(u: Unit) -> tuple[float, float]:
    p = u.position
    return (p.x, p.y)


def walkable(bot: SparringBot, pos: tuple[float, float], direction: tuple[float, float], dist: float) -> Point2 | None:
    for x, y in step_points(pos, direction, dist):
        p = Point2((x, y))
        try:
            if bot.in_map_bounds(p) and bot.in_pathing_grid(p):
                return p
        except (IndexError, AssertionError):
            continue
    return None
