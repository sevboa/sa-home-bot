from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.config import DevlabModelConfig
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import http
from sa_home_bot.devlab.hub.store import HubStore
from sa_home_bot.devlab.models.backends import build_backend
from sa_home_bot.devlab.models.gateway import Gateway
from sa_home_bot.devlab.models.hubclient import HubClient
from sa_home_bot.devlab.models.ledger import Ledger, today
from sa_home_bot.devlab.models.limits import RateWindow
from tests.unit.support.fakes import FakeNodes

SECRET = "sk-provider-secret-key"
MODEL = "free-llama"
UPSTREAM_MODEL = "meta/llama-3"
JOB = "job-1"
EXECUTOR_TOKEN = "dlh_executor"
CLIENT_TOKEN = "dlh_client"
PRINCIPALS = {
    EXECUTOR_TOKEN: {protocol.FIELD_NAME: "pc", protocol.FIELD_ROLE: protocol.ROLE_EXECUTOR},
    CLIENT_TOKEN: {protocol.FIELD_NAME: "me", protocol.FIELD_ROLE: protocol.ROLE_CLIENT},
}
AUTH = {"Authorization": f"Bearer {EXECUTOR_TOKEN}", protocol.HEADER_JOB: JOB}
CHAT = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
USAGE = {"prompt_tokens": 1000, "completion_tokens": 500}
GATEWAY_URL = "http://gw.test:8721"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Provider:
    def __init__(self) -> None:
        self.received: list[dict] = []
        self.headers: list[dict] = []
        self.gate = asyncio.Event()
        self.stream_usage = True
        self.status = 200
        self.usage = USAGE

    async def chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.received.append(body)
        self.headers.append(dict(request.headers))
        if self.status != 200:
            return web.json_response({"error": "provider says no"}, status=self.status)
        if not body.get("stream"):
            return web.json_response(
                {"choices": [{"message": {"content": "hello"}}], "usage": self.usage}
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"choices": [{"delta": {"content": "one"}}]}\n\n')
        await self.gate.wait()
        last = {"choices": [], "usage": self.usage} if self.stream_usage else {"choices": []}
        await response.write(b"data: " + json.dumps(last).encode() + b"\n\ndata: [DONE]\n\n")
        return response


class FakeHub:
    def __init__(self) -> None:
        self.introspect_calls = 0
        self.addresses: list[str] = []
        self.heartbeats: list[dict] = []
        self.heartbeat_replies: list[dict] = []

    async def introspect(self, request: web.Request) -> web.Response:
        self.introspect_calls += 1
        body = await request.json()
        self.addresses.append(body[protocol.FIELD_ADDRESS])
        principal = PRINCIPALS.get(body[protocol.FIELD_TOKEN])
        if principal is None:
            raise web.HTTPNotFound()
        return web.json_response(principal)

    async def heartbeat(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.heartbeats.append(body)
        reply = {protocol.FIELD_OK: True}
        if self.heartbeat_replies:
            reply.update(self.heartbeat_replies.pop(0))
        return web.json_response(reply)


@pytest.fixture
def provider():
    return Provider()


@pytest.fixture
def hub_state():
    return FakeHub()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def setup(tmp_path, provider, hub_state, clock):
    provider_app = web.Application()
    provider_app.router.add_post("/v1/chat/completions", provider.chat)
    hub_app = web.Application()
    hub_app.router.add_post(protocol.HTTP_INTROSPECT_PATH, hub_state.introspect)
    hub_app.router.add_post(protocol.HTTP_HEARTBEAT_PATH, hub_state.heartbeat)
    provider_server = TestServer(provider_app)
    hub_server = TestServer(hub_app)
    await provider_server.start_server()
    await hub_server.start_server()
    ledger = Ledger(tmp_path)
    await ledger.open()
    opened: list = []

    async def make(**overrides):
        spec = DevlabModelConfig(
            base_url=str(provider_server.make_url("/v1")),
            model=UPSTREAM_MODEL,
            api_key=SECRET,
            free=True,
            price_in_per_m=2.0,
            price_out_per_m=4.0,
            context=8192,
            **overrides,
        )
        backend = build_backend(MODEL, spec)
        hub = HubClient(str(hub_server.make_url("")), "dlh_gateway", clock=clock)
        windows = {MODEL: RateWindow(spec.rpm, clock=clock, sleep=clock.sleep)}
        gateway = Gateway({MODEL: backend}, ledger, hub, windows, url=GATEWAY_URL)
        client = TestClient(TestServer(gateway.build_app()))
        await client.start_server()
        opened.extend([client, backend, hub])
        return gateway, client

    yield make
    for item in reversed(opened):
        await (item.close() if hasattr(item, "close") else asyncio.sleep(0))
    await ledger.close()
    await provider_server.close()
    await hub_server.close()


async def test_heartbeat_body_includes_used_today(setup):
    gateway, _ = await setup(rpd=10)
    body = await gateway.heartbeat_body()
    assert protocol.FIELD_USED_TODAY in body
    used = body[protocol.FIELD_USED_TODAY]
    assert used["day"] == today()
    assert used["requests"] == {MODEL: 0}


async def test_heartbeat_body_used_today_after_request(setup, provider):
    gateway, client = await setup(rpd=10)
    await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    body = await gateway.heartbeat_body()
    used = body[protocol.FIELD_USED_TODAY]
    assert used["requests"] == {MODEL: 1}


async def test_shared_key_false_ignores_shared(setup, provider, hub_state):
    gateway, client = await setup(rpd=10, shared_key=False)
    hub_state.heartbeat_replies.append({
        protocol.FIELD_SHARED_USED: {"day": today(), "requests": {MODEL: 5}}
    })
    await gateway.hub.heartbeat(await gateway.heartbeat_body())
    assert await gateway.used_today(MODEL) == 0
    resp = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    assert resp.status == 200


async def test_shared_key_true_adds_shared(setup, provider, hub_state):
    gateway, client = await setup(rpd=10, shared_key=True)
    hub_state.heartbeat_replies.append({
        protocol.FIELD_SHARED_USED: {"day": today(), "requests": {MODEL: 4}}
    })
    reply = await gateway.hub.heartbeat(await gateway.heartbeat_body())
    gateway.note_shared(reply)
    assert await gateway.used_today(MODEL) == 4
    for _ in range(6):
        resp = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
        assert resp.status == 200
    resp = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    assert resp.status == 429


async def test_shared_key_yesterday_not_counted(setup, provider, hub_state):
    gateway, client = await setup(rpd=10, shared_key=True)
    yesterday = "2000-01-01"
    hub_state.heartbeat_replies.append({
        protocol.FIELD_SHARED_USED: {"day": yesterday, "requests": {MODEL: 100}}
    })
    reply = await gateway.hub.heartbeat(await gateway.heartbeat_body())
    gateway.note_shared(reply)
    assert await gateway.used_today(MODEL) == 0
    resp = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    assert resp.status == 200


async def test_shared_key_wrong_day_in_reply_ignored(setup, provider, hub_state):
    gateway, client = await setup(rpd=10, shared_key=True)
    hub_state.heartbeat_replies.append({
        protocol.FIELD_SHARED_USED: {"day": "not-a-date", "requests": {MODEL: 5}}
    })
    reply = await gateway.hub.heartbeat(await gateway.heartbeat_body())
    gateway.note_shared(reply)
    assert await gateway.used_today(MODEL) == 0


async def test_shared_key_empty_reply_keeps_old(setup, provider, hub_state):
    gateway, client = await setup(rpd=10, shared_key=True)
    hub_state.heartbeat_replies.append({
        protocol.FIELD_SHARED_USED: {"day": today(), "requests": {MODEL: 3}}
    })
    reply = await gateway.hub.heartbeat(await gateway.heartbeat_body())
    gateway.note_shared(reply)
    assert await gateway.used_today(MODEL) == 3
    hub_state.heartbeat_replies.append({protocol.FIELD_OK: True})
    reply = await gateway.hub.heartbeat(await gateway.heartbeat_body())
    gateway.note_shared(reply)
    assert await gateway.used_today(MODEL) == 3


async def test_hub_shared_used_sums_other_gateways(tmp_path):
    store = HubStore(tmp_path)
    await store.open()
    today_str = "2024-01-15"
    await store.heartbeat(
        type("P", (), {"name": "gw1", "role": protocol.ROLE_GATEWAY})(),
        {
            protocol.FIELD_MODELS: [{protocol.MODEL_NAME: MODEL}],
            protocol.FIELD_URL: "http://gw1",
            protocol.FIELD_USED_TODAY: {"day": today_str, "requests": {MODEL: 3}},
        },
    )
    await store.heartbeat(
        type("P", (), {"name": "gw2", "role": protocol.ROLE_GATEWAY})(),
        {
            protocol.FIELD_MODELS: [{protocol.MODEL_NAME: MODEL}],
            protocol.FIELD_URL: "http://gw2",
            protocol.FIELD_USED_TODAY: {"day": today_str, "requests": {MODEL: 4}},
        },
    )
    await store.heartbeat(
        type("P", (), {"name": "gw3", "role": protocol.ROLE_GATEWAY})(),
        {
            protocol.FIELD_MODELS: [{protocol.MODEL_NAME: MODEL}],
            protocol.FIELD_URL: "http://gw3",
            protocol.FIELD_USED_TODAY: {"day": "2024-01-14", "requests": {MODEL: 100}},
        },
    )
    shared = await store.shared_used("gw1", today_str)
    assert shared == {MODEL: 4}
    shared = await store.shared_used("gw2", today_str)
    assert shared == {MODEL: 3}
    await store.close()


async def test_hub_heartbeat_returns_shared_used(tmp_path):
    store = HubStore(tmp_path)
    await store.open()
    today_str = "2024-01-15"
    await store.heartbeat(
        type("P", (), {"name": "gw", "role": protocol.ROLE_GATEWAY})(),
        {
            protocol.FIELD_MODELS: [{protocol.MODEL_NAME: MODEL}],
            protocol.FIELD_URL: "http://gw",
            protocol.FIELD_USED_TODAY: {"day": today_str, "requests": {MODEL: 3}},
        },
    )
    await store.heartbeat(
        type("P", (), {"name": "gw2", "role": protocol.ROLE_GATEWAY})(),
        {
            protocol.FIELD_MODELS: [{protocol.MODEL_NAME: MODEL}],
            protocol.FIELD_URL: "http://gw2",
            protocol.FIELD_USED_TODAY: {"day": today_str, "requests": {MODEL: 4}},
        },
    )
    from sa_home_bot.devlab.hub.executor_api import heartbeat

    nodes = FakeNodes({"gw": {"127.0.0.1"}, "gw2": {"127.0.0.1"}})
    app = http.build_app(store, nodes)
    app.router.add_post(protocol.HTTP_HEARTBEAT_PATH, heartbeat)

    async with TestClient(TestServer(app)) as client:
        token = await store.issue_token("gw", protocol.ROLE_GATEWAY)
        headers = {"Authorization": f"Bearer {token}"}
        body = {
            protocol.FIELD_MODELS: [{protocol.MODEL_NAME: MODEL}],
            protocol.FIELD_URL: "http://gw",
            protocol.FIELD_USED_TODAY: {"day": today_str, "requests": {MODEL: 3}},
        }
        resp = await client.post(protocol.HTTP_HEARTBEAT_PATH, json=body, headers=headers)
        data = await resp.json()
        assert protocol.FIELD_SHARED_USED in data
        assert data[protocol.FIELD_SHARED_USED] == {"day": today_str, "requests": {MODEL: 4}}
    await store.close()


async def test_rpd_left_in_heartbeat_includes_shared(setup, provider, hub_state):
    gateway, client = await setup(rpd=10, shared_key=True)
    hub_state.heartbeat_replies.append({
        protocol.FIELD_SHARED_USED: {"day": today(), "requests": {MODEL: 4}}
    })
    reply = await gateway.hub.heartbeat(await gateway.heartbeat_body())
    gateway.note_shared(reply)
    body = await gateway.heartbeat_body()
    assert body[protocol.FIELD_LIMITS][MODEL][protocol.LIMIT_RPD_LEFT] == 6


async def test_hub_client_heartbeat_returns_json(setup, hub_state):
    gateway, _ = await setup()
    reply = await gateway.hub.heartbeat(await gateway.heartbeat_body())
    assert reply == {protocol.FIELD_OK: True}


async def test_hub_client_heartbeat_returns_none_on_error():
    hub = HubClient("http://127.0.0.1:1", "dlh_gateway")
    reply = await hub.heartbeat({})
    assert reply is None
    await hub.close()


async def test_note_shared_invalid_reply_ignored(setup):
    gateway, _ = await setup(rpd=10, shared_key=True)
    gateway.note_shared(None)
    assert gateway._shared is None
    gateway.note_shared({protocol.FIELD_OK: True})
    assert gateway._shared is None
    gateway.note_shared({protocol.FIELD_SHARED_USED: "not a dict"})
    assert gateway._shared is None
    gateway.note_shared({protocol.FIELD_SHARED_USED: {"day": 123, "requests": {}}})
    assert gateway._shared is None
    gateway.note_shared(
        {protocol.FIELD_SHARED_USED: {"day": "2024-01-01", "requests": "not a dict"}}
    )
    assert gateway._shared is None


async def test_note_shared_valid_reply_stored(setup):
    gateway, _ = await setup(rpd=10, shared_key=True)
    gateway.note_shared({protocol.FIELD_SHARED_USED: {"day": "2024-01-01", "requests": {MODEL: 5}}})
    assert gateway._shared == ("2024-01-01", {MODEL: 5})


async def test_heartbeat_body_used_today_only_own(setup, provider, hub_state):
    gateway, client = await setup(rpd=10, shared_key=True)
    hub_state.heartbeat_replies.append({
        protocol.FIELD_SHARED_USED: {"day": today(), "requests": {MODEL: 4}}
    })
    await gateway.hub.heartbeat(await gateway.heartbeat_body())
    await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    body = await gateway.heartbeat_body()
    assert body[protocol.FIELD_USED_TODAY]["requests"][MODEL] == 1


async def test_shared_key_multiple_models(tmp_path, provider, hub_state, clock):
    MODEL2 = "paid-gpt"
    UPSTREAM_MODEL2 = "gpt-4"
    spec1 = DevlabModelConfig(
        base_url="http://provider/v1",
        model=UPSTREAM_MODEL,
        api_key=SECRET,
        free=True,
        price_in_per_m=2.0,
        price_out_per_m=4.0,
        context=8192,
        rpd=10,
        shared_key=True,
    )
    spec2 = DevlabModelConfig(
        base_url="http://provider/v1",
        model=UPSTREAM_MODEL2,
        api_key=SECRET,
        free=False,
        price_in_per_m=10.0,
        price_out_per_m=30.0,
        context=8192,
        rpd=5,
        shared_key=True,
    )
    backend1 = build_backend(MODEL, spec1)
    backend2 = build_backend(MODEL2, spec2)
    hub_app = web.Application()
    hub_app.router.add_post(protocol.HTTP_INTROSPECT_PATH, hub_state.introspect)
    hub_app.router.add_post(protocol.HTTP_HEARTBEAT_PATH, hub_state.heartbeat)
    hub_server = TestServer(hub_app)
    await hub_server.start_server()
    hub = HubClient(str(hub_server.make_url("")), "dlh_gateway", clock=clock)
    windows = {
        MODEL: RateWindow(spec1.rpm, clock=clock, sleep=clock.sleep),
        MODEL2: RateWindow(spec2.rpm, clock=clock, sleep=clock.sleep),
    }
    ledger = Ledger(tmp_path)
    await ledger.open()
    gateway = Gateway({MODEL: backend1, MODEL2: backend2}, ledger, hub, windows, url=GATEWAY_URL)
    hub_state.heartbeat_replies.append(
        {
            protocol.FIELD_SHARED_USED: {
                "day": today(),
                "requests": {MODEL: 3, MODEL2: 2},
            }
        }
    )
    reply = await gateway.hub.heartbeat(await gateway.heartbeat_body())
    gateway.note_shared(reply)
    assert await gateway.used_today(MODEL) == 3
    assert await gateway.used_today(MODEL2) == 2
    await ledger.close()
    await hub.close()
    await hub_server.close()
    await backend1.close()
    await backend2.close()
