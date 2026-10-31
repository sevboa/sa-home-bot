from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.config import DevlabModelConfig, DevlabModelsConfig
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.models.backends import (
    LLAMA_CRASH_WINDOW_S,
    LLAMA_MAX_CRASHES,
    LlamaCppBackend,
    ModelUnavailableError,
    build_backend,
    model_unit,
)
from sa_home_bot.devlab.models.gateway import Gateway
from sa_home_bot.devlab.models.ledger import Ledger
from sa_home_bot.devlab.models.limits import RateWindow
from sa_home_bot.utils.process import ProcResult

MODEL = "flint"
UNIT = model_unit(MODEL) + ".service"
GGUF = "/models/flint.gguf"
CHAT = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
IDLE_S = 600


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


class FakeSystemd:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.active = False
        self.start_rc = 0
        self.die_on_start = False

    async def __call__(self, argv: list[str]) -> ProcResult:
        self.calls.append(list(argv))
        if argv[0] == "systemd-run":
            if self.start_rc == 0:
                self.active = not self.die_on_start
            return ProcResult(self.start_rc, b"", b"boom" if self.start_rc else b"")
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return ProcResult(0 if self.active else 3, b"", b"")
        if argv[:3] == ["systemctl", "--user", "stop"]:
            self.active = False
        return ProcResult(0, b"", b"")

    def starts(self) -> int:
        return sum(1 for c in self.calls if c[0] == "systemd-run")

    def stops(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["systemctl", "--user", "stop"]]


class FakeLlama:
    def __init__(self) -> None:
        self.loading_polls = 0
        self.health_polls = 0
        self.chats = 0
        self.gate = asyncio.Event()

    async def health(self, request: web.Request) -> web.Response:
        self.health_polls += 1
        if self.health_polls <= self.loading_polls:
            return web.json_response({"error": "loading"}, status=503)
        return web.json_response({"status": "ok"})

    async def chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.chats += 1
        if not body.get("stream"):
            return web.json_response({"choices": [], "usage": {}})
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"choices": []}\n\n')
        await self.gate.wait()
        await response.write(b"data: [DONE]\n\n")
        return response


class StubHub:
    async def introspect(self, token: str):
        return type("Principal", (), {"role": protocol.ROLE_EXECUTOR})()


@pytest.fixture
def systemd():
    return FakeSystemd()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def llama():
    return FakeLlama()


@pytest.fixture
async def make(systemd, clock, llama):
    app = web.Application()
    app.router.add_get("/health", llama.health)
    app.router.add_post("/v1/chat/completions", llama.chat)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    created: list[LlamaCppBackend] = []

    def build(**overrides) -> LlamaCppBackend:
        spec = DevlabModelConfig(
            kind="llamacpp", gguf=GGUF, context=8192, idle_unload_s=IDLE_S, **overrides
        )
        backend = LlamaCppBackend(
            MODEL,
            spec,
            binary="llama-server",
            port=server.port,
            runner=systemd,
            clock=clock,
            sleep=clock.sleep,
        )
        created.append(backend)
        return backend

    yield build
    for backend in created:
        await backend.close()
    await server.close()


async def request(backend: LlamaCppBackend, **extra) -> int:
    upstream = await backend.chat({**CHAT, **extra})
    async with upstream:
        await upstream.read()
    backend.finished()
    return upstream.status


async def test_argv_has_slice_weight_taskset_threads_context_and_no_gpu():
    spec = DevlabModelConfig(
        kind="llamacpp",
        gguf=GGUF,
        context=8192,
        threads=6,
        cpus="0-5",
        extra_args=["--flash-attn", "on"],
    )
    argv = LlamaCppBackend(
        MODEL, spec, binary="/opt/llama-server", port=8731, runner=FakeSystemd()
    ).argv()
    assert argv[:4] == ["systemd-run", "--user", "--quiet", "--collect"]
    assert f"--unit={model_unit(MODEL)}" in argv
    assert f"--slice={protocol.DEVLAB_SLICE}" in argv
    assert f"--property=CPUWeight={protocol.DEVLAB_SLICE_CPU_WEIGHT}" in argv
    start = argv.index("taskset")
    assert argv[start : start + 4] == ["taskset", "-c", "0-5", "/opt/llama-server"]
    tail = argv[start + 3 :]
    assert tail[tail.index("--model") + 1] == GGUF
    assert tail[tail.index("--host") + 1] == "127.0.0.1"
    assert tail[tail.index("--port") + 1] == "8731"
    assert tail[tail.index("--n-gpu-layers") + 1] == "0"
    assert tail[tail.index("--ctx-size") + 1] == "8192"
    assert tail[tail.index("--threads") + 1] == "6"
    assert tail[-2:] == ["--flash-attn", "on"]


async def test_argv_without_cpus_threads_context_has_no_taskset():
    spec = DevlabModelConfig(kind="llamacpp", gguf=GGUF)
    argv = LlamaCppBackend(MODEL, spec, binary="llama-server", port=8731).argv()
    assert "taskset" not in argv
    assert "--threads" not in argv
    assert "--ctx-size" not in argv


async def test_llamacpp_config_defaults_and_ports():
    cfg = DevlabModelsConfig.model_validate(
        {
            "llamacpp_port_min": 9000,
            "llamacpp_port_max": 9000,
            "flint": {"kind": "llamacpp", "gguf": GGUF},
            "glm": {"kind": "llamacpp", "gguf": GGUF},
        }
    )
    assert cfg.entries["flint"].free is True
    assert cfg.entries["flint"].remote is False
    assert build_backend("flint", cfg.entries["flint"], cfg)._port == 9000
    with pytest.raises(RuntimeError, match="портов"):
        build_backend("glm", cfg.entries["glm"], cfg)
    with pytest.raises(ValueError, match="gguf"):
        DevlabModelConfig(kind="llamacpp")


async def test_first_request_starts_unit_and_waits_for_health(make, systemd, llama, clock):
    llama.loading_polls = 3
    backend = make()
    assert backend.loaded() is False
    assert await request(backend) == 200
    assert backend.loaded() is True
    assert systemd.starts() == 1
    assert llama.health_polls == 4
    assert clock.now > 1000.0
    assert llama.chats == 1


async def test_parallel_first_requests_start_one_process(make, systemd, llama):
    llama.loading_polls = 2
    backend = make()
    statuses = await asyncio.gather(*(request(backend) for _ in range(4)))
    assert statuses == [200] * 4
    assert systemd.starts() == 1
    assert llama.chats == 4


async def test_idle_unload_after_timeout_and_restart_on_demand(make, systemd, clock):
    backend = make()
    await request(backend)
    clock.now += IDLE_S - 1
    await backend.reap()
    assert backend.loaded() is True
    clock.now += 2
    await backend.reap()
    assert backend.loaded() is False
    assert systemd.active is False
    assert ["systemctl", "--user", "stop", UNIT] in systemd.stops()
    await request(backend)
    assert systemd.starts() == 2
    assert backend.loaded() is True


async def test_active_stream_is_not_unloaded(make, systemd, llama, clock):
    backend = make()
    upstream = await backend.chat({**CHAT, "stream": True})
    assert b"data" in await upstream.content.readany()
    clock.now += IDLE_S * 3
    await backend.reap()
    assert backend.loaded() is True
    assert systemd.active is True
    llama.gate.set()
    assert b"[DONE]" in await upstream.content.read()
    upstream.release()
    backend.finished()
    clock.now += IDLE_S
    await backend.reap()
    assert backend.loaded() is False


async def test_crashed_unit_is_started_again(make, systemd):
    backend = make()
    await request(backend)
    systemd.active = False
    await request(backend)
    assert systemd.starts() == 2
    assert backend.loaded() is True


async def test_reap_notices_crash(make, systemd):
    backend = make()
    await request(backend)
    systemd.active = False
    await backend.reap()
    assert backend.loaded() is False


async def test_frequent_crashes_give_clear_error_then_recover(make, systemd, clock):
    backend = make()
    systemd.die_on_start = True
    for _ in range(LLAMA_MAX_CRASHES):
        with pytest.raises(ModelUnavailableError, match="упала при загрузке"):
            await request(backend)
    starts = systemd.starts()
    with pytest.raises(ModelUnavailableError, match="следующая попытка"):
        await request(backend)
    assert systemd.starts() == starts
    systemd.die_on_start = False
    clock.now += LLAMA_CRASH_WINDOW_S
    assert await request(backend) == 200


async def test_failed_systemd_run_is_unavailable(make, systemd):
    systemd.start_rc = 1
    backend = make()
    with pytest.raises(ModelUnavailableError, match="не удалось запустить"):
        await request(backend)
    assert backend.loaded() is False


async def test_close_stops_loaded_unit(make, systemd):
    backend = make()
    await request(backend)
    await backend.close()
    assert systemd.active is False
    assert backend.loaded() is False


async def test_gateway_heartbeat_loaded_and_503(make, systemd, tmp_path):
    backend = make()
    ledger = Ledger(tmp_path)
    await ledger.open()
    gateway = Gateway({MODEL: backend}, ledger, StubHub(), {MODEL: RateWindow(0)})
    client = TestClient(TestServer(gateway.build_app()))
    await client.start_server()
    try:
        before = (await gateway.heartbeat_body())[protocol.FIELD_MODELS][0]
        assert before[protocol.MODEL_LOADED] is False
        assert before[protocol.MODEL_FREE] is True
        assert before[protocol.MODEL_REMOTE] is False
        headers = {"Authorization": "Bearer dlh_x"}
        resp = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=headers)
        assert resp.status == 200
        after = (await gateway.heartbeat_body())[protocol.FIELD_MODELS][0]
        assert after[protocol.MODEL_LOADED] is True
        systemd.start_rc = 1
        systemd.active = False
        resp = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=headers)
        assert resp.status == 503
        assert "не удалось запустить" in (await resp.json())[protocol.FIELD_ERROR]
    finally:
        await client.close()
        await ledger.close()
