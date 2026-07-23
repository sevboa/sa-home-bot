"""Идле-свип диалогов /ai: курсивное закрытие треда после простоя.

Независим от идле-таймера самой службы llm (llm/service.py::idle_loop) —
тот гасит контейнер на winpc по своей activity, этот шлёт сообщение в чат
по активности диалога в БД бота. Оба используют один и тот же порог из
конфига (llm.idle_sleep_after_s), но не координируются протоколом: сервис
может не знать о боте (и наоборот), у каждого своя, независимо верная,
причина считать диалог/сессию простаивающей.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store

log = logging.getLogger(__name__)

CLOSING_TEXT = "<i>Альфред не дождался обращения и уходит к себе в подсобку</i>"

_SWEEP_INTERVAL_S = 120.0


async def sweep_once(store: Store, notifier: Notifier, idle_after_s: float) -> None:
    threshold = datetime.now(tz=UTC) - timedelta(seconds=idle_after_s)
    for chat_id, dialogue_id in await store.open_idle_ai_dialogues(threshold):
        await notifier.send_direct(chat_id, CLOSING_TEXT)
        await store.mark_ai_dialogue_closed(chat_id, dialogue_id, datetime.now(tz=UTC))


async def run_idle_sweep(store: Store, notifier: Notifier, settings: Settings) -> None:
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_S)
        try:
            await sweep_once(store, notifier, settings.llm.idle_sleep_after_s)
        except Exception:  # noqa: BLE001 — один сбойный свип не должен убить цикл
            log.exception("ai_idle: сбой свипа диалогов /ai")
