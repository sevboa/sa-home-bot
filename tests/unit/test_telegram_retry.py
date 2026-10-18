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


class FlakyCall:
    def __init__(self, error: Exception | None = None, failures: int = 0) -> None:
        self.error = error
        self.failures = failures
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.error is not None and self.calls <= self.failures:
            raise self.error
        return "ok"


def _network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=None, message="timeout")


async def test_succeeds_without_retry_when_the_first_call_works():
    call = FlakyCall()
    assert await call_with_network_retry(call, what="probe") == "ok"
    assert call.calls == 1


async def test_succeeds_after_transient_network_errors():
    call = FlakyCall(_network_error(), failures=2)
    assert await call_with_network_retry(call, what="probe", attempts=3, delay_s=0.0) == "ok"
    assert call.calls == 3


async def test_non_network_error_is_not_retried():
    call = FlakyCall(RuntimeError("bad token"), failures=99)
    with pytest.raises(RuntimeError):
        await call_with_network_retry(call, what="probe", attempts=3, delay_s=0.0)
    assert call.calls == 1


async def test_exhausting_attempts_raises_the_last_exception():
    call = FlakyCall(_network_error(), failures=99)
    with pytest.raises(TelegramNetworkError):
        await call_with_network_retry(call, what="probe", attempts=3, delay_s=0.0)
    assert call.calls == 3


async def test_retry_after_waits_the_requested_time_then_succeeds():
    call = FlakyCall(TelegramRetryAfter(method=None, message="flood", retry_after=1), failures=1)
    assert await call_with_network_retry(call, what="probe", attempts=2) == "ok"
    assert call.calls == 2
