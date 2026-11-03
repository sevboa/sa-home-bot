from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path

from sa_home_bot.config import DevlabConfig, Settings
from sa_home_bot.devlab.agent.loop import AgentLimits
from sa_home_bot.devlab.agent.model import ModelFactory
from sa_home_bot.devlab.executor.agent_sandbox import SandboxFactory, podman_sandbox_factory
from sa_home_bot.devlab.executor.agent_worker import AgentSettings, AgentWorker, SlotRegistry
from sa_home_bot.devlab.executor.busy import BusyTracker
from sa_home_bot.devlab.executor.hub import ExecutorHub
from sa_home_bot.devlab.executor.podman import PodmanTestRunner
from sa_home_bot.devlab.executor.routing import GatewayModels, ModelCatalog
from sa_home_bot.devlab.executor.runner import TestRunner
from sa_home_bot.devlab.executor.service import ExecutorService
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


def build_runner(cfg: DevlabConfig, pause: PauseClock | None = None) -> TestRunner:
    return PodmanTestRunner(cfg, pause=pause)


def work_dir_of(cfg: DevlabConfig) -> Path:
    return Path(cfg.work_dir) if cfg.work_dir else Path(cfg.root) / EXECUTOR_DIR


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


def _parse_memory_gb(memory: str) -> int:
    memory = memory.lower().strip()
    if memory.endswith("g"):
        return int(memory[:-1])
    if memory.endswith("m"):
        return max(1, int(memory[:-1]) // 1024)
    if memory.endswith("k"):
        return max(1, int(memory[:-1]) // (1024 * 1024))
    return int(memory)


def _format_memory_gb(gb: int) -> str:
    return f"{gb}g"


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
    hub = ExecutorHub(cfg.hub_url, cfg.hub_token)
    catalog = ModelCatalog(hub)
    gateways = GatewayModels(catalog, cfg.hub_token)
    busy = BusyTracker(cfg.owner_wait_s)
    tester = TestWorker(hub, runner, WorkerSettings(
        hub_url=cfg.hub_url,
        token=cfg.hub_token,
        work_dir=work_dir,
        poll_interval_s=cfg.poll_interval_s,
        heartbeat_interval_s=cfg.heartbeat_interval_s,
    ), busy)

    slot_registry = SlotRegistry()
    agent_workers: list[AgentWorker] = []
    agent_slots = cfg.agent_slots
    base_cpus = cfg.agent_cpus
    base_memory_gb = _parse_memory_gb(cfg.agent_memory)
    per_slot_cpus = base_cpus / agent_slots
    per_slot_memory_gb = max(2, base_memory_gb // agent_slots)

    for i in range(agent_slots):
        if i == 0:
            slot_work_dir = work_dir
        else:
            slot_work_dir = work_dir / f"agent-{i}"
            slot_work_dir.mkdir(parents=True, exist_ok=True)

        worker_settings = WorkerSettings(
            hub_url=cfg.hub_url,
            token=cfg.hub_token,
            work_dir=slot_work_dir,
            poll_interval_s=cfg.poll_interval_s,
            heartbeat_interval_s=cfg.heartbeat_interval_s,
        )
        agent_settings = AgentSettings(
            **vars(worker_settings),
            limits=agent_limits(cfg),
            slot_index=i,
            slot_registry=slot_registry,
        )
        agent = AgentWorker(
            hub,
            models or gateways,
            sandboxes
            or podman_sandbox_factory(
                cfg, pause=gate, cpus=per_slot_cpus, memory=_format_memory_gb(per_slot_memory_gb)
            ),
            agent_settings,
            catalog,
            busy,
        )
        agent_workers.append(agent)

    service = ExecutorService(settings.node.id or socket.gethostname(), busy)
    server = ProtoServer(cfg.socket, service, token=settings.swarm.token)
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()
    log.info(
        "Служба devlab (исполнитель) запущена: хаб %s, каталог %s, слотов агентов %d",
        hub.url,
        work_dir,
        agent_slots,
    )
    neighbor = start_following(gate, cfg.neighbor_socket or settings.llm.socket)

    def all_held_jobs() -> list[str]:
        jobs: list[str] = []
        for aw in agent_workers:
            jobs.extend(aw.held_jobs)
        return jobs

    await sweep_orphans(containers, work_dir, all_held_jobs())
    tasks = [
        asyncio.create_task(tester.run(heartbeat=False)),
        *[asyncio.create_task(aw.run(heartbeat=False)) for aw in agent_workers],
        asyncio.create_task(
            beat_forever(hub, busy, (tester, *agent_workers), cfg.heartbeat_interval_s, tester)
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
        await server.stop()
        await hub.close()
        log.info("Служба devlab остановлена чисто")


async def beat_forever(
    hub: ExecutorHub,
    busy: BusyTracker,
    workers: tuple[WorkerLoops, ...],
    interval_s: float,
    scrubber: WorkerLoops,
) -> None:
    async def beat() -> None:
        jobs = [job for worker in workers for job in worker.jobs]
        progress = {key: n for worker in workers for key, n in worker.progress.items()}
        await send_heartbeat(hub, busy, jobs, progress)

    await forever(beat, interval_s, "Heartbeat", scrubber.scrub)
