"""Само-рестарт telegram-bot из его же карточки: прощание вместо ошибки.

Нода, получив restart своей службы telegram-bot, убивает сам процесс бота —
ответа не дождаться. Раньше умирающий бот успевал отправить «⚠️ Нода
недоступна»; теперь прощается заранее и глотает ошибку рвущегося линка.
"""

import pytest_asyncio

from sa_home_bot.bot.handlers.node import (
    SELF_RESTART_CB_KEY,
    SELF_RESTART_TEXT,
    SELF_STOP_TEXT,
    on_dynamic_action,
)
from sa_home_bot.bot.node_view import NODE_DOWN_TEXT
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import Settings, TelegramConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.models import Subscription


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.edits: list[str] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class FakeCallback:
    def __init__(self, data: str, cb_id: str = "cb-1") -> None:
        self.data = data
        self.id = cb_id
        self.message = FakeMessage()
        self.answered: list = []

    async def answer(self, *args, **kwargs):
        self.answered.append(args)


class DyingNodeLink:
    """Линк, у которого команда рвётся (бот умирает, не дождавшись ответа)."""

    display_name = "нода"
    connected = True

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []

    async def command(self, action, args=None, dst=None):
        self.commands.append((action, args or {}))
        raise ServiceUnavailableError("клиент закрыт")

    async def get_state(self, dst=None):
        raise ServiceUnavailableError("нет связи")

    async def describe(self, dst=None):
        return None

    async def actions(self):
        return ()


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _settings() -> Settings:
    return Settings(telegram=TelegramConfig(token="x"), subscriptions=[])


def _sub() -> Subscription:
    return Subscription(
        chat_id=1,
        name="me",
        allowed_commands=frozenset({"restart@node", "stop@node", "restart_node@node"}),
    )


async def _dispatch(callback, store, link):
    await on_dynamic_action(
        callback,
        store=store,
        node_link=link,
        apps_link=link,
        config=_settings(),
        subscription=_sub(),
    )


async def test_self_restart_says_goodbye_not_node_down(store):
    link = DyingNodeLink()
    callback = FakeCallback("act:node:restart:telegram-bot")
    await _dispatch(callback, store, link)

    assert callback.message.answers == [SELF_RESTART_TEXT]
    assert NODE_DOWN_TEXT not in callback.message.answers
    assert callback.message.edits == []  # карточка не перерисовывается
    assert link.commands == [("restart", {"name": "telegram-bot"})]


async def test_self_stop_message_mentions_nodectl(store):
    link = DyingNodeLink()
    callback = FakeCallback("act:node:stop:telegram-bot")
    await _dispatch(callback, store, link)
    assert callback.message.answers == [SELF_STOP_TEXT]
    assert "nodectl" in SELF_STOP_TEXT


async def test_restart_node_of_own_node_is_self_shutdown(store):
    link = DyingNodeLink()
    callback = FakeCallback("act:node:restart_node")
    await _dispatch(callback, store, link)
    assert callback.message.answers == [SELF_RESTART_TEXT]
    assert link.commands == [("restart_node", {})]


async def test_replayed_callback_after_restart_is_ignored(store):
    # Telegram мог не подтвердить offset до смерти бота — тот же callback
    # приезжает повторно после рестарта и НЕ должен перезапустить бота снова.
    link = DyingNodeLink()
    first = FakeCallback("act:node:restart:telegram-bot", cb_id="same-id")
    await _dispatch(first, store, link)
    assert await store.get_state(SELF_RESTART_CB_KEY) == "same-id"

    replayed = FakeCallback("act:node:restart:telegram-bot", cb_id="same-id")
    await _dispatch(replayed, store, link)
    assert replayed.message.answers == []  # молча подтверждён, команд не было
    assert len(link.commands) == 1


async def test_peer_telegram_bot_restart_goes_normal_path(store):
    # Рестарт ЧУЖОГО telegram-bot (node_id задан) — обычная ветка с перерисовкой
    # (здесь линк мёртв, поэтому честная ошибка недоступности пира, не прощание).
    link = DyingNodeLink()
    callback = FakeCallback("act:node:restart:telegram-bot:arch-t480")
    await _dispatch(callback, store, link)
    assert callback.message.answers != [SELF_RESTART_TEXT]
