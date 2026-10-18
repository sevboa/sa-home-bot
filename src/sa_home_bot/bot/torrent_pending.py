from __future__ import annotations

from dataclasses import dataclass

from sa_home_bot.utils.pending import PendingStore

MAX_PENDING = 128


@dataclass(frozen=True)
class PendingTorrent:
    source: str
    name: str


class PendingTorrents(PendingStore[PendingTorrent]):
    def __init__(self, maxsize: int = MAX_PENDING) -> None:
        super().__init__(maxsize)
