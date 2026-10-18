from __future__ import annotations

import base64
import hashlib
import logging

from aiogram.types import Message

from sa_home_bot import wake_core
from sa_home_bot.bot.ai_lines import (
    ALBERT_HICCUP,
    ALBERT_HICCUP_MD,
    ALBERT_UNAVAILABLE,
    ALBERT_UNAVAILABLE_MD,
    STEPS_TEXT,
    STEPS_TEXT_PLAIN,
)
from sa_home_bot.bot.alfred_voice import ALFRED_PREFIX_HTML, ALFRED_PREFIX_MD
from sa_home_bot.bot.rich_stream import (
    RichStreamSession,
    finalize_status_or_answer,
    push_status_or_answer,
)
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.addresses import LLM_ADDRESS, LLM_NODE, LLM_SERVICE
from sa_home_bot.proto.chunks import CHUNK_BYTES
from sa_home_bot.proto.messages import ProtoError, new_id

log = logging.getLogger(__name__)

ACTION_TRANSCRIBE_VOICE = "transcribe_voice"
ACTION_STT_UPLOAD_CHUNK = "stt_chunk"

_INLINE_VOICE_B64_BYTES = 800_000

VOICE_TOO_LONG_TEXT = (
    ALFRED_PREFIX_HTML + "Простите, это голосовое слишком длинное — покороче, пожалуйста, сэр."
)
VOICE_TOO_LONG_TEXT_MD = (
    ALFRED_PREFIX_MD + "Простите, это голосовое слишком длинное — покороче, пожалуйста, сэр."
)
VOICE_RECOGNITION_FAILED_TEXT = (
    ALFRED_PREFIX_HTML + "Не расслышал — голосовое, кажется, пустое или неразборчивое."
)
VOICE_RECOGNITION_FAILED_TEXT_MD = (
    ALFRED_PREFIX_MD + "Не расслышал — голосовое, кажется, пустое или неразборчивое."
)
VOICE_LISTENING_TEXT = "<i>Альфред напряжённо вслушивается в голосовое сообщение</i>"
VOICE_LISTENING_TEXT_PLAIN = "Альфред напряжённо вслушивается в голосовое сообщение"


async def transcribe_voice_message(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    rich_session: RichStreamSession | None = None,
) -> str | None:
    voice = message.voice
    if voice is None or message.bot is None or message.chat is None:
        return None

    if voice.duration > config.llm.stt_max_duration_s:
        await finalize_status_or_answer(
            message, rich_session, VOICE_TOO_LONG_TEXT_MD, VOICE_TOO_LONG_TEXT
        )
        return None

    state = await wake_core.fetch_state(
        node_link, LLM_ADDRESS, timeout_s=wake_core.PRESENCE_TIMEOUT_S
    )
    if state is None or state.get("asleep"):
        await push_status_or_answer(message, rich_session, STEPS_TEXT_PLAIN, STEPS_TEXT)

    outcome = await wake_core.ensure_service_ready(
        node_link,
        store,
        LLM_NODE,
        LLM_SERVICE,
        warmup_timeout_s=config.llm.warmup_timeout_s,
    )
    if outcome != wake_core.READY:
        await finalize_status_or_answer(
            message, rich_session, ALBERT_UNAVAILABLE_MD, ALBERT_UNAVAILABLE
        )
        return None

    await push_status_or_answer(
        message, rich_session, VOICE_LISTENING_TEXT_PLAIN, VOICE_LISTENING_TEXT
    )
    buf = await message.bot.download(voice)
    raw = buf.read()

    try:
        transcript = await _transcribe(node_link, raw, message.chat.id, config)
    except (ProtoError, ServiceUnavailableError, TimeoutError) as exc:
        log.warning("voice_stt: не удалось распознать голосовое: %s", exc)
        await finalize_status_or_answer(message, rich_session, ALBERT_HICCUP_MD, ALBERT_HICCUP)
        return None

    if not transcript.strip():
        await finalize_status_or_answer(
            message, rich_session, VOICE_RECOGNITION_FAILED_TEXT_MD, VOICE_RECOGNITION_FAILED_TEXT
        )
        return None
    return transcript.strip()


async def transcribe_bytes(
    node_link: ServiceLink, raw: bytes, chat_id: int, config: Settings
) -> str:
    return (await _transcribe(node_link, raw, chat_id, config)).strip()


async def _transcribe(node_link: ServiceLink, raw: bytes, chat_id: int, config: Settings) -> str:
    audio_b64 = base64.b64encode(raw).decode()
    timeout = config.llm.stt_request_timeout_s
    if len(audio_b64) <= _INLINE_VOICE_B64_BYTES:
        result = await node_link.command(
            ACTION_TRANSCRIBE_VOICE,
            {"audio_b64": audio_b64, "chat_id": chat_id},
            dst=LLM_ADDRESS,
            timeout=timeout,
        )
    else:
        session_id = new_id()
        await _upload_chunked(node_link, session_id, raw)
        result = await node_link.command(
            ACTION_TRANSCRIBE_VOICE,
            {
                "session_id": session_id,
                "expected_size": len(raw),
                "expected_sha256": hashlib.sha256(raw).hexdigest(),
                "chat_id": chat_id,
            },
            dst=LLM_ADDRESS,
            timeout=timeout,
        )
    return str(result.get("transcript") or "")


async def _upload_chunked(node_link: ServiceLink, session_id: str, raw: bytes) -> None:
    offset = 0
    total = len(raw)
    while offset < total:
        chunk = raw[offset : offset + CHUNK_BYTES]
        await node_link.command(
            ACTION_STT_UPLOAD_CHUNK,
            {
                "session_id": session_id,
                "offset": offset,
                "data_b64": base64.b64encode(chunk).decode(),
            },
            dst=LLM_ADDRESS,
        )
        offset += len(chunk)
