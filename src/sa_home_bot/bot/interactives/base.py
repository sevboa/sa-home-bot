from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sa_home_bot.db.store import Store

STATUS_OFFERED = "offered"
STATUS_ACTIVE = "active"
STATUS_IDLE = "idle"
STATUS_PAUSED = "paused"
STATUS_DECLINED = "declined"
STATUS_DONE = "done"

EFFECT_ACTIVE = "active"
EFFECT_EXPIRED = "expired"

TRANSCRIPT_LINES = 14
TRANSCRIPT_LINE_CHARS = 500


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    offer_text: str
    trigger_re: re.Pattern[str]
    scene_frame: str
    ladder: tuple[str, ...]
    finale_directive: str
    director_prompt: str
    fallback_faults: tuple[str, ...]
    min_turns_before_finale: int = 4
    stage_soft_cap: int = 3
    min_turns_on_stage: int = 1
    nudges: tuple[str, ...] = ()
    item_kind: str | None = None

    canary: bool = False
    trigger: str = "complaint"
    offer_buttons: tuple[str, str] = ("Да", "Нет")
    decline_cooldown: timedelta | None = None
    start_place: str | None = None
    stage_places: tuple[str, ...] = ()
    move_stages: tuple[int, ...] = ()
    after_agree: str = ""
    finale_effect: str = "Внутри радиостанции обнаруживается страшное: {fault}."
    finale_running_line: str = ""
    finale_fault_hint: str = ""
    auto_stages: bool = True
    auto_finale: bool = True
    stage_gate: Callable[[Run, int], int] | None = None
    extra_input: Callable[[Run], str] | None = None
    extra_fields: str = ""
    finale_flags: tuple[str, ...] = ()
    finish_after_finale: bool = False
    stage_items: tuple[tuple[int, str], ...] = ()
    act_prompt: str = ""
    act_fields: str = ""

    @property
    def last_stage(self) -> int:
        return len(self.ladder) - 1


@dataclass
class Run:
    scenario: str
    chat_id: int
    user_id: int
    status: str = STATUS_OFFERED
    stage: int = 0
    turns_total: int = 0
    turns_on_stage: int = 0
    finale: bool = False
    finale_fault: str | None = None
    finale_form_sent: bool = False
    directive: str | None = None
    last_effect: str | None = None
    pending_effect: str | None = None
    notes: list[str] = field(default_factory=list)
    photo_turn: int | None = None
    mood: str | None = None
    item_traits: list[str] = field(default_factory=list)
    item_seed: int | None = None
    item_key: str | None = None
    item_drawn: list[str] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)
    offered_at: str | None = None
    declined_until: str | None = None
    updated_at: str | None = None
    world: dict[str, Any] = field(default_factory=dict)

    def log(self, who: str, text: str) -> None:
        line = " ".join(text.split())
        if len(line) > TRANSCRIPT_LINE_CHARS:
            line = line[:TRANSCRIPT_LINE_CHARS] + "…"
        self.transcript.append(f"{who}: {line}")
        del self.transcript[:-TRANSCRIPT_LINES]

    def to_json(self) -> str:
        data = asdict(self)
        if not data["world"]:
            del data["world"]
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> Run:
        data: dict[str, Any] = json.loads(raw)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _now() -> datetime:
    return datetime.now(tz=UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_iso(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


class InteractiveStore:
    def __init__(self, store: Store) -> None:
        self._store = store

    @staticmethod
    def _run_key(chat_id: int, scenario: str) -> str:
        return f"interactive_run:{chat_id}:{scenario}"

    async def load_run(self, chat_id: int, scenario: str) -> Run | None:
        raw = await self._store.get_state(self._run_key(chat_id, scenario))
        if not raw:
            return None
        try:
            return Run.from_json(raw)
        except (ValueError, TypeError):
            return None

    async def save_run(self, run: Run) -> None:
        run.updated_at = iso(_now())
        await self._store.set_state(self._run_key(run.chat_id, run.scenario), run.to_json())

    async def is_completed(self, scenario: str, user_id: int) -> bool:
        return bool(await self._store.get_state(f"interactive_done:{scenario}:{user_id}"))

    async def mark_completed(self, scenario: str, user_id: int) -> None:
        await self._store.set_state(f"interactive_done:{scenario}:{user_id}", iso(_now()))

    async def get_effect(self, key: str, user_id: int) -> str | None:
        return await self._store.get_state(f"user_effect:{key}:{user_id}")

    async def set_effect(self, key: str, user_id: int, value: str) -> None:
        await self._store.set_state(f"user_effect:{key}:{user_id}", value)


    async def set_timed_effect(self, key: str, user_id: int, expires_at: datetime | None) -> None:
        value = {"on": True, "expires_at": iso(expires_at) if expires_at else None}
        await self.set_effect(key, user_id, json.dumps(value))

    async def timed_effect(self, key: str, user_id: int, now: datetime) -> str | None:
        raw = await self.get_effect(key, user_id)
        if not raw or raw == "0":
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return EFFECT_ACTIVE
        until = parse_iso(data.get("expires_at")) if isinstance(data, dict) else None
        if until is not None and until <= now:
            return EFFECT_EXPIRED
        return EFFECT_ACTIVE

    async def clear_effect(self, key: str, user_id: int) -> None:
        await self._store.delete_state(f"user_effect:{key}:{user_id}")

    async def plain_turns(self, chat_id: int) -> int:
        raw = await self._store.get_state(f"interactives_plain:{chat_id}")
        return int(raw) if raw and raw.isdigit() else 0

    async def set_plain_turns(self, chat_id: int, count: int) -> None:
        if await self.plain_turns(chat_id) == count:
            return
        await self._store.set_state(f"interactives_plain:{chat_id}", str(count))

    async def is_opted_out(self, chat_id: int) -> bool:
        return await self._store.get_state(f"interactives_opt_out:{chat_id}") == "1"

    async def set_opted_out(self, chat_id: int, opted_out: bool) -> None:
        await self._store.set_state(f"interactives_opt_out:{chat_id}", "1" if opted_out else "0")
