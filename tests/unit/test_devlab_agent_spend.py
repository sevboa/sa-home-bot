from __future__ import annotations

from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.loop import AgentLimits
from sa_home_bot.devlab.agent.model import GatewayChat, ModelError, ModelReply
from sa_home_bot.devlab.executor.agent_worker import AGENT_STATE_FILE, AgentState
from sa_home_bot.devlab.executor.hub import ExecutorHub
from sa_home_bot.devlab.executor.routing import (
    CatalogEntry,
    FailoverChat,
    ModelCatalog,
    price_reply,
)
from sa_home_bot.utils.files import load_model
from tests.unit import test_devlab_executor as executor_tests
from tests.unit.support.fakes import ScriptedChatModel, StaticCatalog, local_entry
from tests.unit.test_devlab_agent import reply_body
from tests.unit.test_devlab_agent_owner import ASK, parked_worker
from tests.unit.test_devlab_agent_worker import FINISH, MODEL, WRITE_DONE, make_worker, submit

env = executor_tests.env
MESSAGES = [{"role": "user", "content": "привет"}]
PAID = replace(WRITE_DONE, cost_usd=0.6)
PAID_ASK = replace(ASK, cost_usd=0.6)
PAID_FINISH = replace(FINISH, cost_usd=0.1)


async def noop_sleep(seconds: float) -> None:
    return None


async def gateway_server(status: int = 200, usage: dict | None = None) -> tuple[TestServer, list]:
    bodies: list = []

    async def handler(request):
        bodies.append(await request.json())
        if status != 200:
            return web.json_response({"error": "лежит"}, status=status)
        return web.json_response(reply_body("ок", usage=usage))

    app = web.Application()
    app.router.add_post(protocol.HTTP_CHAT_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    return server, bodies


def failover(entries: list[CatalogEntry], session: aiohttp.ClientSession) -> FailoverChat:
    return FailoverChat(
        StaticCatalog(*entries),
        lambda entry: GatewayChat(entry.url, "tok", "m", "job", session=session),
        "m",
        retry_s=0.0,
        sleep=noop_sleep,
    )


def entry_at(server: TestServer, gateway: str, **fields) -> CatalogEntry:
    return CatalogEntry("m", gateway, str(server.make_url("")).rstrip("/"), **fields)


async def test_catalog_gives_executor_gateway_urls_and_clients_do_not(env):
    for name, url in (("jeeves", "http://j:8721"), ("wooster", "http://w:8721")):
        token = await env.store.issue_token(name, protocol.ROLE_GATEWAY)
        principal = await env.store.authenticate(token)
        beat = {
            protocol.FIELD_MODELS: [{protocol.MODEL_NAME: "glm", protocol.MODEL_REMOTE: True}],
            protocol.FIELD_URL: url,
        }
        await env.store.heartbeat(principal, beat)
    hub = ExecutorHub(env.hub_url, env.executor_token)
    env.hubs.append(hub)

    entries = await ModelCatalog(hub).entries("glm")

    assert [(e.gateway, e.url) for e in entries] == [
        ("jeeves", "http://j:8721"),
        ("wooster", "http://w:8721"),
    ]
    listed = await env.mcp.call(protocol.TOOL_MODELS)
    assert all(protocol.MODEL_URL not in item for item in listed)
    assert await ModelCatalog(hub).names() == ["glm"]


async def test_failover_switches_gateway_keeping_dialog():
    dead, dead_bodies = await gateway_server(status=503)
    alive, alive_bodies = await gateway_server(usage={"prompt_tokens": 5, "completion_tokens": 2})
    session = aiohttp.ClientSession()
    try:
        chat = failover([entry_at(dead, "jeeves"), entry_at(alive, "wooster")], session)
        first = await chat.complete(MESSAGES, [])
        second = await chat.complete(MESSAGES, [])
    finally:
        await session.close()
        await dead.close()
        await alive.close()

    assert first.content == "ок" and (first.tokens_in, first.tokens_out) == (5, 2)
    assert len(dead_bodies) == 1
    assert len(alive_bodies) == 2
    assert alive_bodies[0]["messages"] == dead_bodies[0]["messages"] == MESSAGES
    assert second.content == "ок"


async def test_failover_all_gateways_down_fails_and_client_error_does_not_switch():
    down, _ = await gateway_server(status=502)
    bad, _ = await gateway_server(status=400)
    spare, spare_bodies = await gateway_server()
    session = aiohttp.ClientSession()
    try:
        with pytest.raises(ModelError, match="все шлюзы"):
            await failover([entry_at(down, "a")], session).complete(MESSAGES, [])
        with pytest.raises(ModelError, match="HTTP 400"):
            await failover([entry_at(bad, "a"), entry_at(spare, "b")], session).complete(
                MESSAGES, []
            )
    finally:
        await session.close()
        for server in (down, bad, spare):
            await server.close()
    assert spare_bodies == []


async def test_failover_without_live_gateways():
    with pytest.raises(ModelError, match="нет на живых шлюзах"):
        await failover([], None).complete(MESSAGES, [])  # type: ignore[arg-type]


def test_price_reply_rules():
    paid = CatalogEntry("m", "g", "u", price_in=1.0, price_out=2.0)
    free = CatalogEntry("m", "g", "u", free=True, price_in=1.0, price_out=2.0)
    reply = ModelReply("", tokens_in=1_000_000, tokens_out=500_000)

    assert price_reply(paid, reply).cost_usd == pytest.approx(2.0)
    assert price_reply(paid, replace(reply, cost_usd=0.5)).cost_usd == 0.5
    quiet = price_reply(free, reply)
    assert quiet.cost_usd == 0.0 and not quiet.paid_free
    charged = price_reply(free, replace(reply, cost_usd=0.01))
    assert charged.cost_usd == 0.01 and charged.paid_free


async def test_spend_is_reported_and_totals_in_hub(env):
    job_id = await submit(env)
    worker = make_worker(env, ScriptedChatModel(PAID, PAID_FINISH))

    assert await worker.claim_once()

    job = await env.store.queue.get_job(job_id)
    assert job.cost_usd == pytest.approx(0.7)
    shown = await env.mcp.call(protocol.TOOL_STATUS, id=job_id)
    assert shown["cost_usd"] == pytest.approx(0.7)
    assert load_model(AgentState, env.tmp_path / "exec" / AGENT_STATE_FILE).spend == {}


async def test_ceiling_is_across_questions_and_survives_restart(env):
    job_id = await submit(env)
    limits = AgentLimits(max_cost_usd=1.0)
    first, _, _ = await parked_worker(env, [PAID, PAID_ASK], limits=limits)
    assert await first.claim_once()
    assert (await env.store.queue.get_job(job_id)).cost_usd == pytest.approx(1.2)

    model = ScriptedChatModel(PAID_FINISH)
    restarted = make_worker(env, model, limits=limits)
    await env.mcp.call(protocol.TOOL_ANSWER, id=job_id, text="давай")
    await restarted.poll_once()
    assert await restarted.claim_once()

    assert model.seen == []
    assert await env.status(job_id) == protocol.STATUS_WAITING_OWNER
    question = await env.store.queue.last_message(job_id, protocol.MSG_QUESTION)
    assert "потолок расхода" in question.payload[protocol.PAYLOAD_TEXT]

    await env.mcp.call(protocol.TOOL_ANSWER, id=job_id, text="продолжай")
    await restarted.poll_once()
    assert await restarted.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert (await env.store.queue.get_job(job_id)).cost_usd == pytest.approx(1.3)


async def test_free_model_charging_money_stops_with_question(env):
    job_id = await submit(env)
    charged = replace(WRITE_DONE, cost_usd=0.01, paid_free=True)
    worker = make_worker(env, ScriptedChatModel(charged))

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_WAITING_OWNER
    question = await env.store.queue.last_message(job_id, protocol.MSG_QUESTION)
    assert "бесплатная модель начала тратить деньги" in question.payload[protocol.PAYLOAD_TEXT]
    assert (await env.store.queue.get_job(job_id)).cost_usd == pytest.approx(0.01)


async def test_remote_model_refused_unless_project_allows(env):
    job_id = await submit(env)
    remote = CatalogEntry(MODEL, "jeeves", "http://j", remote=True)
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    worker = make_worker(env, model, catalog=StaticCatalog(remote))

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_FAILED
    failed = await env.store.queue.last_message(job_id, protocol.MSG_FAILED)
    assert "allow_remote_models" in failed.payload[protocol.PAYLOAD_TEXT]
    assert model.seen == []


async def test_claim_offers_only_models_the_executor_has(env):
    await submit(env)
    worker = make_worker(env, ScriptedChatModel(), catalog=StaticCatalog(local_entry("other")))

    assert not await worker.claim_once()


async def test_resume_remarks_survive_claim_before_poll(env):
    job_id = await submit(env)
    worker, _, _ = await parked_worker(env, [WRITE_DONE, ASK])
    assert await worker.claim_once()
    model = ScriptedChatModel(FINISH)
    worker._models = lambda name, jid: model

    await env.mcp.call(protocol.TOOL_RESUME, id=job_id, text="учти кодировку")
    assert await worker.claim_once()
    await worker.poll_once()

    assert "учти кодировку" in model.seen[0][1]["content"]
    assert await env.status(job_id) == protocol.STATUS_DONE
