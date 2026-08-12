"""call_with_network_retry: bounded retry для сетевых вызовов Telegram Bot API."""

from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from sa_home_bot.bot.telegram_retry import call_with_network_retry


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


async def test_succeeds_without_retry_when_the_first_call_works():
    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        return "ok"

    result = await call_with_network_retry(call, what="probe")
    assert result == "ok"
    assert calls["n"] == 1


async def test_succeeds_after_transient_network_errors():
    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TelegramNetworkError(method=None, message="timeout")
        return "ok"

    result = await call_with_network_retry(call, what="probe", attempts=3, delay_s=0.0)
    assert result == "ok"
    assert calls["n"] == 3


async def test_non_network_error_is_not_retried():
    """bad token / forbidden / not found — не транзиентные, ретраить
    бессмысленно, должны пробрасываться сразу без единой лишней попытки."""
    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        raise RuntimeError("bad token")

    with pytest.raises(RuntimeError):
        await call_with_network_retry(call, what="probe", attempts=3, delay_s=0.0)
    assert calls["n"] == 1


async def test_exhausting_attempts_raises_the_last_exception():
    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        raise TelegramNetworkError(method=None, message="timeout")

    with pytest.raises(TelegramNetworkError):
        await call_with_network_retry(call, what="probe", attempts=3, delay_s=0.0)
    assert calls["n"] == 3


async def test_retry_after_waits_the_requested_time_then_succeeds():
    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TelegramRetryAfter(method=None, message="flood", retry_after=1)
        return "ok"

    result = await call_with_network_retry(call, what="probe", attempts=2)
    assert result == "ok"
    assert calls["n"] == 2
