from __future__ import annotations

import base64
import logging

from aiogram.types import Message

from sa_home_bot import wake_core
from sa_home_bot.bot.alfred_voice import ALFRED_PREFIX_MD
from sa_home_bot.bot.rich_stream import RichStreamSession, push_status_or_answer
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.addresses import LLM_ADDRESS, LLM_NODE, LLM_SERVICE
from sa_home_bot.proto.chunks import download_chunks
from sa_home_bot.proto.messages import ProtoError

log = logging.getLogger(__name__)

ACTION_SYNTHESIZE_SPEECH = "synthesize_speech"
ACTION_TTS_DOWNLOAD_CHUNK = "tts_chunk"

VOICE_SYNTH_TEXT = "<i>Альфред проговаривает ответ вслух</i>"
VOICE_SYNTH_TEXT_PLAIN = "Альфред проговаривает ответ вслух"

VOICE_REPLY_SENT_MD = ALFRED_PREFIX_MD + "🔊 Ответил голосом выше."


async def synthesize_voice_reply(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    rich_session: RichStreamSession | None,
    text: str,
) -> bytes | None:
    if message.chat is None:
        return None
    outcome = await wake_core.ensure_service_ready(
        node_link, store, LLM_NODE, LLM_SERVICE, warmup_timeout_s=config.llm.warmup_timeout_s
    )
    if outcome != wake_core.READY:
        return None
    await push_status_or_answer(message, rich_session, VOICE_SYNTH_TEXT_PLAIN, VOICE_SYNTH_TEXT)
    try:
        result = await node_link.command(
            ACTION_SYNTHESIZE_SPEECH,
            {"text": text, "chat_id": message.chat.id},
            dst=LLM_ADDRESS,
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
            return await download_chunks(
                node_link,
                ACTION_TTS_DOWNLOAD_CHUNK,
                LLM_ADDRESS,
                base_args={"session_id": session_id},
                expected_sha256=result.get("sha256"),
                integrity_error="скачанное голосовое не прошло проверку целостности (sha256)",
            )
        except (ProtoError, ServiceUnavailableError, TimeoutError, ValueError) as exc:
            log.warning("voice_tts: не удалось скачать голосовое: %s", exc)
            return None
    return None
