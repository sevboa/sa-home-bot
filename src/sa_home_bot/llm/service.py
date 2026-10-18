from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from PIL import Image

from sa_home_bot import __version__
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.llm import (
    image_prompt,
    imagegen,
    item_paste,
    ollama,
    photo_check,
    stt,
    tts,
    vision,
)
from sa_home_bot.llm.model_profiles import REASON_LEVELS, ModelProfile, load_profiles
from sa_home_bot.llm.prompt import (
    DEFAULT_PERSONA_PROMPT,
    ROUTER_SYSTEM_PROMPT,
    strip_math_notation,
)
from sa_home_bot.llm.speech_therapy import SpeechTherapist
from sa_home_bot.llm.ttl_store import TtlStore
from sa_home_bot.node.events import (
    EVENT_LLM_IDLE_SLEEP,
    EVENT_LLM_SERVICE_RESTART,
    EVENT_LLM_SPEECH_CURED,
    EVENT_LLM_WENT_IDLE,
)
from sa_home_bot.proto.addresses import LLM_SERVICE
from sa_home_bot.proto.chunks import CHUNK_BYTES
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
    new_id,
)
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.utils.ids import random_seed32

log = logging.getLogger(__name__)

SERVICE_NAME = LLM_SERVICE


def _log_ollama_timings(what: str, result: dict[str, Any]) -> None:
    def _speed(count: Any, duration_ns: Any) -> str:
        if not isinstance(count, int) or not isinstance(duration_ns, int) or duration_ns <= 0:
            return "?"
        return f"{count / (duration_ns / 1e9):.0f} ток/с"

    thinking = (result.get("message") or {}).get("thinking") or ""
    load_ns = result.get("load_duration")
    log.info(
        "llm: %s — промпт %s ток (%s), генерация %s ток (%s), thinking %d симв., "
        "загрузка модели %s, всего %s, стоп: %s",
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
        result.get("done_reason", "?"),
    )


def _usage_stats(cfg: LlmConfig, result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("prompt_eval_count", "eval_count", "done_reason"):
        value = result.get(key)
        if value is not None:
            out[key] = value
    if out:
        out["num_ctx"] = cfg.num_ctx
    return out


ACTION_ASK = "ask"
ACTION_CHAT = "chat"
ACTION_CHAT_PROGRESS = "chat_progress"
ACTION_SLEEP = "sleep"
ACTION_WARMUP = "warmup"
ACTION_LOOK_AT_PHOTO = "look_at_photo"

ACTION_TRANSCRIBE_VOICE = "transcribe_voice"
ACTION_STT_UPLOAD_CHUNK = "stt_chunk"

ACTION_SYNTHESIZE_SPEECH = "synthesize_speech"
ACTION_TTS_DOWNLOAD_CHUNK = "tts_chunk"

ACTION_GENERATE_IMAGE = "generate_image"
IMAGE_PHASE_COMPOSE = "compose"
IMAGE_PHASE_DRAW = "draw"

ACTION_ITEM_PORTRAIT = "item_portrait"
ITEM_PORTRAIT_MODEL = "turbo"
ITEM_PORTRAIT_BACKGROUND = "(isolated on plain light grey background)1.3"
_ITEM_ATTEMPTS_MAX = 4

ACTION_SET_SPEECH_CLEAR = "set_speech_clear"

ROLE_DIRECTOR = "director"
ROLE_SUMMARIZER = "summarizer"
_SUMMARIZER_OPTIONS: dict[str, Any] = {"temperature": 0, "num_predict": 2048}

PHOTO_PROCESS_FAILED_TEXT = (
    "Не получилось обработать фото — возможно, файл повреждён. Пришлите его ещё раз, сэр."
)
PHOTO_NOT_FOUND_TEXT = "Не нахожу это фото — похоже, сохранённая копия уже устарела."

_IDLE_CHECK_INTERVAL_S = 60.0
_STREAMING_ENTRY_TTL_S = 120.0
_STT_UPLOAD_TTL_S = 300.0
_INLINE_TTS_B64_BYTES = 800_000
_TTS_SESSION_TTL_S = 300.0

EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _noop_emit(event_type: str, data: dict[str, Any]) -> None:
    pass


def _bounded_number_arg(args: dict[str, Any], key: str, kind: type, lo: float, hi: float) -> Any:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtoError(ERR_BAD_REQUEST, f"{key} должен быть числом")
    if not lo <= value <= hi:
        raise ProtoError(ERR_BAD_REQUEST, f"{key} вне диапазона {lo}..{hi}")
    return kind(value)


def _imagegen_options(args: dict[str, Any]) -> dict[str, Any]:
    mode = args.get("mode") or "free"
    if mode not in imagegen.MODES:
        raise ProtoError(ERR_BAD_REQUEST, f"неизвестный режим {mode!r}")
    context = args.get("context")
    ref = None
    ref_b64 = args.get("ref_png_b64")
    if ref_b64:
        try:
            ref = Image.open(io.BytesIO(base64.b64decode(ref_b64)))
            ref.load()
        except Exception:
            raise ProtoError(ERR_BAD_REQUEST, "образец не читается как картинка") from None
    ip_scale = _bounded_number_arg(args, "ip_scale", float, 0.0, 1.5)
    if ref is not None and mode == "scene" and ip_scale is None:
        ip_scale = imagegen.DEFAULT_IP_SCALE
    model = args.get("model")
    if model is not None and model not in imagegen.MODELS:
        raise ProtoError(
            ERR_BAD_REQUEST, f"неизвестная модель {model!r}, есть: {', '.join(imagegen.MODELS)}"
        )
    if (
        model is not None
        and imagegen.MODELS[model].kind != "sd15"
        and ref is not None
        and mode == "scene"
    ):
        raise ProtoError(
            ERR_BAD_REQUEST, f"у {model} нет IP-Adapter — сцена с образцом только на SD1.5"
        )
    spec = imagegen.MODELS.get(model) if model is not None else None
    loras: list[tuple[str, float]] = []
    for item in args.get("loras") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ProtoError(ERR_BAD_REQUEST, "loras — список пар [имя, вес]")
        name, weight = item
        if name not in imagegen.LORAS:
            raise ProtoError(
                ERR_BAD_REQUEST, f"неизвестная LoRA {name!r}, есть: {', '.join(imagegen.LORAS)}"
            )
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < weight <= 2:
            raise ProtoError(ERR_BAD_REQUEST, f"вес LoRA {name} — число 0..2")
        lora = imagegen.LORAS[name]
        fits = imagegen.lora_fits(lora, spec) if spec is not None else lora.kind == "sd15"
        if not fits:
            raise ProtoError(
                ERR_BAD_REQUEST,
                f"LoRA {name} — для {lora.kind}, а модель {model or 'эталон'}",
            )
        loras.append((name, float(weight)))
    if mode == "variant" and ref is None:
        raise ProtoError(ERR_BAD_REQUEST, "для variant нужен образец")
    if ref is not None and mode not in ("variant", "scene"):
        raise ProtoError(ERR_BAD_REQUEST, "образец нужен только в variant и scene")
    emphasize = args.get("emphasize")
    if not isinstance(emphasize, list):
        emphasize = []
    paste = _paste_option(args.get("paste"), ref)
    light = args.get("light")
    return {
        "mode": mode,
        "context": context.strip() if isinstance(context, str) else "",
        "emphasize": [
            " ".join(e.split())[:80] for e in emphasize if isinstance(e, str) and e.strip()
        ][:5],
        "raw": bool(args.get("raw")),
        "style": args.get("style", True) is not False,
        "seed": _bounded_number_arg(args, "seed", int, 0, 2**32 - 1),
        "strength": _bounded_number_arg(args, "strength", float, 0.05, 1.0),
        "ip_scale": ip_scale if mode == "scene" else None,
        "steps": _bounded_number_arg(args, "steps", int, 1, 30),
        "guidance": _bounded_number_arg(args, "guidance", float, 1.0, 10.0),
        "size": _bounded_number_arg(args, "size", int, 16, 512),
        "colors": _bounded_number_arg(args, "colors", int, 0, 256),
        "model": model,
        "loras": loras,
        "ref": ref,
        "paste": paste,
        "light": " ".join(light.split())[:160] if isinstance(light, str) else "",
        "light_move": args.get("light_move") is True,
    }


def _restyle_option(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProtoError(ERR_BAD_REQUEST, "restyle — объект {model, loras, strength}")
    options = _imagegen_options(
        {"model": raw.get("model"), "loras": raw.get("loras"), "mode": "free"}
    )
    strength = _bounded_number_arg(raw, "strength", float, 0.05, 0.9)
    if strength is None:
        raise ProtoError(ERR_BAD_REQUEST, "restyle.strength — число 0.05..0.9")
    prompt = raw.get("prompt")
    return {
        "model": options["model"],
        "loras": options["loras"],
        "strength": strength,
        "prompt": " ".join(prompt.split()) if isinstance(prompt, str) else "",
    }


def _paste_option(raw: Any, ref: Image.Image | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProtoError(ERR_BAD_REQUEST, "paste — объект {key, place}")
    if ref is not None:
        raise ProtoError(ERR_BAD_REQUEST, "paste не сочетается с образцом")
    key = raw.get("key")
    if not isinstance(key, str) or not item_paste.KEY_RE.match(key):
        raise ProtoError(ERR_BAD_REQUEST, "paste.key — [a-z0-9_-], до 64 знаков")
    place = raw.get("place") or "desk"
    if place not in item_paste.PLACES:
        raise ProtoError(ERR_BAD_REQUEST, f"paste.place — одно из {', '.join(item_paste.PLACES)}")
    width = _bounded_number_arg(raw, "width", float, 0.1, 1.0)
    hint = raw.get("hint")
    return {
        "key": key,
        "place": place,
        "width": width,
        "hint": " ".join(hint.split())[:200] if isinstance(hint, str) else "",
    }


class LlmService:
    def __init__(
        self,
        settings: Settings,
        *,
        emit: EventEmitter = _noop_emit,
        speech_rand: Callable[[], float] | None = None,
    ) -> None:
        self._cfg: LlmConfig = settings.llm
        self._persona_prompt = self._cfg.persona_prompt or DEFAULT_PERSONA_PROMPT
        if not self._cfg.persona_prompt:
            log.warning(
                "settings.llm.persona_prompt пуст — используется безликая заглушка "
                "DEFAULT_PERSONA_PROMPT вместо характера персонажа"
            )
        registry = load_profiles(self._cfg.model_profiles_path)
        self._profile: ModelProfile
        self._profile, matched = registry.resolve(self._cfg.model)
        if "num_ctx" not in self._cfg.model_fields_set:
            self._cfg.num_ctx = self._profile.num_ctx
        (log.warning if not matched else log.info)(
            "llm: model %s → профиль %s (control=%s, router=%s, num_ctx=%s, thinking_hidden=%s)%s",
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
        self._last_activity = utc_now()
        self._asleep = False
        self._warmup_confirmed = False
        self._active_chat_ids: set[int] = set()
        self._keepalive = ollama.WslKeepalive(
            self._cfg, duration_s=self._cfg.idle_sleep_after_s + 60.0
        )
        self._speech = SpeechTherapist(
            self._cfg, **({"rand": speech_rand} if speech_rand is not None else {})
        )
        self._streaming: TtlStore[dict[str, Any]] = TtlStore(_STREAMING_ENTRY_TTL_S)
        self._stt_uploads: TtlStore[bytearray] = TtlStore(_STT_UPLOAD_TTL_S)
        self._tts_sessions: TtlStore[bytes] = TtlStore(_TTS_SESSION_TTL_S)

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(self._cfg.model,),
            model_profile=replace(self._profile.summary(), num_ctx=self._cfg.num_ctx).to_payload(),
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
                                "(по умолчанию, Альфред), 'router' (служебный "
                                "триаж без персонажа, см. llm/prompt.py) или "
                                "'director' (Ведущий интерактивов: system из "
                                "аргумента system, ответ JSON)"
                            ),
                        ),
                        ActionParam(
                            name="system",
                            type="string",
                            required=False,
                            title="Системный промпт для role=director",
                        ),
                        ActionParam(
                            name="user_id",
                            type="int",
                            required=False,
                            title="Кому адресован ответ (для «чистой речи», Этап 47)",
                        ),
                        ActionParam(
                            name="speech_clear",
                            type="bool",
                            required=False,
                            title="Чистая речь для user_id (зеркало БД бота, Этап 47)",
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
                            name="raw_image",
                            type="string",
                            required=False,
                            title=(
                                "Картинка base64 вместо файла по photo_key — своя "
                                "картинка Альфреда из базы бота (не сохраняется)"
                            ),
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
                    id=ACTION_GENERATE_IMAGE,
                    title="Нарисовать картинку",
                    params=(
                        ActionParam(
                            name="description",
                            type="string",
                            required=False,
                            title="Что нарисовать — свободно, любой язык и длина",
                        ),
                        ActionParam(
                            name="prompt",
                            type="string",
                            required=False,
                            title="Готовый промпт по-английски (если нет description)",
                        ),
                        ActionParam(
                            name="negative",
                            type="string",
                            required=False,
                            title="Чего на картинке быть не должно (по-английски)",
                        ),
                        ActionParam(
                            name="chat_id",
                            type="int",
                            required=False,
                            title="Chat, откуда пришёл запрос",
                        ),
                        ActionParam(
                            name="keep_key",
                            type="string",
                            required=False,
                            title=(
                                "Снимок Альфреда: сохранить 512-оригинал под этим "
                                "ключом и сверить его зрением (ответ — поле inspect)"
                            ),
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_ITEM_PORTRAIT,
                    title="Портрет сюжетного предмета + вырезка для вставки в сцены",
                    params=(
                        ActionParam(
                            name="prompt",
                            type="string",
                            required=True,
                            title="Архетип и черты по-английски, веса compel «(…)1.3»",
                        ),
                        ActionParam(name="key", type="string", required=True, title="Ключ вырезки"),
                        ActionParam(name="seed", type="int", required=False, title="Зерно"),
                        ActionParam(
                            name="attempts",
                            type="int",
                            required=False,
                            title="Сколько зёрен пробовать при промахе проверки",
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
                ActionSpec(
                    id=ACTION_SET_SPEECH_CLEAR,
                    title="Включить/выключить картавость для гостя (Этап 47)",
                    params=(
                        ActionParam(name="user_id", type="int", required=True, title="Гость"),
                        ActionParam(name="clear", type="bool", required=True, title="Чистая речь"),
                    ),
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "model": self._cfg.model,
            "asleep": self._asleep or not self._warmup_confirmed,
            "speech_therapy": self._speech.snapshot(),
        }

    def _think_arg(self, args: dict[str, Any]) -> bool | str | None:
        reason = args.get("reason")
        if not isinstance(reason, str) or reason not in REASON_LEVELS:
            think = args.get("think")
            if think is not None and not isinstance(think, bool):
                raise ProtoError(ERR_BAD_REQUEST, "think должен быть булевым значением")
            reason = "high" if think is True else "off"
        return self._profile.think_arg(reason)

    async def _touch(self, chat_id: Any = None) -> None:
        self._last_activity = utc_now()
        self._asleep = False
        self._warmup_confirmed = True
        if isinstance(chat_id, int):
            self._active_chat_ids.add(chat_id)
        if self._cfg.container_backend == "wsl-docker" and not self._keepalive.alive:
            await self._keepalive.start()

    def _speech_target(self, args: dict[str, Any]) -> int | None:
        user_id = args.get("user_id")
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return None
        clear = args.get("speech_clear")
        if isinstance(clear, bool):
            self._speech.set_clear(user_id, clear)
        return user_id

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_SET_SPEECH_CLEAR:
            user_id = args.get("user_id")
            clear = args.get("clear")
            if not isinstance(user_id, int) or isinstance(user_id, bool):
                raise ProtoError(ERR_BAD_REQUEST, "user_id должен быть целым числом")
            if not isinstance(clear, bool):
                raise ProtoError(ERR_BAD_REQUEST, "clear должен быть булевым значением")
            changed = self._speech.set_clear(user_id, clear)
            return {"user_id": user_id, "clear": clear, "changed": changed}
        if action == ACTION_ASK:
            prompt = args.get("prompt")
            if not isinstance(prompt, str) or not prompt:
                raise ProtoError(ERR_BAD_REQUEST, "prompt должен быть непустой строкой")
            chat_id = args.get("chat_id")
            await self._touch(chat_id)
            speaker = self._speech_target(args)
            result = await ollama.generate(self._cfg, prompt, self._persona_prompt)
            _log_ollama_timings("ask", result)
            cleaned = strip_math_notation(result.get("response", ""))
            response, remark, just_cured = self._speech.process(cleaned, chat_id, speaker)
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
            if role not in ("persona", "router", ROLE_DIRECTOR, ROLE_SUMMARIZER):
                raise ProtoError(ERR_BAD_REQUEST, f"неизвестная role: {role!r}")
            if role == ROLE_DIRECTOR:
                return await self._director_chat(messages, args)
            if role == ROLE_SUMMARIZER:
                return await self._summarizer_chat(messages, args)
            think = self._think_arg(args)
            system = self._persona_prompt
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
            speaker = self._speech_target(args)
            request_id = args.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not request_id):
                raise ProtoError(ERR_BAD_REQUEST, "request_id должен быть непустой строкой")
            if request_id:
                result = await self._chat_streamed(
                    request_id, messages, system, tools=tools, think=think
                )
            else:
                result = await ollama.chat(self._cfg, messages, system, tools=tools, think=think)
            _log_ollama_timings(f"chat/{role}", result)
            message = result.get("message", {})
            tool_calls = message.get("tool_calls")
            if tool_calls:
                return {
                    "tool_calls": tool_calls,
                    "model": self._cfg.model,
                    **_usage_stats(self._cfg, result),
                }
            cleaned = strip_math_notation(message.get("content", ""))
            reply, remark, just_cured = self._speech.process(cleaned, chat_id, speaker)
            if just_cured:
                await self._emit_speech_cured()
            out: dict[str, Any] = {
                "response": reply,
                "model": self._cfg.model,
                **_usage_stats(self._cfg, result),
            }
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
            speaker = self._speech_target(args)
            raw_image = args.get("raw_image")
            if isinstance(raw_image, str) and raw_image:
                try:
                    stored_b64 = await asyncio.to_thread(
                        vision.prepare, base64.b64decode(raw_image, validate=True), self._cfg
                    )
                except Exception:
                    log.warning("vision: картинка %s не разобрана", photo_key, exc_info=True)
                    return {"response": PHOTO_NOT_FOUND_TEXT, "model": self._cfg.model}
            else:
                stored_b64 = await asyncio.to_thread(vision.load_stored, photo_key, self._cfg)
            if stored_b64 is None:
                return {"response": PHOTO_NOT_FOUND_TEXT, "model": self._cfg.model}
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
            reply, remark, just_cured = self._speech.process(cleaned, chat_id, speaker)
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
            buf = self._stt_uploads.get(session_id)
            if buf is None:
                buf = bytearray()
                self._stt_uploads.put(session_id, buf)
            if offset != len(buf):
                raise ProtoError(
                    ERR_BAD_REQUEST, f"неожиданный offset: {offset}, ожидался {len(buf)}"
                )
            try:
                chunk = base64.b64decode(data_b64, validate=True)
            except Exception as exc:
                raise ProtoError(ERR_BAD_REQUEST, "data_b64 не является валидным base64") from exc
            buf.extend(chunk)
            self._stt_uploads.touch(session_id)
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
                buf = self._stt_uploads.pop(session_id)
                if buf is None:
                    raise ProtoError(ERR_BAD_REQUEST, f"неизвестная сессия загрузки: {session_id}")
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
            session_id = new_id()
            self._tts_sessions.put(session_id, audio_bytes)
            return {
                "session_id": session_id,
                "size": len(audio_bytes),
                "sha256": hashlib.sha256(audio_bytes).hexdigest(),
                "format": "ogg",
            }
        if action == ACTION_GENERATE_IMAGE:
            request_id = args.get("request_id")
            try:
                return await self._generate_image(args)
            finally:
                self._image_phase(request_id, None)
        if action == ACTION_ITEM_PORTRAIT:
            if not self._cfg.imagegen_enabled:
                raise ProtoError(ERR_BAD_REQUEST, "генерация картинок на этой ноде выключена")
            return await self._item_portrait(args)
        if action == ACTION_TTS_DOWNLOAD_CHUNK:
            session_id = args.get("session_id")
            offset = args.get("offset")
            length = args.get("length") or CHUNK_BYTES
            if not isinstance(session_id, str) or not session_id:
                raise ProtoError(ERR_BAD_REQUEST, "session_id должен быть непустой строкой")
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise ProtoError(ERR_BAD_REQUEST, "offset должен быть неотрицательным целым числом")
            data = self._tts_sessions.get(session_id)
            if data is None:
                raise ProtoError(ERR_BAD_REQUEST, f"неизвестная сессия синтеза: {session_id}")
            chunk = data[offset : offset + int(length)]
            eof = offset + len(chunk) >= len(data)
            if eof:
                self._tts_sessions.pop(session_id)
            else:
                self._tts_sessions.touch(session_id)
            return {"data_b64": base64.b64encode(chunk).decode(), "offset": offset, "eof": eof}
        if action == ACTION_CHAT_PROGRESS:
            request_id = args.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                raise ProtoError(ERR_BAD_REQUEST, "request_id должен быть непустой строкой")
            entry = self._streaming.get(request_id)
            if entry is None:
                return {"partial": "", "done": False}
            return {"partial": entry["partial"], "done": entry["done"]}
        if action == ACTION_SLEEP:
            await self._sleep_now(quiet=bool(args.get("quiet")))
            return {"asleep": True}
        if action == ACTION_WARMUP:
            await ollama.ensure_running(self._cfg)
            await ollama.preload(self._cfg)
            await self._touch()
            return {"asleep": False}
        raise ValueError(f"необъявленное действие: {action}")

    async def _paste_item(
        self,
        scene: dict[str, Any],
        options: dict[str, Any],
        where: item_paste.Placement | None,
    ) -> tuple[dict[str, Any], tuple[int, int, int, int]]:
        paste = options["paste"]
        path = item_paste.cut_path(self._cfg, paste["key"])
        cut = await asyncio.to_thread(lambda: Image.open(path).convert("RGBA"))
        where = where or item_paste.PLACEMENTS[paste["place"]]
        if paste["width"] is not None:
            where = replace(where, width=paste["width"] * scene["original"].width)
        pasted, mask, box = await asyncio.to_thread(item_paste.paste, scene["original"], cut, where)
        prompt = scene["prompt"]
        if paste["hint"]:
            prompt = f"{prompt}, {paste['hint']}"
        weight = self._cfg.item_mood_lora_weight
        try:
            harmonized = await imagegen.generate_image(
                prompt,
                scene["full_negative"],
                self._cfg,
                seed=scene["seed"],
                ref=pasted,
                strength=self._cfg.item_harmonize_strength,
                style=options["style"] and not options["raw"],
                fit=not options["raw"],
                size=options["size"],
                colors=options["colors"],
                model=options["model"],
                loras=[(name, weight) for name, _ in options["loras"]] if weight > 0 else [],
            )
        except Exception:
            log.warning("imagegen: вставка предмета не удалась", exc_info=True)
            raise ProtoError(ERR_INTERNAL, "не удалось нарисовать картинку") from None
        final = await asyncio.to_thread(item_paste.restore, harmonized["original"], pasted, mask)
        png, width, height = imagegen.shrink_to_png(
            final,
            options["size"] or self._cfg.imagegen_size,
            self._cfg.imagegen_colors if options["colors"] is None else options["colors"],
        )
        log.info(
            "imagegen: предмет %s (%s) вставлен в %s за %.1fс",
            paste["key"],
            paste["place"],
            box,
            harmonized["seconds"],
        )
        return {
            **scene,
            "png": png,
            "width": width,
            "height": height,
            "original": final,
            "seconds": scene["seconds"] + harmonized["seconds"],
        }, box

    async def _desk_placement(self, scene: Image.Image, place: str) -> item_paste.Placement | None:
        try:
            image_b64 = await asyncio.to_thread(vision.jpeg_b64, scene)
            result = await ollama.chat(
                self._cfg,
                [{"role": "user", "content": item_paste.DESK_BOX_QUESTION, "images": [image_b64]}],
                item_paste.DESK_BOX_SYSTEM,
                tools=None,
                think=self._profile.think_arg("off"),
                response_format="json",
                options={"temperature": 0, "num_predict": 128},
            )
            data = json.loads(result.get("message", {}).get("content", ""))
        except Exception:
            log.warning("imagegen: рамку стола не узнать", exc_info=True)
            return None
        box = data.get(item_paste.DESK_BOX_KEY) if isinstance(data, dict) else None
        where = item_paste.desk_placement(box, scene.width, place)
        log.info("imagegen: стол %s → %s", box, where)
        return where

    async def _generate_image(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self._cfg.imagegen_enabled:
            raise ProtoError(ERR_BAD_REQUEST, "генерация картинок на этой ноде выключена")
        description = args.get("description")
        prompt = args.get("prompt")
        if isinstance(description, str) and description.strip():
            source = description.strip()
        elif isinstance(prompt, str) and prompt.strip():
            source = None
        else:
            raise ProtoError(ERR_BAD_REQUEST, "нужно непустое description или prompt")
        negative = args.get("negative")
        negative = negative.strip() if isinstance(negative, str) else ""
        options = _imagegen_options(args)
        paste = options["paste"]
        if paste is not None and not item_paste.cut_path(self._cfg, paste["key"]).exists():
            raise ProtoError(ERR_BAD_REQUEST, f"нет вырезки предмета {paste['key']!r}")
        await self._touch(args.get("chat_id"))
        prompt_seconds = 0.0
        if source is not None:
            if options["raw"]:
                prompt = source
            elif self._cfg.imagegen_prompt_agent:
                self._image_phase(args.get("request_id"), IMAGE_PHASE_COMPOSE)
                request = image_prompt.build_request(
                    source, options["mode"], options["context"], options["emphasize"]
                )
                started = time.monotonic()
                prompt, agent_negative = await image_prompt.compose(
                    request, self._cfg, think=self._profile.think_arg("off")
                )
                prompt_seconds = time.monotonic() - started
                negative = negative or agent_negative
                log.info("imagegen: промптер за %.1fс: %r -> %r", prompt_seconds, request, prompt)
            else:
                prompt = source
        if not options["raw"]:
            negative = negative or self._cfg.imagegen_negative
            extra = args.get("negative_extra")
            if isinstance(extra, str) and extra.strip():
                negative = f"{negative}, {extra.strip()}" if negative else extra.strip()
            if "bad hands" not in negative:
                negative = ", ".join(x for x in (negative, image_prompt.HANDS_NEGATIVE) if x)
            if options["light"]:
                prompt = image_prompt.put_second(
                    prompt, options["light"], move=options["light_move"]
                )
            if paste is not None and paste["place"] == "desk":
                prompt = f"{item_paste.DESK_COMPOSITION}, {prompt.strip()}"
        self._image_phase(args.get("request_id"), IMAGE_PHASE_DRAW)

        async def draw(seed: int | None) -> dict[str, Any]:
            try:
                return await imagegen.generate_image(
                    prompt.strip(),
                    negative,
                    self._cfg,
                    seed=seed,
                    ref=options["ref"],
                    strength=options["strength"],
                    ip_scale=options["ip_scale"],
                    steps=options["steps"],
                    guidance=options["guidance"],
                    style=options["style"] and not options["raw"],
                    fit=not options["raw"],
                    size=options["size"],
                    colors=options["colors"],
                    model=options["model"],
                    loras=options["loras"],
                )
            except imagegen.ImagegenError as exc:
                log.warning("imagegen: %s", exc)
                raise ProtoError(ERR_INTERNAL, str(exc)) from None
            except Exception:
                log.warning("imagegen: не удалось сгенерировать картинку", exc_info=True)
                raise ProtoError(ERR_INTERNAL, "не удалось нарисовать картинку") from None

        result = await draw(options["seed"])
        item_box = None
        if paste is not None:
            where = await self._desk_placement(result["original"], paste["place"])
            redraws = item_paste.DESK_REDRAWS if options["seed"] is None else 0
            for _ in range(redraws):
                if where is not None:
                    break
                spent = result["seconds"]
                result = await draw(None)
                result["seconds"] += spent
                where = await self._desk_placement(result["original"], paste["place"])
                log.info("imagegen: фон перерисован под стол (seed %s)", result.get("seed"))
            result, item_box = await self._paste_item(result, options, where)
        inspection = await self._inspect_snapshot(args, result.get("original"))
        return {
            **({"inspect": inspection} if inspection is not None else {}),
            "png_b64": base64.b64encode(result["png"]).decode(),
            "width": result["width"],
            "height": result["height"],
            "seconds": round(result["seconds"], 1),
            "prompt": result.get("prompt", prompt.strip()),
            "prompt_seconds": round(prompt_seconds, 1),
            "full_prompt": result.get("full_prompt"),
            "full_negative": result.get("full_negative"),
            "tokens": result.get("tokens"),
            "seed": result.get("seed"),
            "steps": result.get("steps"),
            "colors": result.get("colors"),
            "model": result.get("model"),
            "loras": result.get("loras"),
            **({"item_box": list(item_box)} if item_box is not None else {}),
        }

    def _image_phase(self, request_id: Any, phase: str | None) -> None:
        if not isinstance(request_id, str) or not request_id:
            return
        if phase is not None:
            self._streaming.put(request_id, {"partial": phase, "done": False}, touched=False)
            return
        self._finish_streaming(request_id)

    def _finish_streaming(self, request_id: str) -> None:
        entry = self._streaming.get(request_id)
        if entry is not None:
            entry["done"] = True
            self._streaming.touch(request_id)

    async def _item_portrait(self, args: dict[str, Any]) -> dict[str, Any]:
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProtoError(ERR_BAD_REQUEST, "нужен непустой prompt")
        key = args.get("key")
        if not isinstance(key, str) or not item_paste.KEY_RE.match(key):
            raise ProtoError(ERR_BAD_REQUEST, "key — [a-z0-9_-], до 64 знаков")
        seed = _bounded_number_arg(args, "seed", int, 0, 2**32 - 1)
        if seed is None:
            seed = random_seed32()
        attempts = _bounded_number_arg(args, "attempts", int, 1, _ITEM_ATTEMPTS_MAX) or 3
        _restyle_option(args.get("restyle"))
        raw_checks = args.get("checks")
        checks = [
            " ".join(c.split())
            for c in (raw_checks if isinstance(raw_checks, list) else [])
            if isinstance(c, str) and c.strip()
        ][:5]
        await self._touch(args.get("chat_id"))
        full = f"{' '.join(prompt.split())}, {ITEM_PORTRAIT_BACKGROUND}"
        best: tuple[dict[str, Any], dict[str, Any] | None] | None = None
        spent = 0.0
        tried = 0
        loop = asyncio.get_running_loop()
        think = self._profile.think_arg("off")
        for n in range(attempts):
            tried = n + 1
            preview_check = self._cfg.item_preview_check and bool(checks) and n < attempts - 1
            verdict: dict[str, Any] = {}

            def preview(image: Image.Image, verdict: dict[str, Any] = verdict) -> bool:
                future = asyncio.run_coroutine_threadsafe(
                    photo_check.inspect(vision.jpeg_b64(image), checks, self._cfg, think=think),
                    loop,
                )
                try:
                    inspection = future.result(timeout=self._cfg.request_timeout_s)
                except Exception:
                    log.warning("imagegen: черновик %s не проверен", key, exc_info=True)
                    return True
                verdict["inspection"] = inspection
                return not inspection or not inspection["missing"]

            started = time.monotonic()
            try:
                result = await imagegen.generate_image(
                    full,
                    self._cfg.imagegen_negative,
                    self._cfg,
                    seed=(seed + n) % 2**32,
                    model=ITEM_PORTRAIT_MODEL,
                    fit=False,
                    weighted=True,
                    preview=preview if preview_check else None,
                )
            except imagegen.PreviewRejected:
                spent += time.monotonic() - started
                missing = (verdict.get("inspection") or {}).get("missing")
                log.info("imagegen: черновик %s, зерно %s: нет %s", key, seed + n, missing)
                continue
            except Exception:
                log.warning("imagegen: портрет предмета %s не нарисован", key, exc_info=True)
                raise ProtoError(ERR_INTERNAL, "не удалось нарисовать предмет") from None
            spent += result["seconds"]
            inspection = verdict.get("inspection")
            if checks and inspection is None:
                image_b64 = await asyncio.to_thread(vision.jpeg_b64, result["original"])
                inspection = await photo_check.inspect(image_b64, checks, self._cfg, think=think)
            misses = len(inspection["missing"]) if inspection else 0
            if best is None or misses < len(best[1]["missing"] if best[1] else []):
                best = (result, inspection)
            if misses == 0:
                break
            log.info(
                "imagegen: портрет %s, зерно %s: нет %s",
                key,
                result["seed"],
                inspection["missing"] if inspection else [],
            )
        assert best is not None
        result, inspection = best
        restyle = _restyle_option(args.get("restyle"))
        if restyle is not None:
            try:
                cursed = await imagegen.generate_image(
                    f"{restyle['prompt'] or prompt}, isolated on plain light grey background",
                    self._cfg.imagegen_negative,
                    self._cfg,
                    seed=result["seed"],
                    ref=result["original"],
                    strength=restyle["strength"],
                    model=restyle["model"],
                    loras=restyle["loras"],
                    fit=False,
                )
            except Exception:
                log.warning("imagegen: перекраска предмета %s не удалась", key, exc_info=True)
            else:
                spent += cursed["seconds"]
                result = {**cursed, "seed": result["seed"]}
        path = item_paste.cut_path(self._cfg, key)

        def _save() -> None:
            cut = item_paste.cut_out(result["original"], self._cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            cut.save(path, format="PNG")

        try:
            await asyncio.to_thread(_save)
        except Exception:
            log.warning("imagegen: вырезка предмета %s не удалась", key, exc_info=True)
            raise ProtoError(ERR_INTERNAL, "не удалось вырезать предмет") from None
        log.info("imagegen: портрет %s готов (%d попыток, %.1fс)", key, tried, spent)
        return {
            "png_b64": base64.b64encode(result["png"]).decode(),
            "width": result["width"],
            "height": result["height"],
            "seed": result["seed"],
            "attempts": tried,
            "seconds": round(spent, 1),
            "full_prompt": result.get("full_prompt"),
            "seen": inspection["description"] if inspection else "",
            "missing": inspection["missing"] if inspection else [],
            "answers": inspection.get("answers", {}) if inspection else {},
        }

    async def _inspect_snapshot(self, args: dict[str, Any], original: Any) -> dict[str, Any] | None:
        keep_key = args.get("keep_key")
        if original is None or not isinstance(keep_key, str) or not keep_key or "/" in keep_key:
            return None
        raw_expect = args.get("expect")
        expect = [
            " ".join(item.split())
            for item in (raw_expect if isinstance(raw_expect, list) else [])
            if isinstance(item, str) and item.strip()
        ][:3]
        try:
            image_b64 = await asyncio.to_thread(
                photo_check.save_original, original, keep_key, self._cfg
            )
        except Exception:
            log.warning("photo_check: оригинал %s не сохранён", keep_key, exc_info=True)
            return None
        started = time.monotonic()
        inspection = await photo_check.inspect(
            image_b64, expect, self._cfg, think=self._profile.think_arg("off")
        )
        if inspection is not None:
            log.info("photo_check: сверка %s за %.1fс", keep_key, time.monotonic() - started)
        return inspection

    async def _chat_streamed(
        self,
        request_id: str,
        messages: list[dict[str, Any]],
        system: str,
        *,
        tools: list[dict[str, Any]] | None,
        think: bool | str | None,
    ) -> dict[str, Any]:
        self._streaming.put(request_id, {"partial": "", "done": False}, touched=False)

        def _on_chunk(text: str) -> None:
            entry = self._streaming.get(request_id)
            if entry is not None:
                entry["partial"] = text

        try:
            return await ollama.chat_stream(
                self._cfg, messages, system, _on_chunk, tools=tools, think=think
            )
        finally:
            self._finish_streaming(request_id)

    async def _director_chat(self, messages: list[Any], args: dict[str, Any]) -> dict[str, Any]:
        system = args.get("system")
        if not isinstance(system, str) or not system:
            raise ProtoError(ERR_BAD_REQUEST, "для role=director нужен непустой system")
        await self._touch(args.get("chat_id"))
        result = await ollama.chat(
            self._cfg,
            messages,
            system,
            tools=None,
            think=self._profile.think_arg("off"),
            response_format="json",
        )
        _log_ollama_timings("chat/director", result)
        return {
            "response": result.get("message", {}).get("content", ""),
            "model": self._cfg.model,
        }

    async def _summarizer_chat(self, messages: list[Any], args: dict[str, Any]) -> dict[str, Any]:
        system = args.get("system")
        if not isinstance(system, str) or not system:
            raise ProtoError(ERR_BAD_REQUEST, "для role=summarizer нужен непустой system")
        await self._touch(args.get("chat_id"))
        result = await ollama.chat(
            self._cfg,
            messages,
            system,
            tools=None,
            think=self._profile.think_arg("off"),
            options=_SUMMARIZER_OPTIONS,
        )
        _log_ollama_timings("chat/summarizer", result)
        return {
            "response": (result.get("message") or {}).get("content", ""),
            "model": self._cfg.model,
            **_usage_stats(self._cfg, result),
        }

    async def _emit_speech_cured(self) -> None:
        try:
            await self._emit(EVENT_LLM_SPEECH_CURED, {})
        except Exception:
            log.exception("llm: не удалось эмитить %s", EVENT_LLM_SPEECH_CURED)

    async def _sleep_now(self, *, quiet: bool = False) -> None:
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
                await self._emit(EVENT_LLM_IDLE_SLEEP, {"chat_ids": chat_ids})
            except Exception:
                log.exception("llm: не удалось эмитить %s", EVENT_LLM_IDLE_SLEEP)

    async def notify_restart(self) -> None:
        if not self._active_chat_ids:
            return
        chat_ids = sorted(self._active_chat_ids)
        try:
            await self._emit(EVENT_LLM_SERVICE_RESTART, {"chat_ids": chat_ids})
        except Exception:
            log.exception("llm: не удалось эмитить %s", EVENT_LLM_SERVICE_RESTART)

    async def _maybe_sleep_idle(self) -> None:
        if self._asleep:
            return
        idle_for = (utc_now() - self._last_activity).total_seconds()
        if idle_for >= self._cfg.idle_sleep_after_s:
            await self._sleep_now()
            try:
                await self._emit(EVENT_LLM_WENT_IDLE, {})
            except Exception:
                log.exception("llm: не удалось эмитить %s", EVENT_LLM_WENT_IDLE)

    async def idle_loop(self) -> None:
        while True:
            await asyncio.sleep(_IDLE_CHECK_INTERVAL_S)
            await self._maybe_sleep_idle()
            self._streaming.sweep()
            self._stt_uploads.sweep()
            self._tts_sessions.sweep()
