"""Оркестрация диалога /ai: вызов службы llm с presence/wake-сценарием.

Персонажи и сценарий — из обсуждения с пользователем 2026-07-23: если нода
winpc недоступна, показать «шаги», молча разбудить через рой (существующий
механизм — см. bot/handlers/wake.py::wake_swarm_node_core), подождать до 30с,
затем «Агнольд» (успех) или «Альбегт» (неудача). Имена персонажей — фиксированные
строки (не вывод модели): их произносит Альфред, отсюда искажение «р→г»
(«Арнольд»→«Агнольд», «Альберт»→«Альбегт»); сами они «р» выговаривают, поэтому
их реплики пишутся без искажений.
"""

from __future__ import annotations

import logging

from aiogram.types import Message

from sa_home_bot.bot import swarm_view
from sa_home_bot.bot.handlers.wake import wake_swarm_node_core
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ERR_UNAVAILABLE, ERR_UNKNOWN_DST, Address, ProtoError

log = logging.getLogger(__name__)

LLM_NODE = "winpc"
LLM_SERVICE = "llm"
ACTION_CHAT = "chat"

STEPS_TEXT = "Вы слышите приближающиеся шаги..."
ARNOLD_WAKING = "<b>Агнольд:</b> Сейчас Альфред подойдёт"
ALBERT_UNAVAILABLE = "<b>Альбегт:</b> К сожалению Альфреда нет на месте, попробуйте позже, сэр"

WAKE_POLL_TIMEOUT_S = 30.0
WAKE_POLL_INTERVAL_S = 3.0


def _is_unavailable(exc: Exception) -> bool:
    if isinstance(exc, ServiceUnavailableError):
        return True
    return isinstance(exc, ProtoError) and exc.code in (ERR_UNAVAILABLE, ERR_UNKNOWN_DST)


async def request_alfred(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    settings: Settings,
    history: list[dict[str, str]],
) -> str | None:
    """Сходить в llm.chat с presence/wake-сценарием.

    Возвращает сырой текст ответа модели, либо None — Альфреда не нашли
    (сообщение об этом пользователю уже отправлено здесь же, вызывающему
    отвечать больше нечего).
    """
    dst = Address(node=LLM_NODE, service=LLM_SERVICE)
    timeout = settings.llm.request_timeout_s

    async def _ask() -> str:
        result = await node_link.command(
            ACTION_CHAT, {"messages": history}, dst=dst, timeout=timeout
        )
        return result.get("response", "")

    try:
        return await _ask()
    except (ServiceUnavailableError, ProtoError) as exc:
        if not _is_unavailable(exc):
            raise

    # --- недоступна: шаги -> молчаливый wake -> poll до 30с -> Агнольд/Альбегт ---
    await message.answer(STEPS_TEXT)
    outcome = await wake_swarm_node_core(node_link, store, LLM_NODE)
    became_available = outcome.ok and await swarm_view.wait_for_service(
        node_link, LLM_NODE, LLM_SERVICE, WAKE_POLL_TIMEOUT_S, WAKE_POLL_INTERVAL_S
    )
    if not became_available:
        await message.answer(ALBERT_UNAVAILABLE)
        return None

    await message.answer(ARNOLD_WAKING)
    try:
        return await _ask()
    except (ServiceUnavailableError, ProtoError) as exc:
        if not _is_unavailable(exc):
            raise
        await message.answer(ALBERT_UNAVAILABLE)
        return None
