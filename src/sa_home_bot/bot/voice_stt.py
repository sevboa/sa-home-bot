"""Приём и распознавание голосовых сообщений Telegram для мультимодального /ai.

Само распознавание делает служба llm на mycraft (faster-whisper, CPU — см.
llm/stt.py); этот модуль на стороне alfred только качает байты и передаёт
их через рой, как уже устроено для фото (bot/handlers/ai.py::
_handle_photo_message). В отличие от фото, здесь нужно дождаться РЕЗУЛЬТАТ
(транскрипт) раньше, чем начнётся обычный текстовый ход диалога — поэтому
явный ``wake_core.ensure_service_ready`` до отправки байт, а не тихая
presence-проверка внутри самого ACTION_CHAT (как у фото).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid

from aiogram.types import Message

from sa_home_bot import wake_core
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Address, ProtoError

log = logging.getLogger(__name__)

# Тот же узел/служба, что и живой /ai (см. bot/ai_flow.py::LLM_NODE/LLM_SERVICE,
# bot/tools.py::LLM_NODE — константа продублирована по тому же паттерну,
# что уже принят в проекте для этого адреса).
LLM_NODE = "mycraft"
LLM_SERVICE = "llm"
_DST = Address(node=LLM_NODE, service=LLM_SERVICE)

ACTION_TRANSCRIBE_VOICE = "transcribe_voice"
ACTION_STT_UPLOAD_CHUNK = "stt_chunk"

# Голосовое целиком одним блобом, если после base64 укладывается в этот
# порог — тот же порядок величины, что у фото (_MAX_RAW_IMAGE_B64_BYTES,
# bot/handlers/ai.py), с запасом от MAX_MESSAGE_BYTES протокола роя (1 МиБ).
# Длиннее — чанками (см. _upload_chunked): реальный путь для голосовых на
# несколько минут, не гипотетический случай.
_INLINE_VOICE_B64_BYTES = 800_000
_CHUNK_BYTES = 700 * 1024

VOICE_TOO_LONG_TEXT = (
    "<b>Альфред:</b> Простите, это голосовое слишком длинное — покороче, пожалуйста, сэр."
)
VOICE_WAITING_TEXT = "<b>Альфред:</b> Секундочку, сэр — жду, пока проснётся нужная машина."
VOICE_UNAVAILABLE_TEXT = (
    "<b>Альфред:</b> Не могу сейчас распознать голосовое — не достучаться до нужной машины."
)
VOICE_RECOGNITION_FAILED_TEXT = (
    "<b>Альфред:</b> Не расслышал — голосовое, кажется, пустое или неразборчивое."
)


async def transcribe_voice_message(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
) -> str | None:
    """Скачать и распознать голосовое пользователя.

    Возвращает текст транскрипта, готовый лечь в ai_turns как обычная
    реплика пользователя. ``None`` — вежливый текст об отказе/ошибке уже
    отправлен, вызывающий должен просто прекратить обработку этого хода
    (как у bot/handlers/ai.py::_handle_photo_message при PHOTO_TOO_LARGE_TEXT).
    """
    voice = message.voice
    if voice is None or message.bot is None or message.chat is None:
        return None

    if voice.duration > config.llm.stt_max_duration_s:
        await message.answer(VOICE_TOO_LONG_TEXT)
        return None

    # Presence-проба только ради статуса ожидания (не часть самого
    # wake-сценария — тот целиком в ensure_service_ready ниже; лишний
    # дешёвый запрос ради UX, тот же компромисс, на который уже пошёл
    # bot/ai_flow.py::request_alfred для своих "шагов").
    state = await wake_core.fetch_state(node_link, _DST, timeout_s=wake_core.PRESENCE_TIMEOUT_S)
    if state is None or state.get("asleep"):
        await message.answer(VOICE_WAITING_TEXT)

    outcome = await wake_core.ensure_service_ready(
        node_link, store, LLM_NODE, LLM_SERVICE,
        warmup_timeout_s=config.llm.warmup_timeout_s,
    )
    if outcome != wake_core.READY:
        await message.answer(VOICE_UNAVAILABLE_TEXT)
        return None

    buf = await message.bot.download(voice)
    raw = buf.read()

    try:
        transcript = await _transcribe(node_link, raw, message.chat.id, config)
    except (ProtoError, ServiceUnavailableError, TimeoutError) as exc:
        log.warning("voice_stt: не удалось распознать голосовое: %s", exc)
        await message.answer(VOICE_UNAVAILABLE_TEXT)
        return None

    if not transcript.strip():
        await message.answer(VOICE_RECOGNITION_FAILED_TEXT)
        return None
    return transcript.strip()


async def _transcribe(
    node_link: ServiceLink, raw: bytes, chat_id: int, config: Settings
) -> str:
    audio_b64 = base64.b64encode(raw).decode()
    timeout = config.llm.stt_request_timeout_s
    if len(audio_b64) <= _INLINE_VOICE_B64_BYTES:
        result = await node_link.command(
            ACTION_TRANSCRIBE_VOICE,
            {"audio_b64": audio_b64, "chat_id": chat_id},
            dst=_DST,
            timeout=timeout,
        )
    else:
        session_id = uuid.uuid4().hex
        await _upload_chunked(node_link, session_id, raw)
        result = await node_link.command(
            ACTION_TRANSCRIBE_VOICE,
            {
                "session_id": session_id,
                "expected_size": len(raw),
                "expected_sha256": hashlib.sha256(raw).hexdigest(),
                "chat_id": chat_id,
            },
            dst=_DST,
            timeout=timeout,
        )
    return str(result.get("transcript") or "")


async def _upload_chunked(node_link: ServiceLink, session_id: str, raw: bytes) -> None:
    offset = 0
    total = len(raw)
    while offset < total:
        chunk = raw[offset : offset + _CHUNK_BYTES]
        await node_link.command(
            ACTION_STT_UPLOAD_CHUNK,
            {
                "session_id": session_id,
                "offset": offset,
                "data_b64": base64.b64encode(chunk).decode(),
            },
            dst=_DST,
        )
        offset += len(chunk)
