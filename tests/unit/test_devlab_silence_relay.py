from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.executor.silence_relay import SilenceRelay
from sa_home_bot.llm.strata_switch import MAIN_DEMAND_FIELD, SILENCE_FIELD, SILENCE_PATH


async def start_switch(bodies: list, status: int = 200) -> TestServer:
    async def silence(request):
        bodies.append(await request.json())
        return web.json_response({}, status=status)

    app = web.Application()
    app.router.add_post(SILENCE_PATH, silence)
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            {protocol.FIELD_SILENCE: {protocol.FIELD_SILENCE_UNTIL: None}},
            {SILENCE_FIELD: {protocol.FIELD_SILENCE_UNTIL: None}},
        ),
        ({protocol.FIELD_SILENCE: None}, {SILENCE_FIELD: None}),
        (
            {protocol.FIELD_SILENCE: None, protocol.FIELD_LOCAL_DEMAND: True},
            {SILENCE_FIELD: None, MAIN_DEMAND_FIELD: True},
        ),
        (
            {protocol.FIELD_SILENCE: None, protocol.FIELD_LOCAL_DEMAND: False},
            {SILENCE_FIELD: None, MAIN_DEMAND_FIELD: False},
        ),
    ],
    ids=["тишина", "тишина null", "спрос True", "спрос False"],
)
async def test_push_sends_silence_body(reply, expected):
    bodies: list = []
    server = await start_switch(bodies)
    relay = SilenceRelay(str(server.make_url("")))
    try:
        await relay.push(reply)
    finally:
        await relay.close()
        await server.close()
    assert bodies == [expected]


async def test_push_skips_reply_without_silence():
    bodies: list = []
    server = await start_switch(bodies)
    relay = SilenceRelay(str(server.make_url("")))
    try:
        await relay.push({protocol.FIELD_QUEUED: 0})
    finally:
        await relay.close()
        await server.close()
    assert bodies == []


async def test_push_logs_demand_change(caplog):
    caplog.set_level("INFO")
    bodies: list = []
    server = await start_switch(bodies)
    relay = SilenceRelay(str(server.make_url("")))
    try:
        await relay.push({protocol.FIELD_SILENCE: None})
        await relay.push({protocol.FIELD_SILENCE: None, protocol.FIELD_LOCAL_DEMAND: True})
        await relay.push({protocol.FIELD_SILENCE: None, protocol.FIELD_LOCAL_DEMAND: True})
    finally:
        await relay.close()
        await server.close()
    infos = [
        record.message
        for record in caplog.records
        if record.levelname == "INFO" and record.name == "sa_home_bot.devlab.executor.silence_relay"
    ]
    assert len(infos) == 2
    assert infos[0] == "Тишина видеокарты передана strata-switch: None, спрос None"
    assert infos[1] == "Тишина видеокарты передана strata-switch: None, спрос True"


async def test_push_swallows_bad_status(caplog):
    bodies: list = []
    server = await start_switch(bodies, status=500)
    relay = SilenceRelay(str(server.make_url("")))
    try:
        await relay.push({protocol.FIELD_SILENCE: None})
        await relay.push({protocol.FIELD_SILENCE: None})
    finally:
        await relay.close()
        await server.close()
    warnings = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert warnings == ["strata-switch не принял тишину: HTTP 500"]


async def test_push_swallows_unreachable_switch(caplog):
    relay = SilenceRelay("http://127.0.0.1:1")
    try:
        await relay.push({protocol.FIELD_SILENCE: None})
        await relay.push({protocol.FIELD_SILENCE: None})
    finally:
        await relay.close()
    warnings = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1 and warnings[0].startswith("strata-switch не принял тишину: ")
