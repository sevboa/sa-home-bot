from __future__ import annotations

import asyncio
from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.loop import AgentLimits
from sa_home_bot.devlab.agent.model import GatewayChat, ModelError, ModelReply, parse_reply
from sa_home_bot.devlab.agent.prompt import with_flow_reminder
from sa_home_bot.devlab.agent.spend import JobSpend, SpendState
from sa_home_bot.devlab.executor.agent_worker import AGENT_STATE_FILE, AgentState
from sa_home_bot.devlab.executor.hub import ExecutorHub
from sa_home_bot.devlab.executor.routing import (
    CatalogEntry,
    FailoverChat,
    ModelCatalog,
    SilenceWatch,
    price_reply,
)
from sa_home_bot.llm.strata_switch import SILENCE_CODE
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


async def gateway_server(
    status: int = 200, usage: dict | None = None, error_body: dict | None = None
) -> tuple[TestServer, list]:
    bodies: list = []

    async def handler(request):
        bodies.append(await request.json())
        if error_body is not None:
            return web.json_response(error_body)
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


async def test_catalog_filters_by_model_kind(env):
    for name, url, remote in (
        ("local-model", "http://l:8721", False),
        ("remote-model", "http://r:8721", True),
    ):
        token = await env.store.issue_token(name, protocol.ROLE_GATEWAY)
        principal = await env.store.authenticate(token)
        beat = {
            protocol.FIELD_MODELS: [
                {protocol.MODEL_NAME: name, protocol.MODEL_REMOTE: remote}
            ],
            protocol.FIELD_URL: url,
        }
        await env.store.heartbeat(principal, beat)
    hub = ExecutorHub(env.hub_url, env.executor_token)
    env.hubs.append(hub)

    catalog = ModelCatalog(hub)
    assert await catalog.names() == ["local-model", "remote-model"]
    assert await catalog.names("local") == ["local-model"]
    assert await catalog.names("remote") == ["remote-model"]


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
    assert alive_bodies[0]["messages"] == dead_bodies[0]["messages"] == with_flow_reminder(MESSAGES)
    assert second.content == "ок"


async def test_failover_switches_gateway_on_200_with_transient_error_body():
    sick, sick_bodies = await gateway_server(
        error_body={"error": {"message": "апстрим", "code": 502}}
    )
    alive, alive_bodies = await gateway_server()
    session = aiohttp.ClientSession()
    try:
        reply = await failover(
            [entry_at(sick, "jeeves"), entry_at(alive, "wooster")], session
        ).complete(MESSAGES, [])
    finally:
        await session.close()
        await sick.close()
        await alive.close()
    assert reply.content == "ок"
    assert len(sick_bodies) == 1 and len(alive_bodies) == 1


async def test_failover_retries_are_bounded_on_200_error_body():
    sick, bodies = await gateway_server(error_body={"error": {"message": "апстрим"}})
    session = aiohttp.ClientSession()
    try:
        with pytest.raises(ModelError, match="все шлюзы.*апстрим"):
            await failover([entry_at(sick, "a")], session).complete(MESSAGES, [])
    finally:
        await session.close()
        await sick.close()
    assert len(bodies) == 6


async def test_failover_exponential_backoff():
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    sick, _ = await gateway_server(error_body={"error": {"message": "апстрим"}})
    session = aiohttp.ClientSession()
    try:
        chat = FailoverChat(
            StaticCatalog(entry_at(sick, "a")),
            lambda entry: GatewayChat(entry.url, "tok", "m", "job", session=session),
            "m",
            rounds=6,
            retry_s=5.0,
            retry_max_s=30.0,
            sleep=record_sleep,
        )
        with pytest.raises(ModelError, match="все шлюзы.*апстрим"):
            await chat.complete(MESSAGES, [])
    finally:
        await session.close()
        await sick.close()

    assert sleeps == [5.0, 10.0, 20.0, 30.0, 30.0]


async def test_failover_non_transient_error_body_fails_at_once():
    bad, bad_bodies = await gateway_server(error_body={"error": {"message": "ключ", "code": 401}})
    spare, spare_bodies = await gateway_server()
    session = aiohttp.ClientSession()
    try:
        with pytest.raises(ModelError, match="ключ"):
            await failover([entry_at(bad, "a"), entry_at(spare, "b")], session).complete(
                MESSAGES, []
            )
    finally:
        await session.close()
        await bad.close()
        await spare.close()
    assert len(bad_bodies) == 1 and spare_bodies == []


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


async def test_failover_silence_waits_without_spending_rounds():
    sleeps: list[float] = []
    calls = 0

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls <= 3:
            return web.json_response({"error": "тишина", "code": SILENCE_CODE}, status=503)
        return web.json_response(reply_body("ок"))

    app = web.Application()
    app.router.add_post(protocol.HTTP_CHAT_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    session = aiohttp.ClientSession()
    try:
        chat = FailoverChat(
            StaticCatalog(entry_at(server, "a")),
            lambda entry: GatewayChat(entry.url, "tok", "m", "job", session=session),
            "m",
            rounds=2,
            silence_wait_s=60.0,
            sleep=record_sleep,
        )
        reply = await chat.complete(MESSAGES, [])
    finally:
        await session.close()
        await server.close()
    assert (reply.content, calls, sleeps) == ("ок", 4, [60.0, 60.0, 60.0])


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
    worker._models = lambda name, jid, style, reminder: model

    await env.mcp.call(protocol.TOOL_RESUME, id=job_id, text="учти кодировку")
    assert await worker.claim_once()
    await worker.poll_once()

    assert "учти кодировку" in model.seen[0][1]["content"]
    assert await env.status(job_id) == protocol.STATUS_DONE


def test_replies_counter_grows_per_record():
    saved = []
    spend = JobSpend(SpendState(), saved.append)
    spend.record(ModelReply("a", tokens_in=1, tokens_out=1))
    spend.record(ModelReply("b", tokens_in=0, tokens_out=0))
    assert spend.replies() == 2


@pytest.mark.parametrize(
    "initial_model,new_model,expected_extensions,expected_model",
    [
        ("", "model-a", 0, "model-a"),
        ("model-a", "model-a", 0, "model-a"),
        ("model-a", "model-b", 1, "model-b"),
    ],
)
def test_switch_model(initial_model, new_model, expected_extensions, expected_model):
    saved = []
    state = SpendState(model=initial_model)
    spend = JobSpend(state, saved.append)
    spend.switch_model(new_model)
    assert spend.extensions == expected_extensions
    assert spend._state.model == expected_model


def test_catalog_entry_reads_context_from_hub():
    entry = CatalogEntry.from_hub({protocol.MODEL_NAME: "m", protocol.MODEL_CONTEXT: 65536})
    bare = CatalogEntry.from_hub({protocol.MODEL_NAME: "m"})

    assert entry.context == 65536
    assert bare.context == 0


@pytest.mark.parametrize("raw", ["abc", [1], {"a": 1}, -5, True, None, "-7", float("inf")])
def test_catalog_entry_ignores_garbage_context(raw):
    entry = CatalogEntry.from_hub({protocol.MODEL_NAME: "m", protocol.MODEL_CONTEXT: raw})

    assert entry.context == 0


async def hanging_then_ok_server() -> tuple[TestServer, list[asyncio.Event]]:
    started: list[asyncio.Event] = []

    async def handler(request):
        await request.json()
        marker = asyncio.Event()
        started.append(marker)
        marker.set()
        if len(started) == 1:
            await asyncio.sleep(30)
        return web.json_response(reply_body("ок"))

    app = web.Application()
    app.router.add_post(protocol.HTTP_CHAT_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    return server, started


async def test_silence_cuts_local_request_without_spending_round():
    server, started = await hanging_then_ok_server()
    session = aiohttp.ClientSession()
    watch = SilenceWatch()
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    try:
        chat = FailoverChat(
            StaticCatalog(entry_at(server, "a", remote=False)),
            lambda entry: GatewayChat(entry.url, "tok", "m", "job", session=session),
            "m",
            rounds=1,
            sleep=record_sleep,
            silence=watch,
        )
        running = asyncio.create_task(chat.complete(MESSAGES, []))
        await asyncio.sleep(0.2)
        watch.update(True)
        reply = await asyncio.wait_for(running, 5.0)
    finally:
        await session.close()
        await server.close()

    assert reply.content == "ок"
    assert len(started) == 2
    assert len(sleeps) == 1


async def test_silence_does_not_touch_remote_request():
    server, started = await hanging_then_ok_server()
    session = aiohttp.ClientSession()
    watch = SilenceWatch()
    try:
        chat = FailoverChat(
            StaticCatalog(entry_at(server, "a", remote=True)),
            lambda entry: GatewayChat(entry.url, "tok", "m", "job", session=session),
            "m",
            rounds=1,
            sleep=noop_sleep,
            silence=watch,
        )
        running = asyncio.create_task(chat.complete(MESSAGES, []))
        await asyncio.sleep(0.2)
        watch.update(True)
        await asyncio.sleep(0.2)
        assert not running.done()
        assert len(started) == 1
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    finally:
        await session.close()
        await server.close()


async def test_connection_error_of_local_model_in_silence_is_silence_not_round():
    session = aiohttp.ClientSession()
    watch = SilenceWatch()
    watch.update(True)
    sleeps: list[float] = []

    async def stop_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    entry = CatalogEntry("m", "a", "http://127.0.0.1:1", remote=False)
    try:
        chat = FailoverChat(
            StaticCatalog(entry),
            lambda e: GatewayChat(e.url, "tok", "m", "job", session=session),
            "m",
            rounds=1,
            silence_wait_s=7.0,
            sleep=stop_sleep,
            silence=watch,
        )
        with pytest.raises(asyncio.CancelledError):
            await chat.complete(MESSAGES, [])
    finally:
        await session.close()

    assert sleeps == [7.0]


def test_parse_reply_reads_reasoning_tokens():
    body = reply_body("ок", usage={"completion_tokens_details": {"reasoning_tokens": 42}})
    assert parse_reply(body).tokens_reasoning == 42
    assert parse_reply(reply_body("ок", usage={"completion_tokens": 5})).tokens_reasoning is None


def test_job_spend_live_counts_total_segment_and_reasoning():
    spend = JobSpend(SpendState(base_tokens=100), lambda state: None)
    assert spend.live()[protocol.FIELD_USAGE_KNOWN] is True
    spend.record(ModelReply("", tokens_in=10, tokens_out=5, tokens_reasoning=3))
    spend.take_report()
    spend.record(ModelReply("", tokens_in=20, tokens_out=5))
    live = spend.live()
    assert live[protocol.FIELD_MODEL_STEPS] == 2
    assert live[protocol.PAYLOAD_TOKENS] == 140
    assert live[protocol.FIELD_SEGMENT_TOKENS] == 40
    assert live[protocol.FIELD_REASONING_TOKENS] == 3


def test_segment_tokens_ignore_base_and_keep_growing_through_parking():
    saved = []
    spend = JobSpend(SpendState(base_tokens=5_000_000), saved.append)
    assert spend.segment_tokens() == 0
    assert spend.total_tokens() == 5_000_000
    spend.record(ModelReply("", tokens_in=10, tokens_out=5))
    spend.extend()
    resumed = JobSpend(saved[-1], saved.append)
    resumed.record(ModelReply("", tokens_in=20, tokens_out=5))
    assert resumed.segment_tokens() == 40
    assert resumed.extensions == 1


def test_job_spend_live_reports_context_window_and_folds():
    spend = JobSpend(SpendState(), lambda state: None)
    assert protocol.FIELD_CONTEXT_TOKENS not in spend.live()
    spend.set_context_limit(118000)
    spend.record(ModelReply("", tokens_in=30000, tokens_out=5))
    spend.note_fold()
    spend.record(ModelReply("", tokens_in=46000, tokens_out=5))
    spend.record(ModelReply(""))
    spend.note_fold()
    live = spend.live()
    assert live[protocol.FIELD_CONTEXT_TOKENS] == 46000
    assert live[protocol.FIELD_CONTEXT_LIMIT] == 118000
    assert live[protocol.FIELD_FOLDS] == 2


def test_job_spend_live_without_limit_has_no_context_limit():
    spend = JobSpend(SpendState(), lambda state: None)
    spend.record(ModelReply("", tokens_in=10, tokens_out=5))
    live = spend.live()
    assert live[protocol.FIELD_FOLDS] == 0
    assert protocol.FIELD_CONTEXT_LIMIT not in live


def test_job_spend_live_without_usage_is_unknown_and_has_no_reasoning():
    spend = JobSpend(SpendState(), lambda state: None)
    spend.record(ModelReply(""))
    live = spend.live()
    assert live[protocol.FIELD_USAGE_KNOWN] is False
    assert protocol.FIELD_REASONING_TOKENS not in live
