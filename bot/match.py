"""Game runner for any seat mix, built from python-sc2's own pieces (SC2Process, Client, _play_game).

python-sc2's run_game handles one participant + built-in AIs, or exactly two participants and no AI (it drops
AIs in that case). A 2v2 with Dickie + an AI ally vs two bots has three participants plus an AI, so this runner
starts one SC2 process per participant (human or bot), creates the game on the first with every seat (AIs too),
and joins all of them with one shared port config (SC2's multiplayer API: one guest port pair per extra
participant).

Maps: an .SC2Map inside the project is sent to SC2 as bytes (RequestSaveMap on every process, then
LocalMap.map_data on create), so nothing has to be copied into the StarCraft II install. A bare name is looked up
in the install's Maps folder by python-sc2, as usual. SC2 writes the saved map into each process's own temp dir,
which python-sc2 deletes when the game closes. Only processes this runner started are ever closed."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

from loguru import logger
from s2clientprotocol import sc2api_pb2 as sc_pb
from sc2 import maps
from sc2.client import Client
from sc2.data import CreateGameError, Result
from sc2.main import _play_game
from sc2.player import AbstractPlayer, Bot, Computer, Human
from sc2.portconfig import Portconfig
from sc2.protocol import ConnectionAlreadyClosedError, ProtocolError
from sc2.sc2process import SC2Process

SAVED_MAP_DIR = "SparringBot"  # relative map path used for maps sent as bytes


def _proc_kwargs(
    player: AbstractPlayer, index: int, window: tuple[int, int] | None, fullscreen: bool, watch: bool = False
) -> dict:
    # watch: the first bot gets the main window (fullscreen), so the coach can read its panel like Dickie's
    if isinstance(player, Human) or (watch and index == 0):
        kw: dict = {"fullscreen": fullscreen}
        if window and not fullscreen:
            kw["resolution"] = window
            kw["placement"] = (0, 0)
        return kw
    # bot clients: small windows, stacked at the top-left; leave them open (closing one ends that bot)
    return {"fullscreen": False, "resolution": (640, 480), "placement": (20 + 40 * index, 20 + 40 * index)}


async def _local_map(controllers, map_ref: Path | str) -> sc_pb.LocalMap:
    if isinstance(map_ref, Path):
        data = map_ref.read_bytes()
        rel = f"{SAVED_MAP_DIR}/{map_ref.name}"
        for c in controllers:
            await c._execute(save_map=sc_pb.RequestSaveMap(map_path=rel, map_data=data))
        logger.info(f"Map {map_ref.name}: sent as {len(data)} bytes to {len(controllers)} SC2 process(es)")
        return sc_pb.LocalMap(map_path=rel, map_data=data)
    return sc_pb.LocalMap(map_path=str(maps.get(map_ref).relative_path))


def _create_request(local_map, players, realtime: bool, random_seed: int | None) -> sc_pb.RequestCreateGame:
    req = sc_pb.RequestCreateGame(local_map=local_map, realtime=realtime, disable_fog=False)
    if random_seed is not None:
        req.random_seed = random_seed
    for p in players:
        s = req.player_setup.add()
        s.type = p.type.value
        if isinstance(p, Computer):
            s.race = p.race.value
            s.difficulty = p.difficulty.value
    return req


async def play(
    map_ref: Path | str,
    players: list[AbstractPlayer],
    realtime: bool,
    save_replay: str | None = None,
    game_time_limit: int | None = None,
    window: tuple[int, int] | None = None,
    fullscreen: bool = False,
    random_seed: int | None = None,
    watch: bool = False,
) -> list[Result | BaseException]:
    """Play one game. Returns one result per participant (human/bot), in seat order."""
    participants = [p for p in players if not isinstance(p, Computer)]
    if not participants:
        raise ValueError("need at least one human or bot")
    portconfig = Portconfig(guests=len(participants) - 1) if len(participants) > 1 else None
    try:
        async with AsyncExitStack() as stack:
            controllers = []
            for i, p in enumerate(participants):
                logger.info(f"Starting SC2 process {i + 1}/{len(participants)} for {p}")
                ctrl = await stack.enter_async_context(SC2Process(**_proc_kwargs(p, i, window, fullscreen, watch)))
                await ctrl.ping()
                controllers.append(ctrl)

            local_map = await _local_map(controllers, map_ref)
            r = await controllers[0]._execute(create_game=_create_request(local_map, players, realtime, random_seed))
            if r.create_game.HasField("error"):
                err = f"Could not create game: {CreateGameError(r.create_game.error)}"
                if r.create_game.HasField("error_details"):
                    err += f": {r.create_game.error_details}"
                raise RuntimeError(err)
            logger.info("Game created: " + ", ".join(str(p) for p in players))

            clients = []
            for i, (p, ctrl) in enumerate(zip(participants, controllers)):
                cl = Client(ctrl._ws, save_replay if i == 0 else None)
                if isinstance(p, Bot) and getattr(p.ai, "raw_affects_selection", None) is not None:
                    cl.raw_affects_selection = p.ai.raw_affects_selection
                clients.append(cl)

            results = await asyncio.gather(
                *(_play_game(p, cl, realtime, portconfig, game_time_limit) for p, cl in zip(participants, clients)),
                return_exceptions=True,
            )
            for cl in clients:
                try:
                    if cl.save_replay_path is not None:
                        await cl.save_replay(cl.save_replay_path)
                    await cl.leave()
                    await cl.quit()
                except (ConnectionAlreadyClosedError, ProtocolError):
                    pass
            return list(results)
    finally:
        if portconfig is not None:
            portconfig.clean()
