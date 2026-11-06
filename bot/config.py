"""Timings and sizes for every sparring bot, in one place. Edit here, or per game with
`run.py --set key=value` (e.g. --set attack_time=3:00 --set first_wave=10). Times are game seconds (Faster speed,
the in-game clock); *_time settings also accept m:ss on the command line. Stdlib only (tested without SC2)."""

from __future__ import annotations

from dataclasses import dataclass

from core import Step, steps


@dataclass(frozen=True)
class BaseConfig:
    worker_cap: int = 16  # stop making workers here
    first_wave: int = 12  # army units gathered before the first attack...
    attack_time: float = 240.0  # ...or attack at this time with at least min_wave units
    min_wave: int = 6
    wave_size: int = 8  # later waves leave home at this size
    late_time: float = 0.0  # from this time (0 = never)...
    late_wave: int = 0  # ...later waves wait for at least this many (cheese trickles grow into real waves)
    orbital: bool = False  # Terran: Orbital Command once a Barracks is ready, MULEs at 50 energy
    defend_radius: float = 25.0  # enemies this close to our townhall/production are fought by the units at home
    reaper_wait: float = 20.0  # a Reaper that backs off from 2+ enemies waits this long at the hideout before it
    # tries again: it stepped out of range, walked back in and out again (Dickie, 2026-10-31: "stop flanking back
    # and forth"); 0 = no wait
    reaper_hideout: bool = True  # a backing-off Reaper heads for its way out under the enemy main's cliff and heals
    # there (Dickie, test 32: straight away from the threat ran it into the back of the main); false = straight
    # away from the threat and heal where it stops (ba15df0)
    place_fail_spends: bool = True  # a due build step we can afford but couldn't place doesn't hold the bank
    # (core.step_saving): Acropolis vs Insane Zerg, 2026-10-31, the natural CC kept failing to place and the bot sat
    # "saving" at 33/31 supply with 1,312 minerals from 4:00 to 5:30+, no depots, no units; false = it holds (ba15df0)
    stage_forward: float = 0.0  # the waiting army stages this far (0..1) from home toward the target (0 = home)
    # attack only when ahead: a wave leaves only when the army on the attack (at home + still out) has at least
    # ahead_ratio x the enemy army supply seen in the last enemy_memory seconds (nothing seen = go; 0 = off)
    ahead_ratio: float = 1.3
    enemy_memory: float = 75.0
    # regroup when losing: sent units pull back home while the enemy army within retreat_radius of their front
    # outweighs them (ours < retreat_ratio x theirs; 0 = off). After a retreat no new wave and no new retreat for
    # retreat_cooldown seconds; "attack now" commits the army (no retreat) for force_commit seconds
    retreat_ratio: float = 0.7
    retreat_radius: float = 12.0
    vikings_per: int = 2  # Vikings per enemy Colossus / Carrier / Tempest / Brood Lord / BC seen (Starports)...
    viking_cap: int = 10  # ...at most this many
    cancel_below: float = 0.5  # unfinished + attacked: cancelled below this share of its expected health (0 = never)
    beaten_hold: float = 90.0  # after a retreat, waves must out-number the army that beat us for this long
    retreat_reach: float = 25.0  # ...but our sent units within this of the front count (the rest of the wave)
    retreat_cooldown: float = 15.0
    force_commit: float = 30.0
    poll_seconds: float = 2.0  # how often (game seconds) bot/out/command.json is read
    chat: bool = True  # say "gl hf" and acknowledge steering commands in chat
    # spending the bank (after the build order): with spend_bank+ minerals add production, one at a time (two once
    # the bank is double), up to prod_cap buildings (done + in progress), not before spend_time
    spend_bank: int = 400
    prod_cap: int = 6
    spend_time: float = 0.0
    # supply ahead (after the first supply building): keep free supply >= supply_base + supply_per_prod * production
    # + supply_per_townhall * townhalls (buildings under way count as free); 1 + production//3 under way at once,
    # at most supply_max_pending. Units and workers wait for the money while a needed depot/pylon can't be afforded.
    supply_base: int = 2
    supply_per_prod: int = 2
    supply_per_townhall: int = 2
    supply_max_pending: int = 3
    # auto-gg: type gg and leave once clearly lost: no townhall and < gg_no_base_units units, or from gg_time on
    # < gg_min_units army + workers; the condition must hold gg_confirm game seconds
    auto_gg: bool = True
    gg_time: float = 360.0
    gg_min_units: int = 6
    gg_no_base_units: int = 5
    gg_confirm: float = 5.0
    # the enemy typed gg (the built-in AI does when it surrenders): leave, and run.py logs the game as a Victory
    # (Dickie, 2026-10-31: "write an exception that accepts surrender"). Bookkeeping only, so not in the baseline
    # preset.
    accept_gg: bool = True
    # bio micro (micro.py: Marines/Marauders of the Terran bots, every step in a fight; micro=false = plain a-move).
    # Only bio with an enemy within micro_radius, at most micro_cap of them per step.
    micro: bool = True
    micro_radius: float = 10.0
    micro_cap: int = 60
    # Marine splits: a Baneling (or cocoon) within split_radius -> run split_step away from it and from the Marines
    # near it (split_spread: weight of the fan-out) for split_window s, then fight 2x that, repeat while Banelings
    # are near; a Marine with its weapon ready shoots a Baneling in range instead of running
    split_radius: float = 8.0
    split_window: float = 1.5
    split_step: float = 3.0
    split_spread: float = 0.6
    # stutter-step: a melee enemy within kite_range (edge to edge; 0 = off) -> step kite_step back while more than
    # kite_frac of the weapon cooldown is left, then shoot; Medivacs stay within medivac_leash of the bio (0 = off)
    kite_range: float = 3.5
    kite_step: float = 2.0
    kite_frac: float = 0.35
    medivac_leash: float = 6.0
    # Vikings stay within viking_leash of their wave's bio and only chase air the bio can cover (0 = off; Dickie,
    # 2026-10-31: "they dive in first")
    viking_leash: float = 7.0
    medivac_wave: bool = True  # a sent Medivac keeps to its own wave's bio and flies home once that wave is dead
    # (Dickie, test 32: it escorted the fresh units at home while its wave fought unhealed); false = closest bio
    build: tuple[Step, ...] = ()


@dataclass(frozen=True)
class SixRaxConfig(BaseConfig):
    """6-Barracks Marine all-in (Terran): 16 SCVs, no gas, six Barracks by ~2:30, Marines nonstop."""

    worker_cap: int = 16
    rax_count: int = 6
    first_wave: int = 16  # ~3:30 with six Barracks
    attack_time: float = 225.0  # 3:45 at the latest
    min_wave: int = 8
    wave_size: int = 8
    prod_cap: int = 8  # banking after the six: up to two more Barracks
    ahead_ratio: float = 1.0  # a cheese: goes at even supply (Marines are cheap, the timing is the point)
    retreat_ratio: float = 0.6
    build: tuple[Step, ...] = steps(
        (14, "SupplyDepot", 1),
        (15, "Barracks", 1),
        (16, "Barracks", 2),
        (16, "Barracks", 3),
        (16, "Barracks", 4),
        (17, "SupplyDepot", 2),
        (17, "Barracks", 5),
        (18, "Barracks", 6),
    )


@dataclass(frozen=True)
class DtRushConfig(BaseConfig):
    """Dark Templar rush (Protoss): 2 gas, Cyber Core -> Twilight -> Dark Shrine, 3 Gateways make DTs (no Warp Gate,
    so they stay Gateways and train DTs directly). A couple of Stalkers hold home; DTs go for the workers, then
    DTs + Zealots/Stalkers/Adepts from more Gateways attack in waves."""

    worker_cap: int = 22
    defenders: int = 2  # Stalkers before the Dark Shrine finishes
    dt_wave: int = 2  # the first DTs leave together; every later hunter goes on its own
    dt_hunters: int = 3  # DTs that hunt workers on their own; DTs beyond these join the army waves
    chrono: bool = True  # chrono Probes, then the Gateways once DTs are possible
    first_wave: int = 10  # the gateway army leaves at 10 units...
    attack_time: float = 330.0  # ...or at 5:30 with min_wave (the DTs hit first)
    min_wave: int = 6
    wave_size: int = 6
    prod_cap: int = 6  # Gateways, added from the bank after the build order
    ahead_ratio: float = 1.0  # a cheese: the gateway army goes at even supply (the hunting DTs are never gated)
    retreat_ratio: float = 0.6
    build: tuple[Step, ...] = steps(
        (14, "Pylon", 1),
        (16, "Gateway", 1),
        (16, "Assimilator", 1),
        (17, "Assimilator", 2),
        (19, "CyberneticsCore", 1),
        (20, "Pylon", 2),
        (21, "TwilightCouncil", 1),
        (22, "Gateway", 2),
        (23, "DarkShrine", 1),
        (24, "Pylon", 3),
        (24, "Gateway", 3),
    )


@dataclass(frozen=True)
class ProxyRaxConfig(BaseConfig):
    """Proxy 2-Barracks Reaper/Marine (Terran): one SCV leaves early and builds two Barracks near the enemy natural;
    the first makes a Reaper (one gas at home), then both make Marines that attack in small waves. Once the cheese
    has stalled (from 4:00) it pivots to a 2-base bio macro game."""

    proxy_scv_time: float = 16.0  # the proxy SCV leaves home at 0:16
    proxy_distance: float = 22.0  # proxy spot: this far from the enemy natural, toward the map centre
    reapers: int = 1
    worker_cap: int = 16
    first_wave: int = 4
    attack_time: float = 150.0  # 2:30: go with min_wave even if the first wave isn't full
    min_wave: int = 2
    wave_size: int = 4
    late_time: float = 240.0  # from 4:00 the Marines wait for 10: no more pairs of 4 into a wall
    late_wave: int = 10
    orbital: bool = True  # 150 of the bank into MULEs (Dickie: "still no Orbital")
    home_rax_time: float = 240.0  # from 4:00 build Barracks at home up to home_rax
    home_rax: int = 1
    prod_cap: int = 5  # banking: more home Barracks, up to 5 in all...
    spend_time: float = 150.0  # ...from 2:30 (after the first wave; the proxy itself stays two Barracks)
    stage_forward: float = 0.4  # army waits at the proxy; once both proxy Barracks are gone, 40% of the way out
    proxy_alive_radius: float = 15.0  # a Barracks this close to the proxy spot counts as a proxy Barracks
    ahead_ratio: float = 1.2  # 4 Marines no longer walk into 3 Zealots (6 supply): it takes 8 (demo: 3 losses vs Hard)
    retreat_ratio: float = 0.7
    # pivot to macro (core.pivot_reasons; tests 10-12 + 18: 0/3 vs Hard Protoss, 5 Barracks, 16 SCVs, 900 banked):
    # from pivot_time (0 = never) the cheese turns into a 2-base bio game, once, at the first sign it has stalled:
    # pivot_bank+ minerals for pivot_bank_hold s, the proxy Barracks gone, pivot_retreats waves pulled back or
    # pivot_holds waves held for not being ahead (0 = that sign off). "pivot" steering does it at once.
    pivot_time: float = 240.0
    pivot_bank: int = 500  # ...also the bank that adds Barracks after the pivot
    pivot_bank_hold: float = 20.0
    pivot_retreats: int = 1
    pivot_holds: int = 1
    # after the pivot (core.pivot_config, bio.BioBot): Command Center at the natural, SCVs to pivot_workers, gas up to
    # pivot_gas (2 per base), Factory -> Starport, Barracks up to pivot_rax, waves of pivot_wave gathering at home
    pivot_workers: int = 40
    pivot_gas: int = 3
    pivot_rax: int = 8
    pivot_wave: int = 16
    techlabs: int = 2  # Tech Labs (Stim, Combat Shield, Concussive, Marauders) on home Barracks; Reactors on the rest
    research: tuple[str, ...] = ("Stimpack", "ShieldWall", "PunisherGrenades")
    ebay_research: tuple[str, ...] = ("TerranInfantryWeaponsLevel1", "TerranInfantryArmorsLevel1")  # after the pivot
    marauder_share: float = 0.35
    marauder_share_rich: float = 0.6
    gas_heavy: int = 600
    medivac_per: int = 8
    medivac_cap: int = 6
    expand_send: int = 250
    stim_hp: float = 0.6
    build: tuple[Step, ...] = steps(
        (14, "SupplyDepot", 1),
        (15, "Barracks", 1),
        (15, "Barracks", 2),
        (16, "Refinery", 1),
    )


@dataclass(frozen=True)
class MmmConfig(BaseConfig):
    """2-base Terran MMM macro (not a cheese): Reaper expand, Factory, Starport, five Barracks (Tech Labs and
    Reactors), Stim -> Combat Shield -> Concussive, +1 attack, Medivacs; first push ~6:30 with Stim and ~40 army
    supply, then waves. Walls the main ramp (two depots + Barracks), lowers the depots, MULEs nonstop."""

    worker_cap: int = 44  # 2 saturated bases: 2 x 16 on minerals + 4 x 3 on gas
    gas_off: float = 800.0  # banked gas above this: gas SCVs mine minerals instead (0 = never) ...
    gas_on: float = 300.0  # ... until the bank is back under this
    third_scvs: int = 40  # the third Command Center goes down at this many SCVs ...
    third_time: float = 330.0  # ... or at this game time (5:30), whichever is first; 0 = no third
    third_supply: float = 100.0  # ... or at this much supply used, even with third_time 0; 0 = off
    third_retry: float = 60.0  # seconds between third CC attempts: a lost CC was re-sent every tick from 6:36 to
    # 7:13, 12 SCVs walked into the Zerg army (Acropolis, 2026-10-31); none at all while the base is attacked
    worker_cap_third: int = 60  # SCV cap once the third is placed (2 saturated bases + the third's minerals)
    third_fly: bool = True  # the third CC is built in the main and flown out once done (core.third_fly_action;
    # Dickie, 2026-11-04: built in place at 5:30 it sat alone for 100 s: 0-5 vs Insane Zerg on Acropolis); false =
    # built at the spot by an SCV
    third_pf: str = "zerg"  # the landed third becomes a Planetary Fortress vs these races (comma list; needs an
    # Engineering Bay; "" = always an Orbital)
    third_grid_keep: int = 12  # third_fly: the main CC keeps clear of this many production-grid spots
    third_stage: float = 0.5  # while the third is wanted the waiting army gathers this far (0..1) from the natural
    # toward it, so an attack on the third meets the bio first; 0 = the usual stage point
    expand_send: int = 250  # an SCV walks to the natural at this bank, once the Orbital is under way (demo 13:
    #   it left at 400 and the Command Center started at 2:01, not ~1:45)
    first_wave: int = 24  # units at home for the first push (Marines + Marauders + Medivacs)...
    push_supply: int = 40  # ...and this much army supply (core.push_ready)...
    attack_time: float = 405.0  # ...or 6:45 with at least min_wave units
    stim_wait: float = 45.0  # without Stimpack the first push waits up to this long after attack_time
    min_wave: int = 16
    wave_size: int = 16  # later waves
    late_time: float = 600.0  # from 10:00 waves wait for 24
    late_wave: int = 24
    techlabs: int = 3  # Barracks Tech Labs (Stim, Combat Shield, Concussive, Marauders); the other Barracks: Reactors
    marauder_share: float = 0.35  # Tech Lab Barracks make Marauders while they are under this share of the bio
    medivac_per: int = 8  # one Medivac per 8 bio units...
    medivac_cap: int = 8  # ...at most 8
    gas_bank: int = 300  # from spend_time, this much gas adds Starports...
    starports: int = 2  # ...up to this many
    recall: float = 0.5  # a sent wave comes home when the enemy at our buildings has this share of its supply (6+);
    # 0 = ba15df0: a sent wave never comes back (Dickie, 2026-10-31: "our bot basically lost a base race")
    prod_clear: bool = True  # a production building off the grid never goes on a mineral-line side, and keeps room
    # for its add-on (Dickie, 2026-10-31: a Factory in the mineral line); false = ba15df0's plain spiral search
    depot_clear: bool = True  # depots off the add-on squares and the free production grid (Dickie, 2026-10-31: depots
    # sat where the Starport Reactor would go); false = ba15df0: behind the minerals, then next to the main
    starport_reactor: bool = True  # each Starport gets a Reactor before its first unit: two Medivacs/Vikings at a time
    # (Dickie, 2026-10-31: "our bot didn't put a reactor on either starport"); false = ba15df0, none
    gas_heavy: int = 600  # at this much gas (demo 15: 1800 banked by 11:30)...
    marauder_share_rich: float = 0.6  # ...Tech Lab Barracks make Marauders up to this share of the bio...
    armory_time: float = 420.0  # ...an Armory from 7:00 (+2 upgrades)...
    tanks: int = 4  # ...and the Factory gets a Tech Lab and makes Siege Tanks up to this many (from tank_gas)
    tank_gas: int = 300
    siege_range: float = 12.0  # tanks siege with enemy ground this close, unsiege when none for unsiege_after s
    unsiege_after: float = 4.0
    mules: bool = True  # Orbital Commands call down MULEs on the fullest mineral field
    scan_cloak: bool = True  # an Orbital scans an undetected cloaked attacker (DT, Banshee, Lurker) with scan_army of
    # our army within 10 of it; MULEs then leave 50 energy for a scan, vs Protoss from scan_reserve_time and vs anyone
    # once cloak is seen (DT vs MMM 6-0, 2026-11-04: every MULE went out at 50, never a scan); false = ba15df0
    scan_reserve_time: float = 240.0
    scan_army: int = 2
    cloak_turrets: bool = True  # once cloak is seen, a Missile Turret in each mining base's mineral line (needs the
    # Engineering Bay, built at 34 supply); false = ba15df0
    wall: bool = True  # first two depots + first Barracks wall the main ramp; depots lowered, raised vs enemies
    wall_radius: float = 8.0  # enemy ground units this close to any wall depot raise the whole wall
    reapers: int = 1  # the opening's Reaper; 2 = a second one right after (Dickie's 2-Reaper demo)
    bunker: bool = False  # --set bunker=true: a Bunker in front of the natural once its CC is placed (Dickie)
    bunker_until: float = 480.0  # game seconds: after this, with no enemy near, it's emptied and salvaged
    bunker_repairers: int = 2  # SCVs repairing it while enemies are at it
    wall_pass: float = 4.0  # our moving ground units this close to a raised wall depot open it (no enemy near)
    wall_scv: bool = True  # any SCV at the wall opens it, mining or idle (Dickie, test 32: an SCV idled outside,
    # shut out); false = only units with orders that aren't gathering (ba15df0)
    wall_hold: float = 6.0  # an opened wall stays open this many seconds after the last passer (Dickie, 2026-10-31:
    # "SCVs are getting stuck because the computer is repeatedly opening and closing the supply depots"); 0 = ba15df0
    wall_safe_supply: float = 8.0  # vs Zerg the wall stays raised until the army has this much supply
    stim_hp: float = 0.6  # Marines/Marauders stim in a fight only above this share of their HP
    prod_cap: int = 8  # banking: up to 8 Barracks in all (5 from the build)...
    spend_bank: int = 500  # ...with 500+ minerals...
    spend_time: float = 300.0  # ...from 5:00
    supply_per_prod: int = 3  # Reactor Barracks make two units at once
    ahead_ratio: float = 1.3  # demo 15: wave 2 (16 units) walked in alone after the first push died at ~7:25
    retreat_ratio: float = 0.7
    supply_max_pending: int = 3
    research: tuple[str, ...] = ("Stimpack", "ShieldWall", "PunisherGrenades")  # Tech Labs, in order
    ebay_research: tuple[str, ...] = (  # Engineering Bay; level 2 waits for the Armory
        "TerranInfantryWeaponsLevel1",
        "TerranInfantryArmorsLevel1",
        "TerranInfantryWeaponsLevel2",
        "TerranInfantryArmorsLevel2",
    )
    build: tuple[Step, ...] = steps(
        (14, "SupplyDepot", 1),  # ramp wall
        (16, "Barracks", 1),  # ramp wall
        (16, "Refinery", 1),
        (19, "Reaper", 1),
        (19, "OrbitalCommand", 1),
        (20, "CommandCenter", 2),  # natural, ~1:45
        (20, "SupplyDepot", 2),  # ramp wall
        (20, "Refinery", 2),  # 2nd gas with the natural: one gas starved the Reaper, Factory and Reactor (demo 14)
        (21, "Factory", 1),
        # Barracks 2-3 before the gas steps: steps go in order, and the Reactor/Starport waiting on gas held them
        # back while 440-575 minerals piled up at 3:00 (demos 16, 17)
        (22, "Barracks", 2),
        (22, "Barracks", 3),
        (22, "BarracksReactor", 1),
        (23, "Starport", 1),
        (32, "Refinery", 3),
        (34, "EngineeringBay", 1),
        (38, "Barracks", 4),
        (38, "Barracks", 5),
        (40, "Refinery", 4),
    )


@dataclass(frozen=True)
class SkytossConfig(BaseConfig):
    """2-base Protoss Skytoss macro (not a cheese): Nexus at the natural, Stargate ~3:00 with Void Rays, Shield
    Batteries at the natural, 3 Stargates, Fleet Beacon ~5:30-6:00, then Carriers (chrono-boosted), +1/+2 air
    weapons; first attack with 6 Carriers or ~80 army supply (or 10:00), then waves. Gateway units only defend."""

    worker_cap: int = 44  # 2 saturated bases: 2 x 16 on minerals + 4 x 3 on gas
    expand_send: int = 250  # a Probe walks to the natural at this bank (once the Gateway is placed)
    defenders: int = 2  # Gateway units for early defence once the Core is done: an Adept, then Stalkers
    zealot_bank: int = 800  # later, Gateways make Zealots only from this mineral bank (or while the base is attacked)
    void_rays: int = 5  # Void Rays before the Fleet Beacon is done (then the gas is saved for Carriers)
    phoenixes: int = 2  # Phoenixes, only once enemy air (Banshee, Medivac, Oracle, Mutalisk...) has been seen
    carriers: int = 16  # Carriers once the Fleet Beacon is done; past this, Void Rays again
    chrono: bool = True  # Chrono Boost by chrono_order
    chrono_order: tuple[str, ...] = ("Stargate", "Nexus", "CyberneticsCore", "Gateway")
    air_research: tuple[str, ...] = (  # Cybernetics Core, in order, once the first Stargate is placed (level 2: Beacon)
        "ProtossAirWeaponsLevel1",
        "ProtossAirArmorsLevel1",
        "ProtossAirWeaponsLevel2",
        "ProtossAirArmorsLevel2",
    )
    first_wave: int = 6  # units at home for the first attack (Carriers / Void Rays / Gateway units)...
    push_carriers: int = 6  # ...and the fleet: this many Carriers (core.fleet_ready)...
    push_supply: int = 80  # ...or this much army supply at home...
    attack_time: float = 600.0  # ...or 10:00 with at least min_wave units
    min_wave: int = 4
    wave_size: int = 4  # later waves: at least this many units...
    wave_supply: int = 30  # ...and this much army supply at home (5 Carriers)
    gas_bank: int = 500  # from spend_time, this much gas adds Stargates...
    prod_cap: int = 5  # ...up to this many Stargates in all (3 from the build)
    spend_time: float = 420.0
    place_give_up: float = 20.0  # a build step whose placement keeps failing stops holding the bank after this long
    place_fail_spends: bool = False  # Skytoss keeps its own answer (place_give_up: holds 20 s while a Pylon makes room)
    supply_per_prod: int = 3  # Stargate units are 2-6 supply
    ahead_ratio: float = 1.3
    retreat_ratio: float = 0.7
    build: tuple[Step, ...] = steps(
        (14, "Pylon", 1),
        (16, "Gateway", 1),
        (17, "Assimilator", 1),
        (19, "Nexus", 2),  # natural, ~1:30
        (20, "CyberneticsCore", 1),
        (20, "Assimilator", 2),
        (21, "Pylon", 2),  # at the natural (powers the Shield Batteries)
        (23, "Stargate", 1),  # ~2:45-3:00, as soon as the Core is done
        (28, "Assimilator", 3),
        (30, "Assimilator", 4),
        (32, "ShieldBattery", 1),  # natural
        (34, "ShieldBattery", 2),
        (38, "Stargate", 2),
        (44, "FleetBeacon", 1, 330.0),  # not before 5:30
        (50, "Stargate", 3, 360.0),
    )


CONFIGS: dict[str, type[BaseConfig]] = {
    "sixrax": SixRaxConfig,
    "dt": DtRushConfig,
    "proxy": ProxyRaxConfig,
    "mmm": MmmConfig,
    "skytoss": SkytossConfig,
}

# run.py --preset NAME: --set values applied before the explicit --set ones (which win). "baseline" = the bot of
# commit ba15df0, which beat Insane Zerg twice (Thunderbird, Ephemeron): every change since is switched off, so
# Dickie can add them back one at a time (2026-10-31). A key a bot doesn't have is skipped for that bot.
PRESETS: dict[str, tuple[str, ...]] = {
    "baseline": (
        "reaper_wait=0",  # no wait after backing off from a crowd
        "reaper_hideout=false",  # back off straight away from the threat, heal where it stops
        "medivac_wave=false",  # Medivacs follow the closest bio
        "viking_leash=0",  # Vikings attack-move with the wave, diving in first
        "wall_scv=false",  # only units with orders that aren't gathering open the wall
        "recall=0",  # a sent wave never comes home
        "prod_clear=false",  # off-grid production: python-sc2's spiral search, anywhere
        "depot_clear=false",  # depots may land on add-on squares
        "starport_reactor=false",  # Starports stay bare
        "wall_hold=0",  # the wall shuts the step the last passer is out of range
        "gas_off=0",  # gas SCVs never moved to minerals
        "third_time=0",  # no third Command Center (and the SCV cap stays worker_cap)
        "third_supply=0",  # ... not at 100 supply either
        "third_fly=false",  # a third (if switched on) is built at the spot by an SCV
        "third_stage=0",  # the waiting army stays at the usual stage point
        "bunker=false",  # no Bunker (already the default)
        "place_fail_spends=false",  # an affordable step that fails to place holds the bank
        "scan_cloak=false",  # MULEs at 50 energy, no Scanner Sweep
        "cloak_turrets=false",  # no Missile Turrets
    ),
}
