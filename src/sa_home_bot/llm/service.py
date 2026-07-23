"""LlmService — ServiceHandler службы llm (Альфред: диалог с Ollama на этой машине).

Действия `ask`/`chat` бьют в Ollama по loopback (см. llm/ollama.py) —
никакого сетевого HTTP наружу, только протокол роя достаёт досюда
(LLM_INTEGRATION_PLAN.md §0). Собственный идле-таймер (idle_loop) сам
останавливает контейнер после простоя, не дожидаясь команды бота — у
службы нет понятия Telegram/чатов, закрытие диалога в чате бот делает
независимо и по тому же порогу конфига (bot/ai_idle.py).
"""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.llm import ollama
from sa_home_bot.llm.prompt import SYSTEM_PROMPT
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)

SERVICE_NAME = "llm"

ACTION_ASK = "ask"
ACTION_CHAT = "chat"
ACTION_SLEEP = "sleep"

_IDLE_CHECK_INTERVAL_S = 60.0


class LlmService:
    def __init__(self, settings: Settings) -> None:
        self._cfg: LlmConfig = settings.llm
        self._node = socket.gethostname()
        self._last_activity = datetime.now(tz=UTC)
        self._asleep = False

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

    def _touch(self) -> None:
        self._last_activity = datetime.now(tz=UTC)
        self._asleep = False

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_ASK:
            prompt = args.get("prompt")
            if not isinstance(prompt, str) or not prompt:
                raise ProtoError(ERR_BAD_REQUEST, "prompt должен быть непустой строкой")
            self._touch()
            result = await ollama.generate(self._cfg, prompt, SYSTEM_PROMPT)
            return {"response": result.get("response", ""), "model": self._cfg.model}
        if action == ACTION_CHAT:
            messages = args.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ProtoError(ERR_BAD_REQUEST, "messages должен быть непустым списком")
            self._touch()
            result = await ollama.chat(self._cfg, messages, SYSTEM_PROMPT)
            reply = result.get("message", {}).get("content", "")
            return {"response": reply, "model": self._cfg.model}
        if action == ACTION_SLEEP:
            await ollama.stop(self._cfg)
            self._asleep = True
            return {"asleep": True}
        # Сервер валидирует action по describe — сюда неизвестное не доходит.
        raise ValueError(f"необъявленное действие: {action}")

    async def _maybe_sleep_idle(self) -> None:
        if self._asleep:
            return
        idle_for = (datetime.now(tz=UTC) - self._last_activity).total_seconds()
        if idle_for >= self._cfg.idle_sleep_after_s:
            await ollama.stop(self._cfg)
            self._asleep = True

    async def idle_loop(self) -> None:
        """Раз в минуту проверять простой; после `idle_sleep_after_s` без
        запросов — погасить контейнер (освободить VRAM)."""
        while True:
            await asyncio.sleep(_IDLE_CHECK_INTERVAL_S)
            await self._maybe_sleep_idle()
