"""bot/notifier.py: TypingIndicator и Notifier — ретраи/устойчивость к сбоям.

Живая находка 2026-08-29: реальный сбой в проде (зависший SOCKS-прокси до
Telegram, aiohttp_socks.ProxyTimeoutError) — обычный Exception без общего
предка с TelegramAPIError. Все методы ниже задуманы best-effort/с ретраем
именно на такие сбои связи (докстринги "не критично"/"ретраи 429"), но раньше
ловили только TelegramAPIError и роняли вызывающего на первом же сетевом
таймауте. См. тот же класс бага в test_rich_stream.py."""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from sa_home_bot.bot import notifier as notifier_module
from sa_home_bot.bot.notifier import MAX_RETRIES, Notifier, TypingIndicator


class FakeBot:
    def __init__(self) -> None:
        self.typing_calls = 0
        self.sent: list[dict] = []
        self.deleted: list[tuple[int, int]] = []
        self.fail_times = 0
        self.fail_exception: Exception = ConnectionError("proxy timed out: 60")
        self.send_message_calls = 0

    async def send_chat_action(self, chat_id, action, message_thread_id=None) -> None:
        self.typing_calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_exception

    async def send_message(self, chat_id, text, reply_parameters=None, reply_markup=None,
                            message_thread_id=None):
        self.send_message_calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_exception

        class _Msg:
            message_id = 1

        self.sent.append({"chat_id": chat_id, "text": text})
        return _Msg()

    async def delete_message(self, chat_id, message_id) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_exception
        self.deleted.append((chat_id, message_id))


@pytest.fixture(autouse=True)
def fast_retry_sleep(monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(notifier_module.asyncio, "sleep", _no_sleep)


async def test_typing_indicator_start_swallows_network_errors():
    bot = FakeBot()
    bot.fail_times = 1
    indicator = TypingIndicator(bot, chat_id=1)

    await indicator.start()  # не бросает — see docstring "не критично"
    await indicator.stop()

    assert bot.typing_calls == 1


async def test_send_direct_retries_on_429_then_succeeds():
    bot = FakeBot()
    bot.fail_exception = TelegramRetryAfter(None, "flood", retry_after=0)
    bot.fail_times = 1
    notifier = Notifier(bot)

    message_id = await notifier.send_direct(1, "привет")

    assert message_id == 1
    assert bot.sent == [{"chat_id": 1, "text": "привет"}]


async def test_send_direct_retries_transient_network_error_then_succeeds():
    # Живая находка 2026-08-30: раньше первый же не-429 сбой (моргнувший
    # SOCKS-прокси до Telegram) давал return None без повтора — на пути
    # финальной отправки ответа это молча его теряло. Теперь транзиентный
    # сбой ретраится, как и 429.
    bot = FakeBot()
    bot.fail_exception = ConnectionError("proxy timed out: 60")
    bot.fail_times = 2  # два обрыва, третья попытка проходит
    notifier = Notifier(bot)

    message_id = await notifier.send_direct(1, "привет")

    assert message_id == 1
    assert bot.sent == [{"chat_id": 1, "text": "привет"}]
    assert bot.send_message_calls == 3


async def test_send_direct_swallows_network_error_and_returns_none():
    bot = FakeBot()
    bot.fail_times = MAX_RETRIES  # ни одна попытка не пройдёт
    notifier = Notifier(bot)

    message_id = await notifier.send_direct(1, "привет")  # не бросает

    assert message_id is None
    assert bot.sent == []
    assert bot.send_message_calls == MAX_RETRIES  # все попытки израсходованы


async def test_send_direct_does_not_retry_permanent_bad_request():
    # TelegramBadRequest (битая разметка / слишком длинно) повтором того же
    # payload не лечится — сдаёмся сразу, попытки не тратим.
    bot = FakeBot()
    bot.fail_exception = TelegramBadRequest(method=None, message="can't parse entities")
    bot.fail_times = 1
    notifier = Notifier(bot)

    message_id = await notifier.send_direct(1, "<b>кривой")

    assert message_id is None
    assert bot.send_message_calls == 1


async def test_delete_message_swallows_network_errors():
    bot = FakeBot()
    bot.fail_times = 1
    notifier = Notifier(bot)

    await notifier.delete_message(1, 42)  # не бросает

    assert bot.deleted == []
