from unittest.mock import ANY

import pytest
from aiogram.types import InputRichBlockThinking

from sa_home_bot.bot.ai_lines import (
    ALBERT_ASLEEP,
    ALBERT_ASLEEP_MD,
    ALBERT_TASK_MISSED,
    ALBERT_TASK_MISSED_MD,
    ALBERT_UNAVAILABLE,
    ALBERT_UNAVAILABLE_MD,
    ARNOLD_WAKING,
    ARNOLD_WAKING_MD,
    CLOSING_TEXT,
    RESTART_TEXT,
    STEPS_TEXT,
    STEPS_TEXT_PLAIN,
)
from sa_home_bot.bot.node_events import (
    TaskRichSessions,
    _handle_task_result,
    build_close_ssh_keyboard,
    build_node_event_handler,
    render_idle_power_blocked,
    render_node_joined,
    render_node_leaving,
    render_node_returned,
    render_restart_applied,
    render_speech_cured,
    render_update_finished,
)
from sa_home_bot.bot.rich_stream import ALFRED_PREFIX_MD
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.config import Settings, SubscriptionConfig
from sa_home_bot.node.events import EVENT_RESTART_APPLIED
from sa_home_bot.proto.messages import Address, make_event
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.tasks import protocol as task_protocol
from tests.unit.support.fakes import FakeNotifier as SupportNotifier


class FakeBot:
    def __init__(self) -> None:
        self.drafts: list[dict] = []
        self.sent: list[dict] = []
        self._next_id = 1

    async def send_rich_message_draft(
        self, *, chat_id, draft_id, rich_message, message_thread_id=None
    ):
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
        self.sent.append(
            {
                "chat_id": chat_id,
                "markdown": rich_message.markdown,
                "reply_parameters": reply_parameters,
                "message_thread_id": message_thread_id,
            }
        )
        msg_id = self._next_id
        self._next_id += 1
        return type("FakeSentMessage", (), {"message_id": msg_id})()


class FakeNotifier(SupportNotifier):
    def __init__(self, bot: FakeBot | None = None) -> None:
        super().__init__(send_direct_result=99)
        self.bot = bot

    @property
    def sent_full(self) -> list[tuple[int, str, int | None, int | None]]:
        rows = zip(self.sent, self.reply_tos, self.threads, strict=True)
        return [(chat, text, reply_to, thread) for (chat, text), reply_to, thread in rows]


class FakeStore:
    def __init__(self, acquainted: tuple[tuple[int, int], ...] = ()) -> None:
        self.recorded_turns: list[tuple] = []
        self.recorded_events: list[tuple] = []
        self._acquainted = acquainted

    async def relationships_for(self, chat_id, status="confirmed"):
        return [{"guest_a": a, "guest_b": b} for a, b in self._acquainted if chat_id in (a, b)]

    async def record_ai_turn(self, *args, **kwargs):
        self.recorded_turns.append((args, kwargs))

    async def record_event(self, event_type, node, text, at):
        self.recorded_events.append((event_type, node, text, at))


def _book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="all", chat_id=1, event_types=["*"]),
            SubscriptionConfig(name="heat_only", chat_id=2, event_types=["overheat_started"]),
        ]
    )


def test_render_node_joined_mentions_id_and_endpoint():
    text = render_node_joined("arch-t480", "tcp://100.110.58.31:8710")
    assert "arch-t480" in text
    assert "tcp://100.110.58.31:8710" in text


async def test_handler_broadcasts_system_on_node_joined():
    book = _book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    env = make_event(
        "node_joined",
        {"node_id": "arch-t480", "endpoint": "tcp://100.110.58.31:8710"},
        src=Address(node="alfred", service="node"),
    )
    await handler(env)

    assert notifier.sent == [(1, render_node_joined("arch-t480", "tcp://100.110.58.31:8710"))]


async def test_handler_journals_system_events_for_alfred():
    book = _book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    env = make_event(
        "node_joined",
        {"node_id": "arch-t480", "endpoint": "tcp://100.110.58.31:8710"},
        src=Address(node="alfred", service="node"),
    )
    await handler(env)

    assert len(store.recorded_events) == 1
    event_type, node, text, _at = store.recorded_events[0]
    assert event_type == "node_joined"
    assert node == "arch-t480"
    assert text == render_node_joined("arch-t480", "tcp://100.110.58.31:8710")


async def test_handler_ignores_other_event_types():
    book = _book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    env = make_event("service_started", {"name": "monitor"}, src=Address(node="alfred"))
    await handler(env)

    assert notifier.sent == []


async def test_handler_ignores_event_without_node_id():
    book = _book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    env = make_event("node_joined", {"endpoint": "tcp://x:1"}, src=Address(node="alfred"))
    await handler(env)

    assert notifier.sent == []


def test_render_update_finished_success_mentions_restart_node():
    text = render_update_finished("arch-t480", True, "0.22.0", None)
    assert "arch-t480" in text and "0.22.0" in text
    assert "restart_node" in text


def test_render_update_finished_failure_shows_error():
    text = render_update_finished("arch-t480", False, None, "network unreachable")
    assert "arch-t480" in text
    assert "network unreachable" in text
    assert "не удалось" in text


def test_render_update_finished_failure_escapes_pip_output():
    text = render_update_finished("mycraft", False, None, "numpy<2,>=1.26 conflicts")
    assert "numpy&lt;2,&gt;=1.26" in text


async def test_handler_broadcasts_on_update_finished_success():
    book = _book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    env = make_event(
        "update_finished",
        {"ok": True, "version": "0.22.0", "error": None},
        src=Address(node="arch-t480", service="node"),
    )
    await handler(env)

    assert notifier.sent == [(1, render_update_finished("arch-t480", True, "0.22.0", None))]


async def test_handler_broadcasts_on_update_finished_failure():
    book = _book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    env = make_event(
        "update_finished",
        {"ok": False, "version": None, "error": "boom"},
        src=Address(node="alfred", service="node"),
    )
    await handler(env)

    assert notifier.sent == [(1, render_update_finished("alfred", False, None, "boom"))]


class FakeMatchEventLink:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, object]] = []
        self._raises = raises

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}, dst))
        if self._raises is not None:
            raise self._raises
        return {"matched": True, "task_id": 42}


async def test_handler_broadcasts_on_restart_applied():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        EVENT_RESTART_APPLIED,
        {"from": "0.72.0", "to": "0.73.0"},
        src=Address(node="arch-t480", service="node"),
    )
    await handler(env)

    text = render_restart_applied("arch-t480", "0.72.0", "0.73.0")
    assert notifier.sent == [(1, text)]
    assert store.recorded_events == [(EVENT_RESTART_APPLIED, "arch-t480", text, ANY)]


async def test_handler_calls_match_event_on_restart_applied():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    link = FakeMatchEventLink()
    handler = build_node_event_handler(book, notifier, store, get_node_link=lambda: link)
    env = make_event(
        EVENT_RESTART_APPLIED,
        {"from": "0.72.0", "to": "0.73.0"},
        src=Address(node="arch-t480", service="node"),
    )
    await handler(env)

    assert link.calls == [
        (
            task_protocol.ACTION_MATCH_EVENT,
            {"node": "arch-t480", "event_type": EVENT_RESTART_APPLIED},
            Address(node=task_protocol.NODE_ID, service=task_protocol.SERVICE_NAME),
        )
    ]


async def test_handler_calls_match_event_on_update_finished():
    from sa_home_bot.bot.node_events import render_update_finished
    from sa_home_bot.node.events import EVENT_UPDATE_FINISHED

    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    link = FakeMatchEventLink()
    handler = build_node_event_handler(book, notifier, store, get_node_link=lambda: link)
    env = make_event(
        "update_finished",
        {"ok": True, "version": "0.75.3"},
        src=Address(node="jeeves", service="node"),
    )
    await handler(env)

    assert notifier.sent == [(1, render_update_finished("jeeves", True, "0.75.3", None))]
    assert link.calls == [
        (
            task_protocol.ACTION_MATCH_EVENT,
            {"node": "jeeves", "event_type": EVENT_UPDATE_FINISHED},
            Address(node=task_protocol.NODE_ID, service=task_protocol.SERVICE_NAME),
        )
    ]


async def test_handler_match_event_failure_is_swallowed_not_raised():
    from sa_home_bot.bot.service_link import ServiceUnavailableError

    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    link = FakeMatchEventLink(raises=ServiceUnavailableError("нет связи"))
    handler = build_node_event_handler(book, notifier, store, get_node_link=lambda: link)
    env = make_event(
        EVENT_RESTART_APPLIED,
        {"from": "0.72.0", "to": "0.73.0"},
        src=Address(node="arch-t480", service="node"),
    )
    await handler(env)

    assert notifier.sent == [(1, render_restart_applied("arch-t480", "0.72.0", "0.73.0"))]


async def test_handler_without_get_node_link_does_not_crash():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        EVENT_RESTART_APPLIED,
        {"from": "0.72.0", "to": "0.73.0"},
        src=Address(node="arch-t480", service="node"),
    )
    await handler(env)

    assert notifier.sent == [(1, render_restart_applied("arch-t480", "0.72.0", "0.73.0"))]


async def test_handler_ignores_update_finished_without_src_node():
    book = _book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    env = make_event("update_finished", {"ok": True, "version": "0.22.0"}, src=None)
    await handler(env)

    assert notifier.sent == []


async def test_handler_sends_closing_text_to_each_listed_chat_on_idle_sleep():
    book = _book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    env = make_event(
        "llm_idle_sleep", {"chat_ids": [7, 42]}, src=Address(node="winpc", service="llm")
    )
    await handler(env)

    assert notifier.sent == [(7, CLOSING_TEXT), (42, CLOSING_TEXT)]


async def test_handler_sends_restart_text_to_each_listed_chat():
    book = _book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    env = make_event(
        "llm_service_restart", {"chat_ids": [7, 42]}, src=Address(node="winpc", service="llm")
    )
    await handler(env)

    assert notifier.sent == [(7, RESTART_TEXT), (42, RESTART_TEXT)]


async def test_handler_restart_event_with_no_chats_sends_nothing():
    book = _book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    env = make_event(
        "llm_service_restart", {"chat_ids": []}, src=Address(node="winpc", service="llm")
    )
    await handler(env)

    assert notifier.sent == []


async def test_handler_broadcasts_speech_cured_to_everyone_including_guests():
    book = SubscriptionBook(
        [
            Subscription(name="plain", chat_id=1, event_types=frozenset({"overheat_started"})),
            Subscription(name="guest", chat_id=2, source="guest", event_types=frozenset()),
            Subscription(name="dead", chat_id=3, broken=True, event_types=frozenset({"*"})),
        ]
    )
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    env = make_event("llm_speech_cured", {}, src=Address(node="winpc", service="llm"))
    await handler(env)

    assert notifier.sent == [(1, render_speech_cured()), (2, render_speech_cured())]


_LLM_CHAT_META = {
    "kind": task_protocol.TASK_KIND_LLM_CHAT,
    "chat_id": 7,
    "dialogue_id": 500,
    "trigger_message_id": 501,
}


async def test_task_prewake_waking_sends_steps_text():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_TASK_PREWAKE,
        {"task_id": 1, "meta": _LLM_CHAT_META, "status": "waking"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)
    assert notifier.sent == [(7, STEPS_TEXT)]


async def test_task_prewake_replies_into_the_right_topic():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    meta = {**_LLM_CHAT_META, "message_thread_id": 42}
    env = make_event(
        task_protocol.EVENT_TASK_PREWAKE,
        {"task_id": 1, "meta": meta, "status": "waking"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)
    assert notifier.sent_full == [(7, STEPS_TEXT, None, 42)]


async def test_task_prewake_ready_sends_arnold_waking():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_TASK_PREWAKE,
        {"task_id": 1, "meta": _LLM_CHAT_META, "status": "ready"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)
    assert notifier.sent == [(7, ARNOLD_WAKING)]


async def test_task_prewake_failed_unreachable_sends_albert_unavailable():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_TASK_PREWAKE,
        {"task_id": 1, "meta": _LLM_CHAT_META, "status": "failed", "reason": "unreachable"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)
    assert notifier.sent == [(7, ALBERT_UNAVAILABLE)]


async def test_task_prewake_failed_warmup_sends_albert_asleep():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_TASK_PREWAKE,
        {"task_id": 1, "meta": _LLM_CHAT_META, "status": "failed", "reason": "warmup_failed"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)
    assert notifier.sent == [(7, ALBERT_ASLEEP)]


async def test_task_prewake_ignores_non_llm_chat_kind():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_TASK_PREWAKE,
        {"task_id": 1, "meta": {"kind": "something_else", "chat_id": 7}, "status": "waking"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)
    assert notifier.sent == []


async def test_task_result_success_replies_to_trigger_and_records_turn():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_TASK_RESULT,
        {
            "task_id": 1,
            "meta": _LLM_CHAT_META,
            "ok": True,
            "result": {"response": "Полил цветы, сэр"},
        },
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent_full == [(7, "<b>Альфред:</b> Полил цветы, сэр", 501, None)]
    assert len(store.recorded_turns) == 1
    args, _kwargs = store.recorded_turns[0]
    assert args[:4] == (7, 99, 500, "assistant")
    assert args[4] == "Полил цветы, сэр"


async def test_task_result_success_replies_into_the_right_topic():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    meta = {**_LLM_CHAT_META, "message_thread_id": 42}
    env = make_event(
        task_protocol.EVENT_TASK_RESULT,
        {"task_id": 1, "meta": meta, "ok": True, "result": {"response": "Готово"}},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent_full == [(7, "<b>Альфред:</b> Готово", 501, 42)]


async def test_task_result_without_dialogue_id_spawns_new_thread_from_sent_id():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    meta = {"kind": task_protocol.TASK_KIND_LLM_CHAT, "chat_id": 7}
    env = make_event(
        task_protocol.EVENT_TASK_RESULT,
        {
            "task_id": 1,
            "meta": meta,
            "ok": True,
            "result": {"response": "Здравствуйте, разрешите представиться"},
        },
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent_full == [
        (7, "<b>Альфред:</b> Здравствуйте, разрешите представиться", None, None)
    ]
    assert len(store.recorded_turns) == 1
    args, _kwargs = store.recorded_turns[0]
    assert args[:4] == (7, 99, 99, "assistant")
    assert args[4] == "Здравствуйте, разрешите представиться"


async def test_task_result_failure_sends_albert_task_missed_as_reply():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_TASK_RESULT,
        {"task_id": 1, "meta": _LLM_CHAT_META, "ok": False, "error": "not warm"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent_full == [(7, ALBERT_TASK_MISSED, 501, None)]
    assert store.recorded_turns == []


async def test_task_result_ignores_non_llm_chat_kind():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_TASK_RESULT,
        {"task_id": 1, "meta": {"kind": "something_else", "chat_id": 7}, "ok": True},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent == []
    assert store.recorded_turns == []


async def test_task_prewake_waking_uses_rich_thinking_block_when_rich():
    book, store = _book(), FakeStore()
    bot = FakeBot()
    notifier = FakeNotifier(bot)
    handler = build_node_event_handler(book, notifier, store, config=Settings())
    env = make_event(
        task_protocol.EVENT_TASK_PREWAKE,
        {"task_id": 1, "meta": _LLM_CHAT_META, "status": "waking"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent == []
    assert bot.drafts == [
        {
            "chat_id": 7,
            "draft_id": ANY,
            "markdown": None,
            "blocks": [InputRichBlockThinking(text=STEPS_TEXT_PLAIN)],
            "message_thread_id": None,
        }
    ]


async def test_task_prewake_ready_replaces_same_draft_with_agnold():
    book, store = _book(), FakeStore()
    bot = FakeBot()
    notifier = FakeNotifier(bot)
    handler = build_node_event_handler(book, notifier, store, config=Settings())

    for status in ("waking", "ready"):
        await handler(
            make_event(
                task_protocol.EVENT_TASK_PREWAKE,
                {"task_id": 1, "meta": _LLM_CHAT_META, "status": status},
                src=Address(node="alfred", service="tasks"),
            )
        )

    assert notifier.sent == []
    assert len(bot.drafts) == 1
    assert bot.sent == [
        {
            "chat_id": 7,
            "markdown": ARNOLD_WAKING_MD,
            "reply_parameters": None,
            "message_thread_id": None,
        }
    ]


async def test_task_prewake_failed_unreachable_uses_albert_unavailable_rich():
    book, store = _book(), FakeStore()
    bot = FakeBot()
    notifier = FakeNotifier(bot)
    handler = build_node_event_handler(book, notifier, store, config=Settings())

    await handler(
        make_event(
            task_protocol.EVENT_TASK_PREWAKE,
            {"task_id": 1, "meta": _LLM_CHAT_META, "status": "waking"},
            src=Address(node="alfred", service="tasks"),
        )
    )
    await handler(
        make_event(
            task_protocol.EVENT_TASK_PREWAKE,
            {"task_id": 1, "meta": _LLM_CHAT_META, "status": "failed", "reason": "unreachable"},
            src=Address(node="alfred", service="tasks"),
        )
    )

    assert notifier.sent == []
    assert bot.sent[0]["markdown"] == ALBERT_UNAVAILABLE_MD


async def test_task_prewake_failed_warmup_uses_albert_asleep_rich():
    book, store = _book(), FakeStore()
    bot = FakeBot()
    notifier = FakeNotifier(bot)
    handler = build_node_event_handler(book, notifier, store, config=Settings())

    await handler(
        make_event(
            task_protocol.EVENT_TASK_PREWAKE,
            {"task_id": 1, "meta": _LLM_CHAT_META, "status": "waking"},
            src=Address(node="alfred", service="tasks"),
        )
    )
    await handler(
        make_event(
            task_protocol.EVENT_TASK_PREWAKE,
            {"task_id": 1, "meta": _LLM_CHAT_META, "status": "failed", "reason": "warmup_failed"},
            src=Address(node="alfred", service="tasks"),
        )
    )

    assert notifier.sent == []
    assert bot.sent[0]["markdown"] == ALBERT_ASLEEP_MD


async def test_task_result_success_reuses_session_started_by_prewake():
    book, store = _book(), FakeStore()
    bot = FakeBot()
    notifier = FakeNotifier(bot)
    handler = build_node_event_handler(book, notifier, store, config=Settings())

    for status in ("waking", "ready"):
        await handler(
            make_event(
                task_protocol.EVENT_TASK_PREWAKE,
                {"task_id": 1, "meta": _LLM_CHAT_META, "status": status},
                src=Address(node="alfred", service="tasks"),
            )
        )
    await handler(
        make_event(
            task_protocol.EVENT_TASK_RESULT,
            {
                "task_id": 1,
                "meta": _LLM_CHAT_META,
                "ok": True,
                "result": {"response": "Полил цветы, сэр"},
            },
            src=Address(node="alfred", service="tasks"),
        )
    )

    assert notifier.sent == []
    assert len(bot.drafts) == 1
    assert [s["markdown"] for s in bot.sent] == [
        ARNOLD_WAKING_MD,
        ALFRED_PREFIX_MD + "Полил цветы, сэр",
    ]
    assert bot.sent[1]["reply_parameters"].message_id == 501
    assert len(store.recorded_turns) == 1


async def test_task_result_failure_uses_albert_task_missed_rich_with_reply():
    book, store = _book(), FakeStore()
    bot = FakeBot()
    notifier = FakeNotifier(bot)
    handler = build_node_event_handler(book, notifier, store, config=Settings())

    await handler(
        make_event(
            task_protocol.EVENT_TASK_RESULT,
            {"task_id": 1, "meta": _LLM_CHAT_META, "ok": False, "error": "not warm"},
            src=Address(node="alfred", service="tasks"),
        )
    )

    assert notifier.sent == []
    assert bot.sent[0]["markdown"] == ALBERT_TASK_MISSED_MD
    assert bot.sent[0]["reply_parameters"].message_id == 501
    assert store.recorded_turns == []


def _tool_call_book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="admin", chat_id=9, event_types=["alfred_tool_call"])]
    )


async def test_tool_call_without_debug_store_sends_short_text_without_button():
    notifier, store = FakeNotifier(), FakeStore()
    handler = build_node_event_handler(_tool_call_book(), notifier, store)
    env = make_event(
        task_protocol.EVENT_TOOL_CALL,
        {"name": "calc", "args": {"expression": "1+1"}, "result": "2"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent == [(9, "🔧 Alfred вызвал инструмент: calc")]
    assert notifier.markups == [None]


async def test_tool_call_with_debug_store_attaches_expand_button_with_real_args_result():
    notifier, store = FakeNotifier(), FakeStore()
    tool_calls = ToolCalls()
    handler = build_node_event_handler(_tool_call_book(), notifier, store, tool_calls=tool_calls)
    env = make_event(
        task_protocol.EVENT_TOOL_CALL,
        {"name": "calc", "args": {"expression": "1+1"}, "result": "2"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent == [(9, "🔧 Alfred вызвал инструмент: calc")]
    markup = notifier.markups[0]
    assert markup is not None
    token = markup.inline_keyboard[0][0].callback_data.split(":")[-1]
    call = tool_calls.get(token)
    assert call is not None
    assert call.name == "calc"
    assert call.args == {"expression": "1+1"}
    assert call.result == "2"


async def test_deliver_message_sends_html_and_records_plain_turn():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_DELIVER_MESSAGE,
        {"chat_id": 7, "html": "<b>Граф</b>: пора спать", "plain": "пора спать"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent_full == [(7, "<b>Граф</b>: пора спать", None, None)]
    assert len(store.recorded_turns) == 1
    args, _kwargs = store.recorded_turns[0]
    assert args[:4] == (7, 99, 99, "assistant")
    assert args[4] == "пора спать"


async def test_deliver_message_respects_thread_id():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_DELIVER_MESSAGE,
        {"chat_id": 7, "html": "текст", "plain": "текст", "message_thread_id": 42},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent_full == [(7, "текст", None, 42)]


async def test_deliver_message_ignores_missing_chat_id_or_text():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    for data in ({"html": "текст"}, {"chat_id": 7}, {"chat_id": 7, "html": ""}):
        await handler(
            make_event(
                task_protocol.EVENT_DELIVER_MESSAGE,
                data,
                src=Address(node="alfred", service="tasks"),
            )
        )
    assert notifier.sent == []
    assert store.recorded_turns == []


async def test_deliver_message_does_not_record_turn_on_failed_send():
    book, store = _book(), FakeStore()

    class FailingNotifier(FakeNotifier):
        async def send_direct(self, *args, **kwargs):
            await super().send_direct(*args, **kwargs)
            return None

    notifier = FailingNotifier()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_DELIVER_MESSAGE,
        {"chat_id": 7, "html": "текст", "plain": "текст"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert store.recorded_turns == []


async def test_deliver_message_checks_acquaintance_for_tasks_tell():
    data = {"chat_id": 7, "html": "текст", "plain": "текст", "require_acquaintance": [5, 7]}
    src = Address(node="alfred", service="tasks")

    notifier, store = FakeNotifier(), FakeStore()
    await build_node_event_handler(_book(), notifier, store)(
        make_event(task_protocol.EVENT_DELIVER_MESSAGE, data, src=src)
    )
    assert notifier.sent == [] and store.recorded_turns == []

    notifier, store = FakeNotifier(), FakeStore(acquainted=((7, 5),))
    await build_node_event_handler(_book(), notifier, store)(
        make_event(task_protocol.EVENT_DELIVER_MESSAGE, data, src=src)
    )
    assert [s[0] for s in notifier.sent_full] == [7]


async def test_handler_broadcasts_on_node_leaving_and_returned():
    book = _book()
    notifier = FakeNotifier()
    handler = build_node_event_handler(book, notifier, FakeStore())

    await handler(
        make_event(
            "node_leaving",
            {"node": "winpc", "kind": "workstation"},
            src=Address(node="winpc", service="node"),
        )
    )
    await handler(
        make_event(
            "node_returned",
            {"node": "winpc", "kind": "workstation"},
            src=Address(node="alfred", service="node"),
        )
    )

    assert notifier.sent == [
        (1, render_node_leaving("winpc")),
        (1, render_node_returned("winpc")),
    ]


def _admin_book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="admin", chat_id=999, allowed_commands=["*"])]
    )


def test_render_idle_power_blocked_lists_sessions():
    text = render_idle_power_blocked("mycraft", ["sevboa, pts/0, с 01:08"])
    assert "mycraft" in text
    assert "sevboa, pts/0, с 01:08" in text


def test_build_close_ssh_keyboard_targets_node():
    keyboard = build_close_ssh_keyboard("mycraft")
    button = keyboard.inline_keyboard[0][0]
    assert button.callback_data == "act:node:close_ssh_sessions::mycraft"


async def test_handler_notifies_admins_with_close_ssh_button():
    book = _admin_book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    await handler(
        make_event(
            "idle_power_blocked",
            {"node": "mycraft", "sessions": ["sevboa, pts/0, с 01:08"]},
            src=Address(node="mycraft", service="node"),
        )
    )

    expected_text = render_idle_power_blocked("mycraft", ["sevboa, pts/0, с 01:08"])
    assert notifier.sent == [(999, expected_text)]
    assert notifier.markups == [build_close_ssh_keyboard("mycraft")]
    assert store.recorded_events == [("idle_power_blocked", "mycraft", expected_text, ANY)]


async def test_handler_ignores_idle_power_blocked_without_sessions():
    book = _admin_book()
    notifier = FakeNotifier()
    handler = build_node_event_handler(book, notifier, FakeStore())

    await handler(
        make_event(
            "idle_power_blocked", {"node": "mycraft", "sessions": []}, src=Address(node="mycraft")
        )
    )

    assert notifier.sent == []


async def test_handler_does_not_notify_non_admin_chats_on_idle_power_blocked():
    book = _book()
    notifier = FakeNotifier()
    handler = build_node_event_handler(book, notifier, FakeStore())

    await handler(
        make_event(
            "idle_power_blocked",
            {"node": "mycraft", "sessions": ["sevboa, pts/0"]},
            src=Address(node="mycraft"),
        )
    )

    assert notifier.sent == []


async def test_task_result_closes_and_forgets_session_even_if_delivery_fails():
    class BrokenStore(FakeStore):
        async def record_ai_turn(self, *args, **kwargs):
            raise RuntimeError("БД недоступна")

    bot = FakeBot()
    notifier = FakeNotifier(bot)
    sessions = TaskRichSessions(notifier, Settings())
    session = sessions.get(1, 7, None)
    await session.push_status("шаги")
    assert session._pump_task is not None

    data = {"task_id": 1, "meta": _LLM_CHAT_META, "ok": True, "result": {"response": "Готово"}}
    with pytest.raises(RuntimeError):
        await _handle_task_result(notifier, BrokenStore(), data, sessions)

    assert sessions._sessions == {}
    assert session._pump_task is None
    assert bot.sent[0]["markdown"] == ALFRED_PREFIX_MD + "Готово"
