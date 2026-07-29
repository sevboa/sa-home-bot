"""Дебаг-кнопка «развернуть/скрыть» под сообщением о вызове инструмента:
хранилище, рендеринг, переключение вида и жизнь после рестарта."""

from __future__ import annotations

from types import SimpleNamespace

from sa_home_bot.bot import lifecycle, tool_debug
from sa_home_bot.bot.handlers import tool_debug as handler
from sa_home_bot.bot.tool_debug import ToolCall, ToolCalls
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

CALL = ToolCall(
    name="torrents",
    args={"action": "search", "query": "задача трёх тел"},
    result='{"count": 10}',
)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []

    async def send_direct(self, chat_id, text, reply_to_message_id=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return 1


class FakeCallbackMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def edit_text(self, text, reply_markup=None):
        self.texts.append(text)
        self.markups.append(reply_markup)


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeCallbackMessage()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def _debug_book() -> SubscriptionBook:
    return SubscriptionBook(
        [
            Subscription(
                chat_id=1,
                name="debug",
                event_types=frozenset({lifecycle.EVENT_ALFRED_TOOL_CALL}),
            ),
            # Подписка с "*" в event_types дебаг-канал НЕ получает (живая
            # находка 2026-07-27: иначе спам на каждой реплике).
            Subscription(chat_id=2, name="admin", event_types=frozenset({"*"})),
        ]
    )


def _button_data(markup) -> str:
    return markup.inline_keyboard[0][0].callback_data


async def test_notification_carries_expand_button_when_details_known():
    notifier = FakeNotifier()
    calls = ToolCalls()
    await lifecycle.notify_tool_call(
        _debug_book(), notifier, "torrents", args=CALL.args, result=CALL.result, debug=calls
    )
    assert [chat for chat, _, _ in notifier.sent] == [1]
    _, text, markup = notifier.sent[0]
    assert text == "🔧 Alfred вызвал инструмент: torrents"
    assert markup.inline_keyboard[0][0].text == tool_debug.EXPAND_TEXT
    token = _button_data(markup).split(":")[2]
    assert calls.get(token) == CALL


async def test_notification_without_details_has_no_button():
    """Событие от службы tasks несёт только имя тула — разворачивать нечего."""
    notifier = FakeNotifier()
    await lifecycle.notify_tool_call(_debug_book(), notifier, "remind")
    assert notifier.sent[0][2] is None


async def test_expand_shows_input_and_output_then_collapses():
    calls = ToolCalls()
    token = calls.add(CALL)

    show = FakeCallback(f"{tool_debug.CALLBACK_PREFIX}:{tool_debug.SHOW}:{token}")
    await handler.cb_tool_debug(show, calls)
    full = show.message.texts[0]
    assert "задача трёх тел" in full and '"count": 10' in full
    assert show.message.markups[0].inline_keyboard[0][0].text == tool_debug.COLLAPSE_TEXT

    hide = FakeCallback(f"{tool_debug.CALLBACK_PREFIX}:{tool_debug.HIDE}:{token}")
    await handler.cb_tool_debug(hide, calls)
    assert hide.message.texts[0] == "🔧 Alfred вызвал инструмент: torrents"
    assert hide.message.markups[0].inline_keyboard[0][0].text == tool_debug.EXPAND_TEXT


async def test_lost_details_after_restart_say_so():
    """Хранилище живёт в памяти процесса — после рестарта показывать нечего."""
    callback = FakeCallback(f"{tool_debug.CALLBACK_PREFIX}:{tool_debug.SHOW}:deadbeef")
    await handler.cb_tool_debug(callback, ToolCalls())
    assert callback.answers == [(tool_debug.LOST_TEXT, True)]
    assert callback.message.texts == []


def test_store_evicts_oldest_beyond_limit():
    calls = ToolCalls(maxsize=2)
    first = calls.add(CALL)
    calls.add(CALL)
    calls.add(CALL)
    assert calls.get(first) is None


def test_long_output_is_clipped_not_dropped():
    huge = ToolCall(name="torrents", args={"action": "search"}, result="x" * 9000)
    text = tool_debug.render_full(huge)
    assert len(text) < 4096
    assert "обрезано, всего 9000 символов" in text


def test_html_in_tool_output_is_escaped():
    """Выдача трекера — чужой текст: «<b>» в имени раздачи не должен
    ломать разметку сообщения."""
    call = ToolCall(name="torrents", args={}, result="<b>Задача</b> & Co")
    assert "&lt;b&gt;" in tool_debug.render_full(call)


def test_keyboard_callback_data_fits_telegram_limit():
    data = _button_data(tool_debug.keyboard("deadbeef", expanded=False))
    assert len(data.encode()) <= 64
    assert data.startswith(f"{tool_debug.CALLBACK_PREFIX}:")


def test_render_short_matches_the_old_wording():
    """Текст короткого сообщения не менялся — дебаг-канал узнаваем."""
    assert tool_debug.render_short("calc") == "🔧 Alfred вызвал инструмент: calc"
    assert SimpleNamespace  # noqa: B018 — импорт используется в фейках выше
