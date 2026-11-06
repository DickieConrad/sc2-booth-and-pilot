"""Sparring-bot logic tests: steering file, build-order steps, configs, overrides, maps and seats.
Stdlib only (no burnysc2, no StarCraft II); run by ci.sh via tools/checks.py tests, or: python3 bot/test_core.py"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import core  # noqa: E402
from core import Command, CommandReader, Step, next_step, opening_done, parse_command  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="sparring_test_"))


# ------------------------------------------------------------------ steering file
def test_parse_values():
    assert parse_command('{"cheese": "now"}') == Command(cheese=True)
    assert parse_command('{"attack": "NOW", "seq": 5}') == Command(attack=True, seq=5)
    assert parse_command('{"stop": true}').stop
    assert parse_command('{"resume": 1, "gg": "yes"}') == Command(resume=True, gg=True)
    assert parse_command('{"cheese": false, "attack": "later", "stop": 0}').empty
    assert parse_command('{"unknown": 1}').empty


def test_parse_bad_input():
    assert parse_command("{not json") is None
    assert parse_command("[1, 2]") is None
    assert parse_command("") is None
    assert parse_command('{"seq": true, "attack": true}').seq is None
    assert parse_command('{"seq": "12", "attack": true}').seq is None


def test_describe():
    assert Command(cheese=True, attack=True).describe() == "cheese attack"
    assert Command().describe() == "nothing"


def reader(name, interval=2.0):
    path = TMP / f"{name}.json"
    return path, CommandReader(path, interval)


def test_reader_missing_file():
    _, r = reader("missing")
    r.prime()
    assert r.poll(0) is None


def test_reader_new_command_once():
    path, r = reader("once")
    r.prime()
    path.write_text('{"attack": "now"}')
    assert r.poll(10) == Command(attack=True)
    assert r.poll(20) is None  # same text: already handled


def test_reader_interval():
    path, r = reader("interval", interval=5)
    r.prime()
    assert r.poll(0) is None  # nothing yet; next read at 5
    path.write_text('{"stop": true}')
    assert r.poll(3) is None  # too soon
    assert r.poll(5) == Command(stop=True)


def test_reader_prime_ignores_stale():
    path, r = reader("stale")
    path.write_text('{"cheese": "now", "seq": 100}')
    r.prime()
    assert r.poll(0) is None
    path.write_text('{"cheese": "now", "seq": 99}')  # older seq: ignored
    assert r.poll(10) is None
    path.write_text('{"cheese": "now", "seq": 101}')
    assert r.poll(20) == Command(cheese=True, seq=101)


def test_reader_bad_json_then_good():
    path, r = reader("partial")
    r.prime()
    path.write_text('{"attack": ')
    assert r.poll(0) is None
    path.write_text('{"attack": "now"}')
    assert r.poll(10) == Command(attack=True)


def test_reader_empty_command():
    path, r = reader("empty")
    r.prime()
    path.write_text('{"seq": 5}')
    assert r.poll(0) is None


def test_write_command_roundtrip():
    path = TMP / "sub" / "command.json"
    data = core.write_command(path, 123, cheese=True, attack=False)
    assert data == {"seq": 123, "cheese": True}
    assert json.loads(path.read_text()) == data
    assert parse_command(path.read_text()) == Command(cheese=True, seq=123)
    assert not list(path.parent.glob("*.tmp"))
    try:
        core.write_command(path, 1, nuke=True)
    except ValueError as e:
        assert "nuke" in str(e)
    else:
        raise AssertionError("unknown action accepted")


def test_write_command_retries_a_locked_rename():
    # Windows: replace() raises PermissionError while the bot has command.json open for its read
    path = TMP / "locked" / "command.json"
    real, calls, tmps = Path.replace, [], []

    def flaky(self, target):
        calls.append(target)
        tmps.append(self.name)
        if len(calls) <= 2:
            raise PermissionError("in use")
        return real(self, target)

    Path.replace = flaky
    try:
        assert core.write_command(path, 7, stop=True, wait=0) == {"seq": 7, "stop": True}
        assert len(calls) == 3 and json.loads(path.read_text())["seq"] == 7
        calls.clear()
        core.write_command(path, 8, stop=True, wait=0)   # a second call: its own temp name, not the first's
        assert tmps[0] != tmps[-1] and tmps[0].endswith(".tmp")
        calls.clear()
        Path.replace = lambda self, target: (_ for _ in ()).throw(PermissionError("still in use"))
        try:
            core.write_command(path, 9, stop=True, retries=2, wait=0)
        except PermissionError:
            pass
        else:
            raise AssertionError("a rename that never works must still fail")
    finally:
        Path.replace = real
    assert json.loads(path.read_text())["seq"] == 8 and not list(path.parent.glob("*.tmp"))   # no temp left behind


def test_writer_then_reader():
    path, r = reader("pair")
    r.prime()
    core.write_command(path, 1, attack=True)
    assert r.poll(0) == Command(attack=True, seq=1)
    core.write_command(path, 2, attack=True)  # same action again, new seq
    assert r.poll(10) == Command(attack=True, seq=2)


# ------------------------------------------------------------------ build orders
ORDER = core.steps((14, "SupplyDepot", 1), (15, "Barracks", 1), (16, "Barracks", 2, 60), (16, "Refinery", 1))


def test_next_step_supply_gate():
    assert next_step(ORDER, 13, {}) is None
    assert next_step(ORDER, 14, {}) == Step(14, "SupplyDepot", 1)


def test_next_step_in_order():
    counts = {"SupplyDepot": 1}
    assert next_step(ORDER, 15, counts) == Step(15, "Barracks", 1)
    counts["Barracks"] = 1
    assert next_step(ORDER, 16, counts, now=30) is None  # time gate 60 holds everything after it
    assert next_step(ORDER, 16, counts, now=60) == Step(16, "Barracks", 2, 60)
    counts["Barracks"] = 2
    assert next_step(ORDER, 16, counts, now=60) == Step(16, "Refinery", 1)
    counts["Refinery"] = 1
    assert next_step(ORDER, 30, counts, now=99) is None
    assert opening_done(ORDER, counts)


def test_next_step_rebuilds_lost():
    counts = {"SupplyDepot": 0, "Barracks": 2, "Refinery": 1}  # depot died
    assert next_step(ORDER, 20, counts) == Step(14, "SupplyDepot", 1)
    assert not opening_done(ORDER, counts)


def test_gate_supply_when_workers_stop():
    dt = config.DtRushConfig()
    done = {"Pylon": 1, "Gateway": 1, "Assimilator": 2}
    # "cheese now" at 18 supply: no more Probes, so the 19-supply Cyber Core would wait forever
    assert next_step(dt.build, 18, done) is None
    sup = core.gate_supply(18, True, 18, dt.worker_cap, training=False)
    assert next_step(dt.build, sup, done) == Step(19, "CyberneticsCore", 1)
    # defenders=0: 22 Probes (the cap), nothing training, below the 23-supply Dark Shrine
    done = {s.item: s.count for s in dt.build[:8]}
    assert next_step(dt.build, 22, done) is None
    assert next_step(dt.build, core.gate_supply(22, False, 22, dt.worker_cap, False), done) == Step(23, "DarkShrine", 1)
    # still growing (workers below the cap, or army training): the real supply gates as before
    assert core.gate_supply(16, False, 15, 22, False) == 16
    assert core.gate_supply(22, False, 22, 22, training=True) == 22


def test_known_orders_skip_new_ability_ids():
    # a newer SC2 client gave a Probe an order with ability 4135, unknown to python-sc2 7.3.0's tables
    class Order:
        def __init__(self, ability_id):
            self.ability_id = ability_id

    orders = [Order(295), Order(4135), Order(1)]
    known = {1: "Smart", 295: "Gather"}
    assert [o.ability_id for o in core.known_orders(orders, known)] == [295, 1]
    assert core.known_orders([], known) == []


# ------------------------------------------------------------------ macro: supply ahead, spending, gateway mix, gg
def test_supply_margin_scales_with_production():
    assert core.supply_margin(0, 1) == 4
    assert core.supply_margin(6, 1) == 16  # 6-rax: 2 + 2*6 + 2*1
    assert core.supply_margin(6, 1, base=4, per_prod=3, per_townhall=0) == 22
    assert core.supply_margin(-1, -1) == 2
    assert [core.supply_pending_cap(p) for p in (0, 2, 3, 6, 9, 30)] == [1, 1, 2, 3, 3, 3]
    assert core.supply_pending_cap(9, hard_cap=5) == 4 and core.supply_pending_cap(9, hard_cap=0) == 1


def test_need_supply():
    six = core.supply_margin(6, 1)  # 16
    cap = core.supply_pending_cap(6)  # 3
    # the coach's blocks: 31/31, 47/47 with six Barracks and nothing under way -> build
    assert core.need_supply(0, 31, 0, six, cap)
    assert core.need_supply(15, 47, 0, six, cap)  # 15 free < 16: build before the block, not at it
    assert not core.need_supply(16, 47, 0, six, cap)
    # a depot under way counts as 8 free: 10 + 8 = 18 >= 16, no second one yet; 4 + 8 = 12 < 16, a second one
    assert not core.need_supply(10, 47, 1, six, cap)
    assert core.need_supply(4, 47, 1, six, cap)
    assert not core.need_supply(0, 47, 3, six, cap)  # pending cap
    assert not core.need_supply(0, 200, 0, six, cap)  # maxed
    assert core.need_supply(3, 23, 0, core.supply_margin(1, 1), 1)  # one Gateway: margin 6


def test_want_production():
    wp = core.want_production
    assert not wp(1500, 300, False, 3, 0, 6, 400)  # opening not done: the build order spends
    assert wp(400, 300, True, 3, 0, 6, 400)
    assert not wp(399, 300, True, 3, 0, 6, 400)  # below the bank
    assert not wp(1500, 300, True, 6, 0, 6, 400)  # at the cap (done + in progress)
    assert not wp(500, 300, True, 3, 1, 6, 400)  # one already under way...
    assert wp(800, 300, True, 3, 1, 6, 400)  # ...two once the bank is double
    assert not wp(5000, 300, True, 3, 2, 6, 400)  # never more than two at a time
    assert not wp(1500, 200, True, 2, 0, 5, 400, after=240)  # proxy: not before 4:00
    assert wp(1500, 240, True, 2, 0, 5, 400, after=240)
    assert not wp(1500, 300, True, 3, 0, 6, 0)  # bank 0 = off


def test_gateway_unit():
    gu = core.gateway_unit
    # Shrine not done: gas stays for the tech; Zealots only from a bank
    assert gu(399, 300, False, True, 0, 3, 400) is None
    assert gu(450, 300, False, True, 0, 3, 400) == "Zealot"
    # Shrine done: a DT whenever affordable
    assert gu(125, 125, True, True, 0, 3, 400) == "DarkTemplar"
    assert gu(2000, 125, True, True, 9, 3, 400) == "DarkTemplar"
    # fewer DTs than hunters: gas waits for the next DT, minerals over the bank make Zealots
    assert gu(300, 100, True, True, 1, 3, 400) is None
    assert gu(500, 100, True, True, 1, 3, 400) == "Zealot"
    # hunters out: the bank goes into the mix, rotating from `turn`, gas units only when affordable
    assert gu(300, 100, True, True, 3, 3, 400, turn=0) == "Zealot"
    assert gu(300, 100, True, True, 3, 3, 400, turn=1) == "Stalker"
    assert gu(300, 100, True, True, 3, 3, 400, turn=2) == "Adept"
    assert gu(110, 30, True, True, 3, 3, 400, turn=1) == "Adept"  # no Stalker money: next in the rotation
    assert gu(110, 0, True, True, 3, 3, 400, turn=1) == "Zealot"
    assert gu(300, 100, True, False, 3, 3, 400, turn=1) == "Zealot"  # no Cyber Core: Zealots only
    assert gu(90, 500, True, True, 3, 3, 400) is None
    assert set(core.GATEWAY_MIX) | {"DarkTemplar"} == set(core.GATEWAY_COST)


def test_staging_point():
    home, target, proxy = (20.0, 20.0), (120.0, 120.0), (95.0, 100.0)
    # ProxyRax with a proxy Barracks standing: the army waits at the proxy (not at home: no double map crossing)
    assert core.staging_point(home, target, proxy, proxy_alive=True, forward=0.4) == proxy
    # proxy gone: stage_forward of the way out
    assert core.staging_point(home, target, proxy, proxy_alive=False, forward=0.4) == (60.0, 60.0)
    # the other bots (stage_forward 0): home, the waiting army defends
    assert core.staging_point(home, target) == home
    assert core.staging_point(home, target, None, True, 0.5) == (70.0, 70.0)  # no proxy spot: the fraction
    assert core.staging_point(home, target, forward=2.0) == target  # clamped to 0..1
    assert core.staging_point(home, target, forward=-1) == home
    assert config.ProxyRaxConfig().stage_forward > 0 and config.SixRaxConfig().stage_forward == 0
    assert config.ProxyRaxConfig().spend_time < config.ProxyRaxConfig().home_rax_time  # proxy: spend the bank early


def test_has_lost():
    hl = core.has_lost
    assert not hl(0, 1, 12)  # game start
    assert not hl(200, 0, 8)  # no townhall but an army: still playing
    assert hl(200, 0, 4)  # no townhall, under 5 units
    assert not hl(359, 1, 3)  # before 6:00 a base with few units isn't lost (proxy SCVs out, early losses)
    assert hl(360, 1, 5) and not hl(360, 1, 6)  # from 6:00: under 6 army + workers
    assert hl(100, 0, 6, no_base_units=7) and not hl(400, 1, 3, gg_time=600)


def test_gg_due_debounce():
    due, since = core.gg_due(True, None, 100, confirm=5)
    assert not due and since == 100
    due, since = core.gg_due(True, since, 103, confirm=5)
    assert not due and since == 100
    assert core.gg_due(True, since, 105, confirm=5) == (True, 100)
    assert core.gg_due(False, since, 104, confirm=5) == (False, None)  # recovered: the clock restarts
    assert core.gg_due(True, None, 50, confirm=0) == (True, 50)  # no debounce


def test_parse_time_limit():
    assert core.parse_time_limit("10:00") == 600 and core.parse_time_limit("7:30") == 450
    assert core.parse_time_limit("90") == 90 and core.parse_time_limit(" 0:45 ") == 45
    assert core.parse_time_limit(None) is None and core.parse_time_limit("") is None
    for bad in ("0", "0:00", "-1:00", "abc", "5:xx", "nan", "inf", "99999:00"):
        try:
            core.parse_time_limit(bad)
        except ValueError as e:
            assert "--time-limit" in str(e)
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_describe_result():
    class R:  # stands in for sc2.data.Result
        def __init__(self, name):
            self.name = name

    assert core.describe_result([R("Victory")]) == "Victory"
    assert core.describe_result([R("Tie")], 600) == "Tie (time limit 10:00 reached: stopped, no winner)"
    assert core.describe_result([R("Tie")]) == "Tie"  # no limit: a real tie
    assert core.describe_result([R("Victory"), R("Defeat")], 600) == "Victory, Defeat"
    assert core.describe_result([RuntimeError("boom")]) == "RuntimeError: boom"
    assert core.describe_result([]) == "no result"
    # bot vs bot: both teams won = a desync (scan-dt-1, -6); one team, or teams unknown, is not
    v, d = R("Victory"), R("Defeat")
    assert core.desynced([v, v], [1, 2]) and not core.desynced([v, d], [1, 2]) and not core.desynced([v, v], [1, 1])
    assert not core.desynced([v, v], None) and not core.desynced([v], [1])
    assert core.describe_result([v, v], 1200, [1, 2]).startswith("Victory, Victory (DESYNC: both teams won")
    assert core.describe_result([v, d], 1200, [1, 2]) == "Victory, Defeat"


def test_new_settings_overridable():
    c = core.apply_overrides(config.DtRushConfig(), ["auto_gg=false", "gg_time=7:30", "prod_cap=8", "spend_bank=300",
                                                     "dt_hunters=2", "supply_per_prod=3", "gg_confirm=2"])
    assert (c.auto_gg, c.gg_time, c.prod_cap, c.spend_bank, c.dt_hunters, c.supply_per_prod, c.gg_confirm) == (
        False, 450.0, 8, 300, 2, 3, 2.0)
    for key, cls in config.CONFIGS.items():
        d = cls()
        assert d.auto_gg and d.spend_bank > 0 and d.supply_base >= 0 and d.supply_max_pending >= 1, key
        prod = max((s.count for s in d.build if s.item in ("Barracks", "Gateway")), default=0)
        assert d.prod_cap >= prod, key  # the cap never undercuts the build order


def test_run_time_limit_args():
    import run

    assert run.parse_args(["--vs", "easy", "--time-limit", "8:00"]).time_limit == "8:00"
    assert run.parse_args([]).time_limit is None
    base = ["--bot", "dt", "--map", "Simple64", "--dry-run"]
    assert run.main([*base, "--vs", "medium", "--time-limit", "8:00"]) == 0
    assert run.main([*base, "--vs", "human", "--time-limit", "8:00"]) == 2  # Dickie's games have no limit
    assert run.main([*base, "--vs", "medium", "--time-limit", "soon"]) == 2


def test_check_order():
    assert core.check_order(ORDER) == []
    bad = core.steps((15, "Barracks", 1), (14, "SupplyDepot", 1), (16, "Barracks", 1), (5, "pylon", 0))
    errs = " | ".join(core.check_order(bad))
    assert "supply goes down" in errs and "count does not rise" in errs
    assert "out of range" in errs and "count < 1" in errs and "CamelCase" in errs


def test_shipped_build_orders_valid():
    for key, cls in config.CONFIGS.items():
        cfg = cls()
        assert cfg.build, key
        assert core.check_order(cfg.build) == [], (key, core.check_order(cfg.build))


def test_shipped_configs_sane():
    for key, cls in config.CONFIGS.items():
        c = cls()
        assert 12 <= c.worker_cap <= 80, key
        assert 1 <= c.min_wave <= c.first_wave and c.wave_size >= 1, key
        assert 0 < c.attack_time < 1200 and c.poll_seconds > 0, key
    six = config.SixRaxConfig()
    assert six.build[-1].item == "Barracks" and six.build[-1].count == six.rax_count == 6
    assert all(s.item != "Refinery" for s in six.build)  # no gas
    dt = config.DtRushConfig()
    items = [s.item for s in dt.build]
    assert items.index("CyberneticsCore") < items.index("TwilightCouncil") < items.index("DarkShrine")
    assert dt.dt_wave >= 1 and dt.defenders >= 0 and dt.dt_hunters >= dt.dt_wave
    px = config.ProxyRaxConfig()
    assert sum(s.item == "Barracks" for s in px.build) == 2 and px.proxy_scv_time < 60


# ------------------------------------------------------------------ overrides
def test_overrides_types():
    c = core.apply_overrides(config.SixRaxConfig(), ["attack_time=3:15", "first_wave=10", "chat=false"])
    assert c.attack_time == 195.0 and c.first_wave == 10 and c.chat is False
    c = core.apply_overrides(config.ProxyRaxConfig(), ["proxy_distance=30", "proxy_scv_time=0:20"])
    assert c.proxy_distance == 30.0 and c.proxy_scv_time == 20.0
    assert config.SixRaxConfig().first_wave == 16  # original untouched


def test_overrides_errors():
    for pairs in (["nope=1"], ["first_wave"], ["chat=maybe"], ["build=1"], ["first_wave=ten"]):
        try:
            core.apply_overrides(config.SixRaxConfig(), pairs)
        except ValueError:
            continue
        raise AssertionError(f"accepted {pairs}")
    c = core.apply_overrides(config.DtRushConfig(), ["rax_count=8", "dt_wave=3"], ignore_unknown=True)
    assert c.dt_wave == 3


def test_mmss():
    assert core.mmss("3:30") == 210 and core.mmss("45") == 45 and core.mmss(12) == 12.0


# ------------------------------------------------------------------ maps
def test_resolve_map():
    d = TMP / "maps"
    (d / "ladder").mkdir(parents=True)
    f = d / "ladder" / "Equilibrium LE.SC2Map"
    f.write_bytes(b"x")
    assert core.resolve_map("equilibrium le", d) == f
    assert core.resolve_map("Equilibrium LE.SC2Map", d) == f
    assert core.resolve_map(None, d) == f  # the only map
    assert core.resolve_map(str(f), d) == f.resolve()
    assert core.resolve_map("Simple64", d) == "Simple64"  # not in the project: SC2's Maps folder
    (d / "Other.SC2Map").write_bytes(b"y")
    try:
        core.resolve_map(None, d)
    except ValueError as e:
        assert "Equilibrium LE" in str(e) and "Other" in str(e)
    else:
        raise AssertionError("ambiguous default map accepted")
    try:
        core.resolve_map(None, TMP / "nomaps")
    except ValueError as e:
        assert "none" in str(e)
    else:
        raise AssertionError("no maps and no --map accepted")


# ------------------------------------------------------------------ seats
def test_seats_1v1():
    s = core.plan_seats("sixrax", "human")
    assert [(x.kind, x.race, x.team) for x in s] == [("human", "terran", 1), ("bot", "terran", 2)]
    s = core.plan_seats("dt", "hard", ai_race="zerg")
    assert [(x.kind, x.race, x.team, x.difficulty) for x in s] == [
        ("bot", "protoss", 1, None),
        ("computer", "zerg", 2, "hard"),
    ]
    assert not core.needs_custom_runner(s)
    assert not core.needs_custom_runner(core.plan_seats("proxy", "human"))


def test_seats_2v2():
    s = core.plan_seats("sixrax", "human", two_v_two=True, bot2="dt")
    assert [(x.kind, x.race, x.team) for x in s] == [
        ("human", "terran", 1),
        ("computer", "protoss", 1),
        ("bot", "terran", 2),
        ("bot", "protoss", 2),
    ]
    assert s[1].difficulty == "hard" and s[3].bot == "dt"
    assert core.needs_custom_runner(s)
    s = core.plan_seats("proxy", "easy", two_v_two=True)
    assert [x.kind for x in s] == ["bot", "bot", "computer", "computer"] and s[1].bot == "proxy"
    assert core.needs_custom_runner(s)


def test_seats_errors():
    for kw in (
        {"bot": "zergling", "vs": "human"},
        {"bot": "dt", "vs": "insane"},
        {"bot": "dt", "vs": "human", "ally_difficulty": "godlike"},
        {"bot": "dt", "vs": "human", "two_v_two": True, "bot2": "x"},
    ):
        try:
            core.plan_seats(**kw)
        except ValueError:
            continue
        raise AssertionError(f"accepted {kw}")


def test_every_config_has_a_race():
    assert set(config.CONFIGS) == set(core.BOT_RACES)


def test_coach_steps():
    assert core.supply_sec(12) == 0 and core.supply_sec(14) == 18 and core.supply_sec(26) == 170
    assert core.item_name("CyberneticsCore") == "Cybernetics Core" and core.item_name("Barracks") == "Barracks"
    six = core.coach_steps(config.SixRaxConfig().build)
    assert [s["action"] for s in six] == ["Supply Depot", "Barracks", "Barracks #2", "Barracks #3", "Barracks #4",
                                          "Supply Depot #2", "Barracks #5", "Barracks #6"]
    secs = [s["sec"] for s in six]
    assert secs[:3] == [18, 33, 48] and all(b - a >= core.STEP_GAP for a, b in zip(secs, secs[1:]))
    assert all(s["t"] == core.clock(s["sec"]) and isinstance(s["supply"], int) for s in six)
    # the `at` time gate wins over the supply estimate
    gated = core.coach_steps(core.steps((14, "Pylon", 1, 100.0)))
    assert gated[0]["sec"] == 100 and gated[0]["t"] == "1:40"
    for key, cls in config.CONFIGS.items():   # every bot: a valid plan for the coach (in order, a cue per step)
        steps = core.coach_steps(cls().build)
        assert steps and [s["sec"] for s in steps] == sorted(s["sec"] for s in steps), key


def test_bot_game_file():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "out" / "bot_game.json"
        data = core.bot_game("DtRush", "protoss", "dt", "Dark Templar rush (Protoss)", config.DtRushConfig().build,
                             1793400000.5)
        core.write_json_atomic(f, data)
        back = json.loads(f.read_text(encoding="utf-8"))
        assert back == data and back["plan"]["name"] == "DtRush: Dark Templar rush (Protoss)"
        assert back["plan"]["steps"][-1]["action"] == "Gateway #3"
        assert not list(f.parent.glob("*.tmp"))
        assert core.clear_bot_game(f, 1.0) is False and f.exists()   # another run's announcement: kept
        assert core.clear_bot_game(f, 1793400000.5) is True and not f.exists()
        assert core.clear_bot_game(f, 1793400000.5) is False   # already gone


def test_announce():
    """run.announce: the first bot seat's in-game name (the player python-sc2 named) and its build go to the file."""
    import run

    class FakePlayer:
        def __init__(self, name):
            self.name = name

    seats = core.plan_seats("dt", "medium", two_v_two=True, bot2="sixrax")
    players = [FakePlayer("DtRush"), FakePlayer("SixRax"), FakePlayer(None), FakePlayer(None)]
    configs = {"dt": config.DtRushConfig(), "sixrax": config.SixRaxConfig()}
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "bot_game.json"
        written = run.announce(seats, players, configs, f)
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["written_at"] == written and (data["player"], data["race"], data["bot"]) == ("DtRush", "protoss",
                                                                                                    "dt")
        assert data["plan"]["name"] == "DtRush: Dark Templar rush (Protoss)"
        assert data["plan"]["steps"] == core.coach_steps(configs["dt"].build)


def test_bot_version():
    """run.git_commit: a plain repo (loose ref, packed ref, detached HEAD) and a worktree whose .git file points at
    the main repo's .git/worktrees/<name> by an absolute path. run.bot_version hashes bot/micro/*.py too."""
    import run

    sha, sha2 = "a" * 40, "b" * 40
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        (repo / ".git" / "refs" / "heads").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (repo / ".git" / "refs" / "heads" / "main").write_text(sha + "\n")
        assert run.git_commit(repo) == sha
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/feat\n")
        (repo / ".git" / "packed-refs").write_text(f"# pack-refs\n{sha2} refs/heads/feat\n")
        assert run.git_commit(repo) == sha2
        wt_git = repo / ".git" / "worktrees" / "wt"
        wt_git.mkdir(parents=True)
        (wt_git / "HEAD").write_text("ref: refs/heads/main\n")
        (wt_git / "commondir").write_text("../..\n")
        wt = repo / ".worktrees" / "wt"
        (wt / "bot" / "micro").mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {wt_git.resolve()}\n")
        assert run.git_commit(wt) == sha
        (wt_git / "HEAD").write_text(sha2 + "\n")
        assert run.git_commit(wt) == sha2  # detached
        assert run.git_commit(Path(d) / "nothing") == ""
        (wt / "bot" / "a.py").write_text("x = 1\n")
        v1 = run.bot_version(wt / "bot")
        (wt / "bot" / "micro" / "m.py").write_text("y = 2\n")
        v2 = run.bot_version(wt / "bot")
        assert v1["commit"] == v2["commit"] == sha2[:7] and v1["code"] != v2["code"]


def test_wave_size_grows_late():
    proxy = config.ProxyRaxConfig()
    assert core.wave_size(proxy, 200.0) == 4 and core.wave_size(proxy, 240.0) == 10
    six = config.SixRaxConfig()   # late_time 0: never changes
    assert core.wave_size(six, 900.0) == six.wave_size


# ------------------------------------------------------------------ MMM (2-base macro)
def test_mmm_config():
    m = config.MmmConfig()
    items = [s.item for s in m.build]
    order = [items.index(i) for i in ("Barracks", "Reaper", "OrbitalCommand", "CommandCenter")]
    assert order == sorted(order)
    assert items.index("Factory") < items.index("Starport") and "BarracksReactor" in items
    assert sum(i == "Barracks" for i in items) == 5 and sum(i == "Refinery" for i in items) == 4
    cc = next(s for s in m.build if s.item == "CommandCenter")
    assert cc.count == 2 and 90 <= core.supply_sec(cc.supply) <= 120   # natural ~1:45 in the coach plan
    assert m.research[0] == "Stimpack" and m.worker_cap == 44 and m.push_supply >= 30
    assert 390 <= m.attack_time <= 420 and m.prod_cap >= 5
    assert 0 < m.expand_send < 400   # the natural's SCV leaves before the 400 is banked
    assert "TerranInfantryArmorsLevel1" in m.ebay_research and m.techlabs >= 3   # gas sinks (demo 13: 1163 gas)
    assert m.starports >= 2 and 0 < m.gas_bank <= 400   # demo 14: 560 gas at 7:30 with one Starport
    assert [s.item for s in m.build].index("Refinery", 1) < [s.item for s in m.build].index("Factory")  # 2nd gas early
    assert "mmm" in core.BOT_RACES and core.plan_seats("mmm", "medium")[0].bot == "mmm"
    s = core.plan_seats("mmm", "bot", bot2="dt")  # 1v1 bot vs bot: two participants, opposite teams
    assert [(x.kind, x.bot, x.race, x.team) for x in s] == [("bot", "mmm", "terran", 1), ("bot", "dt", "protoss", 2)]
    assert core.plan_seats("mmm", "bot")[1].bot == "mmm"
    try:
        core.plan_seats("mmm", "bot", two_v_two=True)
        raise AssertionError("--vs bot --2v2 must be refused")
    except ValueError:
        pass


def test_wall_depot():
    corners = [(30.0, 20.0), (27.0, 23.0)]
    assert core.wall_depot(corners, []) == (27.0, 23.0)   # stable order: sorted
    assert core.wall_depot(corners, [(27.0, 23.0)]) == (30.0, 20.0)
    assert core.wall_depot(corners, [(27.2, 23.1), (30.0, 20.0)]) is None   # both up: later depots go elsewhere
    assert core.wall_depot([], []) is None   # no wall on this ramp


def test_behind_minerals():
    assert core.behind_minerals((10.0, 10.0), (10.0, 17.0), 3.0) == (10.0, 20.0)
    x, y = core.behind_minerals((0.0, 0.0), (6.0, 8.0), 5.0)
    assert abs(x - 9.0) < 1e-9 and abs(y - 12.0) < 1e-9
    assert core.behind_minerals((5.0, 5.0), (5.0, 5.0)) == (5.0, 5.0)


def test_production_spots():
    start, minerals = (50.5, 50.5), (50.5, 58.5)   # minerals north of the townhall
    spots = core.production_spots(start, minerals)
    assert spots and all(p[1] <= start[1] for p in spots)   # never on the mineral side
    assert all(((p[0] - 50.5) ** 2 + (p[1] - 50.5) ** 2) ** 0.5 >= 7 for p in spots)
    assert all(p[0] % 1 == 0.5 and p[1] % 1 == 0.5 for p in spots)   # 3x3 centres stay on .5
    d = [((p[0] - 50.5) ** 2 + (p[1] - 50.5) ** 2) ** 0.5 for p in spots]
    assert d == sorted(d)   # nearest first
    for i, a in enumerate(spots):   # no two overlap, and nobody covers another's add-on (+2.5, -0.5)
        for b in spots[i + 1:]:
            assert abs(a[0] - b[0]) >= 7 or abs(a[1] - b[1]) >= 4


def test_depot_action():
    assert core.depot_action(lowered=False, enemy_near=False) == "lower"
    assert core.depot_action(lowered=True, enemy_near=True) == "raise"
    assert core.depot_action(lowered=True, enemy_near=False) is None
    assert core.depot_action(lowered=False, enemy_near=True) is None


def test_next_research():
    order = ("Stimpack", "ShieldWall", "PunisherGrenades")
    assert core.next_research(order, set()) == "Stimpack"
    assert core.next_research(order, {"Stimpack"}) == "ShieldWall"
    assert core.next_research(order, {"ShieldWall"}) == "Stimpack"   # in order, whatever got done first
    assert core.next_research(order, set(order)) is None


def test_barracks_addon():
    assert core.barracks_addon(0, 0, 2, opening_reactor=False) is None   # the opening's Reactor comes first
    assert core.barracks_addon(1, 0, 2) == "TechLab" and core.barracks_addon(1, 1, 2) == "TechLab"
    assert core.barracks_addon(1, 2, 2) == "Reactor" and core.barracks_addon(3, 2, 2) == "Reactor"


def test_bio_unit_and_medivacs():
    assert core.bio_unit(False, 0, 0) == "Marine"   # no Tech Lab: Marines
    assert core.bio_unit(True, 0, 0) == "Marauder"
    assert core.bio_unit(True, 12, 2) == "Marauder" and core.bio_unit(True, 12, 5) == "Marine"
    assert not core.want_medivac(0, 0) and core.want_medivac(0, 3)
    assert core.want_medivac(1, 16, per=8) and not core.want_medivac(2, 16, per=8)
    assert not core.want_medivac(8, 200, per=8, cap=8)


def test_army_supply_and_push():
    assert core.army_supply({"Marine": 20, "Marauder": 6, "Medivac": 3}) == 38
    assert core.army_supply({"Marine": -1, "Hellion": 2}) == 2
    pr = core.push_ready
    assert pr(False, 0, False, 0, 40, 405)   # later waves: no extra gate
    assert not pr(True, 60, False, 380, 40, 405)   # no stim yet: wait
    assert pr(True, 40, True, 380, 40, 405)   # stim + 40 supply: go
    assert not pr(True, 30, True, 380, 40, 405)   # stim, too small, before attack_time
    assert pr(True, 30, True, 405, 40, 405)   # attack_time reached
    assert not pr(True, 50, False, 440, 40, 405, stim_wait=45) and pr(True, 50, False, 450, 40, 405, stim_wait=45)


# ------------------------------------------------------------------ attack only when ahead / regroup when losing
def test_army_ahead():
    ahead = core.army_ahead
    assert ahead(4, 0, 1.3)   # nothing seen yet: a cheese's first wave goes on its timer
    assert not ahead(4, 6, 1.2)   # ProxyRax: 4 Marines vs 3 Zealots
    assert ahead(8, 6, 1.2)   # 8 Marines do go
    assert not ahead(16, 22, 1.3) and ahead(29, 22, 1.3) and not ahead(28, 22, 1.3)
    assert ahead(1, 50, 0)   # ratio 0: off
    assert ahead(10, 10, 1.0)   # even counts as ahead at x1.0


def test_should_retreat():
    sr = core.should_retreat
    assert sr(12, 20, 0.7)   # demo 15: 12 left vs 20 nearby
    assert not sr(14, 20, 0.7)   # 14 >= 0.7 x 20
    assert not sr(5, 0, 0.7)   # no enemy army near
    assert not sr(0, 0, 0.7)
    assert not sr(1, 30, 0)   # ratio 0: off
    assert sr(0, 2, 0.7)


def test_enemy_memory():
    m = core.EnemyMemory(keep=60)
    assert m.supply(0) == 0 and len(m) == 0
    m.see([(1, 2), (2, 2), (3, 2)], 10)   # three Zealots
    assert m.supply(10) == 6
    m.see([(1, 2)], 40)   # one still in view, two went into the fog: still remembered
    assert m.supply(65) == 6 and len(m) == 3
    assert m.supply(71) == 2   # the two not seen since 0:10 are dropped after 60 s
    m.forget(1)   # killed
    m.forget(99)   # unknown tag: no error
    assert m.supply(72) == 0 and len(m) == 0
    m.see([(5, 0.5), (6, 0.5)], 100)   # Zerglings count half
    m.see([(5, 0.5)], 101)   # the same unit seen again counts once
    assert m.supply(101) == 1
    m.see([(7, 3)], 102)   # a Ravager morph: the tag's supply is updated on the next sighting
    m.see([(7, 3)], 103)
    assert m.supply(103) == 4


def test_want_vikings():
    wv = core.want_vikings
    assert not wv(0, 0) and wv(1, 0) and wv(1, 1) and not wv(1, 2)   # 2 per Colossus
    assert wv(8, 9) and not wv(8, 10)   # capped at 10
    assert core.army_supply({"Viking": 3}) == 6


def test_replay_rotation():
    day = 86400.0
    files = [(f"r{i}", 100 * day - i * day) for i in range(20)]   # r0 newest, one a day
    assert core.replays_to_prune(files, 100 * day) == [f"r{i}" for i in range(15, 20)]   # older than 14 days
    assert core.replays_to_prune(files, 100 * day, 30, 5) == [f"r{i}" for i in range(5, 20)]   # only the 5 newest
    assert core.keep_replay(True, "veryhard") and core.keep_replay(True, "cheatinsane")
    assert not core.keep_replay(False, "cheatinsane") and not core.keep_replay(True, "hard")


def test_replay_name():
    name = core.replay_name("mmm", "veryhard", "zerg", "20261031-010613")
    assert name == "mmm-vs-veryhard-zerg-20261031-010613.SC2Replay"
    assert core.replay_name("proxy", "human", "zerg", "x") == "proxy-vs-human-x.SC2Replay"


def test_reaper_action():
    ra = core.reaper_action
    assert ra(0.45, False, True, None, True) == "heal" and ra(0.55, False, True, None, True) == "hunt"
    assert ra(0.8, True, True, None, True) == "heal"   # Dickie: back off at 50%, return near full
    assert ra(0.96, True, True, None, True) == "hunt"   # healed: back to work
    assert ra(1.0, False, False, 2.0, True) == "kite" and ra(1.0, False, True, 2.0, True) == "hunt"
    assert ra(1.0, False, True, 5.0, True, threats=2) == "kite"   # 2+ threats close: keep away even when ready
    assert ra(1.0, False, True, None, False) == "scout"   # no workers near: walk on, never shoot buildings


def test_cancel_a_dying_building():
    sc = core.should_cancel
    assert sc(0.6, 0.3, True) and not sc(0.6, 0.5, True)   # demo 23: the natural CC under Zerglings
    assert not sc(0.6, 0.1, False) and not sc(1.0, 0.1, True) and not sc(0.6, 0.3, True, 0)
    assert not sc(0.01, 0.1, True) and not sc(0.62, 0.34, True)   # fresh / on track for its progress: kept
    assert sc(0.05, 0.05, True)   # just placed and already half gone


def test_wall_closes_as_one():
    wc = core.wall_closed
    assert wc(True, False, 20, 8)   # enemy at either depot: the whole wall goes up
    assert wc(False, True, 0, 8) and not wc(False, True, 8, 8)   # vs Zerg: shut until 8 army supply
    assert not wc(False, False, 0, 8)   # vs P/T with nothing near: lowered so the army walks out
    assert not wc(False, True, 0, 8, passing=True)   # vs Zerg: opens for our SCV/units walking through
    assert wc(True, True, 0, 8, passing=True)   # ... but never with an enemy at the wall


def test_no_production_in_the_mineral_line():
    th, mf = (50.5, 50.5), (50.5, 58.5)
    assert core.mining_side((50.5, 56.5), th, mf) and core.mining_side((46.5, 55.5), th, mf)   # in the line
    assert not core.mining_side((50.5, 42.5), th, mf)   # the far side of the CC
    assert not core.mining_side((50.5, 66.5), th, mf)   # well behind it (pad 5): not the mining area


def test_third_fly_action():
    a = core.third_fly_action
    assert a(at_spot=False, flying=False, ready=False, busy=False, contested=False) == "wait"   # building in the main
    assert a(False, False, True, True, False) == "wait"          # training / morphing: can't lift
    assert a(False, False, True, False, False) == "lift"
    assert a(False, False, True, False, True) == "wait"          # spot contested: stay down in the main
    assert a(False, True, True, False, False) == "land"
    assert a(False, True, True, False, True) == "hover"          # enemy at the spot mid-flight: back over the main
    assert a(True, False, True, False, True) == "landed"         # landed beats everything (PF/Orbital next)
    assert a(True, True, True, False, False) == "land"           # over the spot but still in the air


def test_third_morph():
    m = core.third_morph
    assert m("Zerg", "zerg", True) == "pf"
    assert m("zerg", "zerg", False) == "orbital"                 # no Engineering Bay: no PF
    assert m("Protoss", "zerg", True) == "orbital"
    assert m("Protoss", "zerg, protoss", True) == "pf"
    assert m("Zerg", "", True) == "orbital"


def test_third_stage_point():
    sp = core.third_stage_point
    assert sp((0, 0), (10, 20), 0.5) == (5.0, 10.0)
    assert sp((0, 0), (10, 20), 0.0) == (0.0, 0.0)
    assert sp((0, 0), (10, 20), 2.0) == (10.0, 20.0)             # clamped


def test_main_cc_spots():
    start, minerals = (50.5, 50.5), (50.5, 60.5)              # mineral line north
    grid = core.production_spots(start, minerals)
    spots = core.main_cc_spots(start, minerals, grid, keep=12)
    assert spots, "the full grid must not block every spot"
    assert all(not core.mining_side(p, start, minerals) for p in spots)
    assert all(p[0] % 1 == 0.5 and p[1] % 1 == 0.5 for p in spots)  # 5x5 centre on .5 like the townhall
    for p in spots:
        for g in grid[:12]:
            assert not (abs(p[0] - g[0]) < 5 and abs(p[1] - g[1]) < 5)
    d = [core._dist(p, start) for p in spots]
    assert d == sorted(d) and d[0] >= 8
    assert len(core.main_cc_spots(start, minerals, grid, keep=len(grid))) < len(spots)


def test_third_at_100_supply():
    td = core.third_due
    assert td(40, 200, 60, 40, 330, 0) and td(30, 330, 60, 40, 330, 0)   # 40 SCVs or 5:30
    assert td(44, 600, 100, 40, 0, 100) and not td(44, 600, 99, 40, 0, 100)   # baseline + third_supply=100
    assert not td(48, 900, 150, 40, 0, 0)   # both off (baseline): never


def test_wave_comes_home_for_a_base_race():
    sr = core.should_recall
    assert sr(20, 30, 0.5) and not sr(10, 30, 0.5)   # his army at our base vs a wave out: home when it's half
    assert not sr(4, 4, 0.5)   # a few units: the defenders at home handle it
    assert not sr(40, 30, 0) and not sr(40, 0, 0.5)   # off (baseline), or no wave out


def test_depots_keep_off_addon_squares():
    th, mf, behind = (50.5, 50.5), (50.5, 58.5), (50.5, 63.5)
    port = (60.5, 40.5)   # its add-on square is centred on (63, 40)
    spots = core.depot_spots(th, mf, behind, [port], [(60.5, 32.5)], radius=20)
    assert spots and spots[0][1] > mf[1]   # behind the mineral line first
    assert all(not (abs(x - 63) < 2 and abs(y - 40) < 2) for x, y in spots)   # never on the Starport's add-on
    assert all(not (abs(x - 60.5) < 2.5 and abs(y - 32.5) < 2.5) for x, y in spots)   # nor on a free grid spot
    assert all(not (abs(x - th[0]) < 3.5 and abs(y - th[1]) < 3.5) for x, y in spots)   # nor on the CC
    assert (52.0, 54.0) not in spots   # the mining area stays open


def test_starport_reactor_slots():
    ps = core.port_slots
    assert ps(False, False, True) == 0   # still to get its Reactor: no unit first
    assert ps(True, True, True) == 2 and ps(False, False, False) == 1   # Reactor: two; off/blocked: one
    assert ps(False, True, True) == 1   # an add-on under way (or a Tech Lab): not 0 forever


def test_wall_stays_open_after_a_passer():
    wp = core.wall_passing
    assert wp(True, 100, -99, 6) == (True, 106)   # an SCV at the wall: open, held to 106
    assert wp(False, 104, 106, 6) == (True, 106)   # gone 4 s later: still open, no raise under the next one
    assert wp(False, 107, 106, 6) == (False, 106)   # past the hold: shuts
    assert wp(False, 100.1, 100, 0) == (False, 100)   # hold 0 (baseline): shuts the next step


def test_enemy_to_beat_after_a_retreat():
    etb = core.enemy_to_beat
    assert etb(6, 12, 217, 225, 90) == 12   # demo 22: beaten by 12 at 3:37, only 6 remembered at 3:45
    assert not core.army_ahead(8, etb(6, 12, 217, 225, 90), 1.2) and core.army_ahead(16, 12, 1.2)
    assert etb(6, 12, 217, 320, 90) == 6   # 90 s later the memory alone counts
    assert etb(20, 12, 217, 225, 90) == 20 and etb(6, 12, None, 225, 90) == 6 and etb(6, 12, 217, 225, 0) == 6


def test_wave_supply_keeps_the_wave_in_the_fight():
    # demo 21: a wave of 32 Marines left on "32 vs 24"; its 3 lead Marines met 12 supply at the front
    wave = [(200 + i, 200, 1) for i in range(3)] + [(150 - i, 200, 1) for i in range(29)]
    assert core.wave_supply(wave) == 32 and not core.should_retreat(core.wave_supply(wave), 12, 0.7)
    assert core.should_retreat(core.wave_supply(wave[:8]), 12, 0.7)   # after real losses (8 left): pull back
    assert core.wave_supply([]) == 0


def test_local_fight():
    lf = core.local_fight
    assert lf([], [(0, 0, 2)]) == (0, 0)
    assert lf([(0, 0, 1)], []) == (0, 0)
    assert lf([(0, 0, 1)], [(50, 50, 2)]) == (0, 0)   # no contact
    wave1 = [(100 + i, 100, 1) for i in range(12)]   # 12 Marines fighting at x 100..111
    enemy = [(104 + i, 104, 2) for i in range(10)]   # 20 supply of enemy
    ours, theirs = lf(wave1, enemy, 12)
    assert ours == 12 and theirs == 20
    assert core.should_retreat(ours, theirs, 0.7)
    trailing = [(30 + i, 30, 1) for i in range(16)]   # wave 2 still mid-map: not part of the fight
    assert lf(wave1 + trailing, enemy, 12) == (12, 20)
    far_enemy = enemy + [(300, 300, 8)]   # an enemy group elsewhere isn't counted
    assert lf(wave1, far_enemy, 12) == (12, 20)
    # test 18: 2 lead Marines meet 6 supply while 15 more are seconds behind: with reach they count, no retreat
    lead = [(200, 200, 1), (201, 200, 1)]
    behind = [(185 - i, 200, 1) for i in range(15)]   # 16-30 behind the lead
    blockers = [(205, 202, 2) for _ in range(3)]
    assert lf(lead + behind, blockers, 12) == (2, 6)
    mine, theirs = lf(lead + behind, blockers, 12, 32)
    assert mine == 17 and theirs == 6 and not core.should_retreat(mine, theirs, 0.7)
    assert lf(wave1 + trailing, enemy, 12, 25) == (12, 20)   # wave 2 at mid-map is still too far to count


def test_cooled():
    assert core.cooled(None, 5, 15)
    assert not core.cooled(10, 20, 15) and core.cooled(10, 25, 15)


def test_ahead_settings():
    for key, cls in config.CONFIGS.items():
        c = cls()
        assert 0 <= c.retreat_ratio < 1 <= c.ahead_ratio <= 2, key   # retreat below even, attack at or above it
        assert 30 <= c.enemy_memory <= 120 and 6 <= c.retreat_radius <= 20, key
        assert c.retreat_cooldown > 0 and c.force_commit >= 0, key
    assert config.ProxyRaxConfig().ahead_ratio * 6 > 4   # 4 Marines never go into 3 Zealots
    assert config.MmmConfig().ahead_ratio >= config.SixRaxConfig().ahead_ratio
    off = core.apply_overrides(config.MmmConfig(), ["ahead_ratio=0", "retreat_ratio=0", "enemy_memory=90"])
    assert off.ahead_ratio == 0 and off.retreat_ratio == 0 and off.enemy_memory == 90


# ------------------------------------------------------------------ ProxyRax pivot to macro
def test_held_since():
    assert core.held_since(False, None, 10) is None
    assert core.held_since(True, None, 10) == 10   # starts holding
    assert core.held_since(True, 10, 25) == 10   # keeps its start
    assert core.held_since(False, 10, 30) is None   # a break resets it


def test_pivot_reasons():
    pr = core.pivot_reasons
    # test 18 at 4:05: 885 banked since ~3:00, 3 retreats, waves held, proxy still up
    assert pr(245, 240, 65, 20, True, 3, 2) == ["bank", "retreats", "waves held"]
    assert pr(239, 240, 999, 20, False, 9, 9) == []   # never before pivot_time: the cheese keeps its timings
    assert pr(900, 0, 999, 20, False, 9, 9) == []   # pivot_time 0: off
    assert pr(240, 240, 0, 20, True, 0, 0) == []   # the cheese is working: no pivot
    assert pr(240, 240, 19, 20, True, 0, 0) == [] and pr(240, 240, 20, 20, True, 0, 0) == ["bank"]
    assert pr(300, 240, 0, 20, False, 0, 0) == ["proxy dead"]
    assert pr(300, 240, 0, 20, True, 1, 0) == ["retreats"]
    assert pr(300, 240, 0, 20, True, 1, 0, min_retreats=2) == []   # needs 2 retreats
    assert pr(300, 240, 0, 20, True, 5, 5, min_retreats=0, min_holds=0) == []   # both signs off
    assert pr(300, 240, 0, 20, True, 0, 1) == ["waves held"]


def test_pivot_config():
    px = config.ProxyRaxConfig()
    assert 0 < px.pivot_time <= 300 and px.pivot_bank >= px.spend_bank   # ~4:00, a real bank
    assert px.pivot_workers > px.worker_cap and px.pivot_rax > px.prod_cap and 2 <= px.pivot_gas <= 4
    assert px.research[:2] == ("Stimpack", "ShieldWall") and px.techlabs == 2
    assert px.ebay_research[0] == "TerranInfantryWeaponsLevel1"   # Dickie: +1 after the pivot
    p = core.pivot_config(px)
    assert p is not px and px.worker_cap == 16   # a copy: the frozen original is untouched
    assert p.worker_cap == 40 and p.prod_cap == 8 and p.spend_bank == px.pivot_bank
    assert core.wave_size(p, 250) == 16 and core.wave_size(p, 900) == 16 and p.min_wave == p.first_wave == 16
    assert p.stage_forward == 0 and p.supply_per_prod == 3   # waves gather at home; supply for Reactor Barracks
    assert p.build == px.build and p.ahead_ratio == px.ahead_ratio   # the shared ahead/regroup logic keeps going
    off = core.apply_overrides(px, ["pivot_time=0", "pivot_wave=12", "pivot_bank_hold=30"])
    assert off.pivot_time == 0 and core.pivot_reasons(900, off.pivot_time, 999, 20, False, 9, 9) == []
    assert core.pivot_config(off).wave_size == 12 and off.pivot_bank_hold == 30
    assert core.apply_overrides(px, ["pivot_time=3:30"]).pivot_time == 210
    # the BioBot fields ProxyRax shares with MMM
    for key in ("techlabs", "research", "ebay_research", "marauder_share", "marauder_share_rich", "gas_heavy",
                "medivac_per", "medivac_cap", "expand_send", "stim_hp"):
        assert hasattr(px, key) and hasattr(config.MmmConfig(), key), key


def test_gas_target():
    assert core.gas_target(1, 3) == 2 and core.gas_target(2, 3) == 3 and core.gas_target(2, 4) == 4
    assert core.gas_target(0, 3) == 0 and core.gas_target(3, 4) == 4


def test_pivot_steering():
    assert parse_command('{"pivot": "now"}') == Command(pivot=True)
    assert not Command(pivot=True).empty and Command(pivot=True).describe() == "pivot"
    assert "pivot" in core.ACTION_KEYS
    path = TMP / "pivot.json"
    assert core.write_command(path, 7, pivot=True) == {"seq": 7, "pivot": True}


# ------------------------------------------------------------------ bio micro (micro.py)
def _close(a, b, tol=1e-6):
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def test_unit_vector_and_rotate():
    assert _close(core.unit_vector((0, 0), (3, 4)), (0.6, 0.8))
    assert _close(core.unit_vector((1, 1), (1, 1)), (1, 0))   # same point: the fallback
    assert _close(core.unit_vector((1, 1), (1, 1), (0, -2)), (0, -1))
    assert _close(core.rotate((1, 0), 90), (0, 1)) and _close(core.rotate((1, 0), -90), (0, -1))


def test_step_points():
    pts = core.step_points((10, 10), (1, 0), 2)
    assert _close(pts[0], (12, 10))   # straight back first
    assert len(pts) == 5 and _close(pts[3], (10, 12)) and _close(pts[4], (10, 8))   # then sideways
    for p in pts:
        assert abs(((p[0] - 10) ** 2 + (p[1] - 10) ** 2) ** 0.5 - 2) < 1e-6


def test_split_direction():
    sd = core.split_direction
    assert _close(sd((5, 0), (0, 0), []), (1, 0))   # alone: straight away from the Baneling
    up = sd((5, 1), (0, 0), [(5, 0), (5, -1)])   # the top Marine of a column fans upward
    down = sd((5, -1), (0, 0), [(5, 0), (5, 1)])
    assert up[0] > 0 and up[1] > 0 and down[0] > 0 and down[1] < 0
    assert abs((up[0] ** 2 + up[1] ** 2) - 1) < 1e-6
    assert _close(sd((5, 1), (0, 0), [(5, 0)], spread=0), sd((5, 1), (0, 0), []))   # spread 0: no fan-out
    on_top = sd((0, 0), (0, 0), [])   # on top of the Baneling: still a unit vector
    assert abs((on_top[0] ** 2 + on_top[1] ** 2) - 1) < 1e-6
    cancel = sd((1, 0), (0, 0), [(2, 0)], spread=1)   # away from bane and pulled back by the mate: sideways
    assert abs((cancel[0] ** 2 + cancel[1] ** 2) - 1) < 1e-6 and abs(cancel[0]) < 1e-6


def test_split_active():
    assert core.split_active(10, 10.5, 1.5) and not core.split_active(10, 11.5, 1.5)
    assert not core.split_active(10, 14.4, 1.5) and core.split_active(10, 14.6, 1.5)   # 3 s fighting, then again
    assert not core.split_active(None, 10, 1.5) and not core.split_active(10, 10, 0)


def test_should_kite():
    sk = core.should_kite
    assert sk(10, 14, 1.0, 3.5)   # just fired, a Zergling at 1: step back
    assert not sk(0, 14, 1.0, 3.5)   # weapon ready: shoot
    assert not sk(4, 14, 1.0, 3.5)   # 4 of 14 left (< 35%): turn and shoot instead
    assert not sk(10, 14, 4.0, 3.5)   # the melee unit is still beyond kite_range
    assert not sk(10, 14, 1.0, 0)   # kite_range 0: off
    assert not sk(10, 0, 1.0, 3.5)   # full cooldown unknown
    assert sk(5, 9, 0.5, 3.5) and not sk(3, 9, 0.5, 3.5)   # stimmed: shorter reload, same share


def test_focus_target():
    ft = core.focus_target
    # (tag, baneling, attacks, hp, dist)
    ling_full, ling_low, bane_far, bane_near, egg = (
        (1, False, True, 35, 2), (2, False, True, 5, 4), (3, True, True, 30, 5), (4, True, True, 30, 3),
        (5, False, False, 1, 1))
    assert ft([], None) is None
    assert ft([ling_full, ling_low], None) == 2   # lowest HP that fights back
    assert ft([ling_full, ling_low, bane_far, bane_near], None) == 4   # Banelings first, the closest
    assert ft([ling_full, egg], None) == 1   # an egg on 1 HP waits for the Zergling
    assert ft([egg], None) == 5
    assert ft([ling_full, ling_low], 1) is None   # current target in range, same tier: keep it (no new order)
    assert ft([ling_full, ling_low, bane_far], 1) == 3   # a Baneling came in range: switch
    assert ft([ling_full, ling_low], 99) == 2   # current target gone or out of range
    assert ft([ling_full, egg], 5) == 1   # shooting an egg while a Zergling is in range: switch
    assert core.target_key(True, True, 30, 2) < core.target_key(False, True, 1, 1) < core.target_key(False, False, 1, 1)


def test_micro_settings():
    for key, cls in config.CONFIGS.items():
        c = cls()
        assert c.micro and c.micro_cap > 0 and 0 < c.split_window <= 3, key
        assert 0 < c.kite_range <= 5 and 0 < c.kite_frac < 1 and c.split_radius >= c.kite_range, key
    off = core.apply_overrides(config.MmmConfig(), ["micro=false", "split_radius=6", "kite_range=0"])
    assert not off.micro and off.split_radius == 6 and off.kite_range == 0


# ------------------------------------------------------------------ Skytoss (2-base Protoss air)
def test_tech_order_errors():
    assert core.tech_order_errors(config.DtRushConfig().build) == []
    bad = core.steps((14, "Pylon", 1), (15, "FleetBeacon", 1), (16, "Gateway", 1), (17, "Stargate", 1),
                     (19, "CyberneticsCore", 1))   # Beacon before any Stargate, Stargate before the Core
    errs = core.tech_order_errors(bad)
    assert len(errs) == 2 and "FleetBeacon" in errs[0] and "needs Stargate" in errs[0]
    assert "17 Stargate" in errs[1] and "needs CyberneticsCore" in errs[1]
    assert core.tech_order_errors(core.steps((14, "Gateway", 1))) == ["step 0 (14 Gateway): needs Pylon earlier in "
                                                                     "the build"]
    assert core.tech_order_errors(core.steps((14, "SupplyDepot", 1), (15, "Barracks", 1))) == []   # Terran: no rules


def test_skytoss_config():
    s = config.SkytossConfig()
    assert core.tech_order_errors(s.build) == [] and core.check_order(s.build) == []
    items = [x.item for x in s.build]
    order = [items.index(i) for i in ("Pylon", "Gateway", "Nexus", "CyberneticsCore", "Stargate", "FleetBeacon")]
    assert order == sorted(order)
    assert items.count("Assimilator") == 4 and items.count("Stargate") == 3 and items.count("ShieldBattery") == 2
    gas2 = items.index("Assimilator", items.index("Assimilator") + 1)
    assert gas2 < items.index("Stargate")   # 2 gas by the Stargate
    plan = {x["action"]: x["sec"] for x in core.coach_steps(s.build)}
    assert 75 <= plan["Nexus #2"] <= 105   # natural ~1:30 in the coach plan
    assert 120 <= plan["Stargate"] <= 190 and 315 <= plan["Fleet Beacon"] <= 380   # Stargate ~3:00, Beacon ~5:30-6:00
    beacon = next(x for x in s.build if x.item == "FleetBeacon")
    assert 300 <= beacon.at <= 360
    assert s.worker_cap == 44 and 0 < s.expand_send < 400 and s.defenders >= 1
    assert s.push_carriers >= 4 and s.push_supply >= 60 and 540 <= s.attack_time <= 660
    assert s.prod_cap >= items.count("Stargate") and s.place_give_up > 0
    assert s.chrono_order[0] == "Stargate" and "Nexus" in s.chrono_order
    assert s.air_research[0] == "ProtossAirWeaponsLevel1"
    assert "skytoss" in core.BOT_RACES and core.BOT_RACES["skytoss"] == "protoss"
    seat = core.plan_seats("skytoss", "hard")[0]
    assert (seat.bot, seat.race) == ("skytoss", "protoss")
    c = core.apply_overrides(s, ["push_carriers=4", "attack_time=9:00", "zealot_bank=0", "chrono=false"])
    assert (c.push_carriers, c.attack_time, c.zealot_bank, c.chrono) == (4, 540.0, 0, False)
    data = core.bot_game("Skytoss", "protoss", "skytoss", "2-base Protoss Skytoss macro (not a cheese)", s.build, 1.0)
    assert data["plan"]["steps"][-1]["action"] == "Stargate #3"


def test_stargate_unit():
    su = core.stargate_unit
    assert su(False, 0, 0, 0, False) == "VoidRay"
    assert su(False, 0, 5, 0, False) is None   # 5 Void Rays: the gas waits for the Beacon and Carriers
    assert su(False, 0, 2, 0, True) == "Phoenix" and su(False, 0, 2, 2, True) == "VoidRay"   # enemy air seen
    assert su(True, 0, 5, 0, True) == "Carrier"   # Beacon done: Carriers first, even with enemy air
    assert su(True, 16, 5, 0, False) == "VoidRay"   # Carrier cap reached
    assert su(False, 0, 0, 0, False, void_ray_cap=0) is None and su(True, 3, 0, 0, False, carrier_cap=4) == "Carrier"


def test_gateway_pick():
    gp = core.gateway_pick
    assert gp(0, 2, False, False, 500, 200, 800) is None   # no Core yet, nothing threatening
    assert gp(0, 2, True, False, 150, 0, 800) == "Zealot"   # attacked before the Core
    assert gp(0, 2, False, True, 500, 200, 800) == "Adept" and gp(1, 2, False, True, 500, 200, 800) == "Stalker"
    assert gp(2, 2, False, True, 500, 200, 800) is None   # defenders done: gas for the Stargates
    assert gp(2, 2, True, True, 500, 200, 800) == "Stalker" and gp(2, 2, True, True, 500, 20, 800) == "Zealot"
    assert gp(2, 2, False, True, 900, 0, 800) == "Zealot" and gp(2, 2, False, True, 900, 0, 0) is None   # 0 = off


def test_chrono_pick():
    order = ("Stargate", "Nexus", "CyberneticsCore")
    assert core.chrono_pick({"Nexus": 1}, order) == "Nexus"   # before the Stargate: Probes
    assert core.chrono_pick({"Nexus": 2, "Stargate": 1, "CyberneticsCore": 1}, order) == "Stargate"
    assert core.chrono_pick({"Nexus": 0, "Stargate": 0, "CyberneticsCore": 1}, order) == "CyberneticsCore"
    assert core.chrono_pick({}, order) is None and core.chrono_pick({"Gateway": 3}, order) is None


def test_fleet_ready():
    fr = core.fleet_ready
    assert not fr(True, 3, 40, 420, 6, 80, 600, 30)   # 7:00, three Carriers: wait
    assert fr(True, 6, 40, 420, 6, 80, 600, 30) and fr(True, 2, 84, 420, 6, 80, 600, 30)   # the fleet is there
    assert fr(True, 0, 10, 600, 6, 80, 600, 30)   # 10:00: go anyway
    assert not fr(False, 9, 24, 900, 6, 80, 600, 30) and fr(False, 0, 30, 0, 6, 80, 600, 30)   # later waves: supply


def test_reaper_escapes_toward_the_exit():
    esc = core.escape_direction
    x, y = esc((10, 10), (12, 10), (10, 0))   # threat east, exit south: back off south-west, not due west
    assert x < 0 and y < 0
    x, y = esc((10, 10), (10, 5), (10, 0))    # the threat between us and the exit: sideways, around it
    assert abs(x) > 0.9 and abs(y) < 0.2
    x, y = esc((10, 10), (10, 15), (10, 0))   # threat behind, exit ahead: straight out
    assert abs(x) < 1e-9 and abs(y + 1) < 1e-9


def test_placement_stuck():
    assert not core.placement_stuck(None, 100, 20)
    assert not core.placement_stuck(90, 100, 20) and core.placement_stuck(80, 100, 20)


def test_step_saving():
    sv = core.step_saving
    assert sv(False, True, False) and not sv(False, False, False)   # unaffordable: only short minerals hold
    assert not sv(True, False, True) and not sv(True, False, True, False)   # placed: never saving
    # Acropolis vs Insane Zerg (2026-10-31): the CC was affordable but failed to place, and the bot held 1,312
    assert not sv(True, False, False)
    assert sv(True, False, False, place_fail_spends=False)   # the switch off: it holds, as ba15df0 did


def test_baseline_preset():
    from dataclasses import fields

    import run

    base = config.PRESETS["baseline"]
    keys = [p.split("=", 1)[0] for p in base]
    known = {f.name for cls in config.CONFIGS.values() for f in fields(cls)}
    assert set(keys) <= known and len(keys) == len(set(keys))
    on, off = config.MmmConfig(), core.apply_overrides(config.MmmConfig(), list(base))
    # every switch is on by default (the new behaviour) and the preset turns each one off
    assert on.medivac_wave and on.reaper_hideout and on.wall_scv and on.place_fail_spends
    assert on.reaper_wait > 0 and on.gas_off > 0 and on.third_time > 0 and not on.bunker
    assert not (off.medivac_wave or off.reaper_hideout or off.wall_scv or off.place_fail_spends or off.bunker)
    assert off.reaper_wait == 0 and off.gas_off == 0 and off.third_time == 0
    assert not config.SkytossConfig().place_fail_spends   # Skytoss keeps its place_give_up
    args = run.parse_args(["--bot", "sixrax", "--preset", "baseline", "--set", "reaper_wait=5"])
    pre = run.preset_overrides(args)
    assert "reaper_wait=0" in pre and not any(p.startswith(("gas_off", "wall_scv")) for p in pre)   # MMM-only out
    dry = ["--map", "Simple64", "--dry-run", "--vs", "easy", "--preset", "baseline"]
    assert run.main(["--bot", "sixrax", *dry]) == 0 and run.main(["--bot", "mmm", *dry, "--set", "gas_off=500"]) == 0
    args.overrides = [*pre, *args.overrides]
    assert run.build_configs(args)["sixrax"].reaper_wait == 5   # an explicit --set wins over the preset


def test_difficulties_include_cheaters():
    # a merge once left a second, shorter DIFFICULTIES below the first: --vs cheatinsane was rejected
    assert core.DIFFICULTIES[-3:] == ("cheatvision", "cheatmoney", "cheatinsane")
    assert Path(core.__file__).read_text(encoding="utf-8").count("\nDIFFICULTIES = ") == 1


def test_enemy_surrender_counts_as_a_win():
    # Dickie, 2026-10-31: "write an exception that accepts surrender"
    assert core.enemy_surrendered([(2, "gg")], me=1) and core.enemy_surrendered([(2, "GG!")], me=1)
    assert not core.enemy_surrendered([(1, "gg")], me=1)  # our own gg (auto-gg) is not a surrender
    assert not core.enemy_surrendered([(2, "eggs")], me=1) and not core.enemy_surrendered([], me=1)
    assert core.surrender_results(["Defeat"], [True], "Victory") == ["Victory"]
    assert core.surrender_results(["Defeat", "Victory"], [False, False], "Victory") == ["Defeat", "Victory"]


def test_vikings_stay_with_the_bio():
    # Dickie, 2026-10-31: the Vikings "dive in first"
    assert not core.viking_pull_back(5, 7, False)  # close enough
    assert core.viking_pull_back(9, 7, False)  # ahead of the bio with nothing to chase near it: back
    assert not core.viking_pull_back(12, 7, True)  # chasing air near the bio is fine...
    assert core.viking_pull_back(15, 7, True)  # ...up to twice the leash
    assert not core.viking_pull_back(30, 0, False)  # 0 = off (baseline)


def test_bot_vs_bot_loads_both_configs():
    # dt-mmm batch, 2026-11-04: --vs bot --bot2 dt seated DT but only built MMM's config (KeyError: 'dt')
    import run

    for argv, want in ((["--bot", "mmm", "--vs", "bot", "--bot2", "dt"], {"mmm", "dt"}),
                       (["--bot", "mmm", "--vs", "bot"], {"mmm"}),
                       (["--bot", "mmm", "--vs", "easy", "--bot2", "dt"], {"mmm"})):
        args = run.parse_args(argv)
        seats = core.plan_seats(args.bot, args.vs, args.two_v_two, args.bot2)
        assert set(run.build_configs(args)) == want >= {x.bot for x in seats if x.kind == "bot"}, argv



def test_mule_ok_keeps_a_scan():
    # keep 0: every Orbital at 50+ MULEs, as before scan_cloak
    assert core.mule_ok([50.0], 0, 0.0) and not core.mule_ok([49.0], 0, 0.0)
    # keep 50: a lone Orbital at 50 holds it, at 100 it MULEs once
    assert not core.mule_ok([50.0], 0, 50.0) and core.mule_ok([100.0], 0, 50.0)
    e = [50.0, 50.0]  # two Orbitals at 50: the first MULEs, the second keeps the scan
    assert core.mule_ok(e, 0, 50.0)
    e[0] -= 50
    assert not core.mule_ok(e, 1, 50.0)


def test_scan_keep():
    assert core.scan_keep(True, True, False, 60.0, 240.0) == 50  # cloak seen: any race, any time
    assert core.scan_keep(True, False, True, 240.0, 240.0) == 50  # Protoss from 4:00
    assert core.scan_keep(True, False, True, 239.0, 240.0) == 0
    assert core.scan_keep(True, False, False, 600.0, 240.0) == 0  # Zerg/Terran: only once seen
    assert core.scan_keep(True, False, True, 600.0, 0.0) == 0  # from_time 0: no early reserve
    assert core.scan_keep(False, True, True, 600.0, 240.0) == 0  # scan_cloak=false


def test_scan_spot():
    army = [(0.0, 1.0), (1.0, 0.0)]
    assert core.scan_spot([(2.0, 2.0)], army, [], 10.0) == (2.0, 2.0)
    assert core.scan_spot([(2.0, 2.0)], army[:1], [], 10.0) is None  # one army unit: no scan
    assert core.scan_spot([(30.0, 0.0)], army, [], 10.0) is None  # no army near it
    assert core.scan_spot([(2.0, 2.0)], army, [(5.0, 5.0, 8.0)], 10.0) is None  # our scan 2 s ago covers it
    assert core.scan_spot([(2.0, 2.0)], army, [(5.0, 5.0, 5.0)], 10.0) == (2.0, 2.0)  # 5 s ago: it wore off/missed
    # the DT with the most DTs within half a scan wins
    dts = [(0.0, 9.0), (2.0, 2.0), (3.0, 3.0), (4.0, 2.0)]
    assert core.scan_spot(dts, army + [(0.0, 8.0), (1.0, 8.0)], [], 10.0) == (2.0, 2.0)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"  test_core: {len(tests)} tests ok")
