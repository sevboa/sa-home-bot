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
from sa_home_bot.llm.prompt import SYSTEM_PROMPT, apply_speech_defect, strip_math_notation
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

ACTION_ASK = "ask"
ACTION_CHAT = "chat"
ACTION_SLEEP = "sleep"

EVENT_IDLE_SLEEP = "llm_idle_sleep"
EVENT_SERVICE_RESTART = "llm_service_restart"

_IDLE_CHECK_INTERVAL_S = 60.0

EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _noop_emit(event_type: str, data: dict[str, Any]) -> None:
    pass


class LlmService:
    def __init__(self, settings: Settings, *, emit: EventEmitter = _noop_emit) -> None:
        self._cfg: LlmConfig = settings.llm
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

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(self._cfg.model,),
            actions=(
                ActionSpec(
                    id=ACTION_ASK,
                    title="Спросить Альфреда",
                    params=(
                        ActionParam(
                            name="prompt", type="string", required=True, title="Вопрос"
                        ),
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
                            title="Режим рассуждения qwen3 (по умолчанию — think_chat из конфига)",
                        ),
                    ),
                ),
                ActionSpec(id=ACTION_SLEEP, title="Уложить модель спать"),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "model": self._cfg.model,
            "asleep": self._asleep,
        }

    async def _touch(self, chat_id: Any = None) -> None:
        self._last_activity = datetime.now(tz=UTC)
        self._asleep = False
        if isinstance(chat_id, int):
            self._active_chat_ids.add(chat_id)
        if not self._keepalive.alive:
            await self._keepalive.start()

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_ASK:
            prompt = args.get("prompt")
            if not isinstance(prompt, str) or not prompt:
                raise ProtoError(ERR_BAD_REQUEST, "prompt должен быть непустой строкой")
            await self._touch(args.get("chat_id"))
            result = await ollama.generate(self._cfg, prompt, SYSTEM_PROMPT)
            response = apply_speech_defect(strip_math_notation(result.get("response", "")))
            return {"response": response, "model": self._cfg.model}
        if action == ACTION_CHAT:
            messages = args.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ProtoError(ERR_BAD_REQUEST, "messages должен быть непустым списком")
            tools = args.get("tools") or None
            think = args.get("think")
            if think is not None and not isinstance(think, bool):
                raise ProtoError(ERR_BAD_REQUEST, "think должен быть булевым значением")
            await self._touch(args.get("chat_id"))
            result = await ollama.chat(self._cfg, messages, SYSTEM_PROMPT, tools=tools, think=think)
            message = result.get("message", {})
            # Модель попросила вызвать инструмент(ы) — служба llm сама по рою
            # не ходит (нет ServiceLink к соседям, только к своей Ollama), это
            # исполняет фронтенд (см. LLM_INTEGRATION_PLAN.md §7.1). Ответ тут
            # НЕ прогоняем через apply_speech_defect — это ещё не финальный
            # текст персонажа, а служебные данные для цикла вызовов.
            tool_calls = message.get("tool_calls")
            if tool_calls:
                return {"tool_calls": tool_calls, "model": self._cfg.model}
            reply = apply_speech_defect(strip_math_notation(message.get("content", "")))
            return {"response": reply, "model": self._cfg.model}
        if action == ACTION_SLEEP:
            await self._sleep_now()
            return {"asleep": True}
        # Сервер валидирует action по describe — сюда неизвестное не доходит.
        raise ValueError(f"необъявленное действие: {action}")

    async def _sleep_now(self) -> None:
        await ollama.stop(self._cfg)
        await self._keepalive.stop()
        self._asleep = True
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

    async def idle_loop(self) -> None:
        """Раз в минуту проверять простой; после `idle_sleep_after_s` без
        запросов — погасить контейнер (освободить VRAM) и, если были чаты,
        уведомить их через llm_idle_sleep."""
        while True:
            await asyncio.sleep(_IDLE_CHECK_INTERVAL_S)
            await self._maybe_sleep_idle()
