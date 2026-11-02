from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.config import DevlabModelConfig
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.models.backends import build_backend
from sa_home_bot.devlab.models.gateway import Gateway
from sa_home_bot.devlab.models.hubclient import HubClient
from sa_home_bot.devlab.models.ledger import Ledger, today
from sa_home_bot.devlab.models.limits import RateWindow
from tests.unit.support.fakes import FakeNodes

SECRET = "sk-provider-secret-key"
MODEL = "openrouter-free"
UPSTREAM_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
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
        self.quota_status = 200
        self.quota_body: dict | None = None

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

    async def key_endpoint(self, request: web.Request) -> web.Response:
        self.headers.append(dict(request.headers))
        if self.quota_status != 200:
            return web.json_response({"error": "unauthorized"}, status=self.quota_status)
        if self.quota_body is None:
            return web.json_response({"data": {"free_model_daily_requests": {"used": 100, "limit": 500, "remaining": 400}}})
        return web.json_response(self.quota_body)


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
    provider_app.router.add_get("/v1/key", provider.key_endpoint)
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


async def test_provider_quota_parses_response(setup, provider):
    gateway, _ = await setup(provider_quota=True)
    await gateway.refresh_provider_quota()
    assert MODEL in gateway._provider
    remaining, limit = gateway._provider[MODEL]
    assert remaining == 400
    assert limit == 500


async def test_provider_quota_401_returns_none(setup, provider):
    provider.quota_status = 401
    gateway, _ = await setup(provider_quota=True)
    await gateway.refresh_provider_quota()
    assert MODEL not in gateway._provider


async def test_provider_quota_broken_json_returns_none(setup, provider):
    provider.quota_body = {"data": {"free_model_daily_requests": "not an object"}}
    gateway, _ = await setup(provider_quota=True)
    await gateway.refresh_provider_quota()
    assert MODEL not in gateway._provider


async def test_provider_quota_missing_field_returns_none(setup, provider):
    provider.quota_body = {"data": {}}
    gateway, _ = await setup(provider_quota=True)
    await gateway.refresh_provider_quota()
    assert MODEL not in gateway._provider


async def test_provider_quota_non_int_returns_none(setup, provider):
    provider.quota_body = {"data": {"free_model_daily_requests": {"used": 100, "limit": 500, "remaining": "lots"}}}
    gateway, _ = await setup(provider_quota=True)
    await gateway.refresh_provider_quota()
    assert MODEL not in gateway._provider


async def test_provider_quota_authorization_header_sent(setup, provider):
    gateway, _ = await setup(provider_quota=True)
    await gateway.refresh_provider_quota()
    auth_headers = [h for h in provider.headers if "Authorization" in h]
    assert len(auth_headers) == 1
    assert auth_headers[0]["Authorization"] == f"Bearer {SECRET}"


async def test_heartbeat_body_includes_provider_quota_after_refresh(setup, provider):
    gateway, _ = await setup(provider_quota=True)
    await gateway.refresh_provider_quota()
    body = await gateway.heartbeat_body()
    limits = body[protocol.FIELD_LIMITS][MODEL]
    assert protocol.LIMIT_PROVIDER_LEFT in limits
    assert protocol.LIMIT_PROVIDER_LIMIT in limits
    assert limits[protocol.LIMIT_PROVIDER_LEFT] == 400
    assert limits[protocol.FIELD_LIMITS][MODEL][protocol.LIMIT_PROVIDER_LIMIT] == 500


async def test_heartbeat_body_no_provider_quota_without_flag(setup, provider):
    gateway, _ = await setup(provider_quota=False)
    await gateway.refresh_provider_quota()
    body = await gateway.heartbeat_body()
    limits = body[protocol.FIELD_LIMITS][MODEL]
    assert protocol.LIMIT_PROVIDER_LEFT not in limits
    assert protocol.LIMIT_PROVIDER_LIMIT not in limits


async def test_failed_refresh_keeps_previous_value(setup, provider):
    gateway, _ = await setup(provider_quota=True)
    await gateway.refresh_provider_quota()
    assert gateway._provider[MODEL] == (400, 500)
    provider.quota_status = 500
    await gateway.refresh_provider_quota()
    assert gateway._provider[MODEL] == (400, 500)


async def test_provider_quota_even_when_rpd_zero(setup, provider):
    gateway, _ = await setup(provider_quota=True, rpd=0)
    await gateway.refresh_provider_quota()
    body = await gateway.heartbeat_body()
    limits = body[protocol.FIELD_LIMITS][MODEL]
    assert protocol.LIMIT_PROVIDER_LEFT in limits
    assert protocol.LIMIT_PROVIDER_LIMIT in limits
    assert limits[protocol.LIMIT_PROVIDER_LEFT] == 400
    assert limits[protocol.LIMIT_PROVIDER_LIMIT] == 500
