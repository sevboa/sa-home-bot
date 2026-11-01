from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sa_home_bot.node.events import (
    EVENT_LLM_CPU_TASK_FINISHED,
    EVENT_LLM_CPU_TASK_STARTED,
    EVENT_LLM_NIGHT_MODE,
    LLM_EVENT_NIGHT,
    LLM_EVENT_TASK_ID,
    LLM_STATE_CPU_TASKS,
    LLM_STATE_NIGHT,
)
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.messages import Envelope, ProtoError
from sa_home_bot.utils.lifespan import cancel_task

log = logging.getLogger(__name__)

Action = Callable[[], Awaitable[None]]
NightAction = Callable[[bool], Awaitable[None]]

RECONNECT_DELAY_S = 5.0
LINK_ERRORS = (ConnectionError, OSError, TimeoutError, ProtoError)


async def nothing() -> None:
    return None


async def ignore_night(night: bool) -> None:
    return None


class NeverFrozen:
    frozen = False

    def frozen_total(self) -> float:
        return 0.0

    async def wait_thawed(self) -> None:
        return None


class NeighborGate:
    def __init__(
        self,
        *,
        freeze: Action,
        thaw: Action,
        freeze_max_s: float,
        on_night: NightAction = ignore_night,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._freeze = freeze
        self._thaw = thaw
        self._freeze_max_s = freeze_max_s
        self._on_night = on_night
        self._clock = clock
        self._tasks: set[str] = set()
        self._frozen_since: float | None = None
        self._frozen_done = 0.0
        self._thawed = asyncio.Event()
        self._thawed.set()
        self._expiry: asyncio.Task[None] | None = None
        self.night = False

    @property
    def frozen(self) -> bool:
        return self._frozen_since is not None

    def frozen_total(self) -> float:
        ongoing = 0.0 if self._frozen_since is None else self._clock() - self._frozen_since
        return self._frozen_done + ongoing

    async def wait_thawed(self) -> None:
        await self._thawed.wait()

    async def task_started(self, task_id: str) -> None:
        self._tasks.add(task_id)
        await self._ensure_frozen()

    async def task_finished(self, task_id: str) -> None:
        self._tasks.discard(task_id)
        if not self._tasks:
            await self._ensure_thawed()

    async def set_night(self, night: bool) -> None:
        if night == self.night:
            return
        self.night = night
        await self._guarded(self._on_night(night), "смена ночного режима")

    async def sync(self, task_ids: list[str], night: bool) -> None:
        self._tasks = set(task_ids)
        await self.set_night(night)
        if self._tasks:
            await self._ensure_frozen()
        else:
            await self._ensure_thawed()

    async def disconnected(self) -> None:
        self._tasks.clear()
        await self._ensure_thawed()

    async def close(self) -> None:
        await cancel_task(self._expiry)
        self._expiry = None
        await self._ensure_thawed()

    async def handle_event(self, env: Envelope) -> None:
        event = env.payload.get("event")
        data = env.payload.get("data") or {}
        if event == EVENT_LLM_CPU_TASK_STARTED:
            await self.task_started(str(data.get(LLM_EVENT_TASK_ID, "")))
        elif event == EVENT_LLM_CPU_TASK_FINISHED:
            await self.task_finished(str(data.get(LLM_EVENT_TASK_ID, "")))
        elif event == EVENT_LLM_NIGHT_MODE:
            await self.set_night(bool(data.get(LLM_EVENT_NIGHT)))

    async def _ensure_frozen(self) -> None:
        if self.frozen:
            return
        self._frozen_since = self._clock()
        self._thawed.clear()
        await self._guarded(self._freeze(), "заморозка")
        self._expiry = asyncio.create_task(self._expire())

    async def _ensure_thawed(self) -> None:
        if not self.frozen:
            return
        current = asyncio.current_task()
        if self._expiry is not None and self._expiry is not current:
            await cancel_task(self._expiry)
        self._expiry = None
        assert self._frozen_since is not None
        self._frozen_done += self._clock() - self._frozen_since
        self._frozen_since = None
        self._thawed.set()
        await self._guarded(self._thaw(), "разморозка")

    async def _expire(self) -> None:
        await asyncio.sleep(self._freeze_max_s)
        log.warning(
            "Конец задачи бота не пришёл за %.0f с, размораживаю по freeze_max_s",
            self._freeze_max_s,
        )
        self._tasks.clear()
        await self._ensure_thawed()

    @staticmethod
    async def _guarded(call: Awaitable[None], what: str) -> None:
        try:
            await call
        except Exception:
            log.exception("Соседство с ботом: %s не удалась", what)


async def follow_llm(gate: NeighborGate, socket: str, delay_s: float = RECONNECT_DELAY_S) -> None:
    logged_down = False
    while True:
        client = ProtoClient(socket, on_event=gate.handle_event)
        try:
            await client.connect()
            state: dict[str, Any] = await client.get_state()
            await gate.sync(
                [str(task) for task in state.get(LLM_STATE_CPU_TASKS, [])],
                bool(state.get(LLM_STATE_NIGHT, False)),
            )
            logged_down = False
            log.info("Соседство с ботом: слежу за событиями llm (%s)", socket)
            await client.join()
        except LINK_ERRORS as exc:
            if not logged_down:
                log.warning(
                    "Соседство с ботом: llm недоступна (%s), повтор каждые %.0f с", exc, delay_s
                )
                logged_down = True
        finally:
            await client.close()
            await gate.disconnected()
        await asyncio.sleep(delay_s)


def start_following(gate: NeighborGate, socket: str) -> asyncio.Task[None]:
    return asyncio.create_task(follow_llm(gate, socket), name="neighbor-llm")


async def stop_following(task: asyncio.Task[None], gate: NeighborGate) -> None:
    await cancel_task(task)
    with contextlib.suppress(Exception):
        await gate.close()
