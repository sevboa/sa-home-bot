from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from sa_home_bot.utils.ids import short_token

ItemT = TypeVar("ItemT")


class PendingStore(Generic[ItemT]):
    def __init__(
        self, maxsize: int, token_factory: Callable[[], str] = short_token
    ) -> None:
        self._maxsize = maxsize
        self._token_factory = token_factory
        self._items: dict[str, ItemT] = {}

    def add(self, item: ItemT) -> str:
        token = self._token_factory()
        self._items[token] = item
        if len(self._items) > self._maxsize:
            self._items.pop(next(iter(self._items)))
        return token

    def get(self, token: str) -> ItemT | None:
        return self._items.get(token)

    def pop(self, token: str) -> ItemT | None:
        return self._items.pop(token, None)

    def __len__(self) -> int:
        return len(self._items)
