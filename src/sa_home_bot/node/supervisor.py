"""Супервизор: дочерние процессы служб, рестарт упавших, события жизненного цикла.

Одна ``SupervisedService`` = одно назначение. Цикл наблюдения: запустить →
ждать выхода → если не останавливали сами, эмитить ``service_failed``, выждать
паузу и перезапустить. Остановка — SIGTERM, по таймауту SIGKILL. События уходят
через async-callback ``emit(event_type, data)`` — в приложении это broadcast
proto-сервера ноды, в тестах — список.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

log = logging.getLogger(__name__)

EventEmitter = Callable[[str, dict], Awaitable[None]]

# Статусы службы (наружу, в get_state ноды).
RUNNING = "running"
RESTARTING = "restarting"  # упала, ждёт паузу перед перезапуском
STOPPED = "stopped"  # остановлена вручную или ещё не запускалась

EVENT_SERVICE_STARTED = "service_started"
EVENT_SERVICE_FAILED = "service_failed"
EVENT_SERVICE_STOPPED = "service_stopped"

# Известные назначения: имя → аргументы CLI этого же пакета.
ASSIGNMENT_ARGS: dict[str, list[str]] = {
    "monitor": ["--service", "monitor"],
    "telegram-bot": ["--service", "bot"],
    "apps": ["--service", "apps"],
}


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class SupervisedService:
    """Одна служба под супервизией: процесс + цикл наблюдения."""

    def __init__(
        self,
        name: str,
        cli_args: list[str],
        *,
        emit: EventEmitter,
        restart_delay_s: float = 5.0,
        stop_timeout_s: float = 90.0,
    ) -> None:
        self.name = name
        self._cli_args = cli_args
        self._emit = emit
        self._restart_delay = restart_delay_s
        self._stop_timeout = stop_timeout_s

        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._spawned = asyncio.Event()  # первый запуск процесса состоялся
        self._desired_running = False
        self._status = STOPPED
        self.restarts = 0  # перезапусков после падений
        self.last_exit_code: int | None = None
        self.started_at: str | None = None

    # --- Наружу (get_state ноды) ---

    @property
    def status(self) -> str:
        return self._status

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self._status,
            "pid": self.pid,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit_code,
            "started_at": self.started_at,
        }

    # --- Управление ---

    async def start(self) -> None:
        if self._desired_running:
            return
        self._desired_running = True
        self._spawned.clear()
        self._task = asyncio.create_task(self._run(), name=f"supervise-{self.name}")
        # Дождаться фактического запуска, чтобы ответ start/restart отражал
        # реальное состояние, а не снимок до спавна процесса.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._spawned.wait(), timeout=5.0)

    async def stop(self) -> None:
        if not self._desired_running:
            return
        self._desired_running = False
        proc = self._proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._stop_timeout)
            except TimeoutError:
                log.warning("Служба %s не остановилась за %.0f с — SIGKILL",
                            self.name, self._stop_timeout)
                proc.kill()
                await proc.wait()
        if self._task is not None:
            await self._task
            self._task = None
        self._status = STOPPED
        await self._emit(EVENT_SERVICE_STOPPED, {"name": self.name})

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # --- Цикл наблюдения ---

    async def _run(self) -> None:
        while self._desired_running:
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "sa_home_bot", *self._cli_args
                )
            except OSError as exc:
                log.error("Не удалось запустить службу %s: %s", self.name, exc)
                self._spawned.set()  # попытка была — start() не должен висеть
                await self._emit(
                    EVENT_SERVICE_FAILED, {"name": self.name, "error": str(exc)}
                )
                self._status = RESTARTING
                await asyncio.sleep(self._restart_delay)
                continue

            self._proc = proc
            self._status = RUNNING
            self._spawned.set()
            self.started_at = _now_iso()
            log.info("Служба %s запущена (pid=%s)", self.name, proc.pid)
            await self._emit(EVENT_SERVICE_STARTED, {"name": self.name, "pid": proc.pid})

            rc = await proc.wait()
            self._proc = None
            self.last_exit_code = rc
            if not self._desired_running:
                break  # остановили сами — stop() эмитит service_stopped
            log.warning("Служба %s завершилась (код %s) — перезапуск через %.0f с",
                        self.name, rc, self._restart_delay)
            self._status = RESTARTING
            self.restarts += 1
            await self._emit(
                EVENT_SERVICE_FAILED, {"name": self.name, "exit_code": rc}
            )
            await asyncio.sleep(self._restart_delay)


class Supervisor:
    """Набор служб ноды по конфигу назначений."""

    def __init__(
        self,
        assignments: list[str],
        config_path: str | None,
        *,
        emit: EventEmitter,
        restart_delay_s: float = 5.0,
        stop_timeout_s: float = 90.0,
    ) -> None:
        self.services: dict[str, SupervisedService] = {}
        for name in assignments:
            args = ASSIGNMENT_ARGS.get(name)
            if args is None:
                log.error("Неизвестное назначение %r — пропускаю "
                          "(знаю: %s)", name, ", ".join(ASSIGNMENT_ARGS))
                continue
            cli_args = list(args)
            if config_path is not None:
                cli_args += ["--config", str(config_path)]
            self.services[name] = SupervisedService(
                name,
                cli_args,
                emit=emit,
                restart_delay_s=restart_delay_s,
                stop_timeout_s=stop_timeout_s,
            )

    async def start_all(self) -> None:
        for svc in self.services.values():
            await svc.start()

    async def stop_all(self) -> None:
        # Останавливаем в обратном порядке назначений (бот раньше монитора).
        for svc in reversed(list(self.services.values())):
            with contextlib.suppress(Exception):
                await svc.stop()

    def get(self, name: str) -> SupervisedService | None:
        return self.services.get(name)
