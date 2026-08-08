"""Супервизия: старт, падение → рестарт → события, ручной стоп, рестарт-команда."""

import asyncio
import contextlib
import logging

import pytest

from sa_home_bot.node.supervisor import (
    EVENT_SERVICE_FAILED,
    EVENT_SERVICE_STARTED,
    EVENT_SERVICE_STOPPED,
    RUNNING,
    STOPPED,
    SupervisedService,
    Supervisor,
    spawn_kwargs,
    terminate_gracefully,
)


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []
        self._waiters: list[tuple[str, asyncio.Event]] = []

    async def emit(self, event_type: str, data: dict) -> None:
        self.items.append((event_type, data))
        for expected, flag in self._waiters:
            if event_type == expected:
                flag.set()

    async def wait_for(self, event_type: str, count: int = 1, timeout: float = 10.0) -> None:
        async with asyncio.timeout(timeout):
            while sum(1 for t, _ in self.items if t == event_type) < count:
                await asyncio.sleep(0.02)


def _service(events: Events, code: str, **kw) -> SupervisedService:
    """Служба-подопытный: python -c "<code>" вместо sa-home-bot."""
    svc = SupervisedService(
        "fake",
        ["-c", code],
        emit=events.emit,
        restart_delay_s=kw.get("restart_delay_s", 0.1),
        stop_timeout_s=kw.get("stop_timeout_s", 5.0),
    )
    # Подменяем модульный запуск на python -c (см. _run: -m sa_home_bot).
    return svc


@pytest.fixture(autouse=True)
def _patch_module_launch(monkeypatch):
    """Запускаем python -c <code> вместо python -m sa_home_bot <args>."""
    import sa_home_bot.node.supervisor as sup

    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(python, dash_m, module, *args, **kwargs):
        assert (dash_m, module) == ("-m", "sa_home_bot")
        return await real_exec(python, *args, **kwargs)

    monkeypatch.setattr(sup.asyncio, "create_subprocess_exec", fake_exec)


async def test_crash_restart_and_events():
    events = Events()
    svc = _service(events, "import sys; sys.exit(3)")
    await svc.start()

    # Падает → событие failed → рестарт → снова started.
    await events.wait_for(EVENT_SERVICE_FAILED, count=1)
    await events.wait_for(EVENT_SERVICE_STARTED, count=2)
    assert svc.restarts >= 1
    assert svc.last_exit_code == 3

    await svc.stop()
    assert svc.status == STOPPED
    assert events.items[-1][0] == EVENT_SERVICE_STOPPED


async def test_long_running_service_stays_up_and_stops_cleanly():
    events = Events()
    svc = _service(events, "import time; time.sleep(60)")
    await svc.start()
    await events.wait_for(EVENT_SERVICE_STARTED)
    assert svc.status == RUNNING
    assert svc.pid is not None

    await svc.stop()
    assert svc.status == STOPPED
    # Ручной стоп — это НЕ падение: failed не эмитился, рестартов нет.
    types = [t for t, _ in events.items]
    assert EVENT_SERVICE_FAILED not in types
    assert svc.restarts == 0


async def test_restart_command():
    events = Events()
    svc = _service(events, "import time; time.sleep(60)")
    await svc.start()
    await events.wait_for(EVENT_SERVICE_STARTED)
    first_pid = svc.pid

    await svc.restart()
    await events.wait_for(EVENT_SERVICE_STARTED, count=2)
    assert svc.status == RUNNING
    assert svc.pid != first_pid

    await svc.stop()


async def test_sigkill_after_stop_timeout():
    events = Events()
    # Служба игнорирует SIGTERM — стоп должен добить SIGKILL'ом за stop_timeout.
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    svc = _service(events, code, stop_timeout_s=0.5)
    await svc.start()
    await events.wait_for(EVENT_SERVICE_STARTED)
    # Дать процессу успеть поставить обработчик сигнала.
    await asyncio.sleep(0.5)

    async with asyncio.timeout(10):
        await svc.stop()
    assert svc.status == STOPPED


# --- устойчивость цикла наблюдения к исключениям (фикс 2026-08-08) ---


async def test_service_restarts_even_if_emit_raises():
    """Падение самого emit (например broadcast упавшему клиенту в момент
    нестабильной сети) не должно убивать наблюдающую задачу навсегда —
    иначе служба остаётся «ничьей» без единого следа в логе (живой инцидент:
    telegram-bot@alfred провисел без перезапуска ~20 минут)."""

    async def raising_emit(event_type: str, data: dict) -> None:
        raise RuntimeError("нода недоступна прямо сейчас")

    svc = SupervisedService(
        "fake", ["-c", "import sys; sys.exit(3)"],
        emit=raising_emit, restart_delay_s=0.02,
    )
    await svc.start()

    async with asyncio.timeout(10):
        while svc.restarts < 3:
            await asyncio.sleep(0.02)

    assert svc.restarts >= 3  # продолжает падать/перезапускаться, а не молчит
    # stop() эмитит EVENT_SERVICE_STOPPED сам, вне цикла наблюдения — не
    # предмет этого теста (тот же raising_emit уронит и его, это ожидаемо).
    with contextlib.suppress(RuntimeError):
        await svc.stop()


async def test_on_spawned_called_on_every_spawn():
    events = Events()
    spawned: list[str] = []
    svc = SupervisedService(
        "fake", ["-c", "import sys; sys.exit(3)"],
        emit=events.emit, restart_delay_s=0.02, on_spawned=spawned.append,
    )
    await svc.start()
    async with asyncio.timeout(10):
        while len(spawned) < 3:
            await asyncio.sleep(0.02)
    assert spawned == ["fake"] * len(spawned)
    await svc.stop()


async def test_on_task_done_logs_unhandled_exception(caplog):
    """Диагностическая страховка: если _run() всё же умрёт необработанной —
    это не должно теряться молча (в реальном инциденте не нашлось даже
    "Task exception was never retrieved" — self._task держит ссылку и
    не даёт задаче собраться GC)."""
    events = Events()
    svc = SupervisedService("fake", ["-c", "pass"], emit=events.emit)

    async def boom() -> None:
        raise RuntimeError("наблюдающая задача упала")

    task = asyncio.create_task(boom())
    with contextlib.suppress(RuntimeError):
        await task

    with caplog.at_level(logging.ERROR):
        svc._on_task_done(task)
    assert "необработанным исключением" in caplog.text


async def test_supervisor_skips_unknown_assignment():
    events = Events()
    sup = Supervisor(
        ["monitor", "no-such-service"], None, emit=events.emit
    )
    assert list(sup.services) == ["monitor"]
    assert sup.get("no-such-service") is None


async def test_supervisor_skips_externally_managed_llm_silently():
    # WSL2 требует интерактивную сессию, которой у Session-0 службы нет
    # (живая находка 2026-07-23) — супервизор не пытается спавнить "llm",
    # но это не "неизвестное назначение" (не логируется как ошибка), а
    # намеренный пропуск: маршрутизация к ней всё равно строится отдельно
    # в build_router() по тому же списку assignments (node/app.py).
    events = Events()
    sup = Supervisor(["monitor", "llm"], None, emit=events.emit)
    assert list(sup.services) == ["monitor"]
    assert sup.get("llm") is None


# --- assign/unassign: назначения в рантайме, без рестарта ноды ---


async def test_assign_adds_service_without_starting_it():
    events = Events()
    sup = Supervisor([], None, emit=events.emit)
    svc = sup.assign("apps")
    assert list(sup.services) == ["apps"]
    assert svc.status == STOPPED  # assign не стартует сама, это отдельный шаг


async def test_assign_is_idempotent_returns_same_instance():
    events = Events()
    sup = Supervisor(["monitor"], None, emit=events.emit)
    existing = sup.get("monitor")
    assert sup.assign("monitor") is existing  # не пересоздаёт уже назначенную


def test_assign_unknown_name_raises():
    events = Events()
    sup = Supervisor([], None, emit=events.emit)
    with pytest.raises(ValueError):
        sup.assign("no-such-service")


async def test_unassign_removes_and_stops_service():
    events = Events()
    sup = Supervisor(["apps"], None, emit=events.emit)
    await sup.unassign("apps")
    assert list(sup.services) == []
    assert sup.get("apps") is None


async def test_unassign_unknown_name_is_noop():
    events = Events()
    sup = Supervisor([], None, emit=events.emit)
    await sup.unassign("no-such-service")  # не бросает


# --- Windows-специфика: process group при спавне, CTRL_BREAK вместо terminate ---


def test_spawn_kwargs_empty_on_linux():
    assert spawn_kwargs() == {}


def test_spawn_kwargs_new_process_group_on_windows(monkeypatch):
    import subprocess
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    assert spawn_kwargs() == {"creationflags": 0x200}


class _FakeProc:
    def __init__(self) -> None:
        self.calls: list = []

    def terminate(self) -> None:
        self.calls.append("terminate")

    def send_signal(self, sig) -> None:
        self.calls.append(("signal", sig))


def test_terminate_gracefully_sigterm_on_linux():
    proc = _FakeProc()
    terminate_gracefully(proc)
    assert proc.calls == ["terminate"]


def test_terminate_gracefully_ctrl_break_on_windows(monkeypatch):
    import signal
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 21, raising=False)
    proc = _FakeProc()
    terminate_gracefully(proc)
    assert proc.calls == [("signal", 21)]
