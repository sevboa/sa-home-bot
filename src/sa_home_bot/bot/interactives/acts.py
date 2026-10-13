"""Действия Альфреда через Ведущего (Этап 59.7, решение владельца 2026-10-13).

Живой прогон 2026-10-13: Альфред сам досочинял исход физических действий
(«замок поддался»), а Ведущий узнавал о них только после ответа и ставил своё
(«замок неподвижен»). Правило: где Альфред и что вокруг — знает код и Ведущий;
Альфред только намеревается и пересказывает.

Тул ``act(action)`` — одно физическое действие за вызов. Обработчик синхронно
спрашивает Ведущего (director.ask_act): сцена, стадия, место, факты мира,
журнал с последней репликой гостя, действие. Ответ — ``result`` (факт, который
перескажет Альфред), стадия (+1 максимум, ``Scenario.stage_gate``), перенос,
факты мира, финал, приметы найденной вещи. Стадия, перенос, финал, флаги и
выдача вещей применяются сразу внутри тула (``Interactives._canary_progress``
— та же логика, что у хода сцены). Случай решает код, как раньше: ключ
(``cellar.roll_key``) и находки; обыск (тул ``search`` и act «порыскать…»)
берёт исход у кода, Ведущий его только обставляет — тем же запросом.

Не больше ``ACTS_PER_TURN`` действий за ход гостя; пятое — «остановись и
спроси собеседника». Несколько вызовов в одном ответе модели выполняются по
одному (``bot.tools.SERIAL_TOOLS``, llm_chat): следующее — после результата
предыдущего.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from sa_home_bot.bot.interactives import cellar, places, underground
from sa_home_bot.bot.interactives.base import STATUS_ACTIVE, Run
from sa_home_bot.bot.interactives.director import ask_act, facts_of, update_facts
from sa_home_bot.bot.interactives.places import Room
from sa_home_bot.proto.messages import Address

ACTS_PER_TURN = 4
ACTION_MAX = 160

TOOL_ACT_UNAVAILABLE = "недоступно: действовать можно только в личном разговоре"
TOOL_ACT_NO_ACTION = "Скажи, что именно ты делаешь: одно короткое физическое действие."
# В кабинете вне сцены физическим действиям нечего решать: Ведущего нет.
TOOL_ACT_CABINET = (
    "Здесь, у себя в кабинете, ничего значимого не поставлено на кон: обычные "
    "домашние дела просто опиши словами, как дворецкий. Инструмент act нужен за "
    "пределами кабинета."
)
TOOL_ACT_LIMIT = (
    "Довольно действий подряд. Остановись: коротко расскажи собеседнику, что "
    "получилось, и спроси, что делать дальше."
)
TOOL_ACT_NO_DIRECTOR = "Ты пробуешь, но ничего заметного не происходит."
TOOL_ACT_RESULT = (
    "Итог: {result} Перескажи собеседнику только этот итог своими словами, "
    "ничего не добавляя от себя. Следующее действие — отдельным вызовом act "
    "уже после этого итога; или остановись и спроси собеседника, что дальше."
)

ACT_DECLARATION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "act",
        "description": (
            "Совершить одно физическое действие там, где ты сейчас: сорвать "
            "доски, дёрнуть дверь, выдвинуть ящик, взять ключ, сдвинуть плиту, "
            "посветить в нишу. Доступно вне кабинета (подвал, места поиска, "
            "катакомбы). Одно действие за вызов; несколько — цепочкой, каждое "
            "отдельным вызовом после результата предыдущего (не больше "
            "четырёх за ответ). Исход тебе неизвестен до результата: мир может "
            "не поддаться или удивить. Сначала вызови, потом пересказывай ТОЛЬКО "
            "результат — не выдумывай исход заранее. Идти по катакомбам — walk, "
            "быстро перейти — go, порыскать — search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "одно физическое действие коротко, например «сорвать "
                        "доски с двери», «дёрнуть ручку ящика», «взять ключ»"
                    ),
                }
            },
            "required": ["action"],
        },
    },
}

# Действие по сути обыск: исход берёт код (таблица находок, бросок ключа),
# поэтому оно идёт через тул search. «Осмотреть» — обыском считается только на
# поиске ключа (иначе это просто взгляд).
_SEARCH_RE = re.compile(
    r"\b(?:обыск\w*|обшар\w*|порыск\w*|порыт\w*|порой\w*|рыться|роюсь|рыщ\w*|"
    r"поищ\w*|поиск\w*|искать|ищу|шарить|шарю|пошар\w*|прошар\w*)",
    re.IGNORECASE,
)
_LOOK_RE = re.compile(r"\b(?:осмотр\w*|загляну\w*|заглядыва\w*)", re.IGNORECASE)
_VERB_HEAD_RE = re.compile(
    r"^\s*(?:попробовать\s+|снова\s+|ещё\s+)?(?:\w+)\s+(?:(?:в|во|на|под|за|по|у|около)\s+)?",
    re.IGNORECASE,
)


def search_where(action: str) -> str:
    """Что обыскивается, из текста действия: без глагола и предлога."""
    rest = _VERB_HEAD_RE.sub("", action, count=1).strip()
    return rest or action


class ActsMixin:
    """Тул act в ``engine.Interactives``. ``self._*`` — атрибуты и методы
    движка (хранилище, часы, ссылка на ноду llm, прогресс сцены)."""

    # --- счётчик за ход гостя ---

    def _acts_state(self) -> dict[str, Any]:
        state = self.__dict__.get("_acts_runtime")
        if state is None:
            state = self.__dict__["_acts_runtime"] = {
                "count": {},
                "guest": {},
                "lock": asyncio.Lock(),
            }
        return state

    def _acts_new_turn(self, chat_id: int, user_text: str) -> None:
        """Новый ход гостя (before_turn): счётчик действий в ноль, реплика
        запомнена — по ней Ведущий проверяет согласие."""
        state = self._acts_state()
        state["count"].pop(chat_id, None)
        state["guest"][chat_id] = user_text

    def _take_act(self, chat_id: int) -> bool:
        """Занять одно действие хода; False — потолок ``ACTS_PER_TURN``."""
        counts = self._acts_state()["count"]
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
        from sa_home_bot.bot.interactives import engine as eng

        run = await self._scene_run(chat_id, user_id)
        if run is None or not eng.REGISTRY[run.scenario].canary:
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
        from sa_home_bot.bot.interactives import engine as eng

        run = await self._scene_run(chat_id, user_id)
        if run is not None and not eng.REGISTRY[run.scenario].canary:
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
        from sa_home_bot.bot.interactives import engine as eng

        scenario = eng.REGISTRY[run.scenario]
        async with self._acts_state()["lock"]:
            guest = self._acts_state()["guest"].get(run.chat_id)
            if persist and guest and run.world.get("guest_logged") != guest:
                # Реплика гостя — в журнал до действия (after_turn не дублирует).
                run.log("Гость", guest)
                run.world["guest_logged"] = guest
            cab = await self._room(run.chat_id, run.user_id)
            run.world["here"] = cab.canon.name_ru if isinstance(cab, Room) else "кабинет"
            window = (await self._transylvania.outside(self._now())).ru() if cab.windowed else None
            allowed = eng.finale_allowed(scenario, run)
            link = self._get_node_link()
            decision = None
            if link is not None and scenario.act_prompt:
                decision = await ask_act(
                    link,
                    Address(node=eng.LLM_NODE, service=eng.LLM_SERVICE),
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
                target = eng.stage_target(
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
