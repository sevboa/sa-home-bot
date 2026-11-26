from __future__ import annotations

import asyncio
import fcntl
import logging
import socket
from pathlib import Path

from sa_home_bot.config import DevlabConfig, Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.loop import AgentLimits
from sa_home_bot.devlab.agent.model import ModelFactory
from sa_home_bot.devlab.executor.agent_sandbox import SandboxFactory, podman_sandbox_factory
from sa_home_bot.devlab.executor.agent_worker import AgentSettings, AgentWorker, SlotRegistry
from sa_home_bot.devlab.executor.busy import BusyTracker
from sa_home_bot.devlab.executor.hub import ExecutorHub
from sa_home_bot.devlab.executor.park import ParkMode
from sa_home_bot.devlab.executor.podman import PodmanTestRunner
from sa_home_bot.devlab.executor.routing import (
    MODELS_ANY,
    GatewayModels,
    ModelCatalog,
    SilenceWatch,
)
from sa_home_bot.devlab.executor.runner import TestRunner
from sa_home_bot.devlab.executor.service import ExecutorService
from sa_home_bot.devlab.executor.silence_relay import SilenceRelay
from sa_home_bot.devlab.executor.sweep import sweep_orphans
from sa_home_bot.devlab.executor.worker import (
    TestWorker,
    WorkerLoops,
    WorkerSettings,
    forever,
    send_heartbeat,
)
from sa_home_bot.devlab.executor.wrapper import PauseClock, WrapperClient
from sa_home_bot.devlab.neighbor import NeighborGate, start_following, stop_following
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)

EXECUTOR_DIR = "executor"
RUNNER_CLOSE_TIMEOUT_S = 20.0
LOCK_FILE = "executor.lock"
LOCK_LOG_INTERVAL_S = 30.0
LOCK_POLL_INTERVAL_S = 1.0


def build_runner(cfg: DevlabConfig, pause: PauseClock | None = None) -> TestRunner:
    return PodmanTestRunner(cfg, pause=pause)


def work_dir_of(cfg: DevlabConfig) -> Path:
    return Path(cfg.work_dir) if cfg.work_dir else Path(cfg.root) / EXECUTOR_DIR


async def acquire_work_lock(work_dir: Path):
    handle = (work_dir / LOCK_FILE).open("a")
    waited = 0.0
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if waited % LOCK_LOG_INTERVAL_S < LOCK_POLL_INTERVAL_S:
                log.warning(
                    "Каталог %s занят другим исполнителем, жду замок (%.0f с)", work_dir, waited
                )
            await asyncio.sleep(LOCK_POLL_INTERVAL_S)
            waited += LOCK_POLL_INTERVAL_S


def agent_limits(cfg: DevlabConfig) -> AgentLimits:
    return AgentLimits(
        max_steps=cfg.agent_max_steps,
        max_wall_s=cfg.agent_max_wall_s,
        max_red_runs=cfg.agent_max_red_runs,
        max_cost_usd=cfg.agent_max_cost_usd,
        max_tokens=cfg.agent_max_tokens,
        context_tokens=cfg.agent_context_tokens,
        reply_tokens=cfg.agent_reply_tokens,
    )


MEMORY_UNITS_MB = {"k": 1 / 1024, "m": 1, "g": 1024}
MIN_SLOT_MEMORY_MB = 2048


def split_memory(memory: str, slots: int) -> str:
    if slots == 1:
        return memory
    total_mb = int(memory[:-1]) * MEMORY_UNITS_MB[memory[-1]]
    return f"{max(MIN_SLOT_MEMORY_MB, int(total_mb // slots))}m"


def slot_dir(work_dir: Path, index: int) -> Path:
    return work_dir if index == 0 else work_dir / f"agent-{index}"


async def run_executor(
    settings: Settings,
    runner: TestRunner | None = None,
    sandboxes: SandboxFactory | None = None,
    models: ModelFactory | None = None,
) -> None:
    cfg = settings.devlab
    if not cfg.hub_url or not cfg.hub_token:
        raise RuntimeError("devlab: нужны hub_url и hub_token (роль executor)")
    containers = WrapperClient(cfg)
    gate = NeighborGate(
        freeze=containers.pause, thaw=containers.unpause, freeze_max_s=cfg.freeze_max_s
    )
    runner = runner or build_runner(cfg, gate)
    work_dir = work_dir_of(cfg)
    work_dir.mkdir(parents=True, exist_ok=True)
    work_lock = await acquire_work_lock(work_dir)
    hub = ExecutorHub(cfg.hub_url, cfg.hub_token)
    catalog = ModelCatalog(hub)
    silence_watch = SilenceWatch()
    gateways = GatewayModels(catalog, cfg.hub_token, silence_watch)
    park = ParkMode()
    busy = BusyTracker(cfg.owner_wait_s, park=park)
    worker_settings = WorkerSettings(
        hub_url=cfg.hub_url,
        token=cfg.hub_token,
        work_dir=work_dir,
        poll_interval_s=cfg.poll_interval_s,
        heartbeat_interval_s=cfg.heartbeat_interval_s,
        specs_dir=Path(cfg.specs_dir).expanduser() if cfg.specs_dir else None,
        park=park,
    )
    tester = TestWorker(hub, runner, worker_settings, busy)
    slots = SlotRegistry()
    kinds = cfg.agent_slot_models or [MODELS_ANY] * cfg.agent_slots
    slot_dirs = [slot_dir(work_dir, i) for i in range(len(kinds))]
    sandbox_factory = sandboxes or podman_sandbox_factory(
        cfg,
        pause=gate,
        cpus=cfg.agent_cpus / len(kinds),
        memory=split_memory(cfg.agent_memory, len(kinds)),
    )
    agents: list[AgentWorker] = []
    for i, directory in enumerate(slot_dirs):
        directory.mkdir(parents=True, exist_ok=True)
        agents.append(
            AgentWorker(
                hub,
                models or gateways,
                sandbox_factory,
                AgentSettings(
                    **{**vars(worker_settings), "work_dir": directory},
                    limits=agent_limits(cfg),
                    profiles=cfg.agent_profiles,
                    slots=slots,
                    models_kind=kinds[i],
                ),
                catalog,
                busy,
            )
        )
    service = ExecutorService(settings.node.id or socket.gethostname(), busy)
    relay = SilenceRelay(cfg.switch_url) if cfg.switch_url else None
    server = ProtoServer(cfg.socket, service, token=settings.swarm.token)
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()
    log.info(
        "Служба devlab (исполнитель) запущена: хаб %s, каталог %s, агентских слотов %d (%s)",
        hub.url,
        work_dir,
        len(kinds),
        ", ".join(kinds),
    )
    neighbor = start_following(gate, cfg.neighbor_socket or settings.llm.socket)

    await sweep_orphans(containers, slot_dirs, [job for agent in agents for job in agent.held_jobs])
    tasks = [
        asyncio.create_task(tester.run(heartbeat=False)),
        *(asyncio.create_task(agent.run(heartbeat=False)) for agent in agents),
        asyncio.create_task(
            beat_forever(
                hub,
                busy,
                (tester, *agents),
                cfg.heartbeat_interval_s,
                agents[0],
                relay=relay,
                silence_watch=silence_watch,
            )
        ),
    ]
    try:
        await lifespan.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(runner.aclose(), RUNNER_CLOSE_TIMEOUT_S)
        except TimeoutError:
            log.warning("Уборка контейнеров не закончилась за %g с", RUNNER_CLOSE_TIMEOUT_S)
        await stop_following(neighbor, gate)
        log.info("Останов службы devlab...")
        await gateways.close()
        if relay is not None:
            await relay.close()
        await server.stop()
        await hub.close()
        log.info("Служба devlab остановлена чисто")
        work_lock.close()


async def beat_forever(
    hub: ExecutorHub,
    busy: BusyTracker,
    workers: tuple[WorkerLoops, ...],
    interval_s: float,
    scrubber: WorkerLoops,
    relay: SilenceRelay | None = None,
    silence_watch: SilenceWatch | None = None,
) -> None:
    async def beat() -> None:
        jobs = [job for worker in workers for job in worker.jobs]
        progress = {key: n for worker in workers for key, n in worker.progress.items()}
        live = {key: entry for worker in workers for key, entry in worker.live.items()}
        reply = await send_heartbeat(hub, busy, jobs, progress, live)
        if silence_watch is not None and protocol.FIELD_SILENCE in reply:
            silence_watch.update(bool(reply[protocol.FIELD_SILENCE]))
        if relay is not None:
            await relay.push(reply)

    await forever(beat, interval_s, "Heartbeat", scrubber.scrub)
