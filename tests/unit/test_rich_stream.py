"""bot/rich_stream.py: RichStreamSession (этап 34, Фаза 2 — Rich-стрим
ответов Альфреда). notify_admins/Notifier плейн-путь не трогается здесь —
см. test_ai_handler.py."""

from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InputRichBlockThinking

from sa_home_bot.bot import notifier as notifier_module
from sa_home_bot.bot import rich_stream as rich_stream_module
from sa_home_bot.bot.rich_stream import ALFRED_PREFIX_MD, RichStreamSession


class FakeSentMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeBot:
    def __init__(self) -> None:
        self.drafts: list[dict] = []
        self.sent: list[dict] = []
        self.draft_fails_times = 0
        self.draft_fail_exception: Exception = TelegramAPIError(None, "draft недоступен")
        self.retry_after_times = 0
        self._next_id = 1
        self.typing_actions: list[int] = []

    async def send_rich_message_draft(
        self, *, chat_id, draft_id, rich_message, message_thread_id=None
    ):
        if self.draft_fails_times > 0:
            self.draft_fails_times -= 1
            raise self.draft_fail_exception
        self.drafts.append(
            {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "markdown": rich_message.markdown,
                "blocks": rich_message.blocks,
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

    async def send_chat_action(self, chat_id, action, message_thread_id=None) -> None:
        self.typing_actions.append(chat_id)


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


async def test_on_partial_swallows_network_errors():
    # Живая находка 2026-08-29: реальный сбой в проде был не TelegramAPIError,
    # а aiohttp_socks.ProxyTimeoutError (зависший SOCKS-прокси до Telegram) —
    # обычный Exception без общего предка с TelegramAPIError. Раньше он
    # улетал наверх и ронял весь /ai-ответ ещё до обращения к модели.
    bot = FakeBot()
    bot.draft_fails_times = 1
    bot.draft_fail_exception = ConnectionError("proxy timed out: 60")
    session = RichStreamSession(bot, chat_id=1)

    await session.on_partial("текст", done=False)  # не бросает

    assert bot.drafts == []


async def test_push_status_sends_thinking_block():
    # Blocks вместо markdown — Telegram сам рисует серую анимацию для
    # InputRichBlockThinking, оборачивать текст самим (курсив и т.п.) не
    # нужно и нельзя (это отдельный тип блока, не markdown-разметка).
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("Альфред проверяет погоду")

    assert bot.drafts[0]["markdown"] is None
    assert bot.drafts[0]["blocks"] == [InputRichBlockThinking(text="Альфред проверяет погоду")]


async def test_push_status_dedups_consecutive_identical_status():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("Альфред думает")
    await session.push_status("Альфред думает")  # не изменилось — пропуск
    await session.push_status("Альфред сёрфит")

    assert [d["blocks"] for d in bot.drafts] == [
        [InputRichBlockThinking(text="Альфред думает")],
        [InputRichBlockThinking(text="Альфред сёрфит")],
    ]


async def test_push_status_and_on_partial_share_dedup_state():
    # Общий self._last_sent между двумя путями (md vs think) — но сигнатуры
    # разного вида, поэтому одинаковый сырой текст не гасит второй вызов.
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("текст")  # thinking-блок
    await session.on_partial("текст", done=False)  # markdown — не дедупится

    assert bot.drafts[0]["blocks"] == [InputRichBlockThinking(text="текст")]
    assert bot.drafts[1]["markdown"] == ALFRED_PREFIX_MD + "текст"


async def test_push_status_swallows_telegram_errors():
    bot = FakeBot()
    bot.draft_fails_times = 1
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("статус")  # не бросает

    assert bot.drafts == []


async def test_push_status_swallows_network_errors():
    # См. test_on_partial_swallows_network_errors — тот же класс бага: это
    # именно тот путь (ai_flow.py::_announce_steps/_on_phase_change), который
    # реально падал в проде до статуса ответа модели.
    bot = FakeBot()
    bot.draft_fails_times = 1
    bot.draft_fail_exception = ConnectionError("proxy timed out: 60")
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


async def test_finalize_status_sends_without_prefix_or_reply():
    # Реплика другого персонажа (Агнольда) — не Альфреда: без
    # ALFRED_PREFIX_MD и без привязки к конкретному сообщению пользователя.
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=42, message_thread_id=9)

    sent = await session.finalize_status("**Агнольд:** Сейчас Альфред подойдёт")

    assert sent is not None
    assert bot.sent[0]["chat_id"] == 42
    assert bot.sent[0]["message_thread_id"] == 9
    assert bot.sent[0]["markdown"] == "**Агнольд:** Сейчас Альфред подойдёт"
    assert bot.sent[0]["reply_parameters"] is None


async def test_finalize_status_resets_dedup_so_next_status_is_not_swallowed():
    # Живая находка 2026-08-10 (третий заход): finalize_status вытесняет
    # активный черновик реальным сообщением — следующий push_status с ТЕМ ЖЕ
    # текстом, что был в черновике до неё, должен всё равно уйти, а не молча
    # пропасть из-за дедупа (черновик, к которому относился дедуп, уже не
    # тот, что на экране).
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("шаги")
    await session.finalize_status("**Агнольд:** ...")
    await session.push_status("шаги")

    assert [d["blocks"] for d in bot.drafts] == [
        [InputRichBlockThinking(text="шаги")],
        [InputRichBlockThinking(text="шаги")],
    ]


# --- keep-alive: живая находка 2026-08-11 — sendRichMessageDraft эфемерен,
# 30-секундный TTL (докстринг aiogram), а долгие ожидания (wake_core.py:
# WAKE_POLL_TIMEOUT_S=180с, WARMUP_TIMEOUT_S=360с) на порядок больше — без
# периодической переотправки черновик гас сам по себе задолго до
# finalize()/finalize_status(). fast_retry_sleep (autouse, см. выше) патчит
# ОБЩИЙ asyncio-модуль (notifier_module.asyncio is rich_stream_module.asyncio
# — один и тот же объект), так что _KEEPALIVE_INTERVAL_S здесь тоже мгновенен
# без отдельного патча; _KEEPALIVE_MAX_TICKS патчится маленьким числом, чтобы
# цикл сам конечно завершался и его можно было дождаться await'ом.


async def test_keepalive_resends_active_draft_until_max_ticks(monkeypatch):
    monkeypatch.setattr(rich_stream_module, "_KEEPALIVE_MAX_TICKS", 2)
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("шаги")
    assert session._keepalive_task is not None
    await session._keepalive_task  # даём циклу дойти до предела (2 тика)

    assert len(bot.drafts) == 3  # исходный push + 2 переотправки
    assert all(d["blocks"] == [InputRichBlockThinking(text="шаги")] for d in bot.drafts)


async def test_keepalive_resends_latest_content_not_stale_one(monkeypatch):
    # Живой сценарий: между тиками keep-alive пришёл новый реальный push
    # (например, вызов другого тула) — переотправлять должен уже ЕГО, а не
    # то, что было на момент запуска цикла.
    monkeypatch.setattr(rich_stream_module, "_KEEPALIVE_MAX_TICKS", 1)
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("шаги")
    await session.push_status("сёрфит интернет")  # тот же цикл, свежее содержимое
    await session._keepalive_task

    assert [d["blocks"] for d in bot.drafts] == [
        [InputRichBlockThinking(text="шаги")],
        [InputRichBlockThinking(text="сёрфит интернет")],
        [InputRichBlockThinking(text="сёрфит интернет")],  # переотправка keep-alive
    ]


async def test_finalize_stops_keepalive():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("шаги")
    task = session._keepalive_task
    assert task is not None

    await session.finalize("ответ")
    # asyncio.sleep(0) здесь не сработал бы как yield — он тоже под
    # патчем fast_retry_sleep (общий asyncio-модуль, см. коммент выше) и
    # не делает реального обращения к планировщику. Дожидаемся именно
    # отменённой задачи — это настоящая синхронизация с циклом событий.
    await asyncio.gather(task, return_exceptions=True)

    assert session._keepalive_task is None
    assert session._active_message is None
    assert task.cancelled() or task.done()


async def test_finalize_status_stops_keepalive():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("шаги")
    task = session._keepalive_task

    await session.finalize_status("**Агнольд:** Сейчас Альфред подойдёт")
    await asyncio.gather(task, return_exceptions=True)

    assert session._keepalive_task is None
    assert session._active_message is None
    assert task.cancelled() or task.done()


async def test_keepalive_not_started_before_first_push():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    assert session._keepalive_task is None
