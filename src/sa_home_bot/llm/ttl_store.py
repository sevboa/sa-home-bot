from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Generic, TypeVar

from sa_home_bot.utils.clock import utc_now

V = TypeVar("V")


class TtlStore(Generic[V]):
    def __init__(self, ttl_s: float, clock: Callable[[], datetime] = utc_now) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._values: dict[str, V] = {}
        self._touched: dict[str, datetime | None] = {}

    def put(self, key: str, value: V, *, touched: bool = True) -> None:
        self._values[key] = value
        self._touched[key] = self._clock() if touched else None

    def touch(self, key: str) -> None:
        if key in self._values:
            self._touched[key] = self._clock()

    def get(self, key: str) -> V | None:
        return self._values.get(key)

    def pop(self, key: str) -> V | None:
        self._touched.pop(key, None)
        return self._values.pop(key, None)

    def __contains__(self, key: object) -> bool:
        return key in self._values

    def __getitem__(self, key: str) -> V:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def sweep(self) -> None:
        now = self._clock()
        stale = [
            key
            for key, touched in self._touched.items()
            if touched is not None and (now - touched).total_seconds() >= self._ttl_s
        ]
        for key in stale:
            self.pop(key)
