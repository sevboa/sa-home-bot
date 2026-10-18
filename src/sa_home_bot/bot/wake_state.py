from __future__ import annotations

import json
from typing import Any

from sa_home_bot.db.store import Store

_KEY_PREFIX = "wake_info:"


def _key(node_id: str) -> str:
    return f"{_KEY_PREFIX}{node_id}"


async def remember(store: Store, node_id: str, wake: dict[str, Any] | None) -> None:
    if not wake or not wake.get("mac"):
        return
    await store.set_state(_key(node_id), json.dumps(wake))


async def cached(store: Store, node_id: str) -> dict[str, Any] | None:
    raw = await store.get_state(_key(node_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and data.get("mac") else None
