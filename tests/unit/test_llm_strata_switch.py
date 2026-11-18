from __future__ import annotations

import asyncio
import signal
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.llm.strata_switch import (
    CHAT_PATH,
    COMPANION_KEY,
    DEFAULT_COMPANION_IDLE_STOP_S,
    DEFAULT_FREEZE_MAX_S,
    DEFAULT_IDLE_S,
    DEFAULT_MAX_WAIT_S,
    DEFAULT_POLL_S,
    DEFAULT_PRIORITY_GRACE_S,
    MODELS_PATH,
    RELEASE_PATH,
    SILENCE_CODE,
    SILENCE_FOREVER_TEXT,
    SILENCE_PATH,
    SILENCE_STATUS,
    STATUS_PATH,
    UPSTREAM_CONNECT_S,
    UPSTREAM_TIMEOUT,
    VRAM_PATH,
    Companion,
    CompanionSettings,
    EngineFreezer,
    LaunchFailed,
    PriorityGate,
    StrataSwitch,
    SwitchBusy,
    SwitchSettings,
    UnknownModel,
    VramFailed,
    Waiter,
    build_app,
    build_companion,
    client_label,
    engine_pids,
    load_settings,
    resolve,
    switch_status,
    upstream_engine_pids,
    upstream_port,
)
from tests.unit.support.waiting import wait_until

LAUNCHERS = {"iq3": ["/models/sa-home/stand/iq3-on.sh"], "qw2": ["/models/sa-home/stand/qw2-on.sh"]}
AUTH = {"Authorization": "Bearer ключ"}
COMPANION = CompanionSettings(
    name="gemma4-alfred",
    upstream="http://127.0.0.1:8086",
    launcher=["/models/sa-home/poly/gemma-on.sh"],
    stopper=["/models/sa-home/poly/gemma-off.sh"],
    idle_stop_s=1.0,
    main_reserve_active_mib=3072,
    main_reserve_idle_mib=0,
)


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


class ScratchUpstream:
    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay
        self.arrived = 0
        self.aborted: list[str] = []
        self.completed: list[str] = []

    async def chat(self, request: web.Request) -> web.StreamResponse:
        self.arrived += 1
        model = str((await request.json()).get("model"))
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.aborted.append(model)
            raise
        except ConnectionError as exc:
            self.aborted.append(f"{model}:{type(exc).__name__}")
            raise
        self.completed.append(model)
        return web.json_response({"choices": [{"message": {"content": "привет"}}]})

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post(CHAT_PATH, self.chat)
        return app


class ScratchStreamUpstream:
    def __init__(self, chunks: int = 4, gap: float = 0.1) -> None:
        self.chunks = chunks
        self.gap = gap
        self.arrived = 0
        self.bodies: list[dict] = []
        self.aborted: list[str] = []
        self.completed: list[str] = []

    async def chat(self, request: web.Request) -> web.StreamResponse:
        self.arrived += 1
        payload = await request.json()
        self.bodies.append(payload)
        model = "поток" if payload.get("stream") else str(payload.get("model"))
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        try:
            for _ in range(self.chunks):
                await asyncio.sleep(self.gap)
                await resp.write(b"data: chunk\n\n")
            await resp.write_eof()
        except (asyncio.CancelledError, ConnectionError) as exc:
            self.aborted.append(f"{model}:{type(exc).__name__}")
            raise
        self.completed.append(model)
        return resp

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post(CHAT_PATH, self.chat)
        return app


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


async def test_request_to_loaded_model_passes_while_switch_waits_for_idle():
    run = FakeRun()
    switch = StrataSwitch(settings(idle_s=30.0), run, StepClock())
    await switch.acquire("iq3")
    switch.release()
    switching = asyncio.create_task(switch.acquire("qw2"))
    await wait_until(lambda: not switching.done() and run.calls == [LAUNCHERS["iq3"]])
    await asyncio.wait_for(switch.acquire("iq3"), timeout=2.0)
    assert switch.active == 1
    assert switch.current == "iq3"
    assert run.calls == [LAUNCHERS["iq3"]]
    switch.release()
    await switching
    assert switch.current == "qw2"
    assert run.calls == [LAUNCHERS["iq3"], LAUNCHERS["qw2"]]
    switch.release()


async def test_request_to_model_being_launched_waits_for_launch():
    gate = asyncio.Event()
    run = FakeRun()
    run.gate = gate
    switch = StrataSwitch(settings(idle_s=0.0), run, StepClock())
    switching = asyncio.create_task(switch.acquire("qw2"))
    await wait_until(lambda: run.calls == [LAUNCHERS["qw2"]])
    assert switch.launching == "qw2"
    assert switch.launch_started is not None
    waiting_request = asyncio.create_task(switch.acquire("qw2"))
    await asyncio.sleep(0.05)
    assert not waiting_request.done()
    gate.set()
    await switching
    await waiting_request
    assert switch.launching is None
    assert switch.launch_started is None
    assert switch.current == "qw2"
    assert switch.active == 2
    assert run.calls == [LAUNCHERS["qw2"]]
    switch.release()
    switch.release()


async def test_waiting_list_shows_waiter_and_drops_it_on_cancel():
    run = FakeRun()
    switch = StrataSwitch(settings(idle_s=30.0), run, StepClock())
    await switch.acquire("iq3")
    switch.release()
    switching = asyncio.create_task(switch.acquire("qw2"))
    await wait_until(lambda: len(switch.waiting) == 1)
    assert (switch.waiting[0].model, switch.waiting[0].priority) == ("qw2", False)
    assert isinstance(switch.waiting[0].since, float)
    blocked = asyncio.create_task(switch.acquire("qw2", immediate=True))
    await wait_until(lambda: len(switch.waiting) == 2)
    assert [(w.model, w.priority) for w in switch.waiting] == [("qw2", False), ("qw2", True)]
    blocked.cancel()
    switching.cancel()
    await wait_until(lambda: switch.waiting == [])
    assert switching.cancelled()
    assert blocked.cancelled()
    assert not switch._lock.locked()
    await switch.acquire("qw2", immediate=True)
    assert switch.current == "qw2"
    assert switch.active == 1
    assert switch.waiting == []
    assert run.calls == [LAUNCHERS["iq3"], LAUNCHERS["qw2"]]
    switch.release()


async def test_waiting_list_drops_waiter_after_switch_busy():
    run = FakeRun()
    switch = StrataSwitch(settings(idle_s=30.0, max_wait_s=0.0), run, StepClock())
    await switch.acquire("iq3")
    switch.release()
    with pytest.raises(SwitchBusy, match="модель занята"):
        await switch.acquire("qw2")
    assert switch.waiting == []
    assert run.calls == [LAUNCHERS["iq3"]]


async def test_queued_request_for_new_model_does_not_relaunch_it():
    gate = asyncio.Event()
    run = FakeRun()
    switch = StrataSwitch(settings(idle_s=0.0), run, StepClock())
    run.gate = gate
    first = asyncio.create_task(switch.acquire("qw2"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(switch.acquire("qw2"))
    await asyncio.sleep(0.05)
    gate.set()
    await first
    await second
    assert run.calls == [LAUNCHERS["qw2"]]
    assert switch.active == 2


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"user": "агент devlab v2y5xn"}, "агент devlab v2y5xn"),
        ({"user": "х" * 100}, "х" * 80),
        ({}, ""),
        ({"user": ""}, ""),
        ({"user": None}, ""),
        ({"user": 17}, ""),
    ],
)
def test_client_label_reads_user_field(payload, expected):
    assert client_label(payload) == expected


async def test_acquire_and_release_track_client_in_serving():
    run = FakeRun()
    switch = StrataSwitch(settings(idle_s=0.05), run, StepClock())
    await switch.acquire("iq3", client="чат владелец")
    assert switch.serving == ["чат владелец"]
    switching = asyncio.create_task(switch.acquire("qw2", client="агент devlab v2y5xn"))
    await wait_until(lambda: len(switch.waiting) == 1)
    assert switch.waiting[0].client == "агент devlab v2y5xn"
    assert switch.serving == ["чат владелец"]
    switch.release("сторонняя подпись")
    assert switch.serving == ["чат владелец"]
    switch.release("чат владелец")
    assert switch.serving == []
    await switching
    assert switch.serving == ["агент devlab v2y5xn"]
    switch.release("агент devlab v2y5xn")
    assert switch.serving == []
    assert switch.active == 0


async def test_acquire_without_client_keeps_empty_label():
    run = FakeRun()
    switch = StrataSwitch(settings(), run, StepClock(step=0.0))
    await switch.start()
    assert switch.serving == []
    await switch.acquire("iq3")
    assert switch.serving == [""]
    switch.release()
    assert switch.serving == []


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


def test_switch_status_without_companion_and_without_activity():
    switch = StrataSwitch(settings(idle_s=2.0), FakeRun(), StepClock(step=0.0))
    gate = PriorityGate(switch.clock, switch.settings.poll_s, switch.settings.priority_grace_s)
    status = switch_status(switch, gate, None)
    assert status == {
        "current": None,
        "launching": None,
        "active": 0,
        "serving": [],
        "idle_for_s": None,
        "idle_s": 2.0,
        "requested_ago_s": {},
        "waiting": [],
        "priority_active": 0,
        "engine_frozen": False,
        "companion": None,
        "silence": None,
    }


def test_switch_status_rounds_times_to_tenth():
    switch = StrataSwitch(settings(idle_s=2.0), FakeRun(), StepClock(step=0.0))
    gate = PriorityGate(switch.clock, switch.settings.poll_s, switch.settings.priority_grace_s)
    now = switch.clock()
    switch.current = "iq3"
    switch.active = 1
    switch.launching = "qw2"
    switch.launch_started = now - 1.234
    switch.last_request_end = now - 5.0
    switch.waiting = [
        Waiter("qw2", now - 0.06, True, "агент devlab v2y5xn"),
        Waiter("iq3", now - 12.0, False),
    ]
    switch.serving = ["чат владелец"]
    gate.priority_active = 2
    status = switch_status(switch, gate, None)
    assert status["current"] == "iq3"
    assert status["active"] == 1
    assert status["launching"] == {"model": "qw2", "for_s": 1.2}
    assert status["serving"] == ["чат владелец"]
    assert status["idle_for_s"] == 5.0
    assert status["idle_s"] == 2.0
    assert status["requested_ago_s"] == {}
    assert status["waiting"] == [
        {"model": "qw2", "for_s": 0.1, "priority": True, "client": "агент devlab v2y5xn"},
        {"model": "iq3", "for_s": 12.0, "priority": False, "client": ""},
    ]
    assert status["priority_active"] == 2


def test_switch_status_shows_companion_fields():
    run = FakeRun()
    switch, companion_settings = companion_switch(run, StepClock(step=0.0))
    gate = PriorityGate(switch.clock, switch.settings.poll_s, switch.settings.priority_grace_s)
    companion = Companion(
        settings=companion_settings,
        switch=switch,
        main_upstream="http://127.0.0.1:1",
        run=run,
        clock=switch.clock,
        poll_s=switch.settings.poll_s,
        max_wait_s=switch.settings.max_wait_s,
    )
    companion.running = True
    companion.begin_request(AUTH, "чат владелец")
    companion.last_request = switch.clock() - 3.0
    status = switch_status(switch, gate, companion)
    assert status["companion"] == {
        "name": COMPANION.name,
        "running": True,
        "active": 1,
        "serving": ["чат владелец"],
        "idle_for_s": 3.0,
        "idle_stop_s": COMPANION.idle_stop_s,
        "requested_ago_s": 0.0,
    }


async def test_switch_status_requested_ago_updates_per_model():
    run = FakeRun()
    switch = StrataSwitch(settings(idle_s=0.0), run, StepClock(step=0.0))
    gate = PriorityGate(switch.clock, switch.settings.poll_s, switch.settings.priority_grace_s)
    await switch.acquire("iq3")
    switch.release()
    await switch.acquire("qw2")
    switch.release()
    switch.clock = StepClock(start=7.0, step=0.0)
    status = switch_status(switch, gate, None)
    assert status["requested_ago_s"] == {"iq3": 7.0, "qw2": 7.0}
    await switch.acquire("iq3")
    switch.release()
    status = switch_status(switch, gate, None)
    assert status["requested_ago_s"] == {"iq3": 0.0, "qw2": 7.0}


def test_companion_requested_ago_counts_from_request_start():
    run = FakeRun()
    switch, companion_settings = companion_switch(run, StepClock(step=0.1))
    gate = PriorityGate(switch.clock, switch.settings.poll_s, switch.settings.priority_grace_s)
    companion = Companion(
        settings=companion_settings,
        switch=switch,
        main_upstream="http://127.0.0.1:1",
        run=run,
        clock=switch.clock,
        poll_s=switch.settings.poll_s,
        max_wait_s=switch.settings.max_wait_s,
    )
    companion.running = True
    companion.begin_request(AUTH, "чат владелец")
    companion.end_request("чат владелец")
    switch.clock = StepClock(start=5.0, step=0.0)
    status = switch_status(switch, gate, companion)
    assert status["companion"]["idle_for_s"] == 4.7
    assert status["companion"]["requested_ago_s"] == 4.8


async def test_switch_status_shows_launching_and_waiting_in_arrival_order():
    gate_event = asyncio.Event()
    run = FakeRun()
    run.gate = gate_event
    switch = StrataSwitch(settings(idle_s=0.0), run, StepClock())
    gate = PriorityGate(switch.clock, switch.settings.poll_s, switch.settings.priority_grace_s)
    switching = asyncio.create_task(switch.acquire("qw2", immediate=True))
    await wait_until(lambda: run.calls == [LAUNCHERS["qw2"]])
    waiter = asyncio.create_task(switch.acquire("qw2"))
    await wait_until(lambda: len(switch.waiting) == 2)
    status = switch_status(switch, gate, None)
    assert status["launching"]["model"] == "qw2"
    assert status["launching"]["for_s"] >= 0.0
    assert [(item["model"], item["priority"]) for item in status["waiting"]] == [
        ("qw2", True),
        ("qw2", False),
    ]
    assert all(item["for_s"] >= 0.0 for item in status["waiting"])
    assert status["current"] is None
    gate_event.set()
    await switching
    await waiter
    status = switch_status(switch, gate, None)
    assert status["launching"] is None
    assert status["waiting"] == []
    assert status["current"] == "qw2"
    assert status["active"] == 2


async def test_status_route_answers_without_authorization():
    switch = StrataSwitch(settings(), FakeRun())
    switch.current = "iq3"
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.get(STATUS_PATH)
        body = await resp.json()
    finally:
        await close(router, upstream_server)
    assert resp.status == 200
    assert body == {
        "current": "iq3",
        "launching": None,
        "active": 0,
        "serving": [],
        "idle_for_s": None,
        "idle_s": 1.0,
        "requested_ago_s": {},
        "waiting": [],
        "priority_active": 0,
        "engine_frozen": False,
        "companion": None,
        "silence": None,
    }


async def test_status_route_shows_active_and_waiting_requests():
    switch = StrataSwitch(settings(idle_s=0.05), FakeRun())
    upstream = ScratchUpstream(delay=0.3)
    upstream_server, router = await run_router(switch, upstream)
    try:
        normal = asyncio.create_task(
            router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        )
        await wait_until(lambda: upstream.arrived == 1)
        switching = asyncio.create_task(
            router.post(CHAT_PATH, json={"model": "qw2", "messages": []}, headers=AUTH)
        )
        await wait_until(lambda: len(switch.waiting) == 1)
        body = await (await router.get(STATUS_PATH)).json()
        assert body["current"] == "iq3"
        assert body["active"] == 1
        assert body["launching"] is None
        assert body["priority_active"] == 0
        assert body["companion"] is None
        assert [(item["model"], item["priority"]) for item in body["waiting"]] == [("qw2", False)]
        assert body["waiting"][0]["for_s"] >= 0.0
        assert (await normal).status == 200
        assert (await switching).status == 200
        assert switch.active == 0
    finally:
        await close(router, upstream_server)


async def test_status_route_shows_client_labels_of_waiting_and_serving_requests():
    switch = StrataSwitch(settings(idle_s=0.05), FakeRun())
    upstream = ScratchUpstream(delay=0.3)
    upstream_server, router = await run_router(switch, upstream)
    try:
        normal = asyncio.create_task(
            router.post(
                CHAT_PATH,
                json={"model": "iq3", "messages": [], "user": "чат владелец"},
                headers=AUTH,
            )
        )
        await wait_until(lambda: upstream.arrived == 1)
        switching = asyncio.create_task(
            router.post(
                CHAT_PATH,
                json={"model": "qw2", "messages": [], "user": "агент devlab v2y5xn"},
                headers=AUTH,
            )
        )
        await wait_until(lambda: len(switch.waiting) == 1)
        body = await (await router.get(STATUS_PATH)).json()
        assert body["serving"] == ["чат владелец"]
        assert body["waiting"][0]["client"] == "агент devlab v2y5xn"
        assert (await normal).status == 200
        assert (await switching).status == 200
        assert switch.serving == []
        body = await (await router.get(STATUS_PATH)).json()
        assert body["serving"] == []
    finally:
        await close(router, upstream_server)


async def test_status_route_without_user_field_has_empty_labels():
    switch = StrataSwitch(settings(idle_s=0.05), FakeRun())
    upstream = ScratchUpstream(delay=0.3)
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        await resp.read()
        body = await (await router.get(STATUS_PATH)).json()
        assert body["serving"] == []
        assert body["waiting"] == []
        assert switch.serving == []
    finally:
        await close(router, upstream_server)


async def test_status_route_reports_companion_state():
    journal: list[str] = []
    run = JournalRun(journal)
    main = VramUpstream(journal)
    partner = Upstream()
    app, router, servers, companion = await companion_router(main, partner, run)
    try:
        body = await (await router.get(STATUS_PATH)).json()
        assert body["current"] == "iq3"
        assert body["companion"] == {
            "name": COMPANION.name,
            "running": False,
            "active": 0,
            "serving": [],
            "idle_for_s": None,
            "idle_stop_s": companion.settings.idle_stop_s,
            "requested_ago_s": None,
        }
        resp = await router.post(
            CHAT_PATH,
            json={"model": COMPANION.name, "messages": [], "user": "агент devlab v2y5xn"},
            headers=AUTH,
        )
        await resp.read()
        body = await (await router.get(STATUS_PATH)).json()
        assert body["companion"]["running"] is True
        assert body["companion"]["active"] == 0
        assert body["companion"]["serving"] == []
        assert body["companion"]["idle_for_s"] >= 0.0
        assert journal == [ACTIVE_RESERVE, LAUNCH]
    finally:
        await close(router, *servers)


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
    assert (loaded.freeze_engine, loaded.freeze_max_s, loaded.llm_socket) == (
        False,
        DEFAULT_FREEZE_MAX_S,
        "",
    )


def test_load_settings_rejects_default_outside_launchers(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text('default = "qw2"\n[launchers]\niq3 = ["/models/iq3-on.sh"]\n', encoding="utf-8")
    with pytest.raises(UnknownModel, match="qw2"):
        load_settings(path)


class VramUpstream(ScratchUpstream):
    def __init__(self, journal: list[str], busy_times: int = 0, delay: float = 0.0) -> None:
        super().__init__(delay=delay)
        self.journal = journal
        self.busy_times = busy_times
        self.vram: list[dict] = []
        self.vram_headers: list[dict] = []

    async def reserve(self, request: web.Request) -> web.Response:
        self.vram_headers.append(dict(request.headers))
        body = await request.json()
        self.vram.append(body)
        self.journal.append(f"vram:{body['reserve_mib']}")
        if len(self.vram) <= self.busy_times:
            return web.json_response({"error": "идёт запрос"}, status=409)
        return web.json_response({"reserve_mib": body["reserve_mib"]})

    def app(self) -> web.Application:
        app = super().app()
        app.router.add_post(VRAM_PATH, self.reserve)
        return app


class JournalRun(FakeRun):
    def __init__(self, journal: list[str], code: int = 0) -> None:
        super().__init__(code)
        self.journal = journal

    async def __call__(self, argv: list[str]) -> int:
        self.journal.append("launch:" + argv[0])
        return await super().__call__(argv)


async def main_upstream(main: VramUpstream) -> tuple[TestServer, str]:
    server = TestServer(main.app())
    await server.start_server()
    return server, str(server.make_url("")).rstrip("/")


def companion_switch(
    run: FakeRun, clock: StepClock, **overrides
) -> tuple[StrataSwitch, CompanionSettings]:
    companion = overrides.pop("companion", COMPANION)
    options = {"companion": companion}
    options.update(overrides)
    return StrataSwitch(settings(**options), run, clock), companion


def loaded_companion(
    switch: StrataSwitch, main_url: str, current: str | None = "iq3"
) -> Companion:
    switch.current = current
    companion = build_companion(switch, main_url)
    assert companion is not None
    return companion


ACTIVE_RESERVE = f"vram:{COMPANION.main_reserve_active_mib}"
IDLE_RESERVE = f"vram:{COMPANION.main_reserve_idle_mib}"
LAUNCH = "launch:" + COMPANION.launcher[0]
STOP = "launch:" + COMPANION.stopper[0]


async def companion_router(
    main: VramUpstream,
    partner: Upstream,
    run: FakeRun,
    clock: StepClock | None = None,
    companion_options: dict | None = None,
    freezer: EngineFreezer | None = None,
    **overrides,
) -> tuple[web.Application, TestClient, list[TestServer], Companion]:
    main_server, main_url = await main_upstream(main)
    partner_server = TestServer(partner.app())
    await partner_server.start_server()
    partner_url = str(partner_server.make_url("")).rstrip("/")
    companion_fields = {"upstream": partner_url, "idle_stop_s": 1000.0}
    companion_fields.update(companion_options or {})
    companion = replace(COMPANION, **companion_fields)
    options = {"companion": companion, "priority_grace_s": 0.0}
    options.update(overrides)
    switch = StrataSwitch(settings(**options), run, clock or StepClock())
    switch.current = "iq3"
    app = build_app(switch, main_url, freezer)
    router = TestClient(TestServer(app))
    await router.start_server()
    companion_instance = app[COMPANION_KEY]
    return app, router, [main_server, partner_server], companion_instance


async def test_companion_serving_holds_client_label_until_request_ends():
    journal: list[str] = []
    run = JournalRun(journal)
    switch, _ = companion_switch(run, StepClock())
    main = VramUpstream(journal)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url)
    try:
        companion.begin_request(AUTH, "агент devlab v2y5xn")
        assert companion.serving == ["агент devlab v2y5xn"]
        companion.begin_request(AUTH)
        assert companion.serving == ["агент devlab v2y5xn", ""]
        companion.end_request("сторонняя подпись")
        assert companion.serving == ["агент devlab v2y5xn", ""]
        companion.end_request("агент devlab v2y5xn")
        assert companion.serving == [""]
        companion.end_request()
        assert companion.serving == []
        assert companion.active == 0
    finally:
        await close(main_server)


async def test_companion_request_interrupts_normal_request_and_uses_own_upstream():
    journal: list[str] = []
    run = JournalRun(journal)
    main = VramUpstream(journal, delay=0.3)
    partner = Upstream()
    app, router, servers, companion = await companion_router(main, partner, run)
    try:
        normal = asyncio.create_task(
            router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        )
        await wait_until(lambda: main.arrived == 1)
        partner_resp = await router.post(
            CHAT_PATH, json={"model": COMPANION.name, "messages": []}, headers=AUTH
        )
        partner_body = await partner_resp.json()
        assert partner_resp.status == 200
        assert partner_body["choices"][0]["message"]["content"] == "привет"
        assert partner.bodies == [{"model": COMPANION.name, "messages": []}]
        assert partner.headers[0]["Authorization"] == "Bearer ключ"
        assert main.aborted == ["iq3"]
        assert journal == [ACTIVE_RESERVE, LAUNCH]
        assert app[COMPANION_KEY].running is True
        normal_resp = await normal
        assert normal_resp.status == 200
        assert (await normal_resp.json())["choices"][0]["message"]["content"] == "привет"
        assert main.arrived == 2
        assert main.completed == ["iq3"]
        assert journal == [ACTIVE_RESERVE, LAUNCH]
        assert app[COMPANION_KEY].active == 0
    finally:
        await close(router, *servers)


async def test_companion_answer_streams_from_its_upstream():
    journal: list[str] = []
    run = JournalRun(journal)
    main = VramUpstream(journal)
    partner = ScratchStreamUpstream(chunks=3, gap=0.05)
    app, router, servers, companion = await companion_router(main, partner, run)
    try:
        resp = await router.post(
            CHAT_PATH,
            json={"model": COMPANION.name, "messages": [], "stream": True},
            headers=AUTH,
        )
        text = await resp.text()
        assert resp.status == 200
        assert [line for line in text.splitlines() if line] == ["data: chunk"] * 3
        assert partner.bodies[0]["stream"] is True
        assert partner.completed == ["поток"]
        assert journal == [ACTIVE_RESERVE, LAUNCH]
        await wait_until(lambda: app[COMPANION_KEY].active == 0)
    finally:
        await close(router, *servers)


async def test_companion_second_request_does_not_launch_again():
    journal: list[str] = []
    run = JournalRun(journal)
    main = VramUpstream(journal)
    partner = Upstream()
    app, router, servers, companion = await companion_router(main, partner, run)
    try:
        for _ in range(2):
            resp = await router.post(
                CHAT_PATH, json={"model": COMPANION.name, "messages": []}, headers=AUTH
            )
            await resp.read()
        assert journal == [ACTIVE_RESERVE, LAUNCH]
        assert len(partner.bodies) == 2
    finally:
        await close(router, *servers)


async def test_companion_answer_is_503_when_main_model_keeps_vram():
    journal: list[str] = []
    run = JournalRun(journal)
    main = VramUpstream(journal, busy_times=10)
    partner = Upstream()
    app, router, servers, companion = await companion_router(
        main, partner, run, StepClock(step=0.5), max_wait_s=1.0
    )
    try:
        resp = await router.post(
            CHAT_PATH, json={"model": COMPANION.name, "messages": []}, headers=AUTH
        )
        answer = await resp.json()
        assert resp.status == 503
        assert "занята" in answer["error"]
        assert app[COMPANION_KEY].running is False
        assert run.calls == []
        assert partner.bodies == []
    finally:
        await close(router, *servers)


async def test_companion_answer_is_502_when_launcher_fails():
    journal: list[str] = []
    run = JournalRun(journal, code=1)
    main = VramUpstream(journal)
    partner = Upstream()
    app, router, servers, companion = await companion_router(main, partner, run)
    try:
        resp = await router.post(
            CHAT_PATH, json={"model": COMPANION.name, "messages": []}, headers=AUTH
        )
        answer = await resp.json()
        assert resp.status == 502
        assert "код 1" in answer["error"]
        assert app[COMPANION_KEY].running is False
        assert journal == [ACTIVE_RESERVE, LAUNCH]
    finally:
        await close(router, *servers)


async def test_companion_answer_is_502_when_polystrata_is_unreachable():
    journal: list[str] = []
    run = JournalRun(journal)
    main = VramUpstream(journal)
    partner = Upstream()
    app, router, servers, companion = await companion_router(main, partner, run)
    await servers[1].close()
    try:
        resp = await router.post(
            CHAT_PATH, json={"model": COMPANION.name, "messages": []}, headers=AUTH
        )
        answer = await resp.json()
        assert resp.status == 502
        assert "PolyStrata" in answer["error"]
        assert app[COMPANION_KEY].active == 0
    finally:
        await close(router, servers[0])


async def test_companion_idle_guard_stops_it_after_router_requests():
    journal: list[str] = []
    run = JournalRun(journal)
    main = VramUpstream(journal)
    partner = Upstream()
    clock = StepClock()
    app, router, servers, companion = await companion_router(
        main, partner, run, clock, poll_s=0.01, companion_options={"idle_stop_s": 0.05}
    )
    try:
        resp = await router.post(
            CHAT_PATH, json={"model": COMPANION.name, "messages": []}, headers=AUTH
        )
        await resp.read()
        assert journal == [ACTIVE_RESERVE, LAUNCH]
        await asyncio.sleep(0.2)
        assert journal == [ACTIVE_RESERVE, LAUNCH, STOP, IDLE_RESERVE]
        assert app[COMPANION_KEY].running is False
    finally:
        await close(router, *servers)


async def test_companion_first_request_shrinks_main_before_launching():
    journal: list[str] = []
    run = JournalRun(journal)
    switch, _ = companion_switch(run, StepClock())
    main = VramUpstream(journal)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url)
    try:
        await companion.ensure_running()
        assert journal == [ACTIVE_RESERVE, LAUNCH]
        assert main.vram == [{"reserve_mib": COMPANION.main_reserve_active_mib}]
        assert main.vram_headers[0]
        assert companion.running is True
        assert companion.active == 0
        assert switch.current == "iq3"
        await companion.ensure_running()
        assert journal == [ACTIVE_RESERVE, LAUNCH]
    finally:
        await close(main_server)


async def test_companion_launch_failure_leaves_it_stopped():
    journal: list[str] = []
    run = JournalRun(journal, code=1)
    switch, _ = companion_switch(run, StepClock())
    main = VramUpstream(journal)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url)
    try:
        with pytest.raises(LaunchFailed, match="код 1"):
            await companion.ensure_running()
        assert companion.running is False
        assert journal == [ACTIVE_RESERVE, LAUNCH]
    finally:
        await close(main_server)


async def test_companion_skips_vram_when_main_model_not_listening():
    journal: list[str] = []
    run = JournalRun(journal)
    switch, _ = companion_switch(run, StepClock())
    main_server, main_url = await main_upstream(VramUpstream(journal))
    await close(main_server)
    companion = loaded_companion(switch, main_url)
    await companion.ensure_running()
    assert journal == [LAUNCH]
    assert companion.running is True


async def test_companion_shrinks_main_after_router_restart_without_known_model():
    journal: list[str] = []
    run = JournalRun(journal)
    switch, _ = companion_switch(run, StepClock())
    main = VramUpstream(journal)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url, current=None)
    try:
        await companion.ensure_running()
        assert journal == [ACTIVE_RESERVE, LAUNCH]
    finally:
        await close(main_server)


async def test_companion_retries_vram_reserve_on_409():
    journal: list[str] = []
    run = JournalRun(journal)
    switch, _ = companion_switch(run, StepClock(), max_wait_s=60.0)
    main = VramUpstream(journal, busy_times=2)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url)
    companion.headers = dict(AUTH)
    try:
        await companion.ensure_running()
        assert main.vram == [{"reserve_mib": COMPANION.main_reserve_active_mib}] * 3
        assert main.vram_headers[0]["Authorization"] == "Bearer ключ"
        assert journal == [ACTIVE_RESERVE] * 3 + [LAUNCH]
    finally:
        await close(main_server)


async def test_companion_gives_up_vram_reserve_after_max_wait():
    journal: list[str] = []
    run = JournalRun(journal)
    switch, _ = companion_switch(run, StepClock(step=0.5), max_wait_s=1.0)
    main = VramUpstream(journal, busy_times=10)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url)
    try:
        with pytest.raises(VramFailed, match="занята"):
            await companion.ensure_running()
        assert companion.running is False
        assert main.vram == [{"reserve_mib": COMPANION.main_reserve_active_mib}] * 2
        assert run.calls == []
    finally:
        await close(main_server)


async def test_companion_stops_after_idle_and_returns_vram_to_main():
    journal: list[str] = []
    run = JournalRun(journal)
    idle_companion = replace(COMPANION, main_reserve_idle_mib=512)
    switch, companion_settings = companion_switch(run, StepClock(), companion=idle_companion)
    main = VramUpstream(journal)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url)
    try:
        companion.begin_request(AUTH)
        await companion.ensure_running()
        companion.end_request()
        await companion.stop_if_idle()
        assert journal == [ACTIVE_RESERVE, LAUNCH]
        assert companion.running is True
        clock_after_request(companion, companion_settings.idle_stop_s)
        await companion.stop_if_idle()
        assert journal == [ACTIVE_RESERVE, LAUNCH, STOP, "vram:512"]
        assert companion.running is False
    finally:
        await close(main_server)


def clock_after_request(companion: Companion, seconds: float) -> None:
    companion.last_request = companion.clock() - seconds


async def test_companion_not_stopped_while_request_is_active():
    journal: list[str] = []
    run = JournalRun(journal)
    switch, companion_settings = companion_switch(run, StepClock())
    main = VramUpstream(journal)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url)
    try:
        companion.begin_request(AUTH)
        await companion.ensure_running()
        clock_after_request(companion, companion_settings.idle_stop_s * 2)
        await companion.stop_if_idle()
        assert journal == [ACTIVE_RESERVE, LAUNCH]
        assert companion.running is True
        companion.end_request()
        clock_after_request(companion, companion_settings.idle_stop_s)
        await companion.stop_if_idle()
        assert journal == [ACTIVE_RESERVE, LAUNCH, STOP, IDLE_RESERVE]
        assert companion.running is False
    finally:
        await close(main_server)


async def test_companion_idle_guard_stops_it_and_is_cancelled_on_cleanup():
    journal: list[str] = []
    run = JournalRun(journal)
    switch, companion_settings = companion_switch(run, StepClock(), poll_s=0.01)
    main = VramUpstream(journal)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url)
    try:
        companion.begin_request(AUTH)
        await companion.ensure_running()
        companion.end_request()
        companion.start_idle_guard()
        clock_after_request(companion, companion_settings.idle_stop_s)
        await wait_until(lambda: len(journal) >= 4)
        assert companion.running is False
        assert journal == [ACTIVE_RESERVE, LAUNCH, STOP, IDLE_RESERVE]
    finally:
        await companion.stop_idle_guard()
        await close(main_server)
    assert companion._idle_task is None


async def test_companion_idle_guard_keeps_running_after_failed_stop():
    journal: list[str] = []
    run = JournalRun(journal, code=1)
    switch, companion_settings = companion_switch(run, StepClock(), poll_s=0.01)
    main = VramUpstream(journal)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url)
    try:
        companion.running = True
        companion.last_request = companion.clock()
        companion.start_idle_guard()
        clock_after_request(companion, companion_settings.idle_stop_s)
        await wait_until(lambda: len(run.calls) >= 2)
        assert companion.running is True
        assert companion._idle_task is not None
        assert not companion._idle_task.done()
        assert len(run.calls) >= 2
        assert all(call == COMPANION.stopper for call in run.calls)
        assert main.vram == []
    finally:
        await companion.stop_idle_guard()
    assert companion._idle_task is None


def test_build_companion_without_settings_returns_none():
    switch = StrataSwitch(settings(), FakeRun())
    assert build_companion(switch, "http://127.0.0.1:8080") is None


def priority_settings(**overrides) -> SwitchSettings:
    options = {
        "aliases": {"alfred": "qw2"},
        "priority": frozenset({"alfred"}),
        "priority_grace_s": 0.0,
        "idle_s": 0.05,
    }
    options.update(overrides)
    return settings(**options)


async def test_priority_interrupts_normal_request_and_retries_it():
    switch = StrataSwitch(priority_settings(), FakeRun())
    upstream = ScratchUpstream(delay=0.3)
    upstream_server, router = await run_router(switch, upstream)
    try:
        normal = asyncio.create_task(
            router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        )
        await wait_until(lambda: upstream.arrived == 1)
        priority_resp = await router.post(
            CHAT_PATH, json={"model": "alfred", "messages": []}, headers=AUTH
        )
        priority_body = await priority_resp.json()
        assert priority_resp.status == 200
        assert priority_body["choices"][0]["message"]["content"] == "привет"
        assert upstream.completed == ["qw2"]
        assert upstream.aborted == ["iq3"]
        normal_resp = await normal
        normal_body = await normal_resp.json()
        assert normal_resp.status == 200
        assert normal_body["choices"][0]["message"]["content"] == "привет"
        assert upstream.arrived == 3
        assert upstream.completed == ["qw2", "iq3"]
        assert upstream.aborted == ["iq3"]
        assert switch.active == 0
    finally:
        await close(router, upstream_server)


async def test_two_normal_requests_without_priority_are_not_interrupted():
    switch = StrataSwitch(priority_settings(), FakeRun())
    upstream = ScratchUpstream(delay=0.2)
    upstream_server, router = await run_router(switch, upstream)
    try:
        first = asyncio.create_task(
            router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        )
        await asyncio.sleep(0.05)
        second = asyncio.create_task(
            router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        )
        first_resp = await first
        second_resp = await second
        assert first_resp.status == 200
        assert second_resp.status == 200
        assert await first_resp.json() == await second_resp.json()
        assert upstream.arrived == 2
        assert upstream.aborted == []
        assert upstream.completed == ["iq3", "iq3"]
    finally:
        await close(router, upstream_server)


async def test_normal_request_waits_for_priority_grace():
    switch = StrataSwitch(priority_settings(priority_grace_s=0.4), FakeRun())
    upstream = ScratchUpstream(delay=0.1)
    upstream_server, router = await run_router(switch, upstream)
    try:
        priority_resp = await router.post(
            CHAT_PATH, json={"model": "alfred", "messages": []}, headers=AUTH
        )
        await priority_resp.read()
        normal = asyncio.create_task(
            router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        )
        await asyncio.sleep(0.15)
        assert upstream.arrived == 1
        normal_resp = await normal
        assert normal_resp.status == 200
        await normal_resp.read()
        assert upstream.arrived == 2
        assert upstream.aborted == []
    finally:
        await close(router, upstream_server)


async def test_started_stream_is_not_interrupted_and_priority_waits_it():
    switch = StrataSwitch(priority_settings(aliases={"alfred": "iq3"}), FakeRun())
    upstream = ScratchStreamUpstream(chunks=4, gap=0.1)
    upstream_server, router = await run_router(switch, upstream)
    try:
        normal = asyncio.create_task(
            router.post(
                CHAT_PATH, json={"model": "iq3", "messages": [], "stream": True}, headers=AUTH
            )
        )
        await asyncio.sleep(0.15)
        priority_resp = await router.post(
            CHAT_PATH, json={"model": "alfred", "messages": []}, headers=AUTH
        )
        await priority_resp.read()
        normal_resp = await normal
        text = await normal_resp.text()
        assert normal_resp.status == 200
        assert [line for line in text.splitlines() if line] == ["data: chunk"] * 4
        assert upstream.aborted == []
        assert upstream.completed == ["поток", "iq3"]
        assert upstream.arrived == 2
    finally:
        await close(router, upstream_server)


async def test_stream_without_bytes_to_client_is_interrupted_and_retried():
    switch = StrataSwitch(priority_settings(aliases={"alfred": "iq3"}), FakeRun())
    upstream = ScratchStreamUpstream(chunks=3, gap=0.3)
    upstream_server, router = await run_router(switch, upstream)
    try:
        normal = asyncio.create_task(
            router.post(
                CHAT_PATH, json={"model": "iq3", "messages": [], "stream": True}, headers=AUTH
            )
        )
        await asyncio.sleep(0.1)
        priority_resp = await router.post(
            CHAT_PATH, json={"model": "alfred", "messages": []}, headers=AUTH
        )
        await priority_resp.read()
        normal_resp = await normal
        text = await normal_resp.text()
        assert normal_resp.status == 200
        assert [line for line in text.splitlines() if line] == ["data: chunk"] * 3
        assert upstream.arrived == 3
        assert upstream.completed == ["iq3", "поток"]
        assert len(upstream.aborted) == 1
    finally:
        await close(router, upstream_server)


async def test_client_abort_of_streaming_normal_request_is_not_retried():
    switch = StrataSwitch(priority_settings(aliases={"alfred": "iq3"}), FakeRun())
    upstream = ScratchStreamUpstream(chunks=6, gap=0.1)
    upstream_server, router = await run_router(switch, upstream)
    try:
        session = aiohttp.ClientSession()

        async def fetch_partial() -> None:
            resp = await session.post(
                router.make_url(CHAT_PATH),
                json={"model": "iq3", "messages": [], "stream": True},
                headers=AUTH,
            )
            try:
                await resp.content.read(12)
            finally:
                resp.close()

        try:
            client = asyncio.create_task(fetch_partial())
            await asyncio.sleep(0.25)
            await client
            await asyncio.sleep(0.5)
        finally:
            await session.close()
        assert upstream.arrived == 1
        assert upstream.completed == []
        assert switch.active == 0
    finally:
        await close(router, upstream_server)


async def test_priority_switches_model_without_waiting_idle_gap():
    switch = StrataSwitch(priority_settings(idle_s=30.0), FakeRun())
    upstream = ScratchUpstream(delay=0.1)
    upstream_server, router = await run_router(switch, upstream)
    try:
        first = await router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        await first.read()
        started = time.monotonic()
        priority_resp = await router.post(
            CHAT_PATH, json={"model": "alfred", "messages": []}, headers=AUTH
        )
        await priority_resp.read()
        assert priority_resp.status == 200
        assert time.monotonic() - started < 2.0
        assert upstream.aborted == []
        assert upstream.completed == ["iq3", "qw2"]
    finally:
        await close(router, upstream_server)


def test_load_settings_reads_aliases_and_priority(tmp_path: Path):
    path = tmp_path / "aliases.toml"
    path.write_text(
        'default = "iq3"\n'
        'priority_grace_s = 4.0\n'
        'priority = ["alfred"]\n'
        "[aliases]\n"
        'alfred = "qwen3.8"\n'
        "[launchers]\n"
        'iq3 = ["/models/iq3-on.sh"]\n'
        '"qwen3.8" = ["/models/qwen-on.sh"]\n',
        encoding="utf-8",
    )
    loaded = load_settings(path)
    assert loaded.aliases == {"alfred": "qwen3.8"}
    assert loaded.priority == frozenset({"alfred"})
    assert loaded.priority_grace_s == 4.0
    assert resolve(loaded, "alfred") == "qwen3.8"
    assert resolve(loaded, "iq3") == "iq3"


def test_load_settings_defaults_for_aliases_and_priority(tmp_path: Path):
    path = tmp_path / "minimal.toml"
    path.write_text('default = "iq3"\n[launchers]\niq3 = ["/models/iq3-on.sh"]\n', encoding="utf-8")
    loaded = load_settings(path)
    assert loaded.aliases == {}
    assert loaded.priority == frozenset()
    assert loaded.priority_grace_s == DEFAULT_PRIORITY_GRACE_S
    assert DEFAULT_PRIORITY_GRACE_S == 10.0


def test_load_settings_rejects_alias_to_unknown_launcher(tmp_path: Path):
    path = tmp_path / "bad_alias.toml"
    path.write_text(
        'default = "iq3"\n'
        "[aliases]\n"
        'alfred = "qwen3.8"\n'
        "[launchers]\n"
        'iq3 = ["/models/iq3-on.sh"]\n',
        encoding="utf-8",
    )
    with pytest.raises(UnknownModel, match="qwen3.8"):
        load_settings(path)


COMPANION_TOML = (
    'default = "iq3"\n'
    "[companion]\n"
    'name = "gemma4-alfred"\n'
    'upstream = "http://127.0.0.1:8086"\n'
    'launcher = ["/models/sa-home/poly/gemma-on.sh", "--gpu"]\n'
    'stopper = ["/models/sa-home/poly/gemma-off.sh"]\n'
    "idle_stop_s = 900.0\n"
    "main_reserve_active_mib = 3072\n"
    'main_reserve_idle_mib = 0\n'
    "[launchers]\n"
    'iq3 = ["/models/iq3-on.sh"]\n'
)


def test_load_settings_reads_companion(tmp_path: Path):
    path = tmp_path / "companion.toml"
    path.write_text(COMPANION_TOML, encoding="utf-8")
    loaded = load_settings(path)
    assert loaded.companion == CompanionSettings(
        name="gemma4-alfred",
        upstream="http://127.0.0.1:8086",
        launcher=["/models/sa-home/poly/gemma-on.sh", "--gpu"],
        stopper=["/models/sa-home/poly/gemma-off.sh"],
        idle_stop_s=900.0,
        main_reserve_active_mib=3072,
        main_reserve_idle_mib=0,
    )


def test_load_settings_companion_defaults(tmp_path: Path):
    path = tmp_path / "companion_minimal.toml"
    path.write_text(
        'default = "iq3"\n'
        "[companion]\n"
        'name = "gemma4-alfred"\n'
        'upstream = "http://127.0.0.1:8086"\n'
        'launcher = ["/models/gemma-on.sh"]\n'
        'stopper = ["/models/gemma-off.sh"]\n'
        "[launchers]\n"
        'iq3 = ["/models/iq3-on.sh"]\n',
        encoding="utf-8",
    )
    companion = load_settings(path).companion
    assert companion is not None
    assert (companion.idle_stop_s, companion.main_reserve_active_mib) == (1800.0, 0)
    assert companion.main_reserve_idle_mib == 0
    assert DEFAULT_COMPANION_IDLE_STOP_S == 1800.0


def test_load_settings_without_companion_keeps_previous_behavior(tmp_path: Path):
    path = tmp_path / "plain.toml"
    path.write_text('default = "iq3"\n[launchers]\niq3 = ["/models/iq3-on.sh"]\n', encoding="utf-8")
    loaded = load_settings(path)
    assert loaded.companion is None
    plain = SwitchSettings(launchers={"iq3": ["/models/iq3-on.sh"]}, default="iq3")
    assert plain.companion is None
    assert loaded == plain


@pytest.mark.parametrize(
    ("companion_name", "match"),
    [("iq3", "launchers"), ("alfred", "aliases")],
)
def test_load_settings_rejects_companion_name_conflict(tmp_path: Path, companion_name, match):
    path = tmp_path / "conflict.toml"
    path.write_text(
        'default = "iq3"\n'
        "[aliases]\n"
        'alfred = "iq3"\n'
        "[companion]\n"
        f'name = "{companion_name}"\n'
        'upstream = "http://127.0.0.1:8086"\n'
        'launcher = ["/models/gemma-on.sh"]\n'
        'stopper = ["/models/gemma-off.sh"]\n'
        "[launchers]\n"
        'iq3 = ["/models/iq3-on.sh"]\n',
        encoding="utf-8",
    )
    with pytest.raises(UnknownModel, match=match):
        load_settings(path)


@pytest.mark.parametrize(
    "body",
    [
        'default = "iq3"\n[companion]\nupstream = "http://127.0.0.1:8086"\n'
        'launcher = ["/models/gemma-on.sh"]\nstopper = ["/models/gemma-off.sh"]\n'
        '[launchers]\niq3 = ["/models/iq3-on.sh"]\n',
        'default = "iq3"\n[companion]\nname = "gemma"\n'
        'launcher = ["/models/gemma-on.sh"]\nstopper = ["/models/gemma-off.sh"]\n'
        '[launchers]\niq3 = ["/models/iq3-on.sh"]\n',
        'default = "iq3"\n[companion]\nname = "gemma"\nupstream = "http://127.0.0.1:8086"\n'
        'stopper = ["/models/gemma-off.sh"]\n[launchers]\niq3 = ["/models/iq3-on.sh"]\n',
        'default = "iq3"\n[companion]\nname = "gemma"\nupstream = "http://127.0.0.1:8086"\n'
        'launcher = ["/models/gemma-on.sh"]\n[launchers]\niq3 = ["/models/iq3-on.sh"]\n',
    ],
)
def test_load_settings_rejects_incomplete_companion(tmp_path: Path, body: str):
    path = tmp_path / "incomplete.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(UnknownModel, match="companion"):
        load_settings(path)


def test_resolve_rejects_unknown_name():
    with pytest.raises(UnknownModel, match="нет-такой"):
        resolve(settings(aliases={"alfred": "iq3"}), "нет-такой")


async def test_models_lists_launchers_and_aliases():
    run = FakeRun()
    switch = StrataSwitch(settings(aliases={"alfred": "qw2"}, priority=frozenset({"alfred"})), run)
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.get(MODELS_PATH)
        body = await resp.json()
    finally:
        await close(router, upstream_server)
    assert resp.status == 200
    assert [item["id"] for item in body["data"]] == ["iq3", "qw2", "alfred"]
    assert run.calls == []
    assert switch.current is None


async def test_models_lists_companion_name():
    run = FakeRun()
    switch = StrataSwitch(settings(aliases={"alfred": "qw2"}, companion=COMPANION), run)
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.get(MODELS_PATH)
        body = await resp.json()
    finally:
        await close(router, upstream_server)
    assert resp.status == 200
    assert [item["id"] for item in body["data"]] == ["iq3", "qw2", "alfred", COMPANION.name]
    assert run.calls == []
    assert switch.current is None


async def test_chat_by_alias_reaches_upstream_with_launcher_name():
    run = FakeRun()
    switch = StrataSwitch(settings(aliases={"alfred": "qw2"}, priority=frozenset({"alfred"})), run)
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.post(
            CHAT_PATH, json={"model": "alfred", "messages": []}, headers=AUTH
        )
        body = await resp.json()
    finally:
        await close(router, upstream_server)
    assert resp.status == 200
    assert body["choices"][0]["message"]["content"] == "привет"
    assert upstream.bodies[0] == {"model": "qw2", "messages": []}
    assert run.calls == [LAUNCHERS["qw2"]]
    assert switch.current == "qw2"
    assert switch.active == 0


class SignalLog:
    def __init__(self, missing: tuple[int, ...] = ()) -> None:
        self.sent: list[tuple[int, int]] = []
        self.missing = missing

    def __call__(self, pid: int, sig: int) -> None:
        if pid in self.missing:
            raise ProcessLookupError(pid)
        self.sent.append((pid, sig))


def frozen_engine(
    pids: list[int] | None = None, clock: StepClock | None = None, max_s: float = 300.0
) -> tuple[EngineFreezer, SignalLog]:
    sent = SignalLog()
    found = [41] if pids is None else pids
    return EngineFreezer(lambda: list(found), clock or StepClock(step=0.0), max_s, sent), sent


def fake_proc(root: Path, pid: int, ppid: int, argv: list[str]) -> None:
    entry = root / str(pid)
    entry.mkdir()
    (entry / "stat").write_text(f"{pid} (strata (x)) S {ppid} {pid} {pid} 0")
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")


def test_engine_pids_finds_serving_descendants_only(tmp_path: Path):
    fake_proc(tmp_path, 10, 1, ["python", "-m", "sa_home_bot.llm.strata_switch"])
    fake_proc(tmp_path, 20, 10, ["sh", "qwen-on.sh"])
    fake_proc(tmp_path, 30, 20, ["python3", "-m", "serve.server"])
    fake_proc(tmp_path, 31, 30, ["strata", "--serve", "--pack", "x"])
    fake_proc(tmp_path, 50, 1, ["strata-poly", "--serve"])
    (tmp_path / "self").mkdir()
    assert engine_pids(10, tmp_path) == [31]


def test_freezer_stops_once_and_continues_after_last_release():
    freezer, sent = frozen_engine([41, 42])
    freezer.hold()
    freezer.hold()
    assert sent.sent == [(41, signal.SIGSTOP), (42, signal.SIGSTOP)]
    freezer.release()
    assert freezer.frozen == [41, 42]
    freezer.release()
    assert sent.sent[2:] == [(41, signal.SIGCONT), (42, signal.SIGCONT)]
    assert freezer.frozen == []
    freezer.release()
    assert freezer.holders == 0
    assert len(sent.sent) == 4


def test_freezer_skips_gone_processes():
    sent = SignalLog(missing=(41,))
    freezer = EngineFreezer(lambda: [41, 42], StepClock(step=0.0), 300.0, sent)
    freezer.hold()
    freezer.release()
    assert sent.sent == [(42, signal.SIGSTOP), (42, signal.SIGCONT)]


def test_freezer_thaws_after_max_and_does_not_refreeze_for_held_priority():
    clock = StepClock(step=0.0)
    freezer, sent = frozen_engine(clock=clock, max_s=5.0)
    freezer.hold()
    clock.now += 4.0
    assert freezer.expire_if_stuck() is False
    clock.now += 2.0
    assert freezer.expire_if_stuck() is True
    assert sent.sent == [(41, signal.SIGSTOP), (41, signal.SIGCONT)]
    freezer.hold()
    assert len(sent.sent) == 2
    freezer.release()
    freezer.release()
    freezer.hold()
    assert sent.sent[-1] == (41, signal.SIGSTOP)


async def test_gate_hold_blocks_normal_admission_and_freezes_engine():
    freezer, sent = frozen_engine()
    gate = PriorityGate(StepClock(step=0.0), 0.01, 0.0, freezer)
    await gate.enter_hold()
    assert gate.priority_active == 1
    assert sent.sent == [(41, signal.SIGSTOP)]
    waiter = asyncio.create_task(gate.wait_normal())
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await gate.leave_hold()
    await asyncio.wait_for(waiter, 1.0)
    assert sent.sent == [(41, signal.SIGSTOP), (41, signal.SIGCONT)]


async def test_warm_companion_freezes_main_instead_of_interrupting_it():
    journal: list[str] = []
    run = JournalRun(journal)
    main = VramUpstream(journal, delay=0.3)
    partner = Upstream()
    freezer, sent = frozen_engine()
    app, router, servers, companion = await companion_router(
        main, partner, run, freezer=freezer
    )
    app[COMPANION_KEY].running = True
    try:
        normal = asyncio.create_task(
            router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        )
        await wait_until(lambda: main.arrived == 1)
        partner_resp = await router.post(
            CHAT_PATH, json={"model": COMPANION.name, "messages": []}, headers=AUTH
        )
        assert partner_resp.status == 200
        assert sent.sent == [(41, signal.SIGSTOP), (41, signal.SIGCONT)]
        assert main.aborted == []
        assert journal == []
        normal_resp = await normal
        assert normal_resp.status == 200
        assert main.arrived == 1
        assert main.completed == ["iq3"]
        assert freezer.holders == 0
    finally:
        await close(router, *servers)


async def test_cold_companion_interrupts_main_then_freezes_it_for_the_answer():
    journal: list[str] = []
    run = JournalRun(journal)
    main = VramUpstream(journal, delay=0.3)
    partner = Upstream()
    freezer, sent = frozen_engine()
    app, router, servers, companion = await companion_router(
        main, partner, run, freezer=freezer
    )
    try:
        normal = asyncio.create_task(
            router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        )
        await wait_until(lambda: main.arrived == 1)
        partner_resp = await router.post(
            CHAT_PATH, json={"model": COMPANION.name, "messages": []}, headers=AUTH
        )
        assert partner_resp.status == 200
        assert main.aborted == ["iq3"]
        assert journal == [ACTIVE_RESERVE, LAUNCH]
        assert sent.sent == [(41, signal.SIGSTOP), (41, signal.SIGCONT)]
        assert (await normal).status == 200
    finally:
        await close(router, *servers)


def test_switch_status_reports_frozen_engine():
    switch = StrataSwitch(settings(), FakeRun(), StepClock(step=0.0))
    freezer, _ = frozen_engine()
    gate = PriorityGate(switch.clock, switch.settings.poll_s, 0.0, freezer)
    freezer.hold()
    assert switch_status(switch, gate, None)["engine_frozen"] is True
    freezer.release()
    assert switch_status(switch, gate, None)["engine_frozen"] is False


def test_load_settings_reads_freeze_options(tmp_path: Path):
    path = tmp_path / "freeze.toml"
    path.write_text(
        'default = "iq3"\n'
        "freeze_engine = true\n"
        "freeze_max_s = 120.0\n"
        'llm_socket = "/run/llm.sock"\n'
        '[launchers]\niq3 = ["/models/iq3-on.sh"]\n',
        encoding="utf-8",
    )
    loaded = load_settings(path)
    assert (loaded.freeze_engine, loaded.freeze_max_s, loaded.llm_socket) == (
        True,
        120.0,
        "/run/llm.sock",
    )


TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when uid inode\n"


def tcp_row(port: int, state: str, inode: int) -> str:
    fields = [f"00000000:{port:04X}", "00000000:0000", state, "0:0", "00:0", "0", "1000", "0"]
    return "   0: " + " ".join(fields) + f" {inode} 1\n"


def own_socket(root: Path, pid: int, inode: int) -> None:
    fd_dir = root / str(pid) / "fd"
    fd_dir.mkdir()
    (fd_dir / "3").symlink_to("/dev/null")
    (fd_dir / "11").symlink_to(f"socket:[{inode}]")


def test_upstream_engine_pids_follows_the_listener_not_the_router(tmp_path: Path):
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(
        TCP_HEADER + tcp_row(8080, "0A", 777) + tcp_row(8080, "01", 778) + tcp_row(8086, "0A", 900)
    )
    fake_proc(tmp_path, 1, 0, ["systemd", "--user"])
    fake_proc(tmp_path, 10, 1, ["python", "-m", "sa_home_bot.llm.strata_switch"])
    fake_proc(tmp_path, 30, 1, ["python3", "-m", "serve.server"])
    fake_proc(tmp_path, 31, 30, ["strata", "--serve", "--pack", "x"])
    fake_proc(tmp_path, 40, 1, ["strata-poly", "--serve"])
    fake_proc(tmp_path, 41, 40, ["strata-poly", "--serve", "--worker"])
    for pid in (1, 10, 31, 41):
        (tmp_path / str(pid) / "fd").mkdir()
    own_socket(tmp_path, 30, 777)
    own_socket(tmp_path, 40, 900)
    assert upstream_engine_pids(8080, tmp_path) == [31]
    assert upstream_engine_pids(8086, tmp_path) == [41]
    assert upstream_engine_pids(9000, tmp_path) == []


@pytest.mark.parametrize(
    ("url", "port"),
    [("http://127.0.0.1:8080", 8080), ("http://host", 80), ("https://host/v1", 443)],
)
def test_upstream_port(url: str, port: int):
    assert upstream_port(url) == port


async def test_release_stops_companion_before_idle_expires():
    journal: list[str] = []
    run = JournalRun(journal)
    idle_companion = replace(COMPANION, main_reserve_idle_mib=512)
    switch, _ = companion_switch(run, StepClock(), companion=idle_companion)
    main = VramUpstream(journal)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url)
    try:
        companion.begin_request(AUTH)
        await companion.ensure_running()
        companion.end_request()
        assert journal == [ACTIVE_RESERVE, LAUNCH]
        assert companion.running is True
        assert await companion.release() == "stopped"
        assert journal == [ACTIVE_RESERVE, LAUNCH, STOP, "vram:512"]
        assert companion.running is False
    finally:
        await close(main_server)


@pytest.mark.parametrize(
    ("launch", "expected", "journal_tail"),
    [(False, "not_running", []), (True, "busy", [ACTIVE_RESERVE, LAUNCH])],
)
async def test_release_refuses_to_stop_companion(launch, expected, journal_tail):
    journal: list[str] = []
    run = JournalRun(journal)
    switch, _ = companion_switch(run, StepClock())
    main = VramUpstream(journal)
    main_server, main_url = await main_upstream(main)
    companion = loaded_companion(switch, main_url)
    try:
        if launch:
            companion.begin_request(AUTH)
            await companion.ensure_running()
        assert await companion.release() == expected
        assert journal == journal_tail
        assert companion.running is launch
    finally:
        await close(main_server)


async def test_release_route_stops_running_companion():
    journal: list[str] = []
    run = JournalRun(journal)
    main = VramUpstream(journal)
    partner = Upstream()
    app, router, servers, _ = await companion_router(
        main, partner, run, companion_options={"main_reserve_idle_mib": 512}
    )
    companion = app[COMPANION_KEY]
    try:
        companion.begin_request({})
        await companion.ensure_running()
        companion.end_request()
        resp = await router.post(RELEASE_PATH, headers=AUTH)
        body = await resp.json()
        assert resp.status == 200
        assert body == {"result": "stopped"}
        assert companion.headers == AUTH
        assert journal == [ACTIVE_RESERVE, LAUNCH, STOP, "vram:512"]
        assert companion.running is False
    finally:
        await close(router, *servers)


async def test_release_route_without_companion_answers_404():
    switch = StrataSwitch(settings(), FakeRun())
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.post(RELEASE_PATH)
        await resp.json()
    finally:
        await close(router, upstream_server)


@pytest.mark.parametrize(
    ("toml", "expected"),
    [
        (
            'default = "iq3"\n'
            '[launchers]\niq3 = ["/models/iq3-on.sh"]\n'
            '[stoppers]\niq3 = ["/models/iq3-off.sh"]\n',
            {"iq3": ["/models/iq3-off.sh"]},
        ),
        (
            'default = "iq3"\n'
            '[launchers]\niq3 = ["/models/iq3-on.sh"]\nqw2 = ["/models/qw2-on.sh"]\n'
            '[stoppers]\niq3 = ["/models/iq3-off.sh"]\nqw2 = ["/models/qw2-off.sh"]\n',
            {"iq3": ["/models/iq3-off.sh"], "qw2": ["/models/qw2-off.sh"]},
        ),
    ],
)
def test_load_settings_reads_stoppers(tmp_path: Path, toml: str, expected: dict[str, list[str]]):
    path = tmp_path / "stoppers.toml"
    path.write_text(toml, encoding="utf-8")
    loaded = load_settings(path)
    assert loaded.stoppers == expected


@pytest.mark.parametrize(
    "toml",
    [
        'default = "iq3"\n[launchers]\niq3 = ["/models/iq3-on.sh"]\n'
        '[stoppers]\nqw2 = ["/models/qw2-off.sh"]\n',
        'default = "iq3"\n[launchers]\niq3 = ["/models/iq3-on.sh"]\n'
        '[stoppers]\nunknown = ["/models/unknown-off.sh"]\n',
    ],
)
def test_load_settings_rejects_stopper_not_in_launchers(tmp_path: Path, toml: str):
    path = tmp_path / "bad_stoppers.toml"
    path.write_text(toml, encoding="utf-8")
    with pytest.raises(UnknownModel, match="stoppers"):
        load_settings(path)


async def test_silence_forever_unloads_main_model_and_chat_returns_503():
    journal: list[str] = []
    run = JournalRun(journal)
    switch = StrataSwitch(
        settings(
            stoppers={"iq3": ["/models/iq3-off.sh"]},
        ),
        run,
        StepClock(),
    )
    switch.current = "iq3"
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.post(SILENCE_PATH, json={"silence": {"until": None}}, headers=AUTH)
        body = await resp.json()
        assert resp.status == 200
        assert body["active"] is True
        assert body["current"] is None
        assert run.calls == [["/models/iq3-off.sh"]]
        assert switch.current is None

        resp = await router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        answer = await resp.json()
        assert resp.status == SILENCE_STATUS
        assert answer["code"] == SILENCE_CODE
        assert answer["error"] == SILENCE_FOREVER_TEXT

        resp = await router.post(SILENCE_PATH, json={"silence": None}, headers=AUTH)
        body = await resp.json()
        assert resp.status == 200
        assert body["active"] is False

        resp = await router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        answer = await resp.json()
        assert resp.status == 200
        assert answer["choices"][0]["message"]["content"] == "привет"
    finally:
        await close(router, upstream_server)


@pytest.mark.parametrize(
    "silence",
    [None, {"until": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()}],
    ids=["off", "expired"],
)
async def test_inactive_silence_keeps_model_loaded_and_chat_works(silence):
    run = JournalRun([])
    switch = StrataSwitch(settings(stoppers={"iq3": ["/models/iq3-off.sh"]}), run, StepClock())
    switch.current = "iq3"
    upstream_server, router = await run_router(switch, Upstream())
    try:
        resp = await router.post(SILENCE_PATH, json={"silence": silence}, headers=AUTH)
        assert (resp.status, (await resp.json())["active"]) == (200, False)
        assert run.calls == [] and switch.current == "iq3"
        resp = await router.post(CHAT_PATH, json={"model": "iq3", "messages": []}, headers=AUTH)
        assert resp.status == 200
    finally:
        await close(router, upstream_server)


async def test_unload_if_free_returns_false_when_active_request():
    run = JournalRun([])
    switch = StrataSwitch(
        settings(
            stoppers={"iq3": ["/models/iq3-off.sh"]},
        ),
        run,
        StepClock(),
    )
    switch.current = "iq3"
    switch.active = 1
    result = await switch.unload_if_free()
    assert result is False
    assert run.calls == []


@pytest.mark.parametrize(
    ("main_idle_stop_s", "now", "active", "expected"),
    [
        (600.0, 1000.0, 0, True),
        (0.0, 1000.0, 0, False),
        (600.0, 1000.0, 1, False),
        (600.0, 599.0, 0, False),
    ],
    ids=["истёк", "нуль", "занят", "не истёк"],
)
async def test_main_idle_expired(main_idle_stop_s, now, active, expected):
    journal: list[str] = []
    run = JournalRun(journal)
    switch = StrataSwitch(
        settings(main_idle_stop_s=main_idle_stop_s), run, StepClock(start=now, step=0.0)
    )
    switch.current = "iq3"
    switch.active = active
    switch.last_request_end = 0.0
    assert switch.main_idle_expired() is expected


async def test_idle_guard_step_unloads_main_model_after_idle():
    journal: list[str] = []
    run = JournalRun(journal)
    switch = StrataSwitch(
        settings(main_idle_stop_s=600.0, stoppers={"iq3": ["/models/iq3-off.sh"]}),
        run,
        StepClock(start=1000.0, step=0.0),
    )
    switch.current = "iq3"
    switch.last_request_end = 0.0
    assert await switch.idle_check() is True
    assert run.calls == [["/models/iq3-off.sh"]]
    assert switch.current is None


def test_load_settings_reads_main_idle_stop_s(tmp_path: Path):
    path = tmp_path / "main_idle.toml"
    path.write_text(
        'default = "iq3"\nmain_idle_stop_s = 600\n[launchers]\niq3 = ["/models/iq3-on.sh"]\n',
        encoding="utf-8",
    )
    loaded = load_settings(path)
    assert loaded.main_idle_stop_s == 600.0


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"silence": 5},
        {"silence": {"until": "завтра"}},
        {"silence": {"until": "2026-11-18T12:00:00"}},
    ],
)
async def test_silence_bad_body_returns_400(body: object):
    switch = StrataSwitch(settings(), FakeRun())
    upstream = Upstream()
    upstream_server, router = await run_router(switch, upstream)
    try:
        resp = await router.post(SILENCE_PATH, json=body, headers=AUTH)
        answer = await resp.json()
        assert resp.status == 400
        assert "тело" in answer["error"]
    finally:
        await close(router, upstream_server)


async def test_silence_with_companion_stops_companion_and_shows_in_status():
    journal: list[str] = []
    run = JournalRun(journal)
    main = VramUpstream(journal)
    partner = Upstream()
    app, router, servers, companion = await companion_router(main, partner, run)
    try:
        companion.begin_request(AUTH)
        await companion.ensure_running()
        companion.end_request()
        assert companion.running is True

        resp = await router.post(SILENCE_PATH, json={"silence": {"until": None}}, headers=AUTH)
        body = await resp.json()
        assert resp.status == 200
        assert body["active"] is True
        assert body["companion_running"] is False
        assert STOP in journal

        resp = await router.get(STATUS_PATH)
        status = await resp.json()
        assert status["silence"] is not None
        assert status["silence"]["until"] is None
    finally:
        await close(router, *servers)
