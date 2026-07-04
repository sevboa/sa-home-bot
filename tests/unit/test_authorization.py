from types import SimpleNamespace

from sa_home_bot.bot.middlewares import (
    DENIED_TEXT,
    AuthorizationMiddleware,
    CallbackAuthorizationMiddleware,
    extract_command,
)
from sa_home_bot.config import SubscriptionConfig
from sa_home_bot.subscriptions.book import SubscriptionBook


def _book():
    return SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="me", chat_id=1, allowed_commands=["status"]),
            SubscriptionConfig(name="broken", chat_id=2, allowed_commands=["status"]),
        ]
    )


def _message(chat_id: int, text: str):
    answered: list[str] = []

    async def answer(t):
        answered.append(t)

    msg = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        text=text,
        answer=answer,
        _answered=answered,
    )
    return msg


async def _passthrough(event, data):
    return "HANDLED"


def test_extract_command():
    assert extract_command("/status") == "status"
    assert extract_command("/status@MyBot arg") == "status"
    assert extract_command("hello") is None
    assert extract_command(None) is None


async def test_universal_command_always_passes():
    mw = AuthorizationMiddleware(_book())
    msg = _message(999, "/ping")  # неподписной чат
    assert await mw(_passthrough, msg, {}) == "HANDLED"


async def test_control_command_allowed():
    mw = AuthorizationMiddleware(_book())
    msg = _message(1, "/status")
    data: dict = {}
    assert await mw(_passthrough, msg, data) == "HANDLED"
    assert data["subscription"].name == "me"


async def test_control_command_without_right_denied():
    mw = AuthorizationMiddleware(_book())
    msg = _message(1, "/scan_now")  # нет в allowed_commands
    result = await mw(_passthrough, msg, {})
    assert result is None
    assert msg._answered == [DENIED_TEXT]


async def test_control_command_unsubscribed_chat_denied():
    mw = AuthorizationMiddleware(_book())
    msg = _message(999, "/status")
    assert await mw(_passthrough, msg, {}) is None
    assert msg._answered == [DENIED_TEXT]


async def test_control_command_broken_chat_denied():
    book = _book()

    class FailBot:
        async def get_chat(self, chat_id):
            if chat_id == 2:
                raise RuntimeError("down")

    await book.validate_on_startup(FailBot())
    mw = AuthorizationMiddleware(book)
    msg = _message(2, "/status")
    assert await mw(_passthrough, msg, {}) is None
    assert msg._answered == [DENIED_TEXT]


# --- callback-кнопки под /status ---


def _callback(chat_id: int | None, data: str):
    alerts: list[tuple[str, bool]] = []

    async def answer(text=None, show_alert=False):
        alerts.append((text, show_alert))

    message = SimpleNamespace(chat=SimpleNamespace(id=chat_id)) if chat_id is not None else None
    return SimpleNamespace(data=data, message=message, answer=answer, _alerts=alerts)


def _cb_book():
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=1, allowed_commands=["status", "downtime"])]
    )


async def test_callback_allowed_action_passes():
    mw = CallbackAuthorizationMiddleware(_cb_book())
    cb = _callback(1, "st:downtime")  # право есть
    data: dict = {}
    assert await mw(_passthrough, cb, data) == "HANDLED"
    assert data["subscription"].name == "me"


async def test_callback_action_without_right_denied():
    mw = CallbackAuthorizationMiddleware(_cb_book())
    cb = _callback(1, "st:scan")  # scan_now не в allowed_commands
    assert await mw(_passthrough, cb, {}) is None
    assert cb._alerts and cb._alerts[0][1] is True  # show_alert


async def test_callback_unknown_data_passes_through():
    mw = CallbackAuthorizationMiddleware(_cb_book())
    cb = _callback(1, "other:thing")
    assert await mw(_passthrough, cb, {}) == "HANDLED"
