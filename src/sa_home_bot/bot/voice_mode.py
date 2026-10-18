from __future__ import annotations

from sa_home_bot.db.store import Store

_KEY_PREFIX = "voice_reply_mode:"
_ON = "on"


def _key(chat_id: int) -> str:
    return f"{_KEY_PREFIX}{chat_id}"


async def is_enabled(store: Store, chat_id: int) -> bool:
    return await store.get_state(_key(chat_id)) == _ON


async def set_enabled(store: Store, chat_id: int, enabled: bool) -> None:
    await store.set_state(_key(chat_id), _ON if enabled else "off")
