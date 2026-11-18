from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.executor.silence_relay import SilenceRelay
from sa_home_bot.llm.strata_switch import SILENCE_FIELD, SILENCE_PATH


async def start_switch(bodies: list, status: int = 200) -> TestServer:
    async def silence(request):
        bodies.append(await request.json())
        return web.json_response({}, status=status)

    app = web.Application()
    app.router.add_post(SILENCE_PATH, silence)
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.mark.parametrize("value", [{protocol.FIELD_SILENCE_UNTIL: None}, None])
async def test_push_sends_silence_body(value):
    bodies: list = []
    server = await start_switch(bodies)
    relay = SilenceRelay(str(server.make_url("")))
    try:
        await relay.push({protocol.FIELD_SILENCE: value})
    finally:
        await relay.close()
        await server.close()
    assert bodies == [{SILENCE_FIELD: value}]


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
