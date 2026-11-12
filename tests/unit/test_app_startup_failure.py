from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError

import sa_home_bot.app as app_module
from sa_home_bot.app import run
from sa_home_bot.config import (
    DatabaseConfig,
    MonitorConfig,
    Settings,
    SubscriptionConfig,
    TelegramConfig,
)
from sa_home_bot.db.connection import Database


def _failing(exc: Exception):
    async def fail(*_args, **_kwargs):
        raise exc

    return fail


class FakeSession:
    def __init__(self, error: Exception | None = None) -> None:
        self.closed = False
        self.error = error

    def middleware(self, middleware):
        return None

    async def close(self):
        self.closed = True
        if self.error is not None:
            raise self.error


class FakeBot:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def get_me(self, request_timeout=None):
        return SimpleNamespace(username="fakebot")


class SpyDatabase(Database):
    def __init__(self, path, opened: list, error: Exception | None = None) -> None:
        super().__init__(path)
        self.closed = False
        self.error = error
        opened.append(self)

    async def close(self):
        self.closed = True
        if self.error is not None:
            raise self.error
        await super().close()


def _settings(db_path) -> Settings:
    return Settings(
        telegram=TelegramConfig(token="x"),
        database=DatabaseConfig(path=db_path),
        monitor=MonitorConfig(socket=str(db_path.parent / "monitor.sock")),
        subscriptions=[SubscriptionConfig(name="me", chat_id=1, event_types=["*"])],
    )


async def test_startup_failure_closes_session_and_db_and_reraises(tmp_path, monkeypatch):
    opened: list[SpyDatabase] = []
    session = FakeSession()
    error = TelegramNetworkError(method=None, message="bad gateway")
    monkeypatch.setattr(app_module, "Database", lambda path: SpyDatabase(path, opened))
    monkeypatch.setattr(app_module, "build_bot", lambda token, proxy="": FakeBot(session))
    monkeypatch.setattr(app_module, "call_with_network_retry", _failing(error))

    with pytest.raises(TelegramNetworkError) as raised:
        await run(_settings(tmp_path / "app.sqlite"))

    assert raised.value is error
    assert session.closed is True
    assert opened[0].closed is True


async def test_startup_close_error_does_not_mask_original(tmp_path, monkeypatch):
    opened: list[SpyDatabase] = []
    error = TelegramNetworkError(method=None, message="bad gateway")
    session = FakeSession(error=RuntimeError("сессия не закрылась"))
    monkeypatch.setattr(
        app_module,
        "Database",
        lambda path: SpyDatabase(path, opened, error=RuntimeError("БД не закрылась")),
    )
    monkeypatch.setattr(app_module, "build_bot", lambda token, proxy="": FakeBot(session))
    monkeypatch.setattr(app_module, "call_with_network_retry", _failing(error))

    with pytest.raises(TelegramNetworkError) as raised:
        await run(_settings(tmp_path / "app.sqlite"))

    assert raised.value is error
    assert session.closed is True
    assert opened[0].closed is True


async def test_startup_failure_before_bot_closes_db(tmp_path, monkeypatch):
    opened: list[SpyDatabase] = []
    error = RuntimeError("миграции не прошли")
    monkeypatch.setattr(app_module, "Database", lambda path: SpyDatabase(path, opened))
    monkeypatch.setattr(app_module, "apply_migrations", _failing(error))

    with pytest.raises(RuntimeError) as raised:
        await run(_settings(tmp_path / "app.sqlite"))

    assert raised.value is error
    assert opened[0].closed is True
