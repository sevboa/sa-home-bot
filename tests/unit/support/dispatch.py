from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Dispatcher
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


@contextmanager
def prod_dispatcher(book, gate, store=None) -> Iterator[Dispatcher]:
    from sa_home_bot.bot.setup import build_dispatcher

    dp = build_dispatcher(book, gate, store)
    try:
        yield dp
    finally:
        for child in list(dp.sub_routers):
            child._parent_router = None
        dp.sub_routers.clear()
