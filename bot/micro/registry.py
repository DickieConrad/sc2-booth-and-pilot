"""Factories in issuance priority order; existing settings remain the only switches."""
from collections.abc import Callable

from config import BaseConfig

from .context import MicroModule
from .escort_medivacs import EscortMedivacs
from .escort_vikings import EscortVikings
from .fight import Fight
from .leash_medivacs import LeashMedivacs
from .scan_cloak import ScanCloak
from .splits import Splits

MODULES: dict[str, Callable[[], MicroModule]] = {
    "escort_medivacs": EscortMedivacs,
    "escort_vikings": EscortVikings,
    "splits": Splits,
    "fight": Fight,
    "leash_medivacs": LeashMedivacs,
    "scan_cloak": ScanCloak,  # Orbitals only: no bio tag to claim
}


def make_micro(cfg: BaseConfig) -> list[MicroModule]:
    if not cfg.micro:
        return []
    disabled = set()
    if cfg.medivac_leash <= 0:
        disabled.update(("escort_medivacs", "leash_medivacs"))
    if getattr(cfg, "viking_leash", 0) <= 0:
        disabled.add("escort_vikings")
    if not getattr(cfg, "scan_cloak", False):  # MMM only (MmmConfig)
        disabled.add("scan_cloak")
    # Splits must run even when disabled to clear old split timers. Fight always
    # retains focus fire; kite_range only gates its stutter-step branch.
    return [factory() for name, factory in MODULES.items() if name not in disabled]
