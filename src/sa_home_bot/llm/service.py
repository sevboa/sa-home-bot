"""LlmService — ServiceHandler службы llm (Альфред: диалог с Ollama на этой машине).

Действия `ask`/`chat` бьют в Ollama по loopback (см. llm/ollama.py) —
никакого сетевого HTTP наружу, только протокол роя достаёт досюда
(LLM_INTEGRATION_PLAN.md §0). Собственный идле-таймер (idle_loop) сам
останавливает контейнер после простоя, не дожидаясь команды бота. Если за
это «тёплое окно» были чаты с реальными запросами (`chat_id` в args
действия `chat`) — при засыпании служба сама эмитит событие
`llm_idle_sleep` со списком этих chat_id (ретранслируется до бота тем же
механизмом, что node_joined/update_finished — см. node/app.py::build_router,
bot/node_events.py) — бот шлёт туда закрывающее сообщение РОВНО один раз,
а не сканирует диалоги сам. Тем же приёмом (см. notify_restart) служба
извещает те же чаты перед собственным остановом (деплой/апдейт/ручной
restart) — событие `llm_service_restart`, другой текст в боте.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import socket
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.llm import ollama, stt, tts, vision
from sa_home_bot.llm.model_profiles import REASON_LEVELS, ModelProfile, load_profiles
from sa_home_bot.llm.prompt import (
    DEFAULT_PERSONA_PROMPT,
    ROUTER_SYSTEM_PROMPT,
    strip_math_notation,
)
from sa_home_bot.llm.speech_therapy import SpeechTherapist
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)

log = logging.getLogger(__name__)

SERVICE_NAME = "llm"


def _log_ollama_timings(what: str, result: dict[str, Any]) -> None:
    """Тайминги ответа Ollama в лог одной строкой.

    Живая находка 2026-08-10: без этого разбор «почему Альфред отвечает 20
    секунд» упирался в гадание — служба брала из ответа только текст, а
    prompt_eval_*/eval_* молча выбрасывала. Настоящая причина оказалась в
    невидимых токенах размышления (message.thinking): на «Привет!» модель
    писала 222 токена ради ответа из пяти слов, и это выглядело как
    «медленно читает контекст», хотя prefill шёл больше 1000 ток/с. Здесь
    видно и то, и другое — сколько токенов промпта и с какой скоростью,
    сколько сгенерировано и сколько из этого ушло в thinking.
    """

    def _speed(count: Any, duration_ns: Any) -> str:
        if not isinstance(count, int) or not isinstance(duration_ns, int) or duration_ns <= 0:
            return "?"
        return f"{count / (duration_ns / 1e9):.0f} ток/с"

    thinking = (result.get("message") or {}).get("thinking") or ""
    load_ns = result.get("load_duration")
    log.info(
        "llm: %s — промпт %s ток (%s), генерация %s ток (%s), thinking %d симв., "
        "загрузка модели %s, всего %s",
        what,
        result.get("prompt_eval_count", "?"),
        _speed(result.get("prompt_eval_count"), result.get("prompt_eval_duration")),
        result.get("eval_count", "?"),
        _speed(result.get("eval_count"), result.get("eval_duration")),
        len(thinking),
        f"{load_ns / 1e9:.1f}с" if isinstance(load_ns, int) else "?",
        f"{result['total_duration'] / 1e9:.1f}с"
        if isinstance(result.get("total_duration"), int)
        else "?",
    )

ACTION_ASK = "ask"
ACTION_CHAT = "chat"
# Экшн-представление (см. PROTOCOL.md, прецедент "downtime" службы monitor)
# под чтение буфера частичной генерации — этап 34, Фаза 2 (Rich-стрим
# ответов). get_state() параметров не принимает (PROTOCOL.md), а Альфред
# может вести несколько диалогов одновременно (разные чаты семьи), поэтому
# частичный текст не кладём одним полем в get_state, а раздаём по
# request_id через обычный command.
ACTION_CHAT_PROGRESS = "chat_progress"
ACTION_SLEEP = "sleep"
# Прогреть контейнер БЕЗ генерации — для службы tasks (tasks/service.py),
# которая будит цель заранее (prewake_loop, за PREWAKE_LEAD_S до due_at) и
# не хочет тратить эту попытку на настоящий ask/chat (живая находка
# 2026-07-24: ensure_running и так вызывается лениво внутри ask/chat, но
# тогда прогрев занял бы ПЕРВЫЙ реальный запрос, а не время заранее).
ACTION_WARMUP = "warmup"
# Мультимодальный /ai (2026-08-10): alfred присылает сырое (необработанное)
# фото ключом "raw_image" внутри последнего элемента messages в ACTION_CHAT
# (см. run_command) — эта служба сама делает ресайз/хранение (llm/vision.py,
# сильнее CPU, уже рядом с Ollama) и возвращает photo_key для ai_turns.
# ACTION_LOOK_AT_PHOTO — повторный точечный просмотр УЖЕ сохранённого фото
# по этому ключу (bot/tools.py::tool_look_at_photo), без похода через
# ресайз заново и без пересылки байт между узлами — файл уже здесь.
ACTION_LOOK_AT_PHOTO = "look_at_photo"

# Голосовые сообщения /ai (см. llm/stt.py) — тот же принцип, что у фото:
# alfred шлёт сырые байты, эта нода их обрабатывает (faster-whisper на CPU,
# не Ollama/Gemma). ACTION_TRANSCRIBE_VOICE — основной путь: голосовое
# целиком одним base64-блобом в args (короткие сообщения). Для длинных
# голосовых, которые после base64 не влезают в протокольный лимит, сначала
# идут чанки ACTION_STT_UPLOAD_CHUNK (bot/voice_stt.py::_upload_chunked, по
# образцу vpn/service.py::ACTION_APK_CHUNK, но в обратном направлении —
# push, не pull), потом сам ACTION_TRANSCRIBE_VOICE уже без audio_b64, с
# session_id вместо него.
ACTION_TRANSCRIBE_VOICE = "transcribe_voice"
ACTION_STT_UPLOAD_CHUNK = "stt_chunk"

# Синтез голосовых ответов /ai (см. llm/tts.py) — Coqui XTTS v2, тоже CPU,
# тоже эта служба (не бот, не Ollama/Gemma) — тот же принцип, что у STT, но
# данные текут в обратную сторону: alfred просит синтезировать текст
# (ACTION_SYNTHESIZE_SPEECH), готовое аудио едет назад инлайном (короткие
# ответы) либо чанками (ACTION_TTS_DOWNLOAD_CHUNK) — pull-паттерн по образцу
# vpn/service.py::ACTION_APK_CHUNK + bot/vpn_apk.py::_download_chunks, не
# push, как у STT-загрузки: тут служба, а не alfred, готовит данные.
ACTION_SYNTHESIZE_SPEECH = "synthesize_speech"
ACTION_TTS_DOWNLOAD_CHUNK = "tts_chunk"

# Отказы фото-путей намеренно возвращаются как обычный {"response": ...}, а
# не отдельным полем-ошибкой: для ACTION_CHAT это просто ложится в ai_turns
# как обычная реплика Альфреда (ai.py не должен знать про фото-специфику),
# для ACTION_LOOK_AT_PHOTO — как обычный текст результата тула, который
# модель сама естественно перескажет пользователю (bot/tools.py).
PHOTO_PROCESS_FAILED_TEXT = (
    "Не получилось обработать фото — возможно, файл повреждён. Пришлите его ещё раз, сэр."
)
PHOTO_NOT_FOUND_TEXT = "Не нахожу это фото — похоже, сохранённая копия уже устарела."

EVENT_IDLE_SLEEP = "llm_idle_sleep"
EVENT_SERVICE_RESTART = "llm_service_restart"
# Отдельно от EVENT_IDLE_SLEEP: тот адресован реальным чатам и намеренно НЕ
# эмитится, если активных chat_id нет (см. run_command/ACTION_WARMUP — прогрев
# не в счёт, test_warmup_does_not_add_chat_id_to_active_chats). Но
# node/app.py::on_local_event нужен сигнал «простой дошёл до сна» БЕЗ этого
# условия — иначе автовыключение (node/service.py::maybe_auto_poweroff_idle)
# никогда не сработало бы на машине, с которой Alfred просто ни разу не
# заговорили (живая находка 2026-08-03 при обкатке на mycraft: тестовый
# warmup не породил EVENT_IDLE_SLEEP вовсе). Эмитится ТОЛЬКО из
# _maybe_sleep_idle — естественного тайм-аута простоя, не ручного sleep.
EVENT_WENT_IDLE = "llm_went_idle"
# Логопед (llm/speech_therapy.py) вылечил Альфреда полностью — разовое
# событие на весь процесс службы, боту нужно известить ВСЕ чаты (не только
# активные, в отличие от EVENT_IDLE_SLEEP/EVENT_SERVICE_RESTART).
EVENT_SPEECH_CURED = "llm_speech_cured"

_IDLE_CHECK_INTERVAL_S = 60.0
# Буфер частичной генерации (этап 34, Фаза 2) переживает завершённый ответ
# на этот срок — чтобы опрашивающий бот успел забрать последний "done"-тик,
# даже если он чуть отстал от реального завершения command(). После этого
# запись подметается в idle_loop — иначе self._streaming растёт без предела,
# если бот упал/переподключился посреди опроса и так и не дочитал ответ.
_STREAMING_ENTRY_TTL_S = 120.0
# Осиротевшие сессии чанкованной загрузки голосового (клиент прервался
# посреди отправки — упал, потерял связь) — иначе self._stt_uploads растёт
# без предела. Короче _STREAMING_ENTRY_TTL_S: там довершённый ответ ждёт
# отставшего опроса бота, здесь — недовершённая загрузка, которую всё равно
# нечем продолжить (offset начался бы заново).
_STT_UPLOAD_TTL_S = 300.0
# Инлайн/чанки готового синтеза — те же величины, что у voice_stt.py на
# приёмной стороне (запас от MAX_MESSAGE_BYTES, base64 раздувает на треть).
# Осиротевшие сессии синтеза (бот не успел/не смог их дочитать чанками) —
# сама причина осиротения другая, чем у _stt_uploads (там недовершённая
# ЗАГРУЗКА, тут неполученный ГОТОВЫЙ результат), TTL взят тем же порядком.
_INLINE_TTS_B64_BYTES = 800_000
_TTS_CHUNK_BYTES = 700 * 1024
_TTS_SESSION_TTL_S = 300.0

EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _noop_emit(event_type: str, data: dict[str, Any]) -> None:
    pass


class LlmService:
    def __init__(
        self,
        settings: Settings,
        *,
        emit: EventEmitter = _noop_emit,
        speech_rand: Callable[[], float] | None = None,
    ) -> None:
        """``speech_rand`` — только для тестов: делает Логопеда (llm/speech_therapy.py)
        детерминированным, не проброшено в конфиг/CLI."""
        self._cfg: LlmConfig = settings.llm
        # Живая находка 2026-07-25: текст персонажа убран из репозитория
        # (см. llm/prompt.py) — берём его из локального config.toml
        # (settings.llm.persona_prompt), фоллбэк на безликую заглушку, если
        # он не заполнен, чтобы служба не падала на пустом system-промпте.
        self._persona_prompt = self._cfg.persona_prompt or DEFAULT_PERSONA_PROMPT
        if not self._cfg.persona_prompt:
            log.warning(
                "settings.llm.persona_prompt пуст — используется безликая заглушка "
                "DEFAULT_PERSONA_PROMPT вместо характера персонажа"
            )
        # Профиль модели — контракт рассуждения, подключаемый вместе с моделью
        # (llm/model_profiles.py). Резолвится один раз по имени модели.
        registry = load_profiles(self._cfg.model_profiles_path)
        self._profile: ModelProfile
        self._profile, matched = registry.resolve(self._cfg.model)
        # num_ctx: явное значение в config.toml побеждает, иначе — дефолт профиля.
        if "num_ctx" not in self._cfg.model_fields_set:
            self._cfg.num_ctx = self._profile.num_ctx
        (log.warning if not matched else log.info)(
            "llm: model %s → профиль %s (control=%s, router=%s, num_ctx=%s, "
            "thinking_hidden=%s)%s",
            self._cfg.model,
            self._profile.name,
            self._profile.think_control,
            self._profile.router,
            self._cfg.num_ctx,
            self._profile.thinking_hidden,
            "" if matched else " — своего профиля для модели нет, взят _default",
        )
        self._node = socket.gethostname()
        self._emit = emit
        self._last_activity = datetime.now(tz=UTC)
        self._asleep = False
        # Живая находка при аудите механизма пробуждения (2026-08-26): после
        # рестарта/деплоя ЭТОГО процесса (не Ollama) presence-проверка
        # (wake_core.ensure_service_ready) читала только self._asleep и
        # честно, но неверно докладывала "не сплю" — хотя модель в VRAM у
        # Ollama на самом деле не загружена (свежий процесс её ещё не
        # трогал). Первый чат-запрос после рестарта молча платил за холодную
        # загрузку (~200с) внутри своего таймаута, без единой реплики
        # персонажа о прогреве. _warmup_confirmed — отдельный от _asleep
        # флаг: True только после РЕАЛЬНОГО успешного обращения к Ollama
        # (_touch/ACTION_WARMUP), False сразу после старта процесса и после
        # каждого сна (см. _sleep_now). get_state() ниже репортит "asleep"
        # как ``self._asleep or not self._warmup_confirmed`` — не трогая
        # сам _asleep и его роль в idle-таймере (_maybe_sleep_idle),
        # который должен продолжать работать и для машины, которую ни разу
        # не трогали (см. EVENT_WENT_IDLE/автовыключение, живая находка
        # 2026-08-03 — заводить для этого отдельный флаг было бы неверно).
        self._warmup_confirmed = False
        self._active_chat_ids: set[int] = set()
        # Живая находка 2026-07-23: короткоживущие вызовы wsl.exe (прогрев,
        # ретраи в llm/ollama.py) сами по себе не держат WSL2-VM живой — она
        # гасла уже через секунды после КАЖДОГО ответа, а не через
        # idle_sleep_after_s. Keepalive-процесс теперь живёт здесь, на весь
        # тёплый период (старт при выходе из простоя, стоп — вместе с
        # реальным сном), не вокруг одного запроса (см. llm/ollama.py).
        self._keepalive = ollama.WslKeepalive(
            self._cfg, duration_s=self._cfg.idle_sleep_after_s + 60.0
        )
        self._speech = SpeechTherapist(
            self._cfg, **({"rand": speech_rand} if speech_rand is not None else {})
        )
        # request_id -> {"partial": str, "done": bool, "done_at": datetime | None}
        # (этап 34, Фаза 2 — см. ACTION_CHAT_PROGRESS выше).
        self._streaming: dict[str, dict[str, Any]] = {}
        # session_id -> накопленные байты чанкованной загрузки голосового
        # (см. ACTION_STT_UPLOAD_CHUNK/ACTION_TRANSCRIBE_VOICE выше) и время
        # последнего чанка (для TTL-очистки осиротевших сессий, idle_loop).
        self._stt_uploads: dict[str, bytearray] = {}
        self._stt_upload_touched: dict[str, datetime] = {}
        # session_id -> готовый синтезированный OGG/Opus (см.
        # ACTION_SYNTHESIZE_SPEECH/ACTION_TTS_DOWNLOAD_CHUNK выше) и время
        # последнего обращения (TTL-очистка осиротевших сессий, idle_loop).
        self._tts_sessions: dict[str, bytes] = {}
        self._tts_session_touched: dict[str, datetime] = {}

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(self._cfg.model,),
            model_profile=self._profile.summary().to_payload(),
            actions=(
                ActionSpec(
                    id=ACTION_ASK,
                    title="Спросить Альфреда",
                    params=(
                        ActionParam(name="prompt", type="string", required=True, title="Вопрос"),
                    ),
                ),
                ActionSpec(
                    id=ACTION_CHAT,
                    title="Диалог с Альфредом",
                    params=(
                        ActionParam(
                            name="messages",
                            type="string",
                            required=True,
                            title="История диалога (список {role, content})",
                        ),
                        ActionParam(
                            name="chat_id",
                            type="int",
                            required=False,
                            title="Chat, откуда пришёл запрос (для llm_idle_sleep)",
                        ),
                        ActionParam(
                            name="tools",
                            type="string",
                            required=False,
                            title="Декларации инструментов (tool-calling, план §7)",
                        ),
                        ActionParam(
                            name="reason",
                            type="string",
                            required=False,
                            title=(
                                "Уровень рассуждения: off|low|medium|high (выдаёт "
                                "router-проход). Перевод в параметр Ollama делает "
                                "профиль модели (llm/model_profiles.py)"
                            ),
                            choices=tuple(REASON_LEVELS),
                        ),
                        ActionParam(
                            name="think",
                            type="bool",
                            required=False,
                            title="УСТАРЕЛО, используйте reason (True→high, False→off)",
                        ),
                        ActionParam(
                            name="role",
                            type="string",
                            required=False,
                            title=(
                                "Какой системный промпт использовать: 'persona' "
                                "(по умолчанию, Альфред) или 'router' (служебный "
                                "триаж без персонажа, см. llm/prompt.py)"
                            ),
                        ),
                        ActionParam(
                            name="request_id",
                            type="string",
                            required=False,
                            title=(
                                "Не передан — обычный блокирующий вызов, как раньше. "
                                "Передан — идём через Ollama-стрим, накопленный текст "
                                "доступен через chat_progress с этим же id (этап 34, "
                                "Фаза 2)"
                            ),
                        ),
                        ActionParam(
                            name="photo_key",
                            type="string",
                            required=False,
                            title=(
                                "Ключ для сохранения фото на диске — обязателен, если "
                                "последнее сообщение messages несёт raw_image"
                            ),
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_LOOK_AT_PHOTO,
                    title="Ещё раз посмотреть на ранее присланное фото",
                    params=(
                        ActionParam(
                            name="photo_key",
                            type="string",
                            required=True,
                            title="Ключ уже сохранённого фото (см. ACTION_CHAT.photo_key)",
                        ),
                        ActionParam(
                            name="question",
                            type="string",
                            required=True,
                            title="Что нужно рассмотреть/уточнить на фото",
                        ),
                        ActionParam(
                            name="chat_id",
                            type="int",
                            required=False,
                            title="Chat, откуда пришёл запрос (для логопеда/llm_idle_sleep)",
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_TRANSCRIBE_VOICE,
                    title="Распознать голосовое сообщение",
                    params=(
                        ActionParam(
                            name="audio_b64",
                            type="string",
                            required=False,
                            title="Аудио целиком, base64 (короткие сообщения)",
                        ),
                        ActionParam(
                            name="session_id",
                            type="string",
                            required=False,
                            title=(
                                "Сессия чанкованной загрузки (длинные сообщения) — "
                                "вместо audio_b64, см. ACTION_STT_UPLOAD_CHUNK"
                            ),
                        ),
                        ActionParam(
                            name="expected_size",
                            type="int",
                            required=False,
                            title="Ожидаемый размер байт (проверка чанкованной загрузки)",
                        ),
                        ActionParam(
                            name="expected_sha256",
                            type="string",
                            required=False,
                            title="sha256 байт (проверка чанкованной загрузки)",
                        ),
                        ActionParam(
                            name="chat_id",
                            type="int",
                            required=False,
                            title="Chat, откуда пришёл запрос",
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_STT_UPLOAD_CHUNK,
                    title="Загрузить кусок голосового сообщения (для длинных)",
                    params=(
                        ActionParam(
                            name="session_id",
                            type="string",
                            required=True,
                            title="Идентификатор сессии загрузки",
                        ),
                        ActionParam(
                            name="offset",
                            type="int",
                            required=True,
                            title="Смещение куска в байтах (текущая длина буфера)",
                        ),
                        ActionParam(
                            name="data_b64",
                            type="string",
                            required=True,
                            title="Кусок аудио, base64",
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_SYNTHESIZE_SPEECH,
                    title="Синтезировать голосовой ответ",
                    params=(
                        ActionParam(
                            name="text", type="string", required=True, title="Текст для озвучки"
                        ),
                        ActionParam(
                            name="chat_id",
                            type="int",
                            required=False,
                            title="Chat, откуда пришёл запрос",
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_TTS_DOWNLOAD_CHUNK,
                    title="Скачать кусок синтезированного аудио",
                    params=(
                        ActionParam(
                            name="session_id",
                            type="string",
                            required=True,
                            title="Идентификатор сессии синтеза",
                        ),
                        ActionParam(
                            name="offset",
                            type="int",
                            required=True,
                            title="Смещение куска в байтах",
                        ),
                        ActionParam(
                            name="length",
                            type="int",
                            required=False,
                            title="Размер куска в байтах",
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_CHAT_PROGRESS,
                    title="Прочитать буфер частичной генерации",
                    params=(
                        ActionParam(
                            name="request_id",
                            type="string",
                            required=True,
                            title="request_id, переданный в chat при запуске стрима",
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_SLEEP,
                    title="Уложить модель спать",
                    params=(
                        ActionParam(
                            name="quiet",
                            type="bool",
                            required=False,
                            title="Молча — не слать чатам llm_idle_sleep",
                        ),
                    ),
                ),
                ActionSpec(id=ACTION_WARMUP, title="Прогреть модель заранее (без ответа)"),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "model": self._cfg.model,
            # "не подтверждено, что модель реально в VRAM" — тоже "сплю" с
            # точки зрения presence-проверки (см. _warmup_confirmed выше).
            "asleep": self._asleep or not self._warmup_confirmed,
            "speech_therapy": self._speech.snapshot(),
        }

    def _think_arg(self, args: dict[str, Any]) -> bool | str | None:
        """Из args действия chat → значение поля ``think`` запроса к Ollama.

        Приоритет: ``reason`` (off|low|medium|high, новый протокол) → голый
        ``think`` (устаревший булев: True→high, False→off) → дефолт ``off``
        (нет намерения — быстрый проход). Перевод уровня в конкретный параметр
        делает профиль модели (llm/model_profiles.py).
        """
        reason = args.get("reason")
        if not isinstance(reason, str) or reason not in REASON_LEVELS:
            think = args.get("think")
            if think is not None and not isinstance(think, bool):
                raise ProtoError(ERR_BAD_REQUEST, "think должен быть булевым значением")
            reason = "high" if think is True else "off"
        return self._profile.think_arg(reason)

    async def _touch(self, chat_id: Any = None) -> None:
        self._last_activity = datetime.now(tz=UTC)
        self._asleep = False
        self._warmup_confirmed = True
        if isinstance(chat_id, int):
            self._active_chat_ids.add(chat_id)
        # Keepalive держит живой WSL2-VM — актуально только для
        # container_backend="wsl-docker" (winpc); на native-Linux (mycraft)
        # Ollama — always-on systemd-сервис, никакого wsl.exe тут нет.
        if self._cfg.container_backend == "wsl-docker" and not self._keepalive.alive:
            await self._keepalive.start()

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_ASK:
            prompt = args.get("prompt")
            if not isinstance(prompt, str) or not prompt:
                raise ProtoError(ERR_BAD_REQUEST, "prompt должен быть непустой строкой")
            chat_id = args.get("chat_id")
            await self._touch(chat_id)
            result = await ollama.generate(self._cfg, prompt, self._persona_prompt)
            _log_ollama_timings("ask", result)
            cleaned = strip_math_notation(result.get("response", ""))
            response, remark, just_cured = self._speech.process(cleaned, chat_id)
            if just_cured:
                await self._emit_speech_cured()
            out: dict[str, Any] = {"response": response, "model": self._cfg.model}
            if remark is not None:
                out["speech_remark"] = remark
            return out
        if action == ACTION_CHAT:
            messages = args.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ProtoError(ERR_BAD_REQUEST, "messages должен быть непустым списком")
            tools = args.get("tools") or None
            role = args.get("role") or "persona"
            if role not in ("persona", "router"):
                raise ProtoError(ERR_BAD_REQUEST, f"неизвестная role: {role!r}")
            # Намерение-уровень рассуждения (off|low|medium|high) от бота, и его
            # перевод в параметр Ollama `think` через профиль этой модели.
            think = self._think_arg(args)
            # Общий префикс для обеих ролей (2026-08-10): system[0] всегда
            # персонаж, а служебная инструкция роутера уезжает ПОСЛЕДНИМ
            # системным сообщением в хвост. Раньше роли брали разный
            # system[0] — то есть два непересекающихся с нулевого токена
            # префикса, и KV-кэш роутерного прохода не годился персонажному
            # (а на SWA-моделях вроде gemma-4 это ещё и рушило кэш целиком:
            # "forcing full prompt re-processing ... likely due to SWA" в
            # логах Ollama). Теперь роутерный проход прогревает кэш для
            # персонажного, и тот платит только за хвост. Триажу это не
            # мешает: его инструкция стоит вплотную к точке генерации, а
            # конкурировать с персонажем за внимание модели она начинала
            # именно когда лежала В ОДНОМ system-блоке с ним (живая находка
            # 2026-07-25, llm/prompt.py) — отдельным сообщением этого нет.
            system = self._persona_prompt
            # raw_image ищем именно в ПОСЛЕДНЕМ элементе — это текущий ход
            # пользователя (см. bot/ai_flow.py::request_alfred, history[-1]
            # всегда текущий ход). Известное ограничение v1: если router-
            # проход (mode="router_think") на ЭТОМ же ходу вызовет тул,
            # messages вырастет и фото-сообщение перестанет быть последним
            # до персонажного прохода — картинка потеряется для финального
            # ответа. Редкий составной случай (фото + вопрос, требующий
            # ещё и отдельного тула в том же ходу), не решается в этой
            # итерации (см. план — чанкованная загрузка/групповые фото
            # тоже отложены).
            last = messages[-1] if isinstance(messages[-1], dict) else None
            if last is not None and "raw_image" in last:
                raw_image = last.pop("raw_image")
                photo_key = args.get("photo_key")
                if not isinstance(raw_image, str) or not raw_image:
                    raise ProtoError(ERR_BAD_REQUEST, "raw_image должен быть непустой строкой")
                if not isinstance(photo_key, str) or not photo_key:
                    raise ProtoError(ERR_BAD_REQUEST, "photo_key обязателен вместе с raw_image")
                try:
                    raw_bytes = base64.b64decode(raw_image, validate=True)
                    resized_b64 = await asyncio.to_thread(
                        vision.resize_and_store, raw_bytes, photo_key, self._cfg
                    )
                except Exception:
                    log.warning("vision: не удалось обработать фото %s", photo_key, exc_info=True)
                    return {"response": PHOTO_PROCESS_FAILED_TEXT, "model": self._cfg.model}
                last["images"] = [resized_b64]
                await asyncio.to_thread(vision.maybe_sweep_sync, self._cfg)
            if role == "router":
                messages = [*messages, {"role": "system", "content": ROUTER_SYSTEM_PROMPT}]
            chat_id = args.get("chat_id")
            await self._touch(chat_id)
            request_id = args.get("request_id")
            if request_id is not None and (
                not isinstance(request_id, str) or not request_id
            ):
                raise ProtoError(ERR_BAD_REQUEST, "request_id должен быть непустой строкой")
            if request_id:
                result = await self._chat_streamed(
                    request_id, messages, system, tools=tools, think=think
                )
            else:
                result = await ollama.chat(self._cfg, messages, system, tools=tools, think=think)
            _log_ollama_timings(f"chat/{role}", result)
            message = result.get("message", {})
            # Модель попросила вызвать инструмент(ы) — служба llm сама по рою
            # не ходит (нет ServiceLink к соседям, только к своей Ollama), это
            # исполняет фронтенд (см. LLM_INTEGRATION_PLAN.md §7.1). Ответ тут
            # НЕ прогоняем через SpeechTherapist — это ещё не финальный
            # текст персонажа, а служебные данные для цикла вызовов.
            tool_calls = message.get("tool_calls")
            if tool_calls:
                return {"tool_calls": tool_calls, "model": self._cfg.model}
            cleaned = strip_math_notation(message.get("content", ""))
            reply, remark, just_cured = self._speech.process(cleaned, chat_id)
            if just_cured:
                await self._emit_speech_cured()
            out: dict[str, Any] = {"response": reply, "model": self._cfg.model}
            if remark is not None:
                out["speech_remark"] = remark
            return out
        if action == ACTION_LOOK_AT_PHOTO:
            photo_key = args.get("photo_key")
            question = args.get("question")
            if not isinstance(photo_key, str) or not photo_key:
                raise ProtoError(ERR_BAD_REQUEST, "photo_key должен быть непустой строкой")
            if not isinstance(question, str) or not question:
                raise ProtoError(ERR_BAD_REQUEST, "question должен быть непустой строкой")
            chat_id = args.get("chat_id")
            await self._touch(chat_id)
            stored_b64 = await asyncio.to_thread(vision.load_stored, photo_key, self._cfg)
            if stored_b64 is None:
                return {"response": PHOTO_NOT_FOUND_TEXT, "model": self._cfg.model}
            # Без tools (узкий вопрос про изображение не требует рекурсивного
            # tool-calling) и без рассуждения — перевод «off» через профиль
            # модели (у gemma-4 это явный think=False; явный True даёт 400).
            result = await ollama.chat(
                self._cfg,
                [{"role": "user", "content": question, "images": [stored_b64]}],
                self._persona_prompt,
                tools=None,
                think=self._profile.think_arg("off"),
            )
            _log_ollama_timings("look_at_photo", result)
            message = result.get("message", {})
            cleaned = strip_math_notation(message.get("content", ""))
            reply, remark, just_cured = self._speech.process(cleaned, chat_id)
            if just_cured:
                await self._emit_speech_cured()
            out = {"response": reply, "model": self._cfg.model}
            if remark is not None:
                out["speech_remark"] = remark
            return out
        if action == ACTION_STT_UPLOAD_CHUNK:
            session_id = args.get("session_id")
            offset = args.get("offset")
            data_b64 = args.get("data_b64")
            if not isinstance(session_id, str) or not session_id:
                raise ProtoError(ERR_BAD_REQUEST, "session_id должен быть непустой строкой")
            if not isinstance(offset, int) or isinstance(offset, bool):
                raise ProtoError(ERR_BAD_REQUEST, "offset должен быть целым числом")
            if not isinstance(data_b64, str):
                raise ProtoError(ERR_BAD_REQUEST, "data_b64 должен быть строкой")
            buf = self._stt_uploads.setdefault(session_id, bytearray())
            if offset != len(buf):
                raise ProtoError(
                    ERR_BAD_REQUEST, f"неожиданный offset: {offset}, ожидался {len(buf)}"
                )
            try:
                chunk = base64.b64decode(data_b64, validate=True)
            except Exception as exc:
                raise ProtoError(ERR_BAD_REQUEST, "data_b64 не является валидным base64") from exc
            buf.extend(chunk)
            self._stt_upload_touched[session_id] = datetime.now(tz=UTC)
            return {"received": len(buf)}
        if action == ACTION_TRANSCRIBE_VOICE:
            audio_b64 = args.get("audio_b64")
            session_id = args.get("session_id")
            if isinstance(audio_b64, str) and audio_b64:
                try:
                    raw_bytes = base64.b64decode(audio_b64, validate=True)
                except Exception as exc:
                    raise ProtoError(
                        ERR_BAD_REQUEST, "audio_b64 не является валидным base64"
                    ) from exc
            elif isinstance(session_id, str) and session_id:
                buf = self._stt_uploads.pop(session_id, None)
                self._stt_upload_touched.pop(session_id, None)
                if buf is None:
                    raise ProtoError(
                        ERR_BAD_REQUEST, f"неизвестная сессия загрузки: {session_id}"
                    )
                raw_bytes = bytes(buf)
                expected_size = args.get("expected_size")
                if isinstance(expected_size, int) and expected_size != len(raw_bytes):
                    raise ProtoError(
                        ERR_BAD_REQUEST,
                        f"размер не совпал: получено {len(raw_bytes)}, ожидалось {expected_size}",
                    )
                expected_sha256 = args.get("expected_sha256")
                if isinstance(expected_sha256, str) and expected_sha256:
                    if hashlib.sha256(raw_bytes).hexdigest() != expected_sha256:
                        raise ProtoError(
                            ERR_BAD_REQUEST, "загруженные данные не прошли проверку sha256"
                        )
            else:
                raise ProtoError(ERR_BAD_REQUEST, "нужен либо audio_b64, либо session_id")
            if not raw_bytes:
                raise ProtoError(ERR_BAD_REQUEST, "пустое аудио")
            chat_id = args.get("chat_id")
            await self._touch(chat_id)
            try:
                transcript = await stt.transcribe_voice(raw_bytes, self._cfg)
            except Exception:
                log.warning("stt: не удалось распознать голосовое сообщение", exc_info=True)
                transcript = ""
            return {"transcript": transcript}
        if action == ACTION_SYNTHESIZE_SPEECH:
            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ProtoError(ERR_BAD_REQUEST, "text должен быть непустой строкой")
            if len(text) > self._cfg.tts_max_text_chars:
                text = text[: self._cfg.tts_max_text_chars]
            chat_id = args.get("chat_id")
            await self._touch(chat_id)
            try:
                audio_bytes = await tts.synthesize_speech(text, self._cfg)
            except Exception:
                log.warning("tts: не удалось синтезировать речь", exc_info=True)
                raise ProtoError(ERR_INTERNAL, "не удалось синтезировать речь") from None
            audio_b64 = base64.b64encode(audio_bytes).decode()
            if len(audio_b64) <= _INLINE_TTS_B64_BYTES:
                return {"audio_b64": audio_b64, "format": "ogg"}
            session_id = uuid.uuid4().hex
            self._tts_sessions[session_id] = audio_bytes
            self._tts_session_touched[session_id] = datetime.now(tz=UTC)
            return {
                "session_id": session_id,
                "size": len(audio_bytes),
                "sha256": hashlib.sha256(audio_bytes).hexdigest(),
                "format": "ogg",
            }
        if action == ACTION_TTS_DOWNLOAD_CHUNK:
            session_id = args.get("session_id")
            offset = args.get("offset")
            length = args.get("length") or _TTS_CHUNK_BYTES
            if not isinstance(session_id, str) or not session_id:
                raise ProtoError(ERR_BAD_REQUEST, "session_id должен быть непустой строкой")
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise ProtoError(ERR_BAD_REQUEST, "offset должен быть неотрицательным целым числом")
            data = self._tts_sessions.get(session_id)
            if data is None:
                raise ProtoError(ERR_BAD_REQUEST, f"неизвестная сессия синтеза: {session_id}")
            chunk = data[offset : offset + int(length)]
            eof = offset + len(chunk) >= len(data)
            self._tts_session_touched[session_id] = datetime.now(tz=UTC)
            if eof:
                self._tts_sessions.pop(session_id, None)
                self._tts_session_touched.pop(session_id, None)
            return {"data_b64": base64.b64encode(chunk).decode(), "offset": offset, "eof": eof}
        if action == ACTION_CHAT_PROGRESS:
            request_id = args.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                raise ProtoError(ERR_BAD_REQUEST, "request_id должен быть непустой строкой")
            entry = self._streaming.get(request_id)
            if entry is None:
                # Гонка первого опроса (бот уже спрашивает, а стрим ещё не
                # успел завести запись) — не ошибка, просто пока пусто.
                return {"partial": "", "done": False}
            return {"partial": entry["partial"], "done": entry["done"]}
        if action == ACTION_SLEEP:
            await self._sleep_now(quiet=bool(args.get("quiet")))
            return {"asleep": True}
        if action == ACTION_WARMUP:
            # НЕ _touch(chat_id) — прогрев не значит, что был реальный чат,
            # ложный chat_id раздул бы список для EVENT_IDLE_SLEEP.
            await ollama.ensure_running(self._cfg)
            # И саму модель — в память: без этого «прогретая» служба всё
            # равно платит за загрузку на первом же реальном запросе (см.
            # ollama.preload, живая находка 2026-07-27).
            await ollama.preload(self._cfg)
            await self._touch()
            return {"asleep": False}
        # Сервер валидирует action по describe — сюда неизвестное не доходит.
        raise ValueError(f"необъявленное действие: {action}")

    async def _chat_streamed(
        self,
        request_id: str,
        messages: list[dict[str, Any]],
        system: str,
        *,
        tools: list[dict[str, Any]] | None,
        think: bool | str | None,
    ) -> dict[str, Any]:
        """Обёртка вокруг ollama.chat_stream: заводит запись в self._streaming
        на время генерации (читает её ACTION_CHAT_PROGRESS), закрывает её
        (done=True, done_at) в finally — даже если Ollama упала, опрашивающий
        бот должен увидеть завершение, а не крутиться до собственного
        таймаута."""
        self._streaming[request_id] = {"partial": "", "done": False, "done_at": None}

        def _on_chunk(text: str) -> None:
            entry = self._streaming.get(request_id)
            if entry is not None:
                entry["partial"] = text

        try:
            return await ollama.chat_stream(
                self._cfg, messages, system, _on_chunk, tools=tools, think=think
            )
        finally:
            entry = self._streaming.get(request_id)
            if entry is not None:
                entry["done"] = True
                entry["done_at"] = datetime.now(tz=UTC)

    def _sweep_streaming(self) -> None:
        now = datetime.now(tz=UTC)
        stale = [
            rid
            for rid, entry in self._streaming.items()
            if entry["done"]
            and entry["done_at"] is not None
            and (now - entry["done_at"]).total_seconds() >= _STREAMING_ENTRY_TTL_S
        ]
        for rid in stale:
            del self._streaming[rid]

    def _sweep_stt_uploads(self) -> None:
        now = datetime.now(tz=UTC)
        stale = [
            sid
            for sid, touched in self._stt_upload_touched.items()
            if (now - touched).total_seconds() >= _STT_UPLOAD_TTL_S
        ]
        for sid in stale:
            self._stt_uploads.pop(sid, None)
            self._stt_upload_touched.pop(sid, None)

    def _sweep_tts_sessions(self) -> None:
        now = datetime.now(tz=UTC)
        stale = [
            sid
            for sid, touched in self._tts_session_touched.items()
            if (now - touched).total_seconds() >= _TTS_SESSION_TTL_S
        ]
        for sid in stale:
            self._tts_sessions.pop(sid, None)
            self._tts_session_touched.pop(sid, None)

    async def _emit_speech_cured(self) -> None:
        try:
            await self._emit(EVENT_SPEECH_CURED, {})
        except Exception:  # noqa: BLE001 — сбой эмита не должен ронять ответ пользователю
            log.exception("llm: не удалось эмитить %s", EVENT_SPEECH_CURED)

    async def _sleep_now(self, *, quiet: bool = False) -> None:
        """``quiet`` — погасить контейнер, не прощаясь с чатами.

        Нужен для штатного роспуска («Альфред, ты свободен» → бот сам уже
        отправил прощание модели, см. bot/ai_flow.py::perform_dismissal):
        `llm_idle_sleep` рендерится в «не дождался обращения и уходит», что
        прямо противоречило бы только что сказанному. Очистка списка чатов
        при этом ВАЖНА и в тихом режиме: сразу после роспуска машину обычно
        выключают, а останов процесса шлёт тем же чатам `llm_service_restart`
        («появились другие дела») — пустой список гасит и его.
        """
        await ollama.stop(self._cfg)
        if self._cfg.container_backend == "wsl-docker":
            await self._keepalive.stop()
        self._asleep = True
        self._warmup_confirmed = False
        if quiet:
            self._active_chat_ids.clear()
            return
        if self._active_chat_ids:
            chat_ids = sorted(self._active_chat_ids)
            self._active_chat_ids.clear()
            try:
                await self._emit(EVENT_IDLE_SLEEP, {"chat_ids": chat_ids})
            except Exception:  # noqa: BLE001 — сбой эмита не должен ронять идле-таймер
                log.exception("llm: не удалось эмитить %s", EVENT_IDLE_SLEEP)

    async def notify_restart(self) -> None:
        """Перед остановом процесса (см. llm/app.py::run_llm, finally-блок) —
        если за текущее тёплое окно были активные чаты, известить их: служба
        перезапускается (деплой/апдейт/ручной restart), а не просто зависла.
        Тот же приём, что EVENT_IDLE_SLEEP (§8.5-соседняя механика), другой
        повод и текст — решение пользователя 2026-07-24. Не трогает
        _active_chat_ids/асинхронный Ollama-контейнер: процесс всё равно
        сейчас завершится, чистить состояние незачем."""
        if not self._active_chat_ids:
            return
        chat_ids = sorted(self._active_chat_ids)
        try:
            await self._emit(EVENT_SERVICE_RESTART, {"chat_ids": chat_ids})
        except Exception:  # noqa: BLE001 — сбой эмита не должен мешать останову
            log.exception("llm: не удалось эмитить %s", EVENT_SERVICE_RESTART)

    async def _maybe_sleep_idle(self) -> None:
        if self._asleep:
            return
        idle_for = (datetime.now(tz=UTC) - self._last_activity).total_seconds()
        if idle_for >= self._cfg.idle_sleep_after_s:
            await self._sleep_now()
            try:
                await self._emit(EVENT_WENT_IDLE, {})
            except Exception:  # noqa: BLE001 — сбой эмита не должен ронять идле-таймер
                log.exception("llm: не удалось эмитить %s", EVENT_WENT_IDLE)

    async def idle_loop(self) -> None:
        """Раз в минуту проверять простой; после `idle_sleep_after_s` без
        запросов — погасить контейнер (освободить VRAM) и, если были чаты,
        уведомить их через llm_idle_sleep."""
        while True:
            await asyncio.sleep(_IDLE_CHECK_INTERVAL_S)
            await self._maybe_sleep_idle()
            self._sweep_streaming()
            self._sweep_stt_uploads()
            self._sweep_tts_sessions()
