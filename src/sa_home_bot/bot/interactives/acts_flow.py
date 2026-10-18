from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass, field
from typing import Any

from sa_home_bot.bot.interactives import cellar, places, underground
from sa_home_bot.bot.interactives.acts import (
    _LOOK_RE,
    _SEARCH_RE,
    ACTION_MAX,
    ACTS_PER_TURN,
    TOOL_ACT_CABINET,
    TOOL_ACT_LIMIT,
    TOOL_ACT_NO_ACTION,
    TOOL_ACT_NO_DIRECTOR,
    TOOL_ACT_RESULT,
    TOOL_ACT_UNAVAILABLE,
    search_where,
)
from sa_home_bot.bot.interactives.base import STATUS_ACTIVE, Run
from sa_home_bot.bot.interactives.core import EngineCore
from sa_home_bot.bot.interactives.director import ask_act, facts_of, update_facts
from sa_home_bot.bot.interactives.places import Room
from sa_home_bot.bot.interactives.registry import REGISTRY
from sa_home_bot.bot.interactives.scene import finale_allowed, stage_target


@dataclass
class _ActsRuntime:
    count: dict[int, int] = field(default_factory=dict)
    guest: dict[int, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ActsMixin(EngineCore):
    """Тул act в ``engine.Interactives``. ``self._*`` — атрибуты и методы
    движка (хранилище, часы, ссылка на ноду llm, прогресс сцены)."""

    # --- счётчик за ход гостя ---

    @functools.cached_property
    def _acts(self) -> _ActsRuntime:
        return _ActsRuntime()

    def _acts_new_turn(self, chat_id: int, user_text: str) -> None:
        """Новый ход гостя (before_turn): счётчик действий в ноль, реплика
        запомнена — по ней Ведущий проверяет согласие."""
        self._acts.count.pop(chat_id, None)
        self._acts.guest[chat_id] = user_text

    def _take_act(self, chat_id: int) -> bool:
        """Занять одно действие хода; False — потолок ``ACTS_PER_TURN``."""
        counts = self._acts.count
        if counts.get(chat_id, 0) >= ACTS_PER_TURN:
            return False
        counts[chat_id] = counts.get(chat_id, 0) + 1
        return True

    # --- факты мира для кадров ---

    async def _scene_facts(self, chat_id: int, user_id: int) -> list[str] | None:
        """Факты мира идущей канареечной сцены — в описание кадра (только
        канарейкам; прочим кадры прежние)."""
        if not self.canary_ok(user_id):
            return None
        run = await self._scene_run(chat_id, user_id)
        if run is None or not REGISTRY[run.scenario].canary:
            return None
        return facts_of(run) or None

    # --- тул act ---

    async def tool_act(self, chat_id: int | None, user_id: int | None, action: Any) -> str:
        """Одно физическое действие Альфреда. Исход даёт Ведущий здесь же,
        синхронно; применяется сразу (стадия, перенос, финал, вещи)."""
        if chat_id is None or user_id is None or not self.canary_ok(user_id):
            return TOOL_ACT_UNAVAILABLE
        if chat_id != user_id:
            return TOOL_ACT_UNAVAILABLE
        text = " ".join(str(action or "").split())[:ACTION_MAX]
        if not text:
            return TOOL_ACT_NO_ACTION
        run = await self._scene_run(chat_id, user_id)
        if run is not None and not REGISTRY[run.scenario].canary:
            run = None  # сцена радио: Альфред в кабинете, act ей не нужен
        at = await places.get_at(self._store, user_id)
        if run is None and (at is None or at.in_cabinet):
            return TOOL_ACT_CABINET
        hunting = (
            run is not None
            and run.scenario == cellar.SCENARIO_ID
            and run.stage == cellar.STAGE_KEY_HUNT
            and not run.finale
        )
        if _SEARCH_RE.search(text) or (hunting and _LOOK_RE.search(text)):
            # Обыск — частный случай действия: исход решает код (tool_search),
            # Ведущий его обставляет.
            return await self.tool_search(chat_id, user_id, search_where(text))
        if not self._take_act(chat_id):
            return TOOL_ACT_LIMIT
        if run is None:
            # Вне сцены и вне кабинета (подвал после финала): Ведущий отвечает,
            # но прогресса, чтобы хранить, нет — факты живут один ход.
            run = Run(
                scenario=underground.SCENARIO_ID,
                chat_id=chat_id,
                user_id=user_id,
                status=STATUS_ACTIVE,
            )
            return await self._act_in_scene(run, text, persist=False)
        return await self._act_in_scene(run, text)

    async def _act_in_scene(
        self,
        run: Run,
        action: str,
        *,
        outcome: str | None = None,
        fallback: str | None = None,
        persist: bool = True,
    ) -> str:
        """Спросить Ведущего об исходе ``action`` и применить ответ. ``outcome``
        — исход, уже решённый кодом (обыск, ключ): Ведущий его обставляет;
        ``fallback`` — что сказать Альфреду, если Ведущий не ответил."""
        scenario = REGISTRY[run.scenario]
        async with self._acts.lock:
            guest = self._acts.guest.get(run.chat_id)
            if persist and guest and run.world.get("guest_logged") != guest:
                # Реплика гостя — в журнал до действия (after_turn не дублирует).
                run.log("Гость", guest)
                run.world["guest_logged"] = guest
            cab = await self._room(run.chat_id, run.user_id)
            run.world["here"] = cab.canon.name_ru if isinstance(cab, Room) else "кабинет"
            window = (await self._transylvania.outside(self._now())).ru() if cab.windowed else None
            allowed = finale_allowed(scenario, run)
            link = self._get_node_link()
            decision = None
            if link is not None and scenario.act_prompt:
                decision = await ask_act(
                    link,
                    self._settings.llm.request_timeout_s,
                    scenario,
                    run,
                    action=action,
                    guest_text=guest,
                    place=cab.describe_ru(),
                    window=window,
                    finale_allowed=allowed,
                    outcome=outcome,
                )
            result = decision.result if decision is not None else (fallback or TOOL_ACT_NO_DIRECTOR)
            stage_before, finale_before = run.stage, run.finale
            # Приметы найденной вещи (59.3/59.6) — как в ходе сцены.
            await self._apply_found_item(run, decision)
            if decision is not None:
                update_facts(run, decision.facts_add, decision.facts_remove)
            if persist and run.status == STATUS_ACTIVE and not run.finale:
                target = stage_target(
                    scenario, run, decision.stage if decision is not None else run.stage
                )
                if target != run.stage:
                    run.stage = target
                    run.turns_on_stage = 0
                    run.directive = None  # писалась под прошлую стадию
                if decision is not None and decision.finale and allowed:
                    run.finale = True
                    run.finale_fault = decision.finale_fault or self._choose(
                        scenario.fallback_faults
                    )
                    run.world["act_finale"] = True
                if run.stage != stage_before or run.finale != finale_before:
                    run.world["act_key"] = True
                await self._canary_progress(
                    scenario,
                    run,
                    decision.move_to if decision is not None else None,
                    stage_before=stage_before,
                    finale_before=finale_before,
                )
            if persist:
                run.log("Действие", action)
                run.log("Итог", result)
                run.world["acted"] = True
                await self._state.save_run(run)
        return TOOL_ACT_RESULT.format(result=result)
