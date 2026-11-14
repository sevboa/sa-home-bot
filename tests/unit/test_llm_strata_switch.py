from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.llm.strata_switch import (
    CHAT_PATH,
    DEFAULT_IDLE_S,
    DEFAULT_MAX_WAIT_S,
    DEFAULT_POLL_S,
    MODELS_PATH,
    UPSTREAM_CONNECT_S,
    UPSTREAM_TIMEOUT,
    LaunchFailed,
    StrataSwitch,
    SwitchBusy,
    SwitchSettings,
    UnknownModel,
    build_app,
    load_settings,
)

LAUNCHERS = {"iq3": ["/models/sa-home/stand/iq3-on.sh"], "qw2": ["/models/sa-home/stand/qw2-on.sh"]}
AUTH = {"Authorization": "Bearer ключ"}


class StepClock:
    def __init__(self, start: float = 0.0, step: float = 0.1) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class FakeRun:
    def __init__(self, code: int = 0, gate: asyncio.Event | None = None) -> None:
        self.code = code
        self.gate = gate
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        if self.gate is not None:
            await self.gate.wait()
        return self.code


def settings(**overrides) -> SwitchSettings:
    options = {
        "launchers": dict(LAUNCHERS),
        "default": "iq3",
        "idle_s": 1.0,
        "max_wait_s": 60.0,
        "poll_s": 0.01,
    }
    options.update(overrides)
    return SwitchSettings(**options)


class Upstream:
    def __init__(self, stream: bool = False) -> None:
        self.stream = stream
        self.bodies: list[dict] = []
        self.headers: list[dict] = []

    async def chat(self, request: web.Request) -> web.StreamResponse:
        self.headers.append(dict(request.headers))
        self.bodies.append(await request.json())
        if not self.stream:
            return web.json_response({"choices": [{"message": {"content": "привет"}}]})
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'data: {"choices": [{"delta": {"content": "priv"}}]}\n\n')
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post(CHAT_PATH, self.chat)
        return app


async def run_router(switch: StrataSwitch, upstream: Upstream) -> tuple[TestServer, TestClient]:
    upstream_server = TestServer(upstream.app())
    await upstream_server.start_server()
    upstream_url = str(upstream_server.make_url("")).rstrip("/")
    router = TestClient(TestServer(build_app(switch, upstream_url)))
    await router.start_server()
    return upstream_server, router


async def close(*clients) -> None:
    for client in clients:
        await client.close()


async def test_acquire_starts_default_once():
    run = FakeRun()
    switch = StrataSwitch(settings(), run, StepClock())
    await switch.start()
    await switch.acquire("iq3")
    switch.release()
    await switch.acquire("iq3")
    switch.release()
    assert switch.current == "iq3"
    assert switch.active == 0
    assert run.calls == [LAUNCHERS["iq3"]]


async def test_switch_waits_for_active_request_then_idle_gap():
    run = FakeRun()
    switch = StrataSwitch(settings(idle_s=1.0), run, StepClock())
    await switch.acquire("iq3")
    switching = asyncio.create_task(switch.acquire("qw2"))
    await asyncio.sleep(0.05)
    assert run.calls == [LAUNCHERS["iq3"]]
    assert not switching.done()
    switch.release()
    await switching
    assert switch.current == "qw2"
    assert switch.active == 1
    assert run.calls == [LAUNCHERS["iq3"], LAUNCHERS["qw2"]]


async def test_switch_busy_after_max_wait():
    run = FakeRun()
    switch = StrataSwitch(settings(idle_s=1.0, max_wait_s=1.0), run, StepClock(step=0.5))
    await switch.acquire("iq3")
    with pytest.raises(SwitchBusy, match="модель занята"):
        await switch.acquire("qw2")
    assert switch.current == "iq3"
    assert run.calls == [LAUNCHERS["iq3"]]


async def test_launch_failure_clears_current():
    run = FakeRun(code=1)
    switch = StrataSwitch(settings(), run, StepClock())
    with pytest.raises(LaunchFailed, match="код 1"):
        await switch.acquire("iq3")
    assert switch.current is None
    assert switch.active == 0


async def test_unknown_model_is_rejected():
    run = FakeRun()
    switch = StrataSwitch(settings(), run, StepClock())
    with pytest.raises(UnknownModel, match="нет-такой"):
        await switch.acquire("нет-такой")
    assert run.calls == []


async def test_request_to_old_model_waits_while_switch_runs():
    gate = asyncio.Event()
    run = FakeRun()
    switch = StrataSwitch(settings(idle_s=0.0), run, StepClock())
    await switch.acquire("iq3")
    switch.release()
    run.gate = gate
    switching = asyncio.create_task(switch.acquire("qw2"))
    await asyncio.sleep(0.05)
    assert run.calls == [LAUNCHERS["iq3"], LAUNCHERS["qw2"]]
    old_model = asyncio.create_task(switch.acquire("iq3"))
    await asyncio.sleep(0.05)
    assert not old_model.done()
    gate.set()
    await switching
    assert switch.current == "qw2"
    assert switch.active == 1
    switch.release()
    await old_model
    assert switch.current == "iq3"
    assert switch.active == 1
    assert run.calls == [LAUNCHERS["iq3"], LAUNCHERS["qw2"], LAUNCHERS["iq3"]]
    switch.release()
    assert switch.active == 0


def test_upstream_timeout_has_no_read_limit():
    assert UPSTREAM_TIMEOUT.total is None
    assert UPSTREAM_TIMEOUT.sock_read is None
    assert UPSTREAM_TIMEOUT.sock_connect == UPSTREAM_CONNECT_S


async def test_relay_session_is_created_with_upstream_timeout(monkeypatch):
    recorded: list[object] = []
    real_session = aiohttp.ClientSession

    def spy_session(*args, **kwargs):
        recorded.append(kwargs.get("timeout"))
        return real_session(*args, **kwargs)

    monkeypatch.setattr(aiohttp, "ClientSession", spy_session)
    switch = StrataSwitch(settings(), FakeRun())
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.post(CHAT_PATH, json={"model": "iq3", "messages": []})
        await resp.read()
    finally:
        await close(router, upstream_server)
    assert UPSTREAM_TIMEOUT in recorded


async def test_models_lists_launchers_without_switching():
    run = FakeRun()
    switch = StrataSwitch(settings(), run)
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.get(MODELS_PATH)
        body = await resp.json()
    finally:
        await close(router, upstream_server)
    assert resp.status == 200
    assert body["object"] == "list"
    assert [item["id"] for item in body["data"]] == ["iq3", "qw2"]
    assert [item["object"] for item in body["data"]] == ["model", "model"]
    assert run.calls == []
    assert switch.current is None


async def test_chat_relays_answer_body_and_authorization():
    switch = StrataSwitch(settings(), FakeRun())
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        body = await resp.json()
    finally:
        await close(router, upstream_server)
    assert resp.status == 200
    assert body["choices"][0]["message"]["content"] == "привет"
    assert upstream.headers[0]["Authorization"] == "Bearer ключ"
    assert upstream.bodies[0] == {"model": "iq3", "messages": []}
    assert switch.current == "iq3"
    assert switch.active == 0


async def test_chat_streams_sse_lines():
    switch = StrataSwitch(settings(), FakeRun())
    upstream = Upstream(stream=True)
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.post(
            CHAT_PATH, json={"model": "qw2", "messages": [], "stream": True}, headers=AUTH
        )
        text = await resp.text()
    finally:
        await close(router, upstream_server)
    assert resp.status == 200
    assert [line for line in text.splitlines() if line] == [
        'data: {"choices": [{"delta": {"content": "priv"}}]}',
        "data: [DONE]",
    ]
    assert upstream.bodies[0]["stream"] is True


@pytest.mark.parametrize(
    ("body", "message"),
    [({"messages": []}, "None"), ({"model": "нет-такой", "messages": []}, "нет-такой")],
)
async def test_chat_rejects_unknown_model_with_400(body, message):
    run = FakeRun()
    switch = StrataSwitch(settings(), run)
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.post(CHAT_PATH, json=body, headers=AUTH)
        answer = await resp.json()
    finally:
        await close(router, upstream_server)
    assert resp.status == 400
    assert message in answer["error"]
    assert upstream.bodies == []
    assert run.calls == []
    assert switch.current is None


@pytest.mark.parametrize(
    ("switch_settings", "run", "loaded", "status", "error"),
    [
        (
            {"max_wait_s": 0.0, "idle_s": 100.0},
            FakeRun(),
            True,
            503,
            "модель занята, переключение невозможно",
        ),
        ({}, FakeRun(code=1), False, 502, "код 1"),
    ],
)
async def test_chat_reports_switch_problems(switch_settings, run, loaded, status, error):
    switch = StrataSwitch(settings(**switch_settings), run)
    if loaded:
        switch.current = "iq3"
    switch.active = 1
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.post(CHAT_PATH, json={"model": "qw2", "messages": []}, headers=AUTH)
        answer = await resp.json()
    finally:
        await close(router, upstream_server)
    assert resp.status == status
    assert error in answer["error"]
    assert upstream.bodies == []
    assert switch.active == 1


async def test_chat_returns_502_when_strata_is_unreachable():
    upstream_server = TestServer(Upstream().app())
    await upstream_server.start_server()
    upstream_url = str(upstream_server.make_url("")).rstrip("/")
    await upstream_server.close()
    switch = StrataSwitch(settings(), FakeRun())
    router = TestClient(TestServer(build_app(switch, upstream_url)))
    await router.start_server()
    try:
        resp = await router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        answer = await resp.json()
    finally:
        await close(router)
    assert resp.status == 502
    assert "Strata" in answer["error"]
    assert switch.current == "iq3"
    assert switch.active == 0


def test_load_settings_reads_toml(tmp_path: Path):
    path = tmp_path / "strata.toml"
    path.write_text(
        'default = "iq3"\n'
        "idle_s = 60.0\n"
        "max_wait_s = 900.0\n"
        "[launchers]\n"
        'iq3 = ["/models/sa-home/stand/iq3-on.sh"]\n'
        'qw2 = ["/models/sa-home/stand/qw2-on.sh", "--gpu"]\n',
        encoding="utf-8",
    )
    loaded = load_settings(path)
    assert loaded.default == "iq3"
    assert loaded.idle_s == 60.0
    assert loaded.max_wait_s == 900.0
    assert loaded.poll_s == 5.0
    assert loaded.launchers == {
        "iq3": ["/models/sa-home/stand/iq3-on.sh"],
        "qw2": ["/models/sa-home/stand/qw2-on.sh", "--gpu"],
    }


def test_load_settings_defaults_are_constants(tmp_path: Path):
    path = tmp_path / "minimal.toml"
    path.write_text('default = "iq3"\n[launchers]\niq3 = ["/models/iq3-on.sh"]\n', encoding="utf-8")
    loaded = load_settings(path)
    plain = SwitchSettings(launchers={"iq3": ["/models/iq3-on.sh"]}, default="iq3")
    expected = (DEFAULT_IDLE_S, DEFAULT_MAX_WAIT_S, DEFAULT_POLL_S)
    assert (loaded.idle_s, loaded.max_wait_s, loaded.poll_s) == expected
    assert (plain.idle_s, plain.max_wait_s, plain.poll_s) == expected
    assert expected == (300.0, 1800.0, 5.0)


def test_load_settings_rejects_default_outside_launchers(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text('default = "qw2"\n[launchers]\niq3 = ["/models/iq3-on.sh"]\n', encoding="utf-8")
    with pytest.raises(UnknownModel, match="qw2"):
        load_settings(path)
