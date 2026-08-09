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
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.llm import ollama
from sa_home_bot.llm.prompt import (
    DEFAULT_PERSONA_PROMPT,
    ROUTER_SYSTEM_PROMPT,
    strip_math_notation,
)
from sa_home_bot.llm.speech_therapy import SpeechTherapist
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
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
        self._node = socket.gethostname()
        self._emit = emit
        self._last_activity = datetime.now(tz=UTC)
        self._asleep = False
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

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(self._cfg.model,),
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
                            name="think",
                            type="bool",
                            required=False,
                            title="Режим рассуждения qwen3 (не передан — модель решает сама)",
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
            "asleep": self._asleep,
            "speech_therapy": self._speech.snapshot(),
        }

    async def _touch(self, chat_id: Any = None) -> None:
        self._last_activity = datetime.now(tz=UTC)
        self._asleep = False
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
            think = args.get("think")
            if think is not None and not isinstance(think, bool):
                raise ProtoError(ERR_BAD_REQUEST, "think должен быть булевым значением")
            role = args.get("role") or "persona"
            if role not in ("persona", "router"):
                raise ProtoError(ERR_BAD_REQUEST, f"неизвестная role: {role!r}")
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
        think: bool | None,
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
