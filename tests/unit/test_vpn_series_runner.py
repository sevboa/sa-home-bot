import asyncio

import pytest

from sa_home_bot.node.vpn_probe_state import ProbeSlot
from sa_home_bot.vpn_series.runner import build_curl_argv, run_burst, run_single, stream_samples


class FakeProcess:
    def __init__(self, chunks: list[bytes], exit_code: int = 0):
        self._chunks = chunks
        self._index = 0
        self.returncode = exit_code
        self.stdout = self
        self._killed = False

    async def read(self, n: int) -> bytes:
        if self._index < len(self._chunks):
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk
        return b""

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self._killed = True


class FakeProcessWithDelay:
    def __init__(self, chunks: list[bytes], delays: list[float], exit_code: int = 0):
        self._chunks = chunks
        self._delays = delays
        self._index = 0
        self.returncode = exit_code
        self.stdout = self
        self._killed = False

    async def read(self, n: int) -> bytes:
        if self._index < len(self._chunks):
            if self._index < len(self._delays):
                await asyncio.sleep(self._delays[self._index])
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk
        return b""

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self._killed = True


def make_fake_spawn(chunks: list[bytes], delays: list[float], exit_code: int = 0):
    async def fake_spawn(*args, **kwargs):
        return FakeProcessWithDelay(chunks, delays, exit_code)
    return fake_spawn


def make_slot_awg() -> ProbeSlot:
    return ProbeSlot(
        server="test-server",
        transport="awg",
        netns="test-ns",
        veth_host="veth0",
        veth_ns="veth1",
        veth_host_addr="10.9.0.1/24",
        veth_ns_addr="10.9.0.2/24",
        subnet="10.9.0.0/24",
        iface="awg0",
        socks_port=None,
    )


def make_slot_reality() -> ProbeSlot:
    return ProbeSlot(
        server="test-server",
        transport="reality",
        netns="test-ns",
        veth_host="veth0",
        veth_ns="veth1",
        veth_host_addr="10.9.0.1/24",
        veth_ns_addr="10.9.0.2/24",
        subnet="10.9.0.0/24",
        iface="awg0",
        socks_port=1080,
    )


def test_build_curl_argv_awg():
    slot = make_slot_awg()
    argv = build_curl_argv(slot, "https://example.com/file", 150.0)
    assert argv[0] == "sudo"
    assert argv[1] == "-n"
    assert "ip" in argv[2]
    assert "netns" in argv
    assert "exec" in argv
    assert "test-ns" in argv
    assert "curl" in argv
    assert "-s" in argv
    assert "-o" in argv
    assert "-" in argv
    assert "-m" in argv
    assert "150" in argv
    assert "https://example.com/file" in argv
    assert "--socks5-hostname" not in argv


def test_build_curl_argv_reality():
    slot = make_slot_reality()
    argv = build_curl_argv(slot, "https://example.com/file", 150.0)
    assert "--socks5-hostname" in argv
    assert "127.0.0.1:1080" in argv


@pytest.mark.asyncio
async def test_stream_samples_steady():
    chunks = [b"x" * 1000 for _ in range(20)]
    delays = [0.002] * 20
    fake_spawn = make_fake_spawn(chunks, delays, exit_code=0)

    samples, rc = await stream_samples(
        ["dummy"], duration_s=2.0, tick_s=0.01, spawn=fake_spawn
    )

    assert rc == 0
    assert samples[-1][1] == 20_000


@pytest.mark.asyncio
async def test_stream_samples_freeze():
    chunks = [b"x" * 16_384, b""]
    delays = [0.0, 0.001]
    fake_spawn = make_fake_spawn(chunks, delays, exit_code=28)

    samples, rc = await stream_samples(
        ["dummy"], duration_s=2.0, tick_s=0.01, spawn=fake_spawn
    )

    assert rc == 28
    assert samples[-1][1] == 16_384


@pytest.mark.asyncio
async def test_stream_samples_killed_after_deadline():
    async def slow_read(*args, **kwargs):
        await asyncio.sleep(10)
        return b""

    class SlowProc:
        def __init__(self):
            self.stdout = self
            self.returncode = 0
            self._killed = False

        async def read(self, n):
            return await slow_read()

        async def wait(self):
            return 0

        def kill(self):
            self._killed = True

    proc = SlowProc()

    async def fake_spawn(*args, **kwargs):
        return proc

    samples, rc = await stream_samples(
        ["dummy"], duration_s=0.1, tick_s=0.05, spawn=fake_spawn
    )

    assert rc is None
    assert proc._killed


@pytest.mark.asyncio
async def test_run_single_ok():
    slot = make_slot_awg()
    chunks = [b"x" * 1000 for _ in range(20)] + [b""]
    delays = [0.001] * 20 + [0.001]
    fake_spawn = make_fake_spawn(chunks, delays, exit_code=0)

    from sa_home_bot.config import VpnSeriesConfig
    cfg = VpnSeriesConfig(
        duration_s=2.0,
        tick_s=0.01,
        freeze_idle_s=0.05,
        min_kbps=0.0,
        burst_connections=6,
    )

    result = await run_single(slot, cfg, spawn=fake_spawn)

    assert result.verdict == "ok"
    assert result.bytes_total == 20_000


@pytest.mark.asyncio
async def test_run_single_freeze():
    slot = make_slot_awg()
    chunks = [b"x" * 16_384, b""]
    delays = [0.0, 0.1]
    fake_spawn = make_fake_spawn(chunks, delays, exit_code=28)

    from sa_home_bot.config import VpnSeriesConfig
    cfg = VpnSeriesConfig(
        duration_s=2.0,
        tick_s=0.01,
        freeze_idle_s=0.05,
        min_kbps=0.0,
        burst_connections=6,
    )

    result = await run_single(slot, cfg, spawn=fake_spawn)

    assert result.verdict == "freeze"
    assert result.stalled_at_bytes == 16_384


@pytest.mark.asyncio
async def test_run_burst():
    slot = make_slot_awg()
    chunks = [b"x" * 1000 for _ in range(10)] + [b""]
    delays = [0.002] * 10 + [0.0]
    fake_spawn = make_fake_spawn(chunks, delays, exit_code=0)

    from sa_home_bot.config import VpnSeriesConfig
    cfg = VpnSeriesConfig(
        duration_s=2.0,
        tick_s=0.01,
        freeze_idle_s=0.05,
        min_kbps=0.0,
        burst_connections=6,
    )

    results = await run_burst(slot, cfg, spawn=fake_spawn)

    assert len(results) == 6
    for r in results:
        assert r.verdict == "ok"


@pytest.mark.asyncio
async def test_stream_samples_tick_even_without_data():
    class HangingProc:
        def __init__(self):
            self.stdout = self
            self.returncode = 0
            self._killed = False
            self._first_read = True

        async def read(self, n):
            if self._first_read:
                self._first_read = False
                return b"x" * 1000
            await asyncio.sleep(10)
            return b""

        async def wait(self):
            return 0

        def kill(self):
            self._killed = True

    proc = HangingProc()

    async def fake_spawn(*args, **kwargs):
        return proc

    samples, rc = await stream_samples(
        ["dummy"], duration_s=0.5, tick_s=0.01, spawn=fake_spawn
    )

    assert rc is None
    assert proc._killed
    times = [s[0] for s in samples]
    assert any(abs(t - 0.01) < 0.02 for t in times)
    assert any(abs(t - 0.02) < 0.02 for t in times)
    assert any(abs(t - 0.03) < 0.02 for t in times)
    assert any(abs(t - 0.04) < 0.02 for t in times)
    assert any(abs(t - 0.05) < 0.02 for t in times)
