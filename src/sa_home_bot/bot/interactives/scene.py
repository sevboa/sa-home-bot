from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sa_home_bot.bot.interactives import items as items_mod
from sa_home_bot.bot.interactives.base import STATUS_IDLE, Run, Scenario
from sa_home_bot.bot.interactives.director import DirectorDecision, facts_of

NOTES_KEEP = 6


NOTE_TRANSCRIPT_LINES = 8


def grow_item(
    kind: items_mod.ItemKind,
    run: Run,
    decision: DirectorDecision | None,
    *,
    key_moment: bool,
) -> str | None:
    """Новая черта предмета сцены за ход: от Ведущего (уже проверена по
    словарю вида), а если он промолчал на переходе стадии или в финале —
    следующая по лестнице. Не больше одной за ход. Возвращает добавленную."""
    new = decision.item_trait if decision is not None else None
    if new in run.item_traits:
        new = None
    if new is None and key_moment:
        new = kind.next_trait(run.item_traits)
    if new is None:
        return None
    run.item_traits.append(new)
    return new


def finale_allowed(scenario: Scenario, run: Run) -> bool:
    """Финал — только с последней стадии и не раньше минимума ходов
    (считая текущий, уже отыгранный ход)."""
    return run.stage >= scenario.last_stage and run.turns_total + 1 >= (
        scenario.min_turns_before_finale
    )


def stage_target(scenario: Scenario, run: Run, wanted: int) -> int:
    """Стадия после решения Ведущего: не больше +1, в пределах лестницы и с
    условиями сюжета поверх (Этап 59: ключ найден кодом — ``stage_gate``).
    Общая для хода сцены (apply_decision) и тула act (Этап 59.7)."""
    target = max(run.stage, min(wanted, run.stage + 1, scenario.last_stage))
    if scenario.stage_gate is not None:
        target = max(run.stage, min(scenario.stage_gate(run, target), scenario.last_stage))
    return target


def apply_decision(
    scenario: Scenario,
    run: Run,
    decision: DirectorDecision | None,
    *,
    choose: Callable[[tuple[str, ...]], str] = random.choice,
) -> str | None:
    """Рамки темпа поверх решения Ведущего. Возвращает текст эффекта для
    гостя (или None). ``decision=None`` — Ведущий не ответил: ход всё равно
    засчитывается, и мягкий потолок продвигает сцену сам."""
    allowed = finale_allowed(scenario, run)
    run.turns_total += 1
    run.turns_on_stage += 1
    if decision is not None and not decision.active:
        run.status = STATUS_IDLE
        return None
    effect = decision.effect if decision is not None else None
    if decision is not None:
        if decision.directive:
            run.directive = decision.directive
        if decision.note:
            run.notes.append(decision.note)
            del run.notes[:-NOTES_KEEP]
        if decision.mood:
            run.mood = decision.mood
    if run.finale:
        run.last_effect = effect or run.last_effect
        return effect

    wanted = decision.stage if decision is not None else run.stage
    if run.turns_on_stage < scenario.min_turns_on_stage:
        wanted = run.stage
    target = stage_target(scenario, run, wanted)
    if (
        scenario.auto_stages
        and target == run.stage
        and run.stage < scenario.last_stage
        and run.turns_on_stage >= scenario.stage_soft_cap
    ):
        target = run.stage + 1
    if target != run.stage:
        run.stage = target
        run.turns_on_stage = 0
        # Директива Ведущего писалась под прошлую стадию — новая подсказка
        # лестницы важнее.
        if decision is None:
            run.directive = None

    wants_finale = decision is not None and decision.finale
    if allowed and (
        wants_finale or (scenario.auto_finale and run.turns_on_stage >= scenario.stage_soft_cap)
    ):
        run.finale = True
        # Поломку можно задать заранее (вернуть гостю прежнюю после сброса
        # сцены) — тогда она главнее выдумки Ведущего, а его эффект, писанный
        # под свою поломку, заменяем шаблонным.
        preset = run.finale_fault
        run.finale_fault = preset or (
            decision.finale_fault
            if decision is not None and decision.finale_fault
            else choose(scenario.fallback_faults)
        )
        if preset or not effect:
            effect = scenario.finale_effect.format(fault=run.finale_fault)
    run.last_effect = effect or run.last_effect
    return effect


def build_scene_note(scenario: Scenario, run: Run, place: str | None = None) -> str:
    parts = [scenario.scene_frame]
    if place:
        parts.append(f"Где ты: {place}")
    facts = facts_of(run)
    if facts:
        # Этап 59.7: что в мире уже так (решил Ведущий, исходы действий).
        parts.append("Что вокруг уже так (не противоречь): " + "; ".join(facts) + ".")
    kind = items_mod.KINDS.get(scenario.item_kind or "")
    if kind is not None and run.item_traits:
        parts.append(
            "Как сейчас выглядит радиостанция: " + "; ".join(kind.traits_ru(run.item_traits)) + "."
        )
    if run.finale:
        parts.append(scenario.finale_directive.format(fault=run.finale_fault))
    else:
        parts.append("Сейчас: " + scenario.ladder[min(run.stage, scenario.last_stage)])
        if scenario.nudges:
            parts.append(scenario.nudges[run.turns_total % len(scenario.nudges)])
    if run.directive:
        parts.append("Подсказка на этот ход: " + run.directive)
    if run.pending_effect:
        parts.append(
            "После твоего прошлого действия произошло вот что: "
            f"{run.pending_effect}\nРасскажи об этом собеседнику в этом ответе "
            "сам, своими словами — как то, что ты сейчас увидел, услышал или "
            "почувствовал. Не говори, что тебе это подсказали."
        )
    if run.transcript:
        lines = "\n".join(run.transcript[-NOTE_TRANSCRIPT_LINES:])
        parts.append("Что уже было в сцене (журнал):\n" + lines)
    return "\n\n".join(parts)


@dataclass
class TurnPlan:
    chat_id: int
    user_id: int | None
    user_text: str
    speech_clear: bool
    # Свежий флаг для каждого раунда запроса к llm (llm_chat.py): гость мог
    # сменить передатчик кнопкой, пока ход ещё идёт.
    speech_clear_now: Callable[[], Awaitable[bool]] | None = None
    note: str | None = None
    scenario: str | None = None
    scene: bool = False  # ход сцены: после ответа — Ведущий
    offered: bool = False  # после ответа — форма согласия
    force_swap_form: bool = False  # финал: форма смены гарантирована
