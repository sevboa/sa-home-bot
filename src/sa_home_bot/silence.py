from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sa_home_bot.db.store import Store

log = logging.getLogger(__name__)

GPU_SILENCE_KEY = "gpu_silence"


@dataclass
class SilenceState:
    until: datetime | None
    started_at: datetime
    set_by: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "until": None if self.until is None else self.until.isoformat(),
                "started_at": self.started_at.isoformat(),
                "set_by": self.set_by,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> SilenceState | None:
        if not raw:
            return None
        try:
            data = json.loads(raw)
            until = data["until"]
            return cls(
                until=None if until is None else datetime.fromisoformat(until),
                started_at=datetime.fromisoformat(data["started_at"]),
                set_by=str(data.get("set_by") or ""),
            )
        except (ValueError, KeyError, TypeError):
            log.warning("silence: запись %s повреждена — считаю, что тишина снята", GPU_SILENCE_KEY)
            return None

    def active(self, now: datetime) -> bool:
        return self.until is None or now < self.until


class SilenceService:
    def __init__(self, store: Store, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    async def load(self) -> SilenceState | None:
        state = SilenceState.from_json(await self.store.get_state(GPU_SILENCE_KEY))
        if state is not None and state.active(self._clock()):
            return state
        return None

    async def start(self, minutes: int | None, set_by: str = "") -> SilenceState:
        if minutes is not None and minutes < 1:
            raise ValueError("срок тишины — от 1 минуты")
        now = self._clock()
        end = None if minutes is None else now + timedelta(minutes=minutes)
        result: list[SilenceState] = []

        def fn(raw: str | None) -> str | None:
            current = SilenceState.from_json(raw)
            if current is not None and current.active(now):
                until = None if end is None or current.until is None else max(current.until, end)
                fresh = SilenceState(until=until, started_at=current.started_at, set_by=set_by)
            else:
                fresh = SilenceState(until=end, started_at=now, set_by=set_by)
            result.append(fresh)
            return fresh.to_json()

        await self.store.update_state(GPU_SILENCE_KEY, fn)
        return result[-1]

    async def off(self) -> bool:
        now = self._clock()
        was_active: list[bool] = []

        def fn(raw: str | None) -> str | None:
            current = SilenceState.from_json(raw)
            was_active.append(current is not None and current.active(now))
            return None

        await self.store.update_state(GPU_SILENCE_KEY, fn)
        return was_active[-1]
