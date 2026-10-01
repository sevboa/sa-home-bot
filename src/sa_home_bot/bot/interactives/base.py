"""Модели и хранение интерактивов (Этап 47, IMPLEMENTATION_PLAN.md).

Интерактив — сюжетная сценка поверх обычного /ai: гость играет с
Альфредом, а сцену ведёт отдельный LLM-агент Ведущий (director.py). Сценарий
(``Scenario``) — единица расширения: текст сцены, лестница подсказок
Альфреду, рамки темпа. Движок (engine.py) один на все сценарии.

Три уровня хранения (решение пользователя 2026-09-27):

- прогресс сцены (``Run``) — в рамках чата: ``interactive_run:<чат>:<сценарий>``;
- завершённость и эффект — глобально на гостя (user_id), во всех его
  чатах: ``interactive_done:<сценарий>:<гость>`` и ``user_effect:<ключ>:<гость>``;
- запрет интерактивов — на переписку: ``interactives_opt_out:<чат>``.

Всё в ``app_state`` бота (тот же паттерн «состояние, привязанное к
сущности, без реляционной схемы», что у bot/voice_mode.py): записей мало,
выборок по ним нет, JSON-снимок сцены читается целиком.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sa_home_bot.db.store import Store

# Статусы сцены в чате.
STATUS_OFFERED = "offered"  # предложена формой согласия, ждём кнопку
STATUS_ACTIVE = "active"  # идёт, Ведущий подключён
STATUS_IDLE = "idle"  # гость сменил тему (решил Ведущий) — согласие в силе
STATUS_PAUSED = "paused"  # гость вышел кнопкой — продолжение только через новое согласие
STATUS_DECLINED = "declined"  # «Не сейчас» / форма протухла — кулдаун
STATUS_DONE = "done"  # завершена (эффект применён)

TRANSCRIPT_LINES = 14
TRANSCRIPT_LINE_CHARS = 500


@dataclass(frozen=True)
class Scenario:
    """Сценарий интерактива. Тексты — персонажные, живут в коде рядом со
    сценарием (решение пользователя: характер важнее чистоты кода).

    ``ladder`` — подсказки Альфреду по стадиям, от мягкой к нестерпимой;
    индекс = стадия. Финал возможен только с последней стадии.
    ``finale_directive`` — шаблон с ``{fault}`` (поломка, которую придумал
    Ведущий). ``min_turns_before_finale`` и ``stage_soft_cap`` — рамки темпа
    поверх решений Ведущего (engine.apply_decision)."""

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
    # Сколько ходов стадия отыгрывается, прежде чем Ведущий может поднять
    # её дальше (живая находка 2026-09-30: Ведущий поднимал стадию каждый
    # ход, и к замене передатчика приходили за 4 реплики).
    min_turns_on_stage: int = 1
    # Подталкивание гостя к следующей реплике, по кругу от хода сцены: чтобы
    # разговор не повисал (пользователь 2026-09-27 — «через раз» совет).
    nudges: tuple[str, ...] = ()
    # Сюжетный предмет сцены (bot/interactives/items.py::KINDS): его облик
    # копится по ходу сцены и в финале достаётся гостю (Этап 49.3).
    item_kind: str | None = None

    @property
    def last_stage(self) -> int:
        return len(self.ladder) - 1


@dataclass
class Run:
    """Прогресс сцены в конкретном чате."""

    scenario: str
    chat_id: int
    user_id: int
    status: str = STATUS_OFFERED
    stage: int = 0
    turns_total: int = 0
    turns_on_stage: int = 0
    finale: bool = False
    finale_fault: str | None = None
    # Форма финала (смена передатчика) хоть раз ушла гостю.
    finale_form_sent: bool = False
    directive: str | None = None
    last_effect: str | None = None
    # Событие от Ведущего, которое Альфред ещё не пересказал гостю (Ведущий
    # гостю не виден — рассказывает сам Альфред на следующем ходу).
    pending_effect: str | None = None
    notes: list[str] = field(default_factory=list)
    # Ход (turns_total), на котором ушёл последний кадр Ведущего.
    photo_turn: int | None = None
    # Настроение кадров сцены от Ведущего (director.MOODS); None — обычное.
    mood: str | None = None
    # Предмет сцены (Этап 49.3): черты (ключи вида, по порядку появления),
    # зерно портрета (одно на сцену — облик меняется чертами, а не
    # зерном), ключ вырезки на ноде llm и черты, с которыми она нарисована.
    item_traits: list[str] = field(default_factory=list)
    item_seed: int | None = None
    item_key: str | None = None
    item_drawn: list[str] = field(default_factory=list)
    # Журнал сцены: «Гость: …», «Альфред: …», «Событие: …». В личке каждое
    # сообщение без реплая — новый тред /ai (bot/handlers/ai.py::
    # _dialogue_id_for), и история треда сцену не держит — держит журнал.
    transcript: list[str] = field(default_factory=list)
    offered_at: str | None = None
    declined_until: str | None = None
    updated_at: str | None = None

    def log(self, who: str, text: str) -> None:
        line = " ".join(text.split())
        if len(line) > TRANSCRIPT_LINE_CHARS:
            line = line[:TRANSCRIPT_LINE_CHARS] + "…"
        self.transcript.append(f"{who}: {line}")
        del self.transcript[:-TRANSCRIPT_LINES]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

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
    """Тонкая обёртка над app_state — все ключи интерактивов в одном месте."""

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

    async def is_opted_out(self, chat_id: int) -> bool:
        return await self._store.get_state(f"interactives_opt_out:{chat_id}") == "1"

    async def set_opted_out(self, chat_id: int, opted_out: bool) -> None:
        await self._store.set_state(f"interactives_opt_out:{chat_id}", "1" if opted_out else "0")
