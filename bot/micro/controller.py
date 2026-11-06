"""BioMicro's stable caller API and persistent state."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from .context import FightCtx
from .helpers import walkable
from .registry import make_micro

if TYPE_CHECKING:
    from sparring import SparringBot


class BioMicro:
    def __init__(self, bot: SparringBot) -> None:
        self.bot = bot
        self.split_since: dict[int, float] = {}  # Marine tag -> first nearby Baneling
        self.stepped: set[int] = set()  # split/kite moves that must resume attacking
        self.full_cd: dict[U, float] = {}  # longest observed cooldown by unit type
        self.scans: list[tuple[float, float, float]] = []  # our recent Scanner Sweeps (micro/scan_cloak.py)

    def step(self) -> None:
        if not self.bot.cfg.micro or self.bot.hold:
            return
        ctx = FightCtx.build(self.bot, self.split_since, self.stepped, self.full_cd, self.scans)
        # Stateless factories use the current config, including replaced configs.
        # Escorts still run with no enemies; combat modules then have empty inputs.
        for module in make_micro(ctx.cfg):
            ctx.claimed.update(module.step(ctx))

    def walkable(self, pos: tuple[float, float], direction: tuple[float, float], dist: float) -> Point2 | None:
        """Compatibility helper used by SparringBot.reaper_escape."""
        return walkable(self.bot, pos, direction, dist)
