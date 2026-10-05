"""Отдача запечатанных копий напарником для восстановления (39.0.8(d)).

Живёт в службе ``vpn`` ноды-хранителя (рядом с ``backup_snapshot_get``): хранилище
``BackupStore`` пишут и процесс ноды (identity), и служба vpn (снапшоты), читать
удобнее всего оттуда, где уже есть приёмник напарника. Объявляется только при
заданном ``[backup].partner`` — то есть на нодах пары.

Кто может звать: любой участник роя с токеном ``[swarm].token`` (как и любое
действие любой службы). Это приемлемо, потому что (1) блобы запечатаны на
публичный ключ alfred — без приватного ключа из ``[backup].private_key_file``
это шум; (2) в открытом виде отдаются лишь метаданные (время, число строк
таблиц), не содержимое; (3) отдаётся только копия ЗАДАННОГО напарника
(``[backup].partner``) — произвольный путь/имя из запроса не принимается.
Лишнего права «расшифровать» на ноде-хранителе нет вообще.

Действия:
- ``backup_store_list {node}`` → ``{node, identity: [{label, meta}], snapshot: [{label, meta}]}``;
- ``backup_store_get {node, kind=identity|snapshot, label=latest}`` →
  ``{kind, label, meta, sealed(base64)}``. Сообщение протокола ≤ 1 МиБ, блоб снапшота
  ≤ ``MAX_SEALED_BYTES`` (640 КиБ) в base64 влезает.
"""

from __future__ import annotations

import base64
from typing import Any

from sa_home_bot.backup.store import BackupStore
from sa_home_bot.proto.messages import ActionParam, ActionSpec

ACTION_STORE_LIST = "backup_store_list"
ACTION_STORE_GET = "backup_store_get"
KINDS = ("identity", "snapshot")


class ServeError(Exception):
    """Запрос отклонён (чужая нода, нет такой версии, неверный вид)."""


def action_specs() -> list[ActionSpec]:
    return [
        ActionSpec(
            id=ACTION_STORE_LIST,
            title="💾 Список хранимых бэкапов",
            params=(ActionParam(name="node", type="string", title="Чья копия"),),
        ),
        ActionSpec(
            id=ACTION_STORE_GET,
            title="💾 Выдать запечатанную копию",
            params=(
                ActionParam(name="node", type="string", title="Чья копия"),
                ActionParam(name="kind", type="string", choices=KINDS, title="Что"),
                ActionParam(name="label", type="string", required=False, title="Версия"),
            ),
        ),
    ]


def _check_node(partner: str, args: dict[str, Any]) -> str:
    node = str(args.get("node", "")).strip()
    if not partner or node != partner:
        raise ServeError(f"эта нода хранит копию только напарника {partner!r}, а не {node!r}")
    return node


def handle(store: BackupStore, partner: str, action: str, args: dict[str, Any]) -> dict | None:
    """None — действие не наше."""
    if action == ACTION_STORE_LIST:
        node = _check_node(partner, args)
        return {
            "node": node,
            "identity": store.identity_versions(node),
            "snapshot": store.snapshot_versions(node),
        }
    if action == ACTION_STORE_GET:
        node = _check_node(partner, args)
        kind = str(args.get("kind", ""))
        label = str(args.get("label") or "latest")
        if kind == "identity":
            item = store.read_identity(node, label)
        elif kind == "snapshot":
            item = store.read_snapshot(node, label)
        else:
            raise ServeError(f"неизвестный вид копии {kind!r} (identity|snapshot)")
        if item is None:
            raise ServeError(f"у этой ноды нет {kind} {label!r} для {node}")
        return {
            "kind": kind,
            "label": label,
            "meta": item.meta,
            "sealed": base64.b64encode(item.blob).decode("ascii"),
        }
    return None
