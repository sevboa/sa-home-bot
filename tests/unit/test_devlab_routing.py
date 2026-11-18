from __future__ import annotations

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.executor.hub import ExecutorHub
from sa_home_bot.devlab.executor.routing import CatalogEntry, ModelCatalog
from tests.unit import test_devlab_executor as executor_tests

env = executor_tests.env


@pytest.mark.parametrize(
    "limits,exhausted",
    [
        (None, False),
        ({}, False),
        ({protocol.LIMIT_RPD_LEFT: 0}, True),
        ({protocol.LIMIT_PROVIDER_LEFT: 0}, True),
        ({protocol.LIMIT_RPD_LEFT: None, protocol.LIMIT_PROVIDER_LEFT: None}, False),
        ({protocol.LIMIT_RPD_LEFT: 5}, False),
    ],
)
def test_catalog_entry_reads_exhausted_from_hub(limits, exhausted):
    data = {protocol.MODEL_NAME: "m"}
    if limits is not None:
        data[protocol.FIELD_LIMITS] = {data[protocol.MODEL_NAME]: limits}
    entry = CatalogEntry.from_hub(data)

    assert entry.exhausted is exhausted


async def test_catalog_names_skip_exhausted_gateway(env):
    model = "glm"
    for name, url, limits in (
        ("wooster", "http://w:8721", {model: {protocol.LIMIT_RPD_LEFT: 0}}),
        ("jeeves", "http://j:8721", None),
    ):
        token = await env.store.issue_token(name, protocol.ROLE_GATEWAY)
        principal = await env.store.authenticate(token)
        beat = {
            protocol.FIELD_MODELS: [{protocol.MODEL_NAME: model, protocol.MODEL_REMOTE: True}],
            protocol.FIELD_URL: url,
        }
        if limits is not None:
            beat[protocol.FIELD_LIMITS] = limits
        await env.store.heartbeat(principal, beat)
    hub = ExecutorHub(env.hub_url, env.executor_token)
    env.hubs.append(hub)

    assert await ModelCatalog(hub).names() == [model]


async def test_catalog_names_exhausted_model_hidden(env):
    model = "glm"
    for name, url in (("wooster", "http://w:8721"), ("jeeves", "http://j:8721")):
        token = await env.store.issue_token(name, protocol.ROLE_GATEWAY)
        principal = await env.store.authenticate(token)
        beat = {
            protocol.FIELD_MODELS: [{protocol.MODEL_NAME: model, protocol.MODEL_REMOTE: True}],
            protocol.FIELD_URL: url,
            protocol.FIELD_LIMITS: {model: {protocol.LIMIT_RPD_LEFT: 0}},
        }
        await env.store.heartbeat(principal, beat)
    hub = ExecutorHub(env.hub_url, env.executor_token)
    env.hubs.append(hub)

    assert await ModelCatalog(hub).names() == []
