from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import app as hub_app
from sa_home_bot.devlab.hub import executor_api, http
from sa_home_bot.devlab.hub import store as store_module
from sa_home_bot.devlab.hub.store import HubStore
from sa_home_bot.utils.clock import utc_now

MODEL = "qwen"
PROJECT = "proj"
API_PATHS = [
    protocol.HTTP_CLAIM_PATH,
    protocol.HTTP_POST_PATH,
    protocol.HTTP_POLL_PATH,
    protocol.HTTP_HEARTBEAT_PATH,
]


@pytest.fixture
async def store(tmp_path):
    hub_store = HubStore(tmp_path)
    await hub_store.open()
    await hub_store.queue.add_project(PROJECT)
    yield hub_store
    await hub_store.close()


@pytest.fixture
async def client(store):
    app = http.build_app(store)
    executor_api.add_routes(app)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def token_for(store, name, role):
    return {"Authorization": f"Bearer {await store.issue_token(name, role)}"}


@pytest.fixture
async def executor(store):
    return await token_for(store, "pc", protocol.ROLE_EXECUTOR)


@pytest.fixture
async def gateway(store):
    return await token_for(store, "gw", protocol.ROLE_GATEWAY)


async def call(client, path, headers, body=None):
    resp = await client.post(path, json=body or {}, headers=headers)
    return resp.status, await resp.json()


async def claim_job(client, store, executor):
    job = await store.queue.submit("me", PROJECT, MODEL, "task")
    _, body = await call(
        client, protocol.HTTP_CLAIM_PATH, executor, {protocol.FIELD_MODELS: [MODEL]}
    )
    return job, body


async def test_claim_gives_job_and_runs_it(client, store, executor):
    job, body = await claim_job(client, store, executor)
    assert body[protocol.FIELD_JOB]["id"] == job.id
    stored = await store.queue.get_job(job.id)
    assert (stored.status, stored.executor) == (protocol.STATUS_RUNNING, "pc")


@pytest.mark.parametrize("models", [[], ["other"]])
async def test_claim_without_fitting_models_is_empty(client, store, executor, models):
    job = await store.queue.submit("me", PROJECT, MODEL, "task")
    status, body = await call(
        client, protocol.HTTP_CLAIM_PATH, executor, {protocol.FIELD_MODELS: models}
    )
    assert (status, body[protocol.FIELD_JOB]) == (200, None)
    assert (await store.queue.get_job(job.id)).status == protocol.STATUS_QUEUED


async def test_post_moves_status_and_counts_spend(client, store, executor):
    job, _ = await claim_job(client, store, executor)
    body = {
        protocol.FIELD_JOB_ID: job.id,
        protocol.FIELD_KIND: protocol.MSG_QUESTION,
        protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_TOKENS: 7},
    }
    status, _ = await call(client, protocol.HTTP_POST_PATH, executor, body)
    assert status == 200
    stored = await store.queue.get_job(job.id)
    assert (stored.status, stored.tokens) == (protocol.STATUS_WAITING_OWNER, 7)


@pytest.mark.parametrize(
    "body",
    [
        {protocol.FIELD_JOB_ID: "nope", protocol.FIELD_KIND: protocol.MSG_STEP_DONE},
        {protocol.FIELD_KIND: protocol.MSG_STEP_DONE},
        {protocol.FIELD_JOB_ID: "x", protocol.FIELD_KIND: protocol.MSG_ANSWER},
    ],
)
async def test_invalid_post_is_rejected(client, executor, body):
    status, answer = await call(client, protocol.HTTP_POST_PATH, executor, body)
    assert status == 400
    assert answer[protocol.FIELD_ERROR]


async def test_post_illegal_transition_is_rejected(client, store, executor):
    job, _ = await claim_job(client, store, executor)
    body = {protocol.FIELD_JOB_ID: job.id, protocol.FIELD_KIND: protocol.MSG_DONE}
    await call(client, protocol.HTTP_POST_PATH, executor, body)
    status, answer = await call(client, protocol.HTTP_POST_PATH, executor, body)
    assert status == 400
    assert protocol.STATUS_DONE in answer[protocol.FIELD_ERROR]


async def test_bad_json_is_rejected(client, executor):
    resp = await client.post(protocol.HTTP_CLAIM_PATH, data="[", headers=executor)
    assert resp.status == 400


async def test_poll_sees_answer_and_stop_and_cursor_advances(client, store, executor):
    job, _ = await claim_job(client, store, executor)
    question = {protocol.FIELD_JOB_ID: job.id, protocol.FIELD_KIND: protocol.MSG_QUESTION}
    await call(client, protocol.HTTP_POST_PATH, executor, question)
    await store.queue.post_from_client("me", job.id, protocol.MSG_ANSWER, {"text": "да"})
    _, first = await call(client, protocol.HTTP_POLL_PATH, executor, {protocol.FIELD_SINCE: 0})
    assert [m["kind"] for m in first[protocol.FIELD_MESSAGES]] == [protocol.MSG_ANSWER]
    await store.queue.post_from_client("me", job.id, protocol.MSG_STOP)
    _, second = await call(
        client,
        protocol.HTTP_POLL_PATH,
        executor,
        {protocol.FIELD_SINCE: first[protocol.FIELD_CURSOR]},
    )
    assert [m["kind"] for m in second[protocol.FIELD_MESSAGES]] == [protocol.MSG_STOP]
    _, third = await call(
        client,
        protocol.HTTP_POLL_PATH,
        executor,
        {protocol.FIELD_SINCE: second[protocol.FIELD_CURSOR]},
    )
    assert third[protocol.FIELD_MESSAGES] == []


async def test_poll_ignores_other_executors_jobs(client, store, executor):
    other = await token_for(store, "pc2", protocol.ROLE_EXECUTOR)
    job = await store.queue.submit("me", PROJECT, MODEL, "task")
    await call(client, protocol.HTTP_CLAIM_PATH, other, {protocol.FIELD_MODELS: [MODEL]})
    await store.queue.post_from_client("me", job.id, protocol.MSG_STOP)
    _, body = await call(client, protocol.HTTP_POLL_PATH, executor)
    assert body[protocol.FIELD_MESSAGES] == []


def gateway_beat(*names):
    return {
        protocol.FIELD_MODELS: [{protocol.MODEL_NAME: name, "free": True} for name in names],
        protocol.FIELD_LIMITS: {"day_left": 5},
    }


async def test_gateway_heartbeat_feeds_catalog_and_stale_drops(client, store, gateway, monkeypatch):
    status, _ = await call(client, protocol.HTTP_HEARTBEAT_PATH, gateway, gateway_beat("a", "b"))
    assert status == 200
    catalog = await store.models()
    assert [m[protocol.MODEL_NAME] for m in catalog] == ["a", "b"]
    assert catalog[0][protocol.MODEL_GATEWAY] == "gw"
    assert catalog[0][protocol.FIELD_LIMITS] == {"day_left": 5}
    later = utc_now() + timedelta(seconds=store_module.HEARTBEAT_FRESH_S + 1)
    monkeypatch.setattr(store_module, "utc_now", lambda: later)
    assert await store.models() == []


async def test_new_gateway_heartbeat_replaces_old(client, store, gateway):
    await call(client, protocol.HTTP_HEARTBEAT_PATH, gateway, gateway_beat("a"))
    await call(client, protocol.HTTP_HEARTBEAT_PATH, gateway, gateway_beat("b"))
    assert [m[protocol.MODEL_NAME] for m in await store.models()] == ["b"]


async def test_executor_heartbeat_not_in_catalog(client, store, executor):
    body = {protocol.FIELD_BUSY: True, protocol.FIELD_JOBS: ["x"]}
    status, _ = await call(client, protocol.HTTP_HEARTBEAT_PATH, executor, body)
    assert status == 200
    assert await store.models() == []


@pytest.mark.parametrize("body", [{}, {"models": "x"}, {"models": [{"free": True}]}])
async def test_bad_gateway_heartbeat_is_rejected(client, gateway, body):
    status, _ = await call(client, protocol.HTTP_HEARTBEAT_PATH, gateway, body)
    assert status == 400


@pytest.mark.parametrize("path", API_PATHS)
async def test_client_role_gets_403_and_no_token_401(client, store, path):
    headers = await token_for(store, "cl", protocol.ROLE_CLIENT)
    assert (await client.post(path, json={}, headers=headers)).status == 403
    assert (await client.post(path, json={})).status == 401


@pytest.mark.parametrize("path", API_PATHS[:3])
async def test_gateway_role_gets_403_on_executor_paths(client, gateway, path):
    assert (await client.post(path, json={}, headers=gateway)).status == 403


async def test_purge_loop_calls_purge_repeatedly():
    calls = []

    class FakeQueue:
        async def purge(self, retention_days):
            calls.append(retention_days)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return 0

    task = asyncio.create_task(hub_app.purge_loop(FakeQueue(), 9, interval_s=0))
    while len(calls) < 3:
        await asyncio.sleep(0)
    task.cancel()
    assert calls[:3] == [9, 9, 9]
