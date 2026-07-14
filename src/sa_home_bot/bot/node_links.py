"""Ссылки-команды навигации: /node_<id> и /svc_<нода>_<служба>.

Telegram делает команды в тексте кликабельными — списки нод и служб не
раздувают клавиатуру кнопками, а несут ссылку прямо в строке (карточка
открывается новым сообщением; отредактировать то же сообщение умеют только
inline-кнопки, они остаются за действиями).

Имена нод и служб содержат дефисы (arch-t480, telegram-bot), команды
Telegram — только [a-z0-9_] до 32 символов: при генерации `-` → `_`,
непредставимые имена ссылку не получают. Обратный разбор неоднозначен
(`/svc_arch_t480_telegram_bot`) — он ведётся сопоставлением с реальными
данными роя: длиннейший нормализованный префикс — нода, остаток — служба.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_VALID_COMMAND = re.compile(r"^[a-z0-9_]+$")
_MAX_COMMAND_LEN = 32  # лимит Telegram на имя команды

NODE_PREFIX = "node_"
SVC_PREFIX = "svc_"


def normalize(name: str) -> str | None:
    """Имя ноды/службы → часть имени команды; None, если непредставимо."""
    norm = name.lower().replace("-", "_")
    return norm if _VALID_COMMAND.fullmatch(norm) else None


def _command(prefix: str, *parts: str) -> str | None:
    norms = [normalize(p) for p in parts]
    if any(n is None for n in norms):
        return None
    name = prefix + "_".join(norms)  # type: ignore[arg-type]
    return f"/{name}" if len(name) <= _MAX_COMMAND_LEN else None


def node_command(node_id: str) -> str | None:
    """Ссылка на карточку ноды: `/node_arch_t480` (None — непредставимо)."""
    return _command(NODE_PREFIX, node_id)


def svc_command(node_id: str, service: str) -> str | None:
    """Ссылка на карточку службы: `/svc_alfred_telegram_bot`."""
    return _command(SVC_PREFIX, node_id, service)


def resolve_node(arg: str, known_ids: Sequence[str]) -> str | None:
    """`node_…`-аргумент → реальный id ноды (точное совпадение normalized)."""
    return next((nid for nid in known_ids if normalize(nid) == arg), None)


def resolve_svc_candidates(arg: str, known_ids: Sequence[str]) -> list[tuple[str, str]]:
    """`svc_…`-аргумент → кандидаты (id ноды, нормализованный хвост-служба).

    Разбор неоднозначен (и нода, и служба могут содержать `_` после
    нормализации), поэтому кандидаты идут от длиннейшего префикса-ноды к
    короткому — вызывающий сверяет хвост со службами конкретной ноды и
    берёт первый совпавший.
    """
    candidates: list[tuple[str, str]] = []
    for nid in known_ids:
        norm = normalize(nid)
        if norm is not None and arg.startswith(norm + "_"):
            candidates.append((nid, arg[len(norm) + 1 :]))
    candidates.sort(key=lambda c: len(c[1]))  # длиннее префикс = короче хвост
    return candidates


def match_service(tail: str, service_names: Sequence[str]) -> str | None:
    """Нормализованный хвост ссылки → реальное имя службы этой ноды."""
    return next((name for name in service_names if normalize(name) == tail), None)
