"""Smoke-тест жизненного цикла app.run без сети (Bot/Dispatcher замоканы)."""

import asyncio
from types import SimpleNamespace

import sentinel_bot.app as app_module
from sentinel_bot.app import STATE_CLEAN_SHUTDOWN, run
from sentinel_bot.config import (
    DatabaseConfig,
    Settings,
    SubscriptionConfig,
    TelegramConfig,
)
from sentinel_bot.db.connection import Database
from sentinel_bot.db.migrations import apply_migrations
from sentinel_bot.db.store import Store


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

        async def _close():
            return None

        self.session = SimpleNamespace(middleware=lambda m: None, close=_close)

    async def get_chat(self, chat_id):
        return object()

    async def set_my_commands(self, commands, scope=None):
        return True

    async def send_message(self, chat_id, text, reply_parameters=None):
        self.sent.append(text)
        return SimpleNamespace(message_id=1)


class FakeDispatcher:
    def __init__(self) -> None:
        self._stop = asyncio.Event()

    async def start_polling(self, bot, **kwargs):
        await self._stop.wait()

    async def stop_polling(self):
        self._stop.set()


class ImmediateLifespan:
    def install_signal_handlers(self):
        pass

    async def wait(self):
        return None


async def test_app_boots_and_shuts_down_cleanly(tmp_path, monkeypatch):
    db_path = tmp_path / "app.sqlite"
    fake_bot = FakeBot()

    monkeypatch.setattr(app_module, "build_bot", lambda token: fake_bot)
    monkeypatch.setattr(app_module, "build_dispatcher", lambda book: FakeDispatcher())
    monkeypatch.setattr(app_module, "Lifespan", ImmediateLifespan)

    settings = Settings(
        telegram=TelegramConfig(token="x"),
        database=DatabaseConfig(path=db_path),
        subscriptions=[
            SubscriptionConfig(name="me", chat_id=1, event_types=["*"]),
        ],
    )

    await asyncio.wait_for(run(settings), timeout=10)

    # Приветствие (старт) и прощание (shutdown) должны уйти подписчику.
    assert any("посту" in t or "сбоя" in t for t in fake_bot.sent)
    assert any("офлайн" in t for t in fake_bot.sent)

    # Флаг чистого завершения выставлен.
    db = Database(db_path)
    await db.open()
    await apply_migrations(db)
    store = Store(db)
    assert await store.get_state(STATE_CLEAN_SHUTDOWN) == "1"
    await db.close()
