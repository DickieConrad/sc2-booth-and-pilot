"""Order-trace characterization of the original BioMicro (stdlib only, no SC2).

The golden traces were recorded and checked against micro.py before extraction.
Orders preserve global issuance order, unit-vs-point targets and step boundaries.
Fake orders do not update the observation: SC2 observations change between steps.
Run directly or via ci.sh -> tools/checks.py tests.
"""

import math
import sys
from dataclasses import replace
from enum import Enum, auto
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import BaseConfig  # noqa: E402


class Kind(Enum):
    MARINE = auto()
    MARAUDER = auto()
    BANELING = auto()
    BANELINGCOCOON = auto()
    ZERGLING = auto()
    ULTRALISK = auto()
    BROODLING = auto()
    ZEALOT = auto()
    DARKTEMPLAR = auto()
    SCV = auto()
    PROBE = auto()
    DRONE = auto()
    MEDIVAC = auto()
    VIKINGFIGHTER = auto()
    OVERLORD = auto()
    ROACH = auto()
    MULE = auto()  # micro/scan_cloak.py (off with BaseConfig)
    ORBITALCOMMAND = auto()


class Point:
    def __init__(self, xy):
        self.x, self.y = xy

    def towards(self, other, distance):
        dx, dy = other.x - self.x, other.y - self.y
        length = math.hypot(dx, dy)
        return Point((self.x + dx / length * distance, self.y + dy / length * distance))


class Unit:
    def __init__(self, tag, kind, x, y=0, **attrs):
        self.tag, self.type_id, self.position = tag, kind, Point((x, y))
        self.weapon_cooldown = 0
        self.radius = 0.5
        self.ground_range = 5
        self.can_attack = self.can_attack_ground = True
        self.can_be_attacked = True
        self.is_structure = self.is_flying = self.is_attacking = False
        self.order_target = None
        self.health, self.shield = 40, 0
        self.orders = []
        self.__dict__.update(attrs)

    def distance_to(self, other):
        p = other.position if isinstance(other, Unit) else other
        return math.hypot(self.position.x - p.x, self.position.y - p.y)

    def target_in_range(self, other):
        return self.distance_to(other) <= self.ground_range + self.radius + other.radius

    def record(self, action, target):
        dest = ('unit', target.tag) if isinstance(target, Unit) else (
            'point', round(target.x, 6), round(target.y, 6))
        self.orders.append((self.tag, action, dest))

    def attack(self, target):
        self.record('attack', target)

    def move(self, target):
        self.record('move', target)


class Units(list):
    def __call__(self, kind):
        return self.of_type(kind)

    def of_type(self, kinds):
        if isinstance(kinds, Kind):
            kinds = {kinds}
        return self.filter(lambda u: u.type_id in kinds)

    def filter(self, predicate):
        return Units(u for u in self if predicate(u))

    def tags_in(self, tags):
        return self.filter(lambda u: u.tag in tags)

    def tags_not_in(self, tags):
        return self.filter(lambda u: u.tag not in tags)

    @property
    def tags(self):
        return {u.tag for u in self}

    def closest_to(self, unit):
        return min(self, key=lambda u: u.distance_to(unit))

    def closest_distance_to(self, unit):
        return self.closest_to(unit).distance_to(unit)

    def closer_than(self, distance, unit):
        return self.filter(lambda u: u.distance_to(unit) < distance)

    def take(self, count):
        return Units(self[:count])

    def find_by_tag(self, tag):
        return next((u for u in self if u.tag == tag), None)


# Only the import surface used by micro is stubbed; core/config remain the real code.
_stubs = {name: ModuleType(name) for name in
          ('sc2', 'sc2.ids', 'sc2.ids.ability_id', 'sc2.ids.unit_typeid', 'sc2.position', 'sc2.unit', 'sc2.units')}
_stubs['sc2.ids.ability_id'].AbilityId = Enum('AbilityId', 'SCANNERSWEEP_SCAN')
_stubs['sc2.ids.unit_typeid'].UnitTypeId = Kind
_stubs['sc2.position'].Point2 = Point
_stubs['sc2.unit'].Unit = Unit
_stubs['sc2.units'].Units = Units
with patch.dict(sys.modules, _stubs):
    from micro import BioMicro


class Bot:
    def __init__(self, units, enemies=(), **settings):
        self.cfg = replace(BaseConfig(), **settings)
        self.units, self.enemy_units = Units(units), Units(enemies)
        self.retreating, self.sent = set(), set()
        self.hold, self.time = False, 0.0
        self.orders = []
        self.path = lambda p: True
        for unit in self.units:
            unit.orders = self.orders
        self.micro = BioMicro(self)

    def rally_point(self):
        return Point((-20, 0))

    def in_map_bounds(self, p):
        return True

    def in_pathing_grid(self, p):
        return self.path(p)

    def step(self, time=0):
        self.time = time
        self.orders.clear()
        self.micro.step()
        return list(self.orders)


def marine(tag=1, x=0, **attrs):
    return Unit(tag, Kind.MARINE, x, **attrs)


def bane(tag=10, x=4, **attrs):
    return Unit(tag, Kind.BANELING, x, ground_range=0.1, **attrs)


def medivac(tag=2, x=15):
    return Unit(tag, Kind.MEDIVAC, x, is_flying=True)


def viking(tag=3, x=12):
    return Unit(tag, Kind.VIKINGFIGHTER, x, is_flying=True)


def traces():
    out = {}
    b = Bot([marine(), medivac(), viking()])
    b.micro.split_since[99] = 1
    b.micro.stepped.add(99)
    out['no_enemies_escorts'] = [b.step()]
    assert not b.micro.split_since and not b.micro.stepped

    for mode in ('far_wave', 'dead_wave', 'closest_bio'):
        b = Bot([marine(), marine(4, 18), medivac(x=20)], medivac_wave=mode != 'closest_bio')
        b.sent = {2} if mode == 'dead_wave' else {1, 2}
        out[mode] = [b.step()]
    b = Bot([medivac(), viking()])
    out['no_bio_home'] = [b.step()]
    b = Bot([medivac(x=-18), viking(x=-18)])
    out['already_home'] = [b.step()]

    for label, cover, x in [('viking_no_cover', False, 12), ('viking_cover', True, 12),
                             ('viking_twice_leash', True, 15)]:
        air = Unit(10, Kind.OVERLORD, 8 if cover else 30, is_flying=True, can_attack=False)
        b = Bot([marine(), viking(x=x)], [air])
        out[label] = [b.step()]

    b = Bot([marine(1, weapon_cooldown=10)], [bane(x=7)], kite_range=0)
    out['split_rhythm'] = [b.step(t) for t in (0, 1.49, 1.5, 4.49, 4.5)]
    b.enemy_units[0].position = Point((9, 0))
    out['split_rhythm'].append(b.step(5))
    assert not b.micro.split_since
    b.enemy_units[0].position = Point((7, 0))
    out['split_rhythm'].append(b.step(5.1))
    b = Bot([marine()], [bane()])
    out['shoot_bane'] = [b.step()]
    b = Bot([marine(weapon_cooldown=10), marine(2, 1, weapon_cooldown=10)], [bane()], kite_range=0)
    out['split_mates'] = [b.step()]
    b = Bot([marine(weapon_cooldown=10)], [bane(x=4)])
    b.path = lambda p: False
    out['blocked_split_fight'] = [b.step()]
    # A failed split fights inline, before the next splitter; regular fighters follow both.
    b = Bot([marine(1, -1, weapon_cooldown=10), marine(2, weapon_cooldown=10),
             Unit(3, Kind.MARAUDER, 0)], [bane()], kite_range=0)
    b.path = lambda p: p.x > -0.5
    out['inline_fallback_order'] = [b.step()]
    b = Bot([marine(weapon_cooldown=10)], [bane(x=7)])
    b.path = lambda p: p.y < 0
    out['turned_split'] = [b.step()]
    b = Bot([marine()], [Unit(10, Kind.BANELINGCOCOON, 4, can_attack=False)])
    out['cocoon_split'] = [b.step()]

    b = Bot([marine()], [Unit(10, Kind.ZERGLING, 3, ground_range=0.1)], split_radius=0)
    out['kite_cooldowns'] = []
    for cd in (10, 4, 3.5, 0):
        b.units[0].weapon_cooldown = cd
        out['kite_cooldowns'].append(b.step())
    b.units[0].weapon_cooldown = 10
    b.step()
    b.units[0].weapon_cooldown = 0
    b.enemy_units[0].position = Point((8, 0))
    out['kite_reengage'] = [b.step()]
    b = Bot([marine(weapon_cooldown=10)], [Unit(10, Kind.ROACH, 3)])
    out['ranged_no_kite'] = [b.step()]

    harmless = Unit(13, Kind.OVERLORD, 1, health=1, can_attack=False)
    b = Bot([marine()], [harmless, Unit(12, Kind.ROACH, 3, health=5, shield=5),
                        Unit(11, Kind.ROACH, 4, health=9), bane()], split_radius=0)
    out['focus_tiers'] = [b.step()]
    b.enemy_units = b.enemy_units.tags_not_in({10})
    out['focus_tiers'].append(b.step())
    b.units[0].is_attacking, b.units[0].order_target = True, 12
    out['focus_tiers'].append(b.step())
    b.enemy_units = Units([harmless])
    out['focus_tiers'].append(b.step())

    for mode in ('micro_off', 'hold', 'retreating'):
        b = Bot([marine(), medivac(), viking()], [bane()], micro=mode != 'micro_off')
        b.hold = mode == 'hold'
        b.retreating = {1, 2, 3} if mode == 'retreating' else set()
        b.micro.split_since[99] = 1
        b.micro.stepped.add(99)
        out[mode] = [b.step()]
        if mode != 'retreating':
            assert b.micro.split_since == {99: 1} and b.micro.stepped == {99}

    for cap in (1, 0, -1):
        b = Bot([marine(2), marine(1), medivac(3)], [bane()], micro_cap=cap)
        out[f'cap_{cap}'] = [b.step()]
    b = Bot([marine(), marine(4, 30), medivac(x=18), medivac(3, 21)], [bane()])
    out['leash_reach_override'] = [b.step()]
    b = Bot([marine(), medivac(), viking()], [bane()], medivac_leash=0, viking_leash=0,
            split_radius=0, kite_range=0)
    out['settings_disabled'] = [b.step()]
    b = Bot([marine(), marine(2, 100)], [bane(can_be_attacked=False), bane(11, is_structure=True)])
    out['enemy_filter'] = [b.step()]
    b = Bot([marine(1, 10), marine(2, 11), marine(3, 9)], [bane(x=0)], split_radius=0)
    out['radius_boundary'] = [b.step()]
    # State is trimmed when a unit leaves the fight and reset when enemies disappear.
    b = Bot([marine(weapon_cooldown=10)], [bane(x=7)], kite_range=0)
    out['state_cleanup'] = [b.step()]
    b.units[0].position = Point((100, 0))
    out['state_cleanup'].append(b.step(1))
    assert not b.micro.split_since and not b.micro.stepped
    b.units[0].position = Point((0, 0))
    out['state_cleanup'].append(b.step(2))
    b.enemy_units.clear()
    out['state_cleanup'].append(b.step(3))
    assert not b.micro.split_since and not b.micro.stepped
    return out


# Filled from the original micro.py, then frozen before the package extraction.
GOLDEN = {'no_enemies_escorts': [[(2, 'attack', ('point', 0, 0)), (3, 'move', ('point', -2.0, 0.0))]],
 'far_wave': [[(2, 'attack', ('point', 0, 0))]],
 'dead_wave': [[(2, 'move', ('point', -20, 0))]],
 'closest_bio': [[]],
 'no_bio_home': [[(2, 'move', ('point', -20, 0)), (3, 'move', ('point', -20, 0))]],
 'already_home': [[]],
 'viking_no_cover': [[(3, 'move', ('point', -2.0, 0.0))]],
 'viking_cover': [[]],
 'viking_twice_leash': [[(3, 'move', ('point', -2.0, 0.0))]],
 'split_rhythm': [[(1, 'move', ('point', -3.0, 0.0))],
                  [(1, 'move', ('point', -3.0, 0.0))],
                  [(1, 'attack', ('point', 7, 0))],
                  [],
                  [(1, 'move', ('point', -3.0, 0.0))],
                  [(1, 'attack', ('point', 9, 0))],
                  [(1, 'move', ('point', -3.0, 0.0))]],
 'shoot_bane': [[(1, 'attack', ('unit', 10))]],
 'split_mates': [[(1, 'move', ('point', -3.0, 0.0)), (2, 'move', ('point', -2.0, 0.0))]],
 'blocked_split_fight': [[(1, 'attack', ('unit', 10))]],
 'inline_fallback_order': [[(1, 'attack', ('unit', 10)),
                            (2, 'move', ('point', -0.0, -3.0)),
                            (3, 'attack', ('unit', 10))]],
 'turned_split': [[(1, 'move', ('point', -2.12132, -2.12132))]],
 'cocoon_split': [[(1, 'move', ('point', -3.0, 0.0))]],
 'kite_cooldowns': [[(1, 'move', ('point', -2.0, 0.0))],
                    [(1, 'move', ('point', -2.0, 0.0))],
                    [(1, 'attack', ('unit', 10))],
                    [(1, 'attack', ('unit', 10))]],
 'kite_reengage': [[(1, 'attack', ('point', 8, 0))]],
 'ranged_no_kite': [[(1, 'attack', ('unit', 10))]],
 'focus_tiers': [[(1, 'attack', ('unit', 10))],
                 [(1, 'attack', ('unit', 11))],
                 [],
                 [(1, 'attack', ('unit', 13))]],
 'micro_off': [[]],
 'hold': [[]],
 'retreating': [[]],
 'cap_1': [[(3, 'attack', ('point', 0, 0)), (2, 'attack', ('unit', 10)), (3, 'attack', ('point', 0, 0))]],
 'cap_0': [[(3, 'attack', ('point', 0, 0))]],
 'cap_-1': [[(3, 'attack', ('point', 0, 0))]],
 'leash_reach_override': [[(2, 'attack', ('point', 30, 0)),
                           (3, 'attack', ('point', 30, 0)),
                           (1, 'attack', ('unit', 10)),
                           (2, 'attack', ('point', 0, 0))]],
 'settings_disabled': [[(1, 'attack', ('unit', 10))]],
 'enemy_filter': [[]],
 'radius_boundary': [[]],
 'state_cleanup': [[(1, 'move', ('point', -3.0, 0.0))], [], [(1, 'move', ('point', -3.0, 0.0))], []]}


def test_golden_orders():
    actual = traces()
    assert actual.keys() == GOLDEN.keys()
    for name, expected in GOLDEN.items():
        assert actual[name] == expected, f'{name}: {actual[name]!r} != {expected!r}'


if __name__ == '__main__':
    test_golden_orders()
    print(f'  test_micro: {len(GOLDEN)} scenarios, '
          f'{sum(len(steps) for steps in GOLDEN.values())} steps, '
          f'{sum(len(orders) for steps in GOLDEN.values() for orders in steps)} orders ok')
