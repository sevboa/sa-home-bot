from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from typing import Any

from aiogram.types import Message

from sa_home_bot.bot import ai_flow, dialogue_context, voice_mode, voice_tts
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.alfred_voice import ALFRED_PREFIX_HTML
from sa_home_bot.bot.interactives.engine import Interactives
from sa_home_bot.bot.notifier import Notifier, chunk_text
from sa_home_bot.bot.pending_actions import PendingActions
from sa_home_bot.bot.rich_stream import RichDraftPolicy, RichStreamSession
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.utils.clock import utc_now

log = logging.getLogger(__name__)

UNHANDLED_ERROR_TEXT = ALFRED_PREFIX_HTML + "Прошу прощения, что-то пошло не так, сэр."
VOICE_FILENAME = "alfred.ogg"
UNDELIVERED_PREVIEW_CHARS = 1500


@dataclass
class TurnServices:
    node_link: ServiceLink
    store: Store
    config: Settings
    book: SubscriptionBook
    notifier: Notifier
    active_ai_chats: ai_flow.ActiveAiChats
    tool_calls: ToolCalls
    pending_actions: PendingActions | None = None
    interactives: Interactives | None = None


def format_answer(raw: str) -> str:
    return ALFRED_PREFIX_HTML + html.escape(raw.strip())


def rich_session_for(message: Message, config: Settings) -> RichStreamSession | None:
    if config.llm.response_mode != "rich" or message.chat is None or message.bot is None:
        return None
    return RichStreamSession(
        message.bot,
        message.chat.id,
        message_thread_id=message.message_thread_id,
        policy=RichDraftPolicy.from_llm_config(config.llm),
    )


def dialogue_id_of(message: Message) -> int:
    return message.message_thread_id or message.message_id


def _is_private(message: Message) -> bool:
    return message.chat is not None and message.chat.type == "private"


async def ask_and_reply(
    message: Message,
    services: TurnServices,
    dialogue_id: int,
    history: list[dict[str, Any]],
    rich_session: RichStreamSession | None,
) -> str | None:
    chat_id = message.chat.id if message.chat else None
    task = asyncio.current_task()
    services.active_ai_chats.register(chat_id, task)
    try:
        return await _run_turn(message, services, dialogue_id, history, rich_session)
    finally:
        services.active_ai_chats.unregister(chat_id, task)


async def _plan_interactive_turn(
    message: Message, services: TurnServices, history: list[dict[str, Any]]
) -> Any | None:
    interactives = services.interactives
    if interactives is None or message.chat is None:
        return None
    current_turn = history[-1] if history else None
    user_text = (
        current_turn["content"]
        if isinstance(current_turn, dict) and isinstance(current_turn.get("content"), str)
        else ""
    )
    try:
        return await interactives.before_turn(
            message.chat.id,
            message.from_user.id if message.from_user else None,
            user_text,
            is_private=_is_private(message),
        )
    except Exception:  # noqa: BLE001
        log.exception("ai: интерактивы — before_turn упал (chat=%s)", message.chat.id)
        return None


def _request_extras(services: TurnServices, turn_plan: Any | None) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if services.pending_actions is not None:
        extras["pending_actions"] = services.pending_actions
    if services.interactives is not None:
        extras["interactive_turn"] = turn_plan
        extras["interactives"] = services.interactives
    return extras


async def _run_turn(
    message: Message,
    services: TurnServices,
    dialogue_id: int,
    history: list[dict[str, Any]],
    rich_session: RichStreamSession | None,
) -> str | None:
    dismissal = ai_tools.DismissalBox()
    speech_remark = ai_flow.SpeechRemarkBox()
    streaming_session = rich_session if _is_private(message) else None
    turn_plan = await _plan_interactive_turn(message, services, history)
    try:
        raw = await _request_reply(
            message,
            services,
            dialogue_id,
            history,
            dismissal,
            speech_remark,
            streaming_session,
            turn_plan,
        )
        if raw is None:
            return None
        if raw == ai_flow.SILENT_REPLY:
            await _wait_for_tool_photo(message, services, streaming_session)
            return None
        if not raw.strip():
            await _report_empty_reply(message, services)
            return None
        sent_message_id = await _deliver_reply(
            message, services, dialogue_id, rich_session, streaming_session, turn_plan, raw
        )
        if sent_message_id is None:
            return None
        await services.store.record_ai_turn(
            message.chat.id, sent_message_id, dialogue_id, "assistant", raw, utc_now()
        )
        await _after_delivery(
            message, services, dialogue_id, history, speech_remark, dismissal, turn_plan, raw
        )
        return raw
    finally:
        await _close_turn(message, services, dialogue_id, rich_session, turn_plan)


async def _request_reply(
    message: Message,
    services: TurnServices,
    dialogue_id: int,
    history: list[dict[str, Any]],
    dismissal: ai_tools.DismissalBox,
    speech_remark: ai_flow.SpeechRemarkBox,
    streaming_session: RichStreamSession | None,
    turn_plan: Any | None,
) -> str | None:
    try:
        return await ai_flow.request_alfred(
            message,
            services.node_link,
            services.store,
            services.config,
            history,
            dialogue_id,
            services.book,
            services.notifier,
            dismissal,
            services.tool_calls,
            speech_remark,
            streaming_session,
            **_request_extras(services, turn_plan),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("ai: необработанная ошибка в диалоге chat=%s", message.chat.id)
        await message.answer(UNHANDLED_ERROR_TEXT)
        await ai_flow.notify_admins(
            services.book,
            services.notifier,
            f"🔥 /ai (chat={message.chat.id}): необработанное исключение {exc!r}",
        )
        return None


async def _wait_for_tool_photo(
    message: Message, services: TurnServices, streaming_session: RichStreamSession | None
) -> None:
    if services.interactives is None or message.chat is None:
        return
    on_status = streaming_session.push_status if streaming_session is not None else None
    await services.interactives.wait_photo(message.chat.id, on_status)


async def _report_empty_reply(message: Message, services: TurnServices) -> None:
    log.warning("ai: пустой ответ модели (chat=%s)", message.chat.id)
    await message.answer(ai_flow.ALBERT_HICCUP)
    await ai_flow.notify_admins(
        services.book,
        services.notifier,
        f"⚠️ /ai (chat={message.chat.id}): модель вернула пустой ответ",
    )


async def _turn_sent_picture(store: Store, message: Message, dialogue_id: int) -> bool:
    calls = await store.tool_calls_for_dialogue(message.chat.id, dialogue_id)
    return any(
        c["trigger_message_id"] == message.message_id
        and c["tool_name"] in dialogue_context.HISTORY_TOOLS
        for c in calls
    )


async def _mood_selfie(
    message: Message,
    services: TurnServices,
    dialogue_id: int,
    streaming_session: RichStreamSession | None,
    turn_plan: Any | None,
    raw: str,
) -> int | None:
    interactives = services.interactives
    if interactives is None or message.chat is None:
        return None
    try:
        if await _turn_sent_picture(services.store, message, dialogue_id):
            return None
        return await interactives.mood_selfie(
            message.chat.id,
            message.from_user.id if message.from_user else None,
            getattr(turn_plan, "user_text", ""),
            raw,
            message_thread_id=message.message_thread_id,
            trigger_message_id=message.message_id,
            on_status=streaming_session.push_status if streaming_session is not None else None,
        )
    except Exception:  # noqa: BLE001
        log.exception("ai: селфи к ответу упало (chat=%s)", message.chat.id)
        return None


async def _send_voice(
    message: Message, services: TurnServices, rich_session: RichStreamSession | None, raw: str
) -> int | None:
    audio_bytes = await voice_tts.synthesize_voice_reply(
        message, services.node_link, services.store, services.config, rich_session, raw
    )
    if audio_bytes is None:
        return None
    voice_message_id = await services.notifier.send_voice(
        message.chat.id,
        audio_bytes,
        filename=VOICE_FILENAME,
        message_thread_id=message.message_thread_id,
    )
    if voice_message_id is not None and rich_session is not None:
        await rich_session.finalize_status(voice_tts.VOICE_REPLY_SENT_MD)
    return voice_message_id


async def _deliver_reply(
    message: Message,
    services: TurnServices,
    dialogue_id: int,
    rich_session: RichStreamSession | None,
    streaming_session: RichStreamSession | None,
    turn_plan: Any | None,
    raw: str,
) -> int | None:
    voice_on = message.chat is not None and await voice_mode.is_enabled(
        services.store, message.chat.id
    )
    selfie_id = None
    if not voice_on:
        selfie_id = await _mood_selfie(
            message, services, dialogue_id, streaming_session, turn_plan, raw
        )
    voice_message_id = await _send_voice(message, services, rich_session, raw) if voice_on else None
    if selfie_id is not None:
        return selfie_id
    if voice_message_id is not None:
        return voice_message_id
    return await _deliver_text(message, services, rich_session, raw)


async def _deliver_text(
    message: Message, services: TurnServices, rich_session: RichStreamSession | None, raw: str
) -> int | None:
    if rich_session is None:
        return (await send_alfred_reply(message, raw)).message_id
    sent_message_id = await send_alfred_reply_rich(message, raw, rich_session, services.notifier)
    if sent_message_id is None:
        log.error(
            "ai: ответ не доставлен ни rich, ни обычным текстом (chat=%s).\n"
            "Полный текст ответа Альфреда:\n%s",
            message.chat.id,
            raw,
        )
        await ai_flow.notify_admins(
            services.book,
            services.notifier,
            f"⚠️ /ai (chat={message.chat.id}): ответ Альфреда не доставлен "
            f"(rich и текстовый фолбэк оба не прошли). Полный текст — в логе "
            f"узла. Начало:\n\n{html.escape(raw[:UNDELIVERED_PREVIEW_CHARS])}",
        )
    return sent_message_id


async def _after_delivery(
    message: Message,
    services: TurnServices,
    dialogue_id: int,
    history: list[dict[str, Any]],
    speech_remark: ai_flow.SpeechRemarkBox,
    dismissal: ai_tools.DismissalBox,
    turn_plan: Any | None,
    raw: str,
) -> None:
    store, config, node_link = services.store, services.config, services.node_link
    user_turn = await store.ai_turn(message.chat.id, message.message_id)
    if user_turn is not None:
        await ai_flow.piggyback_dialogue_episode(
            node_link, message.chat.id, str(user_turn["content"]), raw
        )
    if getattr(history, "compress_after", False) and message.chat is not None:
        dialogue_context.COMPRESSOR.schedule(
            node_link,
            store,
            config,
            message.chat.id,
            dialogue_id,
            ai_flow.llm_address(),
            num_ctx=await ai_flow.resolve_num_ctx(config, store, ai_flow.llm_address()),
        )
    if speech_remark.text is not None:
        await message.answer(speech_remark.text)
    if turn_plan is not None and services.interactives is not None:
        try:
            await services.interactives.after_turn(
                turn_plan,
                raw,
                dialogue_id=dialogue_id,
                message_thread_id=message.message_thread_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("ai: интерактивы — after_turn упал (chat=%s)", message.chat.id)
    if dismissal.mode is not None:
        await ai_flow.perform_dismissal(
            message, node_link, dismissal.mode, services.book, services.notifier
        )


async def _close_turn(
    message: Message,
    services: TurnServices,
    dialogue_id: int,
    rich_session: RichStreamSession | None,
    turn_plan: Any | None,
) -> None:
    if rich_session is not None:
        await rich_session.aclose()
    current = asyncio.current_task()
    cancelling = current is not None and current.cancelling()
    if services.pending_actions is not None and message.chat is not None and not cancelling:
        try:
            await services.pending_actions.flush_drafts(
                message.chat.id, dialogue_id, message.message_thread_id
            )
        except Exception:  # noqa: BLE001
            log.exception("ai: не удалось отправить формы (chat=%s)", message.chat.id)
    interactives = services.interactives
    if interactives is not None and message.chat is not None:
        if cancelling:
            interactives.discard_forms(message.chat.id)
        else:
            try:
                await interactives.flush_forms(
                    message.chat.id,
                    turn_plan,
                    dialogue_id=dialogue_id,
                    message_thread_id=message.message_thread_id,
                )
            except Exception:  # noqa: BLE001
                log.exception("ai: не удалось отправить формы сцены (chat=%s)", message.chat.id)


async def send_alfred_reply(message: Message, raw: str) -> Message:
    chunks = chunk_text(format_answer(raw))
    sent = await message.reply(chunks[0])
    for chunk in chunks[1:]:
        sent = await message.answer(chunk)
    return sent


async def send_alfred_reply_rich(
    message: Message, raw: str, session: RichStreamSession, notifier: Notifier
) -> int | None:
    sent = await session.finalize(raw, reply_to_message_id=message.message_id)
    if sent is not None:
        return sent.message_id
    log.warning("ai: rich-ответ не ушёл — откат на обычный текст (chat=%s)", message.chat.id)
    return await notifier.send_direct(
        message.chat.id,
        format_answer(raw),
        reply_to_message_id=message.message_id,
        message_thread_id=message.message_thread_id,
    )
