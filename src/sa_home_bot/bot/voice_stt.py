"""Приём и распознавание голосовых сообщений Telegram для мультимодального /ai.

Само распознавание делает служба llm на mycraft (faster-whisper, CPU — см.
llm/stt.py); этот модуль на стороне alfred только качает байты и передаёт
их через рой, как уже устроено для фото (bot/handlers/ai.py::
_handle_photo_message). В отличие от фото, здесь нужно дождаться РЕЗУЛЬТАТ
(транскрипт) раньше, чем начнётся обычный текстовый ход диалога — поэтому
явный ``wake_core.ensure_service_ready`` до отправки байт, а не тихая
presence-проверка внутри самого ACTION_CHAT (как у фото).

Статусы (ожидание пробуждения/распознавание) идут через ``RichStreamSession``
той же механикой "thinking-блока", что и «шаги»/«задумчивость»/статусы тулов
в bot/ai_flow.py — не отдельным изобретением: где смысл ровно тот же (машина
просыпается), переиспользуются буквально те же константы (STEPS_TEXT,
ALBERT_UNAVAILABLE/ALBERT_HICCUP), а не заводится параллельный набор фраз,
который со временем неизбежно разъедется (тот же класс риска, что и
think_style, разошедшийся по трём файлам — живая находка 2026-08-10 в
ai_flow.py). Сессия — общая с финальным ответом Альфреда (передаётся
вызывающим, см. bot/handlers/ai.py::_handle_voice_message): статус "слушаю"
и сам ответ должны делить один черновик, а не создавать два независимых
(незакрытый черновик иначе будет освежаться keep-alive'ом до 30 минут вникуда
— живая находка 2026-08-11 в rich_stream.py про ровно эту ловушку).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid

from aiogram.types import Message

from sa_home_bot import wake_core
from sa_home_bot.bot import ai_flow
from sa_home_bot.bot.rich_stream import RichStreamSession
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

# Терминальные реплики (Альфред присутствует и реагирует сам — в отличие от
# «недоступна»/«отвлёкся», см. ai_flow.ALBERT_*, которые голосом ниже и
# переиспользуются напрямую, не дублируются). MD-варианты — для
# rich_session.finalize_status (thinking-блок не понимает HTML, см.
# STEPS_TEXT_PLAIN в ai_flow.py про ту же пару и почему их два).
VOICE_TOO_LONG_TEXT = (
    "<b>Альфред:</b> Простите, это голосовое слишком длинное — покороче, пожалуйста, сэр."
)
VOICE_TOO_LONG_TEXT_MD = (
    "**Альфред:** Простите, это голосовое слишком длинное — покороче, пожалуйста, сэр."
)
VOICE_RECOGNITION_FAILED_TEXT = (
    "<b>Альфред:</b> Не расслышал — голосовое, кажется, пустое или неразборчивое."
)
VOICE_RECOGNITION_FAILED_TEXT_MD = (
    "**Альфред:** Не расслышал — голосовое, кажется, пустое или неразборчивое."
)
# Статус (эфемерный thinking-блок/plain-фолбэк) на время скачивания+
# распознавания — без него между "скачали голосовое" и готовым ответом
# пользователь не видел вообще никакой реакции, если mycraft уже не спала
# (ожидание пробуждения — отдельная фаза, см. ai_flow.STEPS_TEXT ниже).
VOICE_LISTENING_TEXT = "<i>Альфред напряжённо вслушивается в голосовое сообщение</i>"
VOICE_LISTENING_TEXT_PLAIN = "Альфред напряжённо вслушивается в голосовое сообщение"


async def _push_status(
    message: Message, rich_session: RichStreamSession | None, plain: str, html: str
) -> None:
    if rich_session is not None:
        await rich_session.push_status(plain)
    else:
        await message.answer(html)


async def _finalize_status(
    message: Message, rich_session: RichStreamSession | None, md: str, html: str
) -> None:
    if rich_session is not None:
        await rich_session.finalize_status(md)
    else:
        await message.answer(html)


async def transcribe_voice_message(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    rich_session: RichStreamSession | None = None,
) -> str | None:
    """Скачать и распознать голосовое пользователя.

    Возвращает текст транскрипта, готовый лечь в ai_turns как обычная
    реплика пользователя. ``None`` — вежливый текст об отказе/ошибке уже
    отправлен, вызывающий должен просто прекратить обработку этого хода
    (как у bot/handlers/ai.py::_handle_photo_message при PHOTO_TOO_LARGE_TEXT).

    ``rich_session`` — та же сессия, что пойдёт в финальный ответ Альфреда
    (см. bot/handlers/ai.py::_handle_voice_message); ``None`` вне rich-режима.
    """
    voice = message.voice
    if voice is None or message.bot is None or message.chat is None:
        return None

    if voice.duration > config.llm.stt_max_duration_s:
        await _finalize_status(message, rich_session, VOICE_TOO_LONG_TEXT_MD, VOICE_TOO_LONG_TEXT)
        return None

    # Presence-проба только ради статуса ожидания (не часть самого
    # wake-сценария — тот целиком в ensure_service_ready ниже; лишний
    # дешёвый запрос ради UX, тот же компромисс, на который уже пошёл
    # bot/ai_flow.py::request_alfred для своих "шагов").
    state = await wake_core.fetch_state(node_link, _DST, timeout_s=wake_core.PRESENCE_TIMEOUT_S)
    if state is None or state.get("asleep"):
        # Тот же смысл, что и "шаги" перед пробуждением winpc/mycraft для
        # текстового /ai — переиспользуем ровно ту же реплику, не заводим
        # параллельную (см. докстринг модуля).
        await _push_status(message, rich_session, ai_flow.STEPS_TEXT_PLAIN, ai_flow.STEPS_TEXT)

    outcome = await wake_core.ensure_service_ready(
        node_link, store, LLM_NODE, LLM_SERVICE,
        warmup_timeout_s=config.llm.warmup_timeout_s,
    )
    if outcome != wake_core.READY:
        await _finalize_status(
            message, rich_session, ai_flow.ALBERT_UNAVAILABLE_MD, ai_flow.ALBERT_UNAVAILABLE
        )
        return None

    await _push_status(message, rich_session, VOICE_LISTENING_TEXT_PLAIN, VOICE_LISTENING_TEXT)
    # Решение пользователя 2026-08-11: typing («печатает») больше НЕ горит
    # здесь — распознавание голосового не пишет текст ответа, за пользователя
    # уже говорит статус "слушаю" выше (thinking-блок/plain-фолбэк). Раньше
    # оба индикатора горели одновременно ("двойной индикатор") — typing
    # обещал печатающийся текст, которого в этой фазе ещё нет.
    buf = await message.bot.download(voice)
    raw = buf.read()

    try:
        transcript = await _transcribe(node_link, raw, message.chat.id, config)
    except (ProtoError, ServiceUnavailableError, TimeoutError) as exc:
        log.warning("voice_stt: не удалось распознать голосовое: %s", exc)
        await _finalize_status(
            message, rich_session, ai_flow.ALBERT_HICCUP_MD, ai_flow.ALBERT_HICCUP
        )
        return None

    if not transcript.strip():
        await _finalize_status(
            message, rich_session, VOICE_RECOGNITION_FAILED_TEXT_MD, VOICE_RECOGNITION_FAILED_TEXT
        )
        return None
    # Успех: сессия НЕ финализируется здесь — черновик "слушаю" остаётся
    # активным, его сменит финальный ответ Альфреда (bot/ai_flow.py::
    # request_alfred/_do_ask_and_reply, та же сессия).
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
