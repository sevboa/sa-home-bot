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
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ERR_UNAVAILABLE, ERR_UNKNOWN_DST, Address, ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import WILDCARD

log = logging.getLogger(__name__)

LLM_NODE = "winpc"
LLM_SERVICE = "llm"
ACTION_CHAT = "chat"

STEPS_TEXT = "Вы слышите приближающиеся шаги..."
ARNOLD_WAKING = "<b>Агнольд:</b> Сейчас Альфред подойдёт"
ALBERT_UNAVAILABLE = "<b>Альбегт:</b> К сожалению Альфреда нет на месте, попробуйте позже, сэр"
ALBERT_ASLEEP = "<b>Альбегт:</b> Альфред, кажется, уснул — обратитесь позже, сэр"

WAKE_POLL_TIMEOUT_S = 30.0
WAKE_POLL_INTERVAL_S = 3.0


def _is_unavailable(exc: Exception) -> bool:
    if isinstance(exc, ServiceUnavailableError):
        return True
    return isinstance(exc, ProtoError) and exc.code in (ERR_UNAVAILABLE, ERR_UNKNOWN_DST)


_GENERIC_ERROR_TEXT = "<b>Альфред:</b> Прошу прощения, не вышло — попробуйте чуть позже."


def _error_text(exc: ProtoError) -> str:  # noqa: ARG001 — деталь только для лога/админа
    # Не «недоступна» (нода жива, но сама генерация упала — например Ollama
    # не поднялась за отведённое время) — раньше улетало необработанным
    # исключением, пользователь не получал вообще никакого ответа. Текст
    # намеренно общий — подробности (exc.message) идут только админу
    # (notify_admins), пользователю не палим внутреннюю кухню/инфраструктуру.
    return _GENERIC_ERROR_TEXT


async def notify_admins(book: SubscriptionBook, notifier: Notifier, text: str) -> None:
    """Диагностика падений /ai — в чаты с полным доступом (allowed_commands
    содержит "*"), не пользователю. Молчаливая деградация («Альбегт», нода
    просто спит) сюда не попадает — только настоящие сбои (см. вызовы ниже)."""
    for sub in book.all():
        if WILDCARD in sub.allowed_commands:
            await notifier.send_direct(sub.chat_id, text)


async def request_alfred(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    settings: Settings,
    history: list[dict[str, str]],
    book: SubscriptionBook,
    notifier: Notifier,
) -> str | None:
    """Сходить в llm.chat с presence/wake-сценарием.

    Возвращает сырой текст ответа модели, либо None — Альфреда не нашли
    (сообщение об этом пользователю уже отправлено здесь же, вызывающему
    отвечать больше нечего).
    """
    dst = Address(node=LLM_NODE, service=LLM_SERVICE)
    timeout = settings.llm.request_timeout_s
    chat_id = message.chat.id if message.chat else "?"

    async def _ask() -> str:
        result = await node_link.command(
            ACTION_CHAT, {"messages": history}, dst=dst, timeout=timeout
        )
        return result.get("response", "")

    # Узнать заранее, не спит ли модель (idle-таймер llm/service.py) — если
    # да, предупредить о прогреве СРАЗУ, а не оставлять пользователя молча
    # ждать до request_timeout_s без всякой обратной связи. Узел при этом
    # доступен (просто отвечает не сразу) — это не сценарий wake ниже.
    steps_shown = False
    asleep_warmup = False
    try:
        state = await node_link.get_state(dst=dst)
    except (ServiceUnavailableError, ProtoError):
        state = None  # не знаем, спит или недоступна вовсе — увидим по chat
    if state is not None and state.get("asleep"):
        await message.answer(STEPS_TEXT)
        steps_shown = True
        asleep_warmup = True

    try:
        return await _ask()
    except ServiceUnavailableError:
        pass
    except ProtoError as exc:
        if not _is_unavailable(exc):
            # Узел был доступен и мы знали, что модель спит (прогрев) — если
            # именно прогрев и не уложился, это не «внутренняя ошибка» в
            # глазах пользователя, а прямое продолжение «шагов»: Альбегт,
            # а не голое извинение Альфреда.
            await message.answer(ALBERT_ASLEEP if asleep_warmup else _error_text(exc))
            await notify_admins(
                book, notifier, f"⚠️ /ai (chat={chat_id}): {exc.code} — {exc.message}"
            )
            return None

    # --- недоступна: шаги (если ещё не показали) -> молчаливый wake -> poll
    # до 30с -> Агнольд/Альбегт ---
    if not steps_shown:
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
    except ServiceUnavailableError:
        await message.answer(ALBERT_UNAVAILABLE)
        return None
    except ProtoError as exc:
        if _is_unavailable(exc):
            await message.answer(ALBERT_UNAVAILABLE)
        else:
            await message.answer(_error_text(exc))
            await notify_admins(
                book, notifier, f"⚠️ /ai (chat={chat_id}, после wake): {exc.code} — {exc.message}"
            )
        return None
