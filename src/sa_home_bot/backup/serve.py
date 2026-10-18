from __future__ import annotations

import base64
from typing import Any

from sa_home_bot.backup.store import BackupStore
from sa_home_bot.proto.messages import ActionParam, ActionSpec

ACTION_STORE_LIST = "backup_store_list"
ACTION_STORE_GET = "backup_store_get"
KINDS = ("identity", "snapshot")


class ServeError(Exception):
    pass


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
