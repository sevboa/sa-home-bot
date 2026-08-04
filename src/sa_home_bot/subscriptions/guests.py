"""Гостевой пакет: подписки, выданные инвайт-кодами (AUTHORIZATION.md §10).

Почему отдельный файл, а не БД бота: гость обязан пережить переезд
службы-синглтона на резервную ноду, а БД не реплицируется — гость молча
исчез бы после failover. Пакет же реплицируется штатно (`node/instances.py`,
`node/replication.py`) — как сателлит основного пакета инстанса:
``instances/telegram-bot.<инстанс>.guests.toml``.

Почему не дописывать в основной пакет: тот правит человек, и второй писатель
в нём — это и риск затереть комментарии владельца, и расхождение ревизий,
если правку сделали в двух местах разом.

Файл целиком ведёт бот и **перезаписывает целиком**, а не дописывает: тогда
отзыв гостя это просто запись без его блока, а не выборочная правка чужого
текста. Отсюда и шапка-предупреждение в самом файле.

TOML пишем вручную, без библиотеки: типов ровно три (строка, целое, список
строк), а тянуть в зависимости писатель TOML ради двух десятков строк — цена
выше пользы. Читает файл обычный pydantic-settings (config.py).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from sa_home_bot.node.instances import atomic_write
from sa_home_bot.subscriptions.models import Subscription

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


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def _toml_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_string(v) for v in sorted(values)) + "]"


def render(guests: Sequence[Subscription]) -> bytes:
    """Гостевые подписки → содержимое пакета (детерминированное).

    Порядок и сортировка списков фиксированы намеренно: пакет реплицируется
    по хешу содержимого, и одна и та же выдача не должна давать разные байты
    (иначе ревизия росла бы на ровном месте).
    """
    lines = [HEADER]
    for guest in sorted(guests, key=lambda g: g.chat_id):
        lines.append("[[guest_subscriptions]]")
        lines.append(f"name = {_toml_string(guest.name)}")
        lines.append(f"chat_id = {guest.chat_id}")
        lines.append(f"event_types = {_toml_list(guest.event_types)}")
        lines.append(f"allowed_commands = {_toml_list(guest.allowed_commands)}")
        lines.append(f"invited_by_chat_id = {guest.invited_by_chat_id}")
        lines.append(f"invited_at = {_toml_string(guest.invited_at)}")
        lines.append(f"invited_user = {_toml_string(guest.invited_user)}")
        lines.append(f"family = {str(guest.family).lower()}")
        lines.append("")
    return ("\n".join(lines)).encode("utf-8")


class GuestStore:
    """Запись гостевого пакета. Без пути (нет инстанса) — молча ничего не умеет."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    @property
    def available(self) -> bool:
        return self.path is not None

    def save(self, guests: Sequence[Subscription]) -> bool:
        """Переписать пакет под текущий состав гостей. False — писать некуда.

        Пустой состав — тоже запись (файл остаётся, но без блоков): удалять
        его незачем, а вот исчезновение файла соседи бы не заметили —
        репликация сравнивает ревизии существующих пакетов.
        """
        if self.path is None:
            return False
        atomic_write(self.path, render(guests))
        log.info("Гостевой пакет %s переписан: %d записей", self.path, len(guests))
        return True
