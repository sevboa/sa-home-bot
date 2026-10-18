from __future__ import annotations

import asyncio
import logging

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InputRichBlockThinking

from sa_home_bot.bot import notifier as notifier_module
from sa_home_bot.bot import rich_stream as rich_stream_module
from sa_home_bot.bot.rich_stream import ALFRED_PREFIX_MD, RichDraftPolicy, RichStreamSession

FAST = RichDraftPolicy(min_interval_s=0.0, min_growth_chars=0, keepalive_idle_s=3600.0)


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
        self.rich_fails_times = 0
        self.rich_fail_exception: Exception = ConnectionError("proxy timed out: 60")
        self.rich_calls = 0
        self._next_id = 1
        self.typing_actions: list[int] = []
        self.events: list[str] = []
        self.draft_retry_after_times = 0
        self.draft_retry_after_s: float = 5
        self.draft_gate: asyncio.Future | None = None
        self.on_rich = None

    async def send_rich_message_draft(
        self, *, chat_id, draft_id, rich_message, message_thread_id=None
    ):
        if self.draft_gate is not None:
            await self.draft_gate
        if self.draft_retry_after_times > 0:
            self.draft_retry_after_times -= 1
            exc = TelegramRetryAfter(None, "flood", retry_after=5)
            exc.retry_after = self.draft_retry_after_s
            raise exc
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
        self.events.append("draft")
        return True

    async def send_rich_message(
        self, *, chat_id, rich_message, reply_parameters=None, message_thread_id=None
    ):
        self.rich_calls += 1
        if self.on_rich is not None:
            self.on_rich()
        self.events.append("rich")
        if self.retry_after_times > 0:
            self.retry_after_times -= 1
            raise TelegramRetryAfter(None, "flood", retry_after=0)
        if self.rich_fails_times > 0:
            self.rich_fails_times -= 1
            raise self.rich_fail_exception
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
        self.events.append("typing")


@pytest.fixture(autouse=True)
def fast_retry_sleep(monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(notifier_module.asyncio, "sleep", _no_sleep)


@pytest.fixture(autouse=True)
async def cancel_leftover_tasks():
    yield
    current = asyncio.current_task()
    leftovers = [t for t in asyncio.all_tasks() if t is not current]
    for task in leftovers:
        task.cancel()
    await asyncio.gather(*leftovers, return_exceptions=True)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def _wait_until(cond, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        assert loop.time() < deadline, "условие не наступило"
        fut = loop.create_future()
        loop.call_later(0.01, fut.set_result, None)
        await fut


async def _real_pause(seconds: float) -> None:
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    loop.call_later(seconds, fut.set_result, None)
    await fut


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
    session = RichStreamSession(bot, chat_id=1, policy=FAST)

    await session.on_partial("", done=False)
    await session.on_partial("текст", done=False)
    await session.on_partial("текст", done=False)
    await session.on_partial("текст ещё", done=True)

    assert [d["markdown"] for d in bot.drafts] == [
        ALFRED_PREFIX_MD + "текст",
        ALFRED_PREFIX_MD + "текст ещё",
    ]


async def test_on_partial_swallows_telegram_errors():
    bot = FakeBot()
    bot.draft_fails_times = 1
    session = RichStreamSession(bot, chat_id=1)

    await session.on_partial("текст", done=False)

    assert bot.drafts == []


async def test_on_partial_swallows_network_errors():
    bot = FakeBot()
    bot.draft_fails_times = 1
    bot.draft_fail_exception = ConnectionError("proxy timed out: 60")
    session = RichStreamSession(bot, chat_id=1)

    await session.on_partial("текст", done=False)

    assert bot.drafts == []


async def test_push_status_sends_thinking_block():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("Альфред проверяет погоду")

    assert bot.drafts[0]["markdown"] is None
    assert bot.drafts[0]["blocks"] == [InputRichBlockThinking(text="Альфред проверяет погоду")]


async def test_push_status_dedups_consecutive_identical_status():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1, policy=FAST)

    await session.push_status("Альфред думает")
    await session.push_status("Альфред думает")
    await session.push_status("Альфред сёрфит")

    assert [d["blocks"] for d in bot.drafts] == [
        [InputRichBlockThinking(text="Альфред думает")],
        [InputRichBlockThinking(text="Альфред сёрфит")],
    ]


async def test_push_status_and_on_partial_share_dedup_state():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1, policy=FAST)

    await session.push_status("текст")
    await session.on_partial("текст", done=False)

    assert bot.drafts[0]["blocks"] == [InputRichBlockThinking(text="текст")]
    assert bot.drafts[1]["markdown"] == ALFRED_PREFIX_MD + "текст"


async def test_push_status_swallows_telegram_errors():
    bot = FakeBot()
    bot.draft_fails_times = 1
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("статус")

    assert bot.drafts == []


async def test_push_status_swallows_network_errors():
    bot = FakeBot()
    bot.draft_fails_times = 1
    bot.draft_fail_exception = ConnectionError("proxy timed out: 60")
    session = RichStreamSession(bot, chat_id=1)

    await session.push_status("статус")

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
    assert bot.sent[0]["markdown"] == ALFRED_PREFIX_MD + "ответ"
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
    assert len(bot.sent) == 1


async def test_finalize_gives_up_after_exhausting_retries():
    bot = FakeBot()
    bot.retry_after_times = notifier_module.MAX_RETRIES
    session = RichStreamSession(bot, chat_id=1)

    sent = await session.finalize("ответ")

    assert sent is None
    assert bot.sent == []


async def test_finalize_retries_transient_network_error_then_succeeds():
    bot = FakeBot()
    bot.rich_fail_exception = ConnectionError("proxy timed out: 60")
    bot.rich_fails_times = 2
    session = RichStreamSession(bot, chat_id=1)

    sent = await session.finalize("ответ")

    assert sent is not None
    assert len(bot.sent) == 1
    assert bot.rich_calls == 3


async def test_finalize_gives_up_after_exhausting_transient_retries():
    bot = FakeBot()
    bot.rich_fail_exception = ConnectionError("proxy timed out: 60")
    bot.rich_fails_times = 99
    session = RichStreamSession(bot, chat_id=1)

    sent = await session.finalize("ответ")

    assert sent is None
    assert bot.sent == []
    assert bot.rich_calls == notifier_module.MAX_RETRIES


async def test_finalize_does_not_retry_permanent_bad_request():
    bot = FakeBot()
    bot.rich_fail_exception = TelegramBadRequest(method=None, message="can't parse entities")
    bot.rich_fails_times = 1
    session = RichStreamSession(bot, chat_id=1)

    sent = await session.finalize("ответ")

    assert sent is None
    assert bot.rich_calls == 1


async def test_finalize_status_sends_without_prefix_or_reply():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=42, message_thread_id=9)

    sent = await session.finalize_status("**Агнольд:** Сейчас Альфред подойдёт")

    assert sent is not None
    assert bot.sent[0]["chat_id"] == 42
    assert bot.sent[0]["message_thread_id"] == 9
    assert bot.sent[0]["markdown"] == "**Агнольд:** Сейчас Альфред подойдёт"
    assert bot.sent[0]["reply_parameters"] is None


async def test_finalize_status_resets_dedup_so_next_status_is_not_swallowed():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1, policy=FAST)

    await session.push_status("шаги")
    await session.finalize_status("**Агнольд:** ...")
    await session.push_status("шаги")

    assert [d["blocks"] for d in bot.drafts] == [
        [InputRichBlockThinking(text="шаги")],
        [InputRichBlockThinking(text="шаги")],
    ]


async def test_text_drafts_throttled_by_interval_latest_wins():
    bot, clock = FakeBot(), FakeClock()
    policy = RichDraftPolicy(min_interval_s=6.0, min_growth_chars=0, keepalive_idle_s=3600.0)
    session = RichStreamSession(bot, chat_id=1, policy=policy, clock=clock)

    await session.on_partial("a", done=False)
    clock.t += 1
    await session.on_partial("ab", done=False)
    clock.t += 1
    await session.on_partial("abc", done=False)
    assert [d["markdown"] for d in bot.drafts] == [ALFRED_PREFIX_MD + "a"]

    clock.t += 4.5
    await session.on_partial("abcd", done=False)

    assert [d["markdown"] for d in bot.drafts] == [
        ALFRED_PREFIX_MD + "a",
        ALFRED_PREFIX_MD + "abcd",
    ]
    assert session._stats.coalesced == 2
    await session.aclose()


async def test_text_drafts_require_min_growth():
    bot, clock = FakeBot(), FakeClock()
    policy = RichDraftPolicy(min_interval_s=0.0, min_growth_chars=10, keepalive_idle_s=3600.0)
    session = RichStreamSession(bot, chat_id=1, policy=policy, clock=clock)

    await session.on_partial("x" * 5, done=False)
    await session.on_partial("x" * 12, done=False)
    await session.on_partial("x" * 15, done=False)
    await session.on_partial("y" * 3, done=False)

    assert [d["markdown"] for d in bot.drafts] == [
        ALFRED_PREFIX_MD + "x" * 5,
        ALFRED_PREFIX_MD + "x" * 15,
        ALFRED_PREFIX_MD + "y" * 3,
    ]
    await session.aclose()


async def test_first_text_after_status_skips_growth_condition():
    bot, clock = FakeBot(), FakeClock()
    policy = RichDraftPolicy(min_interval_s=0.0, min_growth_chars=500, keepalive_idle_s=3600.0)
    session = RichStreamSession(bot, chat_id=1, policy=policy, clock=clock)

    await session.push_status("Альфред думает")
    await session.on_partial("Добрый", done=False)

    assert bot.drafts[1]["markdown"] == ALFRED_PREFIX_MD + "Добрый"
    await session.aclose()


async def test_single_limiter_shared_by_status_and_text():
    bot, clock = FakeBot(), FakeClock()
    policy = RichDraftPolicy(min_interval_s=6.0, min_growth_chars=0, keepalive_idle_s=3600.0)
    session = RichStreamSession(bot, chat_id=1, policy=policy, clock=clock)

    await session.push_status("Альфред сёрфит")
    clock.t += 2
    await session.on_partial("Нашёл", done=False)
    clock.t += 2
    await session.push_status("Альфред читает")

    assert len(bot.drafts) == 1
    assert session._pending is not None and session._pending.kind == "think"
    await session.aclose()


async def test_tool_statuses_coalesce_and_pump_sends_latest():
    bot = FakeBot()
    policy = RichDraftPolicy(min_interval_s=0.05, min_growth_chars=0, keepalive_idle_s=3600.0)
    session = RichStreamSession(bot, chat_id=1, policy=policy)

    await session.push_status("тул 1")
    await session.push_status("тул 2")
    await session.push_status("тул 3")
    await _wait_until(lambda: len(bot.drafts) >= 2)

    assert [d["blocks"] for d in bot.drafts] == [
        [InputRichBlockThinking(text="тул 1")],
        [InputRichBlockThinking(text="тул 3")],
    ]
    assert session._stats.status == 2
    assert session._stats.coalesced == 1
    await session.aclose()


async def test_retry_after_pauses_all_drafts_and_keeps_content(caplog):
    bot, clock = FakeBot(), FakeClock()
    bot.draft_retry_after_times = 1
    bot.draft_retry_after_s = 5
    policy = RichDraftPolicy(min_interval_s=0.0, min_growth_chars=0, keepalive_idle_s=3600.0)
    session = RichStreamSession(bot, chat_id=1, policy=policy, clock=clock)

    with caplog.at_level(logging.WARNING, logger="sa_home_bot.bot.rich_stream"):
        await session.push_status("шаги")
    assert bot.drafts == []
    assert "429" in caplog.text
    assert session._pending is not None

    clock.t += 3
    await session.on_partial("текст", done=False)
    assert bot.drafts == []

    clock.t += 2.5
    await session.on_partial("текст ещё", done=False)
    assert [d["markdown"] for d in bot.drafts] == [ALFRED_PREFIX_MD + "текст ещё"]
    assert session._stats.retry_after == 1

    sent = await session.finalize("ответ")
    assert sent is not None


async def test_pump_resends_after_retry_after_expires():
    bot = FakeBot()
    bot.draft_retry_after_times = 1
    bot.draft_retry_after_s = 0.05
    policy = RichDraftPolicy(min_interval_s=0.0, min_growth_chars=0, keepalive_idle_s=3600.0)
    session = RichStreamSession(bot, chat_id=1, policy=policy)

    await session.push_status("шаги")
    await _wait_until(lambda: len(bot.drafts) == 1)

    assert bot.drafts[0]["blocks"] == [InputRichBlockThinking(text="шаги")]
    await session.aclose()


async def test_other_draft_failures_warned_with_limit(caplog, monkeypatch):
    monkeypatch.setattr(rich_stream_module, "_FAILURE_WARN_LIMIT", 2)
    bot = FakeBot()
    bot.draft_fails_times = 5
    session = RichStreamSession(bot, chat_id=1, policy=FAST)

    with caplog.at_level(logging.WARNING, logger="sa_home_bot.bot.rich_stream"):
        for i in range(5):
            await session.push_status(f"статус {i}")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert session._stats.failures == 5
    await session.aclose()


async def test_keepalive_only_when_idle():
    bot = FakeBot()
    policy = RichDraftPolicy(min_interval_s=0.0, min_growth_chars=0, keepalive_idle_s=0.3)
    session = RichStreamSession(bot, chat_id=1, policy=policy)

    for i in range(10):
        await session.on_partial("x" * (i + 1), done=False)
        await _real_pause(0.05)
    assert session._stats.keepalive == 0
    assert session._stats.text == 10

    await _wait_until(lambda: session._stats.keepalive >= 1)
    assert bot.drafts[-1]["markdown"] == ALFRED_PREFIX_MD + "x" * 10
    await session.aclose()


async def test_keepalive_flushes_small_tail_below_growth_threshold():
    bot = FakeBot()
    policy = RichDraftPolicy(min_interval_s=0.0, min_growth_chars=100, keepalive_idle_s=0.1)
    session = RichStreamSession(bot, chat_id=1, policy=policy)

    await session.on_partial("a", done=False)
    await session.on_partial("ab", done=False)
    await _wait_until(lambda: len(bot.drafts) >= 2)

    assert bot.drafts[1]["markdown"] == ALFRED_PREFIX_MD + "ab"
    assert session._stats.text == 2
    assert session._stats.keepalive == 0
    await session.aclose()


async def test_keepalive_not_started_before_first_push():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1)

    assert session._pump_task is None


async def test_finalize_stops_pump_before_rich_message():
    bot = FakeBot()
    policy = RichDraftPolicy(min_interval_s=0.0, min_growth_chars=0, keepalive_idle_s=0.05)
    session = RichStreamSession(bot, chat_id=1, policy=policy)
    snapshot = {}
    bot.on_rich = lambda: snapshot.update(
        pump=session._pump_task, typing=session._typing._task, active=session._active_message
    )

    await session.push_status("шаги")
    task = session._pump_task
    assert task is not None
    await session.finalize("ответ")

    assert snapshot == {"pump": None, "typing": None, "active": None}
    assert task.done()
    await _real_pause(0.2)
    assert bot.events == ["draft", "rich"]


async def test_inflight_keepalive_cannot_land_after_final():
    bot = FakeBot()
    policy = RichDraftPolicy(min_interval_s=0.0, min_growth_chars=0, keepalive_idle_s=0.05)
    session = RichStreamSession(bot, chat_id=1, policy=policy)

    await session.push_status("шаги")
    loop = asyncio.get_running_loop()
    bot.draft_gate = loop.create_future()
    await _real_pause(0.15)
    await session.finalize("ответ")
    if not bot.draft_gate.done():
        bot.draft_gate.set_result(None)
    await _real_pause(0.1)

    assert bot.events == ["draft", "rich"]


async def test_streaming_uses_no_typing():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1, policy=FAST)

    await session.push_status("думает")
    await session.on_partial("текст", done=False)
    await session.push_status("сёрфит")
    await session.on_partial("текст ещё", done=False)
    await session.finalize("ответ")

    assert bot.typing_actions == []


async def test_streaming_disabled_sends_only_typing_and_final(caplog):
    bot = FakeBot()
    policy = RichDraftPolicy(streaming=False)
    session = RichStreamSession(bot, chat_id=1, policy=policy)
    snapshot = {}
    bot.on_rich = lambda: snapshot.update(typing=session._typing._task)

    await session.push_status("думает")
    await session.on_partial("текст", done=False)
    await session.push_status("сёрфит")
    sent = await session.finalize("ответ")

    assert sent is not None
    assert bot.drafts == []
    assert len(bot.typing_actions) == 1
    assert snapshot == {"typing": None}
    assert bot.sent[0]["markdown"] == ALFRED_PREFIX_MD + "ответ"
    assert session._pump_task is None

    with caplog.at_level(logging.INFO, logger="sa_home_bot.bot.rich_stream"):
        await session.aclose()
    assert "стрим=нет" in caplog.text
    assert "typing=1" in caplog.text


async def test_summary_logged_once_per_session(caplog):
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1, policy=FAST)

    await session.push_status("шаги")
    await session.on_partial("текст", done=False)
    await session.finalize("ответ")
    with caplog.at_level(logging.INFO, logger="sa_home_bot.bot.rich_stream"):
        await session.aclose()
        await session.aclose()

    lines = [r.getMessage() for r in caplog.records if "итог сессии" in r.getMessage()]
    assert len(lines) == 1
    assert "текст=1 статусы=1 keep-alive=0" in lines[0]
    assert "429=0" in lines[0]


async def test_policy_from_llm_config_matches_defaults():
    from sa_home_bot.config import LlmConfig

    assert RichDraftPolicy.from_llm_config(LlmConfig()) == RichDraftPolicy()


async def test_finalize_status_stops_pump():
    bot = FakeBot()
    session = RichStreamSession(bot, chat_id=1, policy=FAST)

    await session.push_status("шаги")
    task = session._pump_task

    await session.finalize_status("**Агнольд:** Сейчас Альфред подойдёт")

    assert session._pump_task is None
    assert session._active_message is None
    assert task.done()
