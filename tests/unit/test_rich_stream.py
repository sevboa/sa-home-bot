"""bot/rich_stream.py: RichStreamSession (этап 34, Фаза 2 — Rich-стрим
ответов Альфреда). notify_admins/Notifier плейн-путь не трогается здесь —
см. test_ai_handler.py."""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from sa_home_bot.bot import notifier as notifier_module
from sa_home_bot.bot.rich_stream import ALFRED_PREFIX_MD, RichStreamSession


class FakeSentMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeBot:
    def __init__(self) -> None:
        self.drafts: list[dict] = []
        self.sent: list[dict] = []
        self.draft_fails_times = 0
        self.retry_after_times = 0
        self._next_id = 1

    async def send_rich_message_draft(
        self, *, chat_id, draft_id, rich_message, message_thread_id=None
    ):
        if self.draft_fails_times > 0:
            self.draft_fails_times -= 1
            raise TelegramAPIError(None, "draft недоступен")
        self.drafts.append(
            {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "markdown": rich_message.markdown,
                "message_thread_id": message_thread_id,
            }
        )
        return True

    async def send_rich_message(
        self, *, chat_id, rich_message, reply_parameters=None, message_thread_id=None
    ):
        if self.retry_after_times > 0:
            self.retry_after_times -= 1
            raise TelegramRetryAfter(None, "flood", retry_after=0)
        self.sent.append(
            {
                "chat_id": chat_id,
                "markdown": rich_message.markdown,
                "reply_parameters": reply_parameters,
                "message_thread_id": message_thread_id,
            }
        )
        msg = FakeSentMessage(self._next_id)
        self._next_id += 1
        return msg


@pytest.fixture(autouse=True)
def fast_retry_sleep(monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(notifier_module.asyncio, "sleep", _no_sleep)


async def test_on_partial_sends_draft_with_prefix():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=123, message_thread_id=7)

    await session.on_partial("Добрый", done=False)

    assert len(bot.drafts) == 1
    assert bot.drafts[0]["chat_id"] == 123
    assert bot.drafts[0]["message_thread_id"] == 7
    assert bot.drafts[0]["markdown"] == ALFRED_PREFIX_MD + "Добрый"
    assert bot.drafts[0]["draft_id"] == session._draft_id


async def test_on_partial_skips_unchanged_and_empty_text():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.on_partial("", done=False)  # пусто — пропуск
    await session.on_partial("текст", done=False)
    await session.on_partial("текст", done=False)  # не изменилось — пропуск
    await session.on_partial("текст ещё", done=True)  # done игнорируется здесь

    assert [d["markdown"] for d in bot.drafts] == [
        ALFRED_PREFIX_MD + "текст",
        ALFRED_PREFIX_MD + "текст ещё",
    ]


async def test_on_partial_swallows_telegram_errors():
    bot = FakeBot()
    bot.draft_fails_times = 1
    session = RichStreamSession(bot, chat_id=1)

    await session.on_partial("текст", done=False)  # не бросает

    assert bot.drafts == []


async def test_push_status_wraps_in_italics_without_alfred_prefix():
    # Живой баг 2026-08-10: фраза сама называет Альфреда в третьем лице —
    # ALFRED_PREFIX_MD дублировал бы имя ("Альфред: Альфред сёрфит...").
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("Альфред проверяет погоду")

    assert bot.drafts[0]["markdown"] == "_Альфред проверяет погоду_"


async def test_push_status_dedups_consecutive_identical_status():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("Альфред думает")
    await session.push_status("Альфред думает")  # не изменилось — пропуск
    await session.push_status("Альфред сёрфит")

    assert [d["markdown"] for d in bot.drafts] == [
        "_Альфред думает_",
        "_Альфред сёрфит_",
    ]


async def test_push_status_and_on_partial_share_dedup_state():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("текст")  # обёрнуто в _текст_, без префикса
    await session.on_partial("текст", done=False)  # другая обёртка — не дедупится

    assert [d["markdown"] for d in bot.drafts] == [
        "_текст_",
        ALFRED_PREFIX_MD + "текст",
    ]


async def test_push_status_swallows_telegram_errors():
    bot = FakeBot()
    bot.draft_fails_times = 1
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("статус")  # не бросает

    assert bot.drafts == []


async def test_two_sessions_get_different_draft_ids():
    bot = FakeBot()
    a = RichStreamSession(bot, chat_id=1)
    b = RichStreamSession(bot, chat_id=1)
    assert a._draft_id != b._draft_id
    assert a._draft_id != 0
    assert b._draft_id != 0


async def test_finalize_sends_rich_message_with_reply_and_prefix():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=42, message_thread_id=9)

    sent = await session.finalize("  ответ  ", reply_to_message_id=100)

    assert sent is not None
    assert bot.sent[0]["chat_id"] == 42
    assert bot.sent[0]["message_thread_id"] == 9
    assert bot.sent[0]["markdown"] == ALFRED_PREFIX_MD + "ответ"  # .strip()
    assert bot.sent[0]["reply_parameters"].message_id == 100


async def test_finalize_without_reply_to_sends_no_reply_parameters():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.finalize("ответ")

    assert bot.sent[0]["reply_parameters"] is None


async def test_finalize_retries_on_429():
    bot = FakeBot()
    bot.retry_after_times = 1
    session = RichStreamSession(bot, chat_id=1)

    sent = await session.finalize("ответ")

    assert sent is not None
    assert len(bot.sent) == 1  # одна успешная попытка после ретрая


async def test_finalize_gives_up_after_exhausting_retries():
    bot = FakeBot()
    bot.retry_after_times = notifier_module.MAX_RETRIES  # больше, чем попыток
    session = RichStreamSession(bot, chat_id=1)

    sent = await session.finalize("ответ")

    assert sent is None
    assert bot.sent == []
