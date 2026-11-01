from __future__ import annotations

import asyncio
import json
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.config import DevlabModelConfig, DevlabModelsConfig, Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.store import HEARTBEAT_FRESH_S
from sa_home_bot.devlab.models.backends import build_backend
from sa_home_bot.devlab.models.gateway import Gateway
from sa_home_bot.devlab.models.hubclient import HEARTBEAT_INTERVAL_S, INTROSPECT_TTL_S, HubClient
from sa_home_bot.devlab.models.ledger import Ledger
from sa_home_bot.devlab.models.limits import MINUTE_S, RateWindow

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

    async def introspect(self, request: web.Request) -> web.Response:
        self.introspect_calls += 1
        body = await request.json()
        self.addresses.append(body[protocol.FIELD_ADDRESS])
        principal = PRINCIPALS.get(body[protocol.FIELD_TOKEN])
        if principal is None:
            raise web.HTTPNotFound()
        return web.json_response(principal)

    async def heartbeat(self, request: web.Request) -> web.Response:
        self.heartbeats.append(await request.json())
        return web.json_response({protocol.FIELD_OK: True})


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


async def test_plain_request_is_proxied_with_provider_key_and_model(setup, provider):
    _, client = await setup()
    resp = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    assert resp.status == 200
    assert (await resp.json())["choices"][0]["message"]["content"] == "hello"
    assert provider.received[0]["model"] == UPSTREAM_MODEL
    assert provider.headers[0]["Authorization"] == f"Bearer {SECRET}"


async def test_stream_chunks_arrive_as_provider_sends_them(setup, provider):
    _, client = await setup()
    resp = await client.post(protocol.HTTP_CHAT_PATH, json={**CHAT, "stream": True}, headers=AUTH)
    assert resp.status == 200
    first = await asyncio.wait_for(resp.content.readany(), timeout=5)
    assert b"one" in first
    assert not provider.gate.is_set()
    provider.gate.set()
    rest = await resp.content.read()
    assert b"[DONE]" in rest
    assert provider.received[0]["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize(
    ("stream", "stream_usage", "tokens"),
    [(False, True, (1000, 500)), (True, True, (1000, 500)), (True, False, (0, 0))],
)
async def test_ledger_counts_tokens_and_dollars_per_job(
    setup, provider, stream, stream_usage, tokens
):
    gateway, client = await setup()
    provider.stream_usage = stream_usage
    provider.gate.set()
    resp = await client.post(protocol.HTTP_CHAT_PATH, json={**CHAT, "stream": stream}, headers=AUTH)
    await resp.read()
    totals = await gateway.ledger.totals(MODEL, JOB)
    tokens_in, tokens_out = tokens
    assert (totals["requests"], totals["tokens_in"], totals["tokens_out"]) == (
        1,
        tokens_in,
        tokens_out,
    )
    assert totals["cost_usd"] == pytest.approx((tokens_in * 2.0 + tokens_out * 4.0) / 1e6)
    assert (await gateway.ledger.totals(MODEL, "other"))["requests"] == 0


async def test_provider_fallback_fields_are_not_forwarded(setup, provider):
    _, client = await setup()
    body = {**CHAT, "models": ["paid/a"], "route": "fallback", "temperature": 0.2}
    await client.post(protocol.HTTP_CHAT_PATH, json=body, headers=AUTH)
    assert "models" not in provider.received[0] and "route" not in provider.received[0]
    assert provider.received[0]["temperature"] == 0.2


async def test_provider_reported_cost_beats_price_list(setup, provider):
    gateway, client = await setup()
    provider.usage = {**USAGE, "cost": 0.0123}
    resp = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    await resp.read()
    assert (await gateway.ledger.totals(MODEL, JOB))["cost_usd"] == pytest.approx(0.0123)


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Bearer dlh_unknown"}, 401),
        ({"Authorization": f"Bearer {CLIENT_TOKEN}"}, 403),
        ({"Authorization": f"Bearer {EXECUTOR_TOKEN}"}, 200),
    ],
)
async def test_access_needs_executor_token(setup, headers, status):
    _, client = await setup()
    resp = await client.get(protocol.HTTP_MODELS_PATH, headers=headers)
    assert resp.status == status


async def test_health_is_public_and_models_listed(setup):
    _, client = await setup()
    assert (await client.get(protocol.HTTP_HEALTH_PATH)).status == 200
    listing = await (await client.get(protocol.HTTP_MODELS_PATH, headers=AUTH)).json()
    assert [m["id"] for m in listing["data"]] == [MODEL]


@pytest.mark.parametrize("token", [EXECUTOR_TOKEN, "dlh_unknown"])
async def test_introspect_answer_is_cached_then_expires(setup, hub_state, clock, token):
    _, client = await setup()
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(3):
        await client.get(protocol.HTTP_MODELS_PATH, headers=headers)
    assert hub_state.introspect_calls == 1
    clock.now += INTROSPECT_TTL_S + 1
    await client.get(protocol.HTTP_MODELS_PATH, headers=headers)
    assert hub_state.introspect_calls == 2


async def test_introspect_sends_client_address_and_caches_per_address(setup, hub_state):
    gateway, client = await setup()
    await client.get(protocol.HTTP_MODELS_PATH, headers=AUTH)
    assert hub_state.addresses == ["127.0.0.1"]
    await gateway.hub.introspect(EXECUTOR_TOKEN, "100.64.0.9")
    await gateway.hub.introspect(EXECUTOR_TOKEN, "100.64.0.9")
    assert hub_state.addresses == ["127.0.0.1", "100.64.0.9"]


async def test_rpm_waits_for_window_instead_of_failing(setup, clock):
    _, client = await setup(rpm=1)
    first = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    second = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    assert (first.status, second.status) == (200, 200)
    assert clock.sleeps == [MINUTE_S]


async def test_rpd_exhausted_gives_429_with_text(setup, provider):
    _, client = await setup(rpd=1)
    assert (await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)).status == 200
    resp = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    assert resp.status == 429
    assert "суточный лимит" in (await resp.json())[protocol.FIELD_ERROR]
    assert len(provider.received) == 1


async def test_unknown_model_is_404(setup):
    _, client = await setup()
    resp = await client.post(protocol.HTTP_CHAT_PATH, json={"model": "nope"}, headers=AUTH)
    assert resp.status == 404


async def test_heartbeat_body_has_catalog_and_remaining_day_limit(setup):
    gateway, client = await setup(rpd=10)
    await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    body = await gateway.heartbeat_body()
    assert body[protocol.FIELD_MODELS] == [
        {
            protocol.MODEL_NAME: MODEL,
            protocol.MODEL_KIND: protocol.MODEL_KIND_OPENAI,
            protocol.MODEL_FREE: True,
            protocol.MODEL_PRICE_IN: 2.0,
            protocol.MODEL_PRICE_OUT: 4.0,
            protocol.MODEL_CONTEXT: 8192,
            protocol.MODEL_REMOTE: True,
        }
    ]
    assert body[protocol.FIELD_LIMITS] == {MODEL: {protocol.LIMIT_RPD_LEFT: 9}}
    assert body[protocol.FIELD_URL] == GATEWAY_URL


async def test_heartbeat_reaches_hub_and_network_error_is_logged(setup, hub_state, caplog):
    gateway, _ = await setup()
    await gateway.hub.heartbeat(await gateway.heartbeat_body())
    assert hub_state.heartbeats[0][protocol.FIELD_MODELS][0][protocol.MODEL_NAME] == MODEL
    dead = HubClient("http://127.0.0.1:1", "dlh_gateway")
    with caplog.at_level(logging.WARNING):
        await dead.heartbeat(await gateway.heartbeat_body())
    await dead.close()
    assert "Heartbeat в хаб не прошёл" in caplog.text


def test_heartbeat_interval_is_below_hub_freshness():
    assert HEARTBEAT_INTERVAL_S < HEARTBEAT_FRESH_S


@pytest.mark.parametrize("provider_status", [200, 500])
async def test_provider_key_never_leaks(setup, provider, hub_state, caplog, provider_status):
    provider.status = provider_status
    gateway, client = await setup()
    with caplog.at_level(logging.DEBUG):
        chat = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
        models = await client.get(protocol.HTTP_MODELS_PATH, headers=AUTH)
        texts = [await chat.text(), await models.text(), json.dumps(await gateway.heartbeat_body())]
        await gateway.hub.heartbeat(await gateway.heartbeat_body())
    assert SECRET not in json.dumps(hub_state.heartbeats)
    assert all(SECRET not in text for text in texts)
    assert SECRET not in caplog.text


async def test_unreachable_provider_is_502_without_key(setup, caplog):
    gateway, client = await setup()
    backend = gateway.backends[MODEL]
    backend.spec = backend.spec.model_copy(update={"base_url": "http://127.0.0.1:1/v1"})
    with caplog.at_level(logging.DEBUG):
        resp = await client.post(protocol.HTTP_CHAT_PATH, json=CHAT, headers=AUTH)
    assert resp.status == 502
    assert SECRET not in await resp.text()
    assert SECRET not in caplog.text


def test_config_collects_named_model_tables():
    cfg = DevlabModelsConfig.model_validate(
        {"port": 9000, "or": {"base_url": "https://x/v1", "free": True}}
    )
    assert cfg.port == 9000
    assert cfg.entries["or"].free is True
    assert cfg.entries["or"].kind == protocol.MODEL_KIND_OPENAI


def test_unknown_key_in_model_table_is_reported():
    from sa_home_bot.config import unknown_config_keys

    raw = {"devlab_models": {"port": 1, "or": {"base_url": "x", "bogus": 1}}}
    assert unknown_config_keys(raw, Settings) == ["devlab_models.or.bogus"]
