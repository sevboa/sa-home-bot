import asyncio

import pytest

from sa_home_bot.config import DevlabConfig
from sa_home_bot.devlab.executor import wrapper
from sa_home_bot.devlab.executor.wrapper import RunnerError, WrapperClient
from sa_home_bot.devlab.neighbor import NeighborGate
from sa_home_bot.utils.process import ProcResult
from tests.unit.support.fakes import Clock


class FakeSpawn:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.release = asyncio.Event()
        self.exec_started = asyncio.Event()
        self.interrupt_rc = 0
        self.fail: set[str] = set()

    def commands(self) -> list[str]:
        return [argv[5] for argv in self.calls]

    async def __call__(self, argv, *, input=None, timeout=None, **kwargs) -> ProcResult:
        self.calls.append(list(argv))
        command = argv[5]
        if command in self.fail:
            return ProcResult(1, b"boom", b"")
        if command == "exec":
            self.exec_started.set()
            await self.release.wait()
            return ProcResult(143 if "interrupt" in self.commands() else 0, b"partial\n", b"")
        return ProcResult(0, b"", b"")


def make_gate(clock: Clock) -> tuple[NeighborGate, list[str]]:
    calls: list[str] = []

    async def freeze() -> None:
        calls.append("freeze")

    async def thaw() -> None:
        calls.append("thaw")

    gate = NeighborGate(freeze=freeze, thaw=thaw, freeze_max_s=300.0, clock=lambda: clock.t)
    return gate, calls


@pytest.fixture(autouse=True)
def fast_ticks(monkeypatch):
    monkeypatch.setattr(wrapper, "EXEC_TICK_S", 0.01)
    monkeypatch.setattr(wrapper, "EXEC_GRACE_S", 0.05)


async def test_exec_without_freeze_times_out_on_wall_clock_and_keeps_partial_output():
    spawn = FakeSpawn()
    client = WrapperClient(DevlabConfig(), spawn)

    async def release_after_interrupt():
        while "interrupt" not in spawn.commands():
            await asyncio.sleep(0.005)
        spawn.release.set()

    helper = asyncio.create_task(release_after_interrupt())
    result = await client.exec("job1", ["sleep", "9"], 0.01)
    await helper
    assert result.timed_out and result.output == b"partial\n"
    assert spawn.commands() == ["exec", "interrupt"]


async def test_timeout_does_not_count_frozen_time():
    clock = Clock(0.0)
    gate, _ = make_gate(clock)
    spawn = FakeSpawn()
    client = WrapperClient(DevlabConfig(), spawn, gate, clock=lambda: clock.t)
    run = asyncio.create_task(client.exec("job1", ["pytest"], 100))
    await spawn.exec_started.wait()
    clock.advance(seconds=60)
    await gate.task_started("a")
    clock.advance(seconds=250)
    await asyncio.sleep(0.05)
    assert not run.done()
    await gate.task_finished("a")
    clock.advance(seconds=30)
    await asyncio.sleep(0.05)
    assert not run.done()
    assert "interrupt" not in spawn.commands()
    clock.advance(seconds=20)
    for _ in range(100):
        if "interrupt" in spawn.commands():
            break
        await asyncio.sleep(0.01)
    assert "interrupt" in spawn.commands()
    spawn.release.set()
    result = await run
    assert result.timed_out


async def test_exec_in_flight_finishing_in_time_is_not_a_timeout():
    spawn = FakeSpawn()
    client = WrapperClient(DevlabConfig(), spawn)
    spawn.release.set()
    result = await client.exec("job1", ["true"], 5)
    assert result.exit_code == 0 and not result.timed_out and result.output == b"partial\n"


async def test_exec_waits_until_containers_are_unpaused():
    clock = Clock(0.0)
    gate, _ = make_gate(clock)
    spawn = FakeSpawn()
    spawn.release.set()
    client = WrapperClient(DevlabConfig(), spawn, gate, clock=lambda: clock.t)
    await gate.task_started("a")
    run = asyncio.create_task(client.exec("job1", ["true"], 5))
    await asyncio.sleep(0.05)
    assert spawn.calls == []
    await gate.task_finished("a")
    result = await run
    assert result.exit_code == 0 and spawn.commands() == ["exec"]


async def test_container_created_during_freeze_is_paused_at_once():
    clock = Clock(0.0)
    gate, _ = make_gate(clock)
    spawn = FakeSpawn()
    client = WrapperClient(DevlabConfig(), spawn, gate)
    await gate.task_started("a")
    await client.create("job1", "tag", 2, "1g", "0-1")
    assert spawn.commands() == ["rm", "create", "pause"]
    assert spawn.calls[-1][6:] == ["job1"]
    await gate.close()


async def test_create_without_freeze_does_not_pause():
    spawn = FakeSpawn()
    await WrapperClient(DevlabConfig(), spawn).create("job1", "tag", 2, "1g", "0-1")
    assert spawn.commands() == ["rm", "create"]


async def test_pause_and_unpause_all_and_one():
    spawn = FakeSpawn()
    client = WrapperClient(DevlabConfig(), spawn)
    await client.pause()
    await client.unpause()
    await client.pause("job1")
    assert [argv[5:] for argv in spawn.calls] == [["pause"], ["unpause"], ["pause", "job1"]]


async def test_pause_failure_raises_runner_error():
    spawn = FakeSpawn()
    spawn.fail.add("pause")
    with pytest.raises(RunnerError, match="пауза"):
        await WrapperClient(DevlabConfig(), spawn).pause()
