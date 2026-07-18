"""/nodes → «Проверить обновление»: живой баг 2026-07-18.

`check_update` не меняет ничего в `get_state()` (в отличие от `update`,
который переустанавливает пакет), поэтому обычный редрайв карточки после
действия ничего не показывал — результат команды (последняя версия vs
установленная) тихо терялся, и пользователю казалось, что кнопка не отвечает.
"""

import pytest_asyncio

from sa_home_bot.bot.handlers.node import on_dynamic_action
from sa_home_bot.config import Settings, TelegramConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ActionSpec, ServiceDescription, ServiceInfo
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
    def __init__(self, data: str) -> None:
        self.data = data
        self.id = "cb-1"
        self.message = FakeMessage()
        self.answered: list = []

    async def answer(self, *args, **kwargs):
        self.answered.append(args)


class FakeNodeLink:
    display_name = "нода"
    connected = True

    def __init__(self, check_update_result: dict) -> None:
        self._result = check_update_result
        self.commands: list[tuple[str, dict]] = []

    async def command(self, action, args=None, dst=None):
        self.commands.append((action, args or {}))
        return self._result

    async def get_state(self, dst=None):
        return {"node": "alfred", "version": "0.24.5", "services": [], "peers": []}

    async def describe(self, dst=None):
        return ServiceDescription(
            info=ServiceInfo(node="alfred", service="node", version="0.24.5"),
            capabilities=(),
            actions=(ActionSpec(id="check_update", title="🔄 Проверить обновление"),),
        )

    async def actions(self):
        return (await self.describe()).actions


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
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset({"check_update@node"}))


async def _dispatch(callback, store, link):
    await on_dynamic_action(
        callback,
        store=store,
        node_link=link,
        apps_link=link,
        config=_settings(),
        subscription=_sub(),
    )


async def test_check_update_sends_message_when_update_available(store):
    link = FakeNodeLink(
        {"repo": "x", "running": "0.24.4", "installed": "0.24.4", "latest": "0.24.5"}
    )
    callback = FakeCallback("act:node:check_update")
    await _dispatch(callback, store, link)

    assert len(callback.message.answers) == 1
    assert "0.24.5" in callback.message.answers[0]
    assert callback.message.edits == []  # get_state() не меняется — карточку не трогаем


async def test_check_update_sends_message_when_up_to_date(store):
    link = FakeNodeLink(
        {"repo": "x", "running": "0.24.5", "installed": "0.24.5", "latest": "0.24.5"}
    )
    callback = FakeCallback("act:node:check_update")
    await _dispatch(callback, store, link)

    assert len(callback.message.answers) == 1
    assert "Обновлений нет" in callback.message.answers[0]
