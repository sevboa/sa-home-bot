"""Синтез голосового ответа Альфреда для /ai (доставка ВМЕСТО текста).

Сам синтез делает служба llm на mycraft (Coqui XTTS v2, CPU — см.
llm/tts.py); этот модуль на стороне alfred просит текст озвучить и забирает
готовый OGG/Opus обратно — по образцу bot/voice_stt.py (та же служба, тот же
узел), но данные текут в обратную сторону: тут ЭТА нода готовит аудио, а
alfred его скачивает, поэтому pull-паттерн чанков как у
vpn/service.py::ACTION_APK_CHUNK + bot/vpn_apk.py::_download_chunks, не
push, как у STT-загрузки голосового пользователя.

Любая ошибка (служба недоступна, синтез упал, скачивание оборвалось) —
``None``: вызывающий (bot/handlers/ai.py::_do_ask_and_reply) должен тихо
откатиться на обычный текстовый ответ, пользователь никогда не остаётся без
ответа вовсе.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from aiogram.types import Message

from sa_home_bot import wake_core
from sa_home_bot.bot.rich_stream import RichStreamSession
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Address, ProtoError

log = logging.getLogger(__name__)

# Тот же узел/служба, что и у STT/живого /ai (см. bot/voice_stt.py::LLM_NODE,
# bot/ai_flow.py::LLM_NODE — константа продублирована по тому же паттерну,
# что уже принят в проекте для этого адреса).
LLM_NODE = "mycraft"
LLM_SERVICE = "llm"
_DST = Address(node=LLM_NODE, service=LLM_SERVICE)

ACTION_SYNTHESIZE_SPEECH = "synthesize_speech"
ACTION_TTS_DOWNLOAD_CHUNK = "tts_chunk"

_CHUNK_BYTES = 700 * 1024

# Статус на время синтеза (thinking-блок/plain-фолбэк, та же механика, что
# VOICE_LISTENING_TEXT в voice_stt.py) — XTTS v2 на CPU занимает секунды, не
# доли секунды, без этого статуса между готовым текстом и голосовым
# сообщением был бы заметный молчаливый разрыв. Референс — голос самого
# Альфреда (не чужой закадровый диктор, живая находка/поправка 2026-08-11:
# первая формулировка ошибочно намекала на озвучку "со стороны", хотя
# синтезируется его собственный голос), поэтому реплика — о том, что он сам
# готовится ответить вслух, без стороннего "кто-то там озвучивает".
VOICE_SYNTH_TEXT = "<i>Альфред прочищает горло, готовясь ответить вслух</i>"
VOICE_SYNTH_TEXT_PLAIN = "Альфред прочищает горло, готовясь ответить вслух"

# Закрывающая статус-черновик реплика после успешной отправки голосового
# (bot/handlers/ai.py::_do_ask_and_reply, rich_session.finalize_status) —
# сам текст ответа уже ушёл голосом, здесь только короткая пометка.
VOICE_REPLY_SENT_MD = "**Альфред:** 🔊 Ответил голосом выше."


async def _push_status(message: Message, rich_session: RichStreamSession | None) -> None:
    if rich_session is not None:
        await rich_session.push_status(VOICE_SYNTH_TEXT_PLAIN)
    else:
        await message.answer(VOICE_SYNTH_TEXT)


async def _download_chunks(
    node_link: ServiceLink, session_id: str, expected_sha256: str | None
) -> bytes:
    parts: list[bytes] = []
    offset = 0
    while True:
        chunk = await node_link.command(
            ACTION_TTS_DOWNLOAD_CHUNK,
            {"session_id": session_id, "offset": offset, "length": _CHUNK_BYTES},
            dst=_DST,
        )
        data = base64.b64decode(chunk["data_b64"])
        if not data:
            break
        parts.append(data)
        offset += len(data)
        if chunk.get("eof"):
            break
    payload = b"".join(parts)
    if expected_sha256 and hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("скачанное голосовое не прошло проверку целостности (sha256)")
    return payload


async def synthesize_voice_reply(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    rich_session: RichStreamSession | None,
    text: str,
) -> bytes | None:
    """Синтезировать голосовой ответ Альфреда. ``None`` — недоступно/сбой,
    вызывающий должен молча откатиться на текст."""
    if message.chat is None:
        return None
    outcome = await wake_core.ensure_service_ready(
        node_link, store, LLM_NODE, LLM_SERVICE, warmup_timeout_s=config.llm.warmup_timeout_s
    )
    if outcome != wake_core.READY:
        return None
    await _push_status(message, rich_session)
    try:
        result = await node_link.command(
            ACTION_SYNTHESIZE_SPEECH,
            {"text": text, "chat_id": message.chat.id},
            dst=_DST,
            timeout=config.llm.tts_request_timeout_s,
        )
    except (ProtoError, ServiceUnavailableError, TimeoutError) as exc:
        log.warning("voice_tts: не удалось синтезировать речь: %s", exc)
        return None
    audio_b64 = result.get("audio_b64")
    if isinstance(audio_b64, str) and audio_b64:
        return base64.b64decode(audio_b64)
    session_id = result.get("session_id")
    if isinstance(session_id, str) and session_id:
        try:
            return await _download_chunks(node_link, session_id, result.get("sha256"))
        except (ProtoError, ServiceUnavailableError, TimeoutError, ValueError) as exc:
            log.warning("voice_tts: не удалось скачать голосовое: %s", exc)
            return None
    return None
