from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable

from sa_home_bot.node import assignments as assignments_mod
from sa_home_bot.node.assignments import Assignment
from sa_home_bot.services import registry
from sa_home_bot.utils.clock import utc_now

log = logging.getLogger(__name__)


def spawn_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


def terminate_gracefully(proc: asyncio.subprocess.Process) -> None:
    if sys.platform == "win32":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.terminate()

EventEmitter = Callable[[str, dict], Awaitable[None]]

RUNNING = "running"
RESTARTING = "restarting"
STOPPED = "stopped"

EVENT_SERVICE_STARTED = "service_started"
EVENT_SERVICE_FAILED = "service_failed"
EVENT_SERVICE_STOPPED = "service_stopped"
EVENT_SERVICE_FENCED = "service_fenced"

FENCED_EXIT_CODE = 11

SUPERVISE_EXIT_TIMEOUT_S = 5.0


class SupervisedService:
    def __init__(
        self,
        name: str,
        cli_args: list[str],
        *,
        emit: EventEmitter,
        restart_delay_s: float = 5.0,
        stop_timeout_s: float = 90.0,
        assignment: Assignment | None = None,
        on_fenced: Callable[[str], None] | None = None,
        on_spawned: Callable[[str], None] | None = None,
    ) -> None:
        self.name = name
        self._on_fenced = on_fenced
        self._on_spawned = on_spawned
        self.assignment = assignment or Assignment(service=name)
        self._cli_args = cli_args
        self._emit = emit
        self._restart_delay = restart_delay_s
        self._stop_timeout = stop_timeout_s

        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._spawned = asyncio.Event()
        self._desired_running = False
        self._status = STOPPED
        self.restarts = 0
        self.last_exit_code: int | None = None
        self.started_at: str | None = None


    @property
    def status(self) -> str:
        return self._status

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def process_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self._status,
            "pid": self.pid,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit_code,
            "started_at": self.started_at,
            "service": self.assignment.service,
            "instance": self.assignment.instance,
            "role": self.assignment.role,
        }


    async def start(self) -> None:
        if self._desired_running:
            return
        self._desired_running = True
        self._spawned.clear()
        self._task = asyncio.create_task(self._run(), name=f"supervise-{self.name}")
        self._task.add_done_callback(self._on_task_done)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._spawned.wait(), timeout=5.0)

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "Служба %s: наблюдающая задача завершилась с необработанным "
                "исключением — служба больше НЕ супервизируется",
                self.name, exc_info=exc,
            )

    async def stop(self) -> None:
        if not self._desired_running:
            return
        self._desired_running = False
        proc = self._proc
        if proc is not None and proc.returncode is None:
            terminate_gracefully(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._stop_timeout)
            except TimeoutError:
                log.warning("Служба %s не остановилась за %.0f с — SIGKILL",
                            self.name, self._stop_timeout)
                proc.kill()
                await proc.wait()
        if self._task is not None:
            task, self._task = self._task, None
            done, _ = await asyncio.wait({task}, timeout=SUPERVISE_EXIT_TIMEOUT_S)
            if not done:
                log.warning(
                    "Служба %s: наблюдающая задача не завершилась за %.0f с — снимаю",
                    self.name,
                    SUPERVISE_EXIT_TIMEOUT_S,
                )
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._status = STOPPED
        await self._emit(EVENT_SERVICE_STOPPED, {"name": self.name})

    async def restart(self) -> None:
        await self.stop()
        await self.start()


    async def _run(self) -> None:
        while self._desired_running:
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "sa_home_bot", *self._cli_args, **spawn_kwargs()
                )
            except OSError as exc:
                log.error("Не удалось запустить службу %s: %s", self.name, exc)
                self._spawned.set()
                await self._emit(
                    EVENT_SERVICE_FAILED, {"name": self.name, "error": str(exc)}
                )
                self._status = RESTARTING
                await asyncio.sleep(self._restart_delay)
                continue

            try:
                self._proc = proc
                self._status = RUNNING
                self._spawned.set()
                self.started_at = utc_now().isoformat()
                log.info("Служба %s запущена (pid=%s)", self.name, proc.pid)
                if self._on_spawned is not None:
                    self._on_spawned(self.name)
                await self._emit(EVENT_SERVICE_STARTED, {"name": self.name, "pid": proc.pid})

                rc = await proc.wait()
                self._proc = None
                self.last_exit_code = rc
                if not self._desired_running:
                    log.info(
                        "Служба %s: процесс завершился (код %s) во время "
                        "остановки (desired_running уже снят) — не перезапускаю",
                        self.name, rc,
                    )
                    break
                if rc == FENCED_EXIT_CODE:
                    log.warning("Служба %s вытеснена другим экземпляром — не перезапускаю",
                                self.name)
                    self._desired_running = False
                    self._status = STOPPED
                    await self._emit(EVENT_SERVICE_FENCED, {"name": self.name})
                    if self._on_fenced is not None:
                        self._on_fenced(self.name)
                    break
                log.warning("Служба %s завершилась (код %s) — перезапуск через %.0f с",
                            self.name, rc, self._restart_delay)
                self._status = RESTARTING
                self.restarts += 1
                await self._emit(
                    EVENT_SERVICE_FAILED, {"name": self.name, "exit_code": rc}
                )
            except Exception:
                log.exception(
                    "Служба %s: необработанная ошибка в цикле наблюдения — "
                    "считаю падением, перезапуск через %.0f с",
                    self.name, self._restart_delay,
                )
                self._proc = None
                if not self._desired_running:
                    break
                self._status = RESTARTING
                self.restarts += 1
                with contextlib.suppress(Exception):
                    await self._emit(
                        EVENT_SERVICE_FAILED,
                        {"name": self.name, "error": "supervisor_internal_error"},
                    )

            await asyncio.sleep(self._restart_delay)


class Supervisor:
    def __init__(
        self,
        assignments: list[str],
        config_path: str | None,
        *,
        emit: EventEmitter,
        restart_delay_s: float = 5.0,
        stop_timeout_s: float = 90.0,
        on_fenced: Callable[[str], None] | None = None,
        on_spawned: Callable[[str], None] | None = None,
    ) -> None:
        self.services: dict[str, SupervisedService] = {}
        self._config_path = config_path
        self._emit = emit
        self._on_fenced = on_fenced
        self._on_spawned = on_spawned
        self._restart_delay_s = restart_delay_s
        self._stop_timeout_s = stop_timeout_s
        for item in assignments:
            try:
                assignment = assignments_mod.parse(item)
            except assignments_mod.AssignmentError as exc:
                log.error("Назначение %r не разбирается (%s) — пропускаю", item, exc)
                continue
            svc_spec = registry.spec(assignment.service)
            if svc_spec is not None and svc_spec.externally_managed:
                log.info(
                    "Назначение %r — внешне управляемый процесс, супервизор его не спавнит "
                    "(только маршрутизация)", item
                )
                continue
            try:
                self.services[assignment.key] = self._make_service(assignment)
            except ValueError:
                log.error("Неизвестное назначение %r — пропускаю "
                          "(знаю: %s)", item, ", ".join(registry.supervised_names()))

    def _make_service(self, assignment: str | Assignment) -> SupervisedService:
        if isinstance(assignment, str):
            assignment = assignments_mod.parse(assignment)
        svc_spec = registry.spec(assignment.service)
        if svc_spec is None or svc_spec.externally_managed:
            raise ValueError(f"неизвестное назначение: {assignment.service!r}")
        cli_args = svc_spec.cli_args
        if assignment.instance:
            cli_args += ["--instance", assignment.instance]
        if self._config_path is not None:
            cli_args += ["--config", str(self._config_path)]
        return SupervisedService(
            assignment.key,
            cli_args,
            emit=self._emit,
            restart_delay_s=self._restart_delay_s,
            stop_timeout_s=self._stop_timeout_s,
            assignment=assignment,
            on_fenced=self._on_fenced,
            on_spawned=self._on_spawned,
        )

    async def start_all(self) -> None:
        for svc in self.services.values():
            spec = registry.spec(svc.assignment.service)
            if spec is not None and spec.singleton:
                log.info("Служба %s — синглтон: запуск решает аренда лидерства", svc.name)
                continue
            await svc.start()

    async def stop_all(self) -> None:
        for svc in reversed(list(self.services.values())):
            with contextlib.suppress(Exception):
                await svc.stop()

    def get(self, name: str) -> SupervisedService | None:
        return self.services.get(name)

    def assign(self, name: str) -> SupervisedService:
        assignment = assignments_mod.parse(name)
        existing = self.services.get(assignment.key)
        if existing is not None:
            return existing
        svc = self._make_service(assignment)
        self.services[assignment.key] = svc
        return svc

    async def unassign(self, name: str) -> None:
        svc = self.services.pop(name, None)
        if svc is not None:
            await svc.stop()
