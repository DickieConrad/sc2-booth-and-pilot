"""One observation and shared micro state for a step; modules own no persistent state."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from config import BaseConfig
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.units import Units

from .helpers import BIO

if TYPE_CHECKING:
    from sparring import SparringBot


@dataclass
class FightCtx:
    bot: SparringBot
    cfg: BaseConfig
    now: float
    enemies: Units
    fighting: Units
    split_since: dict[int, float]
    stepped: set[int]
    full_cd: dict[U, float]
    spots: list[tuple[float, float]] = field(default_factory=list)
    claimed: set[int] = field(default_factory=set)
    split_tags: set[int] = field(default_factory=set)
    scans: list[tuple[float, float, float]] = field(default_factory=list)  # our Scanner Sweeps (x, y, time)

    @classmethod
    def build(cls, bot: SparringBot, split_since: dict[int, float], stepped: set[int],
              full_cd: dict[U, float], scans: list[tuple[float, float, float]] | None = None) -> FightCtx:
        cfg = bot.cfg
        enemies = bot.enemy_units.filter(lambda e: e.can_be_attacked and not e.is_structure)
        fighting = bot.units.take(0)
        if enemies:
            r = cfg.micro_radius
            xs = [e.position.x for e in enemies]
            ys = [e.position.y for e in enemies]
            x0, x1, y0, y1 = min(xs) - r, max(xs) + r, min(ys) - r, max(ys) + r
            fighting = bot.units.of_type(BIO).filter(
                lambda u: u.tag not in bot.retreating and x0 <= u.position.x <= x1 and y0 <= u.position.y <= y1
                and enemies.closest_distance_to(u) <= r
            ).take(max(0, cfg.micro_cap))
        tags = fighting.tags
        for tag in list(split_since):
            if tag not in tags:
                del split_since[tag]
        stepped.intersection_update(tags)
        return cls(bot, cfg, bot.time, enemies, fighting, split_since, stepped, full_cd,
                   scans=scans if scans is not None else [])


class MicroModule(Protocol):
    """Issue orders for unclaimed tags and return exactly the tags ordered this step.

    The final Medivac leash explicitly retains the legacy escort override.
    """

    name: str

    def step(self, ctx: FightCtx) -> set[int]: ...
