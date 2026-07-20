"""Торренты, ожидающие выбора директории (карточка «куда сохранить?»).

Состояние живёт от «прислали файл» до «нажали кнопку» — секунды разговора,
не бизнес-данные: в БД смысла нет, переживать рестарт бота незачем.
Ограниченный по размеру словарь с вытеснением самого старого — тот же
приём, что и `SeenEvents` (node/app.py), не эксклюзивный для событий.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

_MAX_PENDING = 128


@dataclass(frozen=True)
class PendingTorrent:
    source: str  # magnet-ссылка/URL или base64 .torrent-файла
    name: str


class PendingTorrents:
    def __init__(self, maxsize: int = _MAX_PENDING) -> None:
        self._maxsize = maxsize
        self._items: dict[str, PendingTorrent] = {}

    def add(self, item: PendingTorrent) -> str:
        token = uuid.uuid4().hex[:8]
        self._items[token] = item
        if len(self._items) > self._maxsize:
            self._items.pop(next(iter(self._items)))
        return token

    def pop(self, token: str) -> PendingTorrent | None:
        return self._items.pop(token, None)
