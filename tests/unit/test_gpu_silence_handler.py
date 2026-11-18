from __future__ import annotations

import re
from datetime import timedelta

import pytest
from aiogram.filters import CommandObject

from sa_home_bot.bot.away import MAX_AWAY, AwayService
from sa_home_bot.bot.handlers import gpu_silence as gpu_silence_handler
from sa_home_bot.bot.middlewares import DENIED_TEXT
from sa_home_bot.silence import SilenceService
from sa_home_bot.subscriptions.models import Subscription
from tests.unit.test_away import _OWNER_SUB, T0, Clock, FakeMessage, _settings

DENIED_SUB = Subscription(chat_id=2, name="guest", allowed_commands=frozenset({"chat@llm"}))
SILENT_SUB = Subscription(
    chat_id=3, name="guest", allowed_commands=frozenset({"chat@llm", "silent"})
)


class Runner:
    def __init__(self) -> None:
        self.kicks = 0

    def kick(self) -> None:
        self.kicks += 1


async def call(
    args: str,
    silence: SilenceService,
    away: AwayService,
    away_runner: Runner,
    subscription: Subscription = _OWNER_SUB,
) -> FakeMessage:
    msg = FakeMessage(1, f"/silent {args}".strip())
    command = CommandObject(prefix="/", command="silent", args=args or None)
    await gpu_silence_handler.cmd_silent(
        msg,
        command,
        silence=silence,
        away=away,
        away_runner=away_runner,
        subscription=subscription,
    )
    return msg


def _kit(store, tmp_path):
    cl = Clock()
    return SilenceService(store, cl), AwayService(store, _settings(tmp_path), clock=cl), Runner()


@pytest.mark.parametrize(
    ("pre", "args", "expected"),
    [
        ("", "", r"🔇 Тишина видеокарты до /silent off\. Альфред в городе на это время\."),
        ("",
         "30",
         r"🔇 Тишина видеокарты до \d{2}\.\d{2} \d{2}:\d{2}\."
         r" Альфред в городе на это время\."),
        ("30", "status", r"🔇 Тишина видеокарты до \d{2}\.\d{2} \d{2}:\d{2}\."),
        ("30", "off", r"🔈 Тишина снята\. Альфред вернулся в замок\. Ждать ответа некому\."),
        ("", "скоро", r"Не понял\. /silent — до /silent off, /silent 30 — на 30 минут, "
         r"/silent off — снять, /silent status — состояние\."),
    ],
    ids=["infinite", "minutes", "status", "off", "unknown"],
)
async def test_cmd_silent_args_and_answers(store, tmp_path, pre, args, expected):
    silence, away, runner = _kit(store, tmp_path)

    if pre:
        first = await call(pre, silence, away, runner)
        assert re.match(
            r"🔇 Тишина видеокарты до \d{2}\.\d{2} \d{2}:\d{2}\. Альфред в городе на это время\.",
            first.sent[-1],
        )

    msg = await call(args, silence, away, runner)
    assert len(msg.sent) == 1
    assert re.fullmatch(expected, msg.sent[-1])


async def test_cmd_silent_infinite_sends_alfred_to_city(store, tmp_path):
    silence, away, _ = _kit(store, tmp_path)
    await call("", silence, away, Runner())
    state = await away.load()
    assert state is not None
    assert state.reason == "тишина видеокарты"
    assert state.until == T0 + MAX_AWAY
    silence_state = await silence.load()
    assert silence_state is not None and silence_state.until is None


async def test_cmd_silent_minutes_matches_silence_and_away(store, tmp_path):
    silence, away, _ = _kit(store, tmp_path)
    await call("30", silence, away, Runner())
    state = await away.load()
    silence_state = await silence.load()
    assert state is not None and silence_state is not None
    assert state.until == silence_state.until == T0 + timedelta(minutes=30)


async def test_cmd_silent_off_returns_alfred(store, tmp_path):
    silence, away, _ = _kit(store, tmp_path)
    await call("30", silence, away, Runner())
    msg = await call("off", silence, away, Runner())
    assert "🔈 Тишина снята." in msg.sent[-1]
    assert await away.load() is None
    assert await silence.load() is None


async def test_cmd_silent_denied_for_guest_without_right(store, tmp_path):
    silence, away, _ = _kit(store, tmp_path)
    msg = await call("30", silence, away, Runner(), subscription=DENIED_SUB)
    assert msg.sent[-1] == DENIED_TEXT
    assert await silence.load() is None
    assert await away.load() is None


async def test_cmd_silent_allowed_for_guest_with_right(store, tmp_path):
    silence, away, _ = _kit(store, tmp_path)
    msg = await call("30", silence, away, Runner(), subscription=SILENT_SUB)
    assert "🔇 Тишина видеокарты" in msg.sent[-1]
    assert await silence.load() is not None
    assert await away.load() is not None
