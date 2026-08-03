"""bot/node_events.py: node_joined/update_finished → системное уведомление,
llm_idle_sleep/llm_service_restart/task_prewake/task_result → адресное
сообщение в конкретные чаты (последние два — от службы tasks, генерализация
2026-07-24 старого напоминания в отдельный сервис отложенных задач),
llm_speech_cured → буквально всем подпискам (broadcast_all, не через
event_types opt-in — Логопед долечил Альфреда, должны узнать все, включая
гостей)."""

from sa_home_bot.bot.ai_flow import (
    ALBERT_ASLEEP,
    ALBERT_TASK_MISSED,
    ALBERT_UNAVAILABLE,
    ARNOLD_WAKING,
    CLOSING_TEXT,
    RESTART_TEXT,
    STEPS_TEXT,
)
from sa_home_bot.bot.node_events import (
    build_close_ssh_keyboard,
    build_node_event_handler,
    render_idle_power_blocked,
    render_node_joined,
    render_node_leaving,
    render_node_returned,
    render_speech_cured,
    render_update_finished,
)
from sa_home_bot.config import SubscriptionConfig
from sa_home_bot.proto.messages import Address, make_event
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.tasks import protocol as task_protocol


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        # Отдельно — с reply_to_message_id, для тестов task_prewake/task_result
        # (остальные тесты этого файла реплаем не пользуются).
        self.sent_full: list[tuple[int, str, int | None]] = []
        self.reply_markups: list[object] = []

    async def send_direct(self, chat_id, text, reply_to_message_id=None, reply_markup=None):
        self.sent.append((chat_id, text))
        self.sent_full.append((chat_id, text, reply_to_message_id))
        self.reply_markups.append(reply_markup)
        return 99


class FakeStore:
    def __init__(self) -> None:
        self.recorded_turns: list[tuple] = []

    async def record_ai_turn(self, *args, **kwargs):
        self.recorded_turns.append((args, kwargs))


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


# --- update_finished: самообновление ноды (без рестарта — только диагностика) ---


def test_render_update_finished_success_mentions_restart_node():
    text = render_update_finished("arch-t480", True, "0.22.0", None)
    assert "arch-t480" in text and "0.22.0" in text
    assert "restart_node" in text


def test_render_update_finished_failure_shows_error():
    text = render_update_finished("arch-t480", False, None, "network unreachable")
    assert "arch-t480" in text
    assert "network unreachable" in text
    assert "не удалось" in text


async def test_handler_broadcasts_on_update_finished_success():
    book = _book()
    notifier = FakeNotifier()
    store = FakeStore()
    handler = build_node_event_handler(book, notifier, store)

    # Событие описывает саму себя: src — та нода, что обновилась (в отличие
    # от node_joined, где src — сосед, принявший присоединение).
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


# --- llm_speech_cured: Логопед долечил Альфреда — всем, включая гостей без
# opt-in (в отличие от system-событий через broadcast_system) ---


async def test_handler_broadcasts_speech_cured_to_everyone_including_guests():
    book = SubscriptionBook(
        [
            # Обычная подписка без "system"/"*" в event_types — accepting()
            # её бы не нашла, broadcast_all находит.
            Subscription(name="plain", chat_id=1, event_types=frozenset({"overheat_started"})),
            # Гостевая — event_types пуст по умолчанию (нет opt-in вообще).
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


# --- task_prewake/task_result: служба tasks (см. tasks/protocol.py) ---

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

    assert notifier.sent_full == [(7, "<b>Альфред:</b> Полил цветы, сэр", 501)]
    assert len(store.recorded_turns) == 1
    args, _kwargs = store.recorded_turns[0]
    assert args[:4] == (7, 99, 500, "assistant")  # 99 — message_id, вернул FakeNotifier
    assert args[4] == "Полил цветы, сэр"


async def test_task_result_failure_sends_albert_task_missed_as_reply():
    book, notifier, store = _book(), FakeNotifier(), FakeStore()
    handler = build_node_event_handler(book, notifier, store)
    env = make_event(
        task_protocol.EVENT_TASK_RESULT,
        {"task_id": 1, "meta": _LLM_CHAT_META, "ok": False, "error": "not warm"},
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent_full == [(7, ALBERT_TASK_MISSED, 501)]
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


async def test_handler_broadcasts_on_node_leaving_and_returned():
    """Этап 23: штатный уход и возвращение — системные уведомления для
    любого типа машины (в отличие от аварийных node_down/node_up)."""
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


# --- idle_power_blocked: автовыключение отложено открытой SSH-сессией ------


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
    handler = build_node_event_handler(book, notifier, FakeStore())

    await handler(
        make_event(
            "idle_power_blocked",
            {"node": "mycraft", "sessions": ["sevboa, pts/0, с 01:08"]},
            src=Address(node="mycraft", service="node"),
        )
    )

    expected_text = render_idle_power_blocked("mycraft", ["sevboa, pts/0, с 01:08"])
    assert notifier.sent == [(999, expected_text)]
    assert notifier.reply_markups == [build_close_ssh_keyboard("mycraft")]


async def test_handler_ignores_idle_power_blocked_without_sessions():
    """Пустой список — событие бы эмитилось только если сессии есть
    (node/service.py::maybe_auto_poweroff_idle), но handler защищается и
    от пустых/битых данных, как и у остальных событий этого модуля."""
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
    """`_book()` (не admin) не должна получить это — WILDCARD в allowed_commands
    нужен именно для notify_admins, event_types="*" тут ни при чём."""
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
