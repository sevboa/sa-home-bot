"""Тумблер «голосовой режим ответа» для /ai — per-chat, в app_state бота.

Включается/выключается не слэш-командой, а естественной просьбой в диалоге
(LLM-тул ``voice_mode``, см. bot/tools.py) — "отвечай голосом" / "вернись к
тексту". Состояние переживает перезапуски и действует до следующей просьбы
сменить режим, поэтому кладём его в ``app_state`` (простой строковый флаг),
тем же паттерном, что уже применён для похожей задачи «состояние, привязанное
к сущности, не нуждающееся в реляционной схеме» — см. bot/wake_state.py.
"""

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
