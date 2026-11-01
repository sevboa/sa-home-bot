from __future__ import annotations

import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Chat, Message, User

BOT_USER = User(id=4242, is_bot=True, first_name="Альфред", username="alfred_test_bot")


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self._next_id = 1000

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage):
            self._next_id += 1
            return Message(
                message_id=self._next_id,
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=BOT_USER,
                text=method.text,
            )
        return True

    async def stream_content(
        self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True
    ):
        yield b""

    async def close(self) -> None:
        return None

    def sent(self, kind: type[TelegramMethod]) -> list[Any]:
        return [request for request in self.requests if isinstance(request, kind)]


def recording_bot() -> tuple[Bot, RecordingSession]:
    session = RecordingSession()
    return Bot("123456:fake-token-for-tests", session=session), session


def _module_routers() -> Iterator[Router]:
    from sa_home_bot.bot import setup

    for module in vars(setup).values():
        if inspect.ismodule(module):
            for value in vars(module).values():
                if isinstance(value, Router):
                    yield value


def _detach_from_dispatchers() -> None:
    for router in _module_routers():
        parent = router._parent_router
        if isinstance(parent, Dispatcher):
            parent.sub_routers.remove(router)
            router._parent_router = None


@contextmanager
def prod_dispatcher(book, gate, store=None) -> Iterator[Dispatcher]:
    from sa_home_bot.bot.setup import build_dispatcher

    try:
        yield build_dispatcher(book, gate, store)
    finally:
        _detach_from_dispatchers()
