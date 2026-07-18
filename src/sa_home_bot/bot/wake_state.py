"""Кэш wake-реквизитов нод роя (MAC/IP/broadcast) в app_state бота.

Нода докладывает свои Ethernet-реквизиты в get_state()["wake"], пока жива
(node/service.py); сеть может усыпить её или сменить DHCP-адрес — кэш здесь
переживает это и читается, когда нода уже недоступна и её нужно разбудить
(bot/handlers/wake.py, bot/swarm_view.py). См. IMPLEMENTATION_PLAN.md,
этап 19 п.6.
"""

from __future__ import annotations

import json
from typing import Any

from sa_home_bot.db.store import Store

_KEY_PREFIX = "wake_info:"


def _key(node_id: str) -> str:
    return f"{_KEY_PREFIX}{node_id}"


async def remember(store: Store, node_id: str, wake: dict[str, Any] | None) -> None:
    """Сохранить свежие реквизиты ноды, если она их доложила (mac непустой).

    Молча ничего не делает при отсутствии реквизитов — например, Wi-Fi-нода
    (arch-t480) никогда не попадёт в кэш, и это ожидаемо: её будить нечем.
    """
    if not wake or not wake.get("mac"):
        return
    await store.set_state(_key(node_id), json.dumps(wake))


async def cached(store: Store, node_id: str) -> dict[str, Any] | None:
    """Последние известные реквизиты ноды, даже если она сейчас не в сети."""
    raw = await store.get_state(_key(node_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and data.get("mac") else None
