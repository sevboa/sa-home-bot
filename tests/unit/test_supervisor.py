import asyncio
import contextlib
import logging

import pytest

from sa_home_bot.node.assignments import ROLE_STANDBY
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
    svc = SupervisedService(
        "fake",
        ["-c", code],
        emit=events.emit,
        restart_delay_s=kw.get("restart_delay_s", 0.1),
        stop_timeout_s=kw.get("stop_timeout_s", 5.0),
    )
    return svc


@pytest.fixture(autouse=True)
def _patch_module_launch(monkeypatch):
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
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    svc = _service(events, code, stop_timeout_s=0.5)
    await svc.start()
    await events.wait_for(EVENT_SERVICE_STARTED)
    await asyncio.sleep(0.5)

    async with asyncio.timeout(10):
        await svc.stop()
    assert svc.status == STOPPED


async def test_service_restarts_even_if_emit_raises():
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

    assert svc.restarts >= 3
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
    events = Events()
    sup = Supervisor(["monitor", "llm"], None, emit=events.emit)
    assert list(sup.services) == ["monitor"]
    assert sup.get("llm") is None


def test_supervisor_keys_slots_by_service_and_instance():
    sup = Supervisor(["telegram-bot@alfred", "telegram-bot@work"], None, emit=Events().emit)
    assert set(sup.services) == {"telegram-bot@alfred", "telegram-bot@work"}


def test_instanced_service_is_spawned_with_its_package():
    sup = Supervisor(["telegram-bot@alfred"], "/etc/sa/config.toml", emit=Events().emit)
    args = sup.services["telegram-bot@alfred"]._cli_args
    assert args == ["--service", "bot", "--instance", "alfred",
                    "--config", "/etc/sa/config.toml"]


async def test_standby_is_not_started_by_node_startup():
    sup = Supervisor(["telegram-bot@alfred:standby"], None, emit=Events().emit)
    await sup.start_all()
    slot = sup.services["telegram-bot@alfred"]
    assert slot.status == "stopped"
    assert slot.pid is None


def test_malformed_assignment_is_skipped_not_fatal(caplog):
    sup = Supervisor(["monitor", "@broken"], None, emit=Events().emit)
    assert set(sup.services) == {"monitor"}


def test_slot_dict_exposes_role_and_instance():
    sup = Supervisor(["telegram-bot@alfred:standby"], None, emit=Events().emit)
    data = sup.services["telegram-bot@alfred"].to_dict()
    assert data["service"] == "telegram-bot"
    assert data["instance"] == "alfred"
    assert data["role"] == ROLE_STANDBY


async def test_assign_adds_service_without_starting_it():
    events = Events()
    sup = Supervisor([], None, emit=events.emit)
    svc = sup.assign("apps")
    assert list(sup.services) == ["apps"]
    assert svc.status == STOPPED


async def test_assign_is_idempotent_returns_same_instance():
    events = Events()
    sup = Supervisor(["monitor"], None, emit=events.emit)
    existing = sup.get("monitor")
    assert sup.assign("monitor") is existing


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
    await sup.unassign("no-such-service")


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
