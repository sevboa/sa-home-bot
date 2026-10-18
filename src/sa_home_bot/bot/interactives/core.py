from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sa_home_bot.bot.interactives import radio
from sa_home_bot.bot.interactives.base import STATUS_ACTIVE, InteractiveStore, Run, Scenario
from sa_home_bot.bot.interactives.registry import REGISTRY
from sa_home_bot.bot.interactives.transylvania import Transylvania
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.utils.clock import utc_now

log = logging.getLogger(__name__)


@dataclass
class _Queued:
    forms: list[tuple[str, str]] = field(default_factory=list)
    user_id: int | None = None
    items: list[tuple[int, str]] = field(default_factory=list)


class EngineCore:
    def __init__(
        self,
        store: Store,
        notifier: Any,
        settings: Settings,
        get_node_link: Callable[[], ServiceLink | None],
        *,
        now: Callable[[], datetime] = utc_now,
        choose: Callable[[tuple[str, ...]], str] = random.choice,
        rng: Callable[[], float] = random.random,
        transylvania: Transylvania | None = None,
    ) -> None:
        self._transylvania = transylvania or Transylvania()
        self._photo_busy: set[int] = set()
        self._photo_jobs: dict[int, asyncio.Task] = {}
        self._card_jobs: dict[int, asyncio.Task] = {}
        self._photo_status: dict[int, Callable[[str], Awaitable[None]]] = {}
        self._photo_join: dict[int, dict[str, Any]] = {}
        self._photo_described: set[int] = set()
        self._photo_tasks: set[asyncio.Task] = set()
        self._portrait_busy: set[int] = set()
        self._store = store
        self._state = InteractiveStore(store)
        self._notifier = notifier
        self._settings = settings
        self._get_node_link = get_node_link
        self._now = now
        self._choose = choose
        self._rng = rng
        self._queued: dict[int, _Queued] = {}

    async def speech_clear(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return await self._state.get_effect(radio.EFFECT_SPEECH_CLEAR, user_id) == "1"

    @property
    def _can_send_photos(self) -> bool:
        return hasattr(self._notifier, "send_photo_ex")

    def _pinned(self, chat_id: int) -> bool:
        return chat_id in self._settings.llm.speech_therapy_pinned_chat_ids

    def canary_ok(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._settings.llm.interactives_canary_user_ids

    def canary_scope(
        self, chat_id: int | None, user_id: int | None, *, private: bool
    ) -> tuple[int, int] | None:
        if chat_id is None or user_id is None or not self.canary_ok(user_id):
            return None
        if private and chat_id != user_id:
            return None
        return chat_id, user_id

    def scenarios_for(self, user_id: int | None) -> list[Scenario]:
        canary = self.canary_ok(user_id)
        return [s for s in REGISTRY.values() if canary or not s.canary]

    async def _scene_run(self, chat_id: int, user_id: int | None) -> Run | None:
        for scenario in self.scenarios_for(user_id):
            run = await self._state.load_run(chat_id, scenario.id)
            if run is not None and run.status == STATUS_ACTIVE:
                return run
        return None

    def _spawn(self, coro: Awaitable[Any], what: str) -> asyncio.Task:
        async def run() -> None:
            try:
                await coro
            except Exception:
                log.exception("interactives: %s не удалась", what)

        task = asyncio.create_task(run())
        return self._track(task)

    def _track(self, task: asyncio.Task) -> asyncio.Task:
        self._photo_tasks.add(task)
        task.add_done_callback(self._photo_tasks.discard)
        return task

    async def set_opted_out(self, chat_id: int, opted_out: bool) -> None:
        await self._state.set_opted_out(chat_id, opted_out)

    async def is_opted_out(self, chat_id: int) -> bool:
        return await self._state.is_opted_out(chat_id)
