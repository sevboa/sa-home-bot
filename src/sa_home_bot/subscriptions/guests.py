from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.toml_emit import toml_string, toml_value
from sa_home_bot.utils.files import atomic_write

log = logging.getLogger(__name__)

HEADER = """\
# Гостевые подписки бота — ФАЙЛ ВЕДЁТ САМ БОТ.
#
# Записи появляются здесь, когда кто-то гасит инвайт-код (/invite), и
# исчезают при отзыве (/guests). Файл перезаписывается целиком, поэтому
# ручные правки и комментарии в нём не сохранятся — владельческие подписки
# правьте в основном пакете instances/telegram-bot.<инстанс>.toml.
#
# Реплицируется по рою наравне с основным пакетом (сателлит), так что гости
# переживают переезд бота на резервную ноду.
"""


def render(guests: Sequence[Subscription]) -> bytes:
    lines = [HEADER]
    for guest in sorted(guests, key=lambda g: g.chat_id):
        lines.append("[[guest_subscriptions]]")
        lines.append(f"name = {toml_string(guest.name)}")
        lines.append(f"chat_id = {guest.chat_id}")
        lines.append(f"event_types = {toml_value(sorted(guest.event_types))}")
        lines.append(f"allowed_commands = {toml_value(sorted(guest.allowed_commands))}")
        lines.append(f"invited_by_chat_id = {guest.invited_by_chat_id}")
        lines.append(f"invited_at = {toml_string(guest.invited_at)}")
        lines.append(f"invited_user = {toml_string(guest.invited_user)}")
        lines.append("")
    return ("\n".join(lines)).encode("utf-8")


class GuestStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    @property
    def available(self) -> bool:
        return self.path is not None

    def save(self, guests: Sequence[Subscription]) -> bool:
        if self.path is None:
            return False
        atomic_write(self.path, render(guests))
        log.info("Гостевой пакет %s переписан: %d записей", self.path, len(guests))
        return True
