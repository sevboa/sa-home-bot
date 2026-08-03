"""Приватные ключи VPN, ещё не отданные файлом, — держим В ПАМЯТИ ЭТОГО
ПРОЦЕССА и только до первого запроса файла (кнопка «Дать файл конфига») или
TTL (``[vpn].config_message_ttl_s``), никогда в БД: vpn/service.py и так не
хранит приватный ключ (см. его докстринг) — секрет существует только на пути
от генерации до вручения гостю.

QR, отправленный сразу вместе с этим (bot/handlers/vpn.py::_send_secret),
уже несёт тот же секрет в сканируемом виде — отложенная выдача файла не
добавляет отдельного уровня защиты сама по себе, только удобство (не
спамить гостя сразу двумя сообщениями подряд, дать выбор способа).

Токен в callback_data — короткий (``secrets.token_urlsafe``), а не сам
секрет: Telegram ограничивает callback_data 64 байтами, туда весь конфиг не
влезает.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class PendingVpnSecret:
    config_text: str
    device_label: str


class PendingVpnSecrets:
    """Реестр процесса — не переживает рестарт (как и всё остальное
    в оперативной памяти бота): после рестарта старые токены просто не
    находятся, гость увидит «ссылка устарела» и попросит доступ заново."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[PendingVpnSecret, float]] = {}

    def put(self, config_text: str, device_label: str, ttl_s: float) -> str:
        token = secrets.token_urlsafe(6)
        self._items[token] = (PendingVpnSecret(config_text, device_label), time.monotonic() + ttl_s)
        return token

    def pop(self, token: str) -> PendingVpnSecret | None:
        """Забрать и удалить — файл по одной ссылке отдаём ровно один раз."""
        entry = self._items.pop(token, None)
        if entry is None:
            return None
        secret, expires_at = entry
        if time.monotonic() > expires_at:
            return None
        return secret

    def discard(self, token: str) -> None:
        self._items.pop(token, None)
