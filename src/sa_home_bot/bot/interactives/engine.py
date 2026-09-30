"""Движок интерактивов (Этап 47): согласие → ходы сцены → финал → эффект.

Встраивание в ход /ai (bot/handlers/ai.py::_do_ask_and_reply):

1. ``before_turn`` — ДО запроса к модели: решает, идёт ли сцена, нужна ли
   форма согласия, и собирает скрытую заметку Альфреду (рамка сцены +
   подсказка стадии + директива Ведущего + что произошло после прошлого
   действия Альфреда + журнал). Заодно отдаёт ``speech_clear`` гостя — он
   едет в каждый запрос к службе llm.
2. Альфред отвечает как обычно, ответ сразу уходит гостю.
3. ``after_turn`` — ПОСЛЕ ответа: журнал, вызов Ведущего, рамки темпа.
   Ведущий гостю НЕ виден (решение пользователя 2026-09-27: всё должно
   выглядеть как обычный диалог с Альфредом): его ``effect`` копится в
   ``Run.pending_effect`` и на следующем ходу уходит Альфреду скрытой
   подсказкой — о случившемся рассказывает сам Альфред.
4. ``flush_forms`` — последним шагом хода: формы (согласие, смена
   передатчика), которые попросили тул или финал. Форма, посланная
   посреди генерации, обогнала бы речь Альфреда (тот же приём, что
   PendingActions.flush_drafts, Этап 45).

Согласие: сцена сначала ``offered`` — невзначай заданный вопрос сценария
(«исправить проблему с коммуникацией?») с «Да»/«Нет», без намёка на игру.
«Да» — сцена стартует; «Нет» — запрет интерактивов в этой переписке
(снимается скрытой командой /interactives); без ответа час — кулдаун.
Сцены — только в личке: в общем чате подсказки сцены сбивали бы Альфреда
в разговоре с остальными. Эффект и
завершённость — на гостя глобально (base.py), так что переключатель после
завершения работает в любом чате.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import html
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import image_tools
from sa_home_bot.bot.interactives import cabinet as cabinet_mod
from sa_home_bot.bot.interactives import radio
from sa_home_bot.bot.interactives.base import (
    STATUS_ACTIVE,
    STATUS_DECLINED,
    STATUS_DONE,
    STATUS_IDLE,
    STATUS_OFFERED,
    STATUS_PAUSED,
    InteractiveStore,
    Run,
    Scenario,
    iso,
    parse_iso,
)
from sa_home_bot.bot.interactives.cabinet import Cabinet
from sa_home_bot.bot.interactives.director import DirectorDecision, ask_director, ask_features
from sa_home_bot.bot.interactives.transylvania import Outside, Transylvania
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings, reminder_reason
from sa_home_bot.db.store import Store
from sa_home_bot.llm.prompt import wrap_system_directive
from sa_home_bot.proto.messages import Address, ProtoError

log = logging.getLogger(__name__)

REGISTRY: dict[str, Scenario] = {radio.RADIO.id: radio.RADIO}

LLM_NODE = "mycraft"
LLM_SERVICE = "llm"
ACTION_SET_SPEECH_CLEAR = "set_speech_clear"
SET_SPEECH_TIMEOUT_S = 15.0

OFFER_TTL = timedelta(hours=1)
DECLINE_COOLDOWN = timedelta(hours=24)
NOTES_KEEP = 6
NOTE_TRANSCRIPT_LINES = 8

_TAG_RE = re.compile(r"<[^>]+>")

# callback_data «ia:<сценарий>:<кнопка>» — всё остальное в app_state.
CALLBACK_PREFIX = "ia"
BTN_PLAY = "p"
BTN_LATER = "l"
BTN_NEVER = "n"
BTN_EXIT = "x"
BTN_SWAP = "s"
BTN_KEEP = "k"
BTN_RETURN_OLD = "r"
BTN_INSTALL_NEW = "i"
BTN_TOGGLE_KEEP = "o"

FORM_OFFER = "offer"
FORM_SWAP = "swap"
FORM_RETURN = "return"
FORM_REINSTALL = "reinstall"

# Форма согласия нарочно не выдаёт, что это игра (решение пользователя
# 2026-09-27): невзначай заданный вопрос сценария и «Да»/«Нет». После
# ответа форма лишь фиксирует его — тоже без слов «сценка»/«игра».
# «Нет» — сценок в этом чате больше не будет (снять — скрытая /interactives).
OFFER_YES_SUFFIX = "\n<i>— Да</i>"
OFFER_NO_SUFFIX = "\n<i>— Нет</i>"
OFFER_EXPIRED_SUFFIX = "\n<i>— Вопрос уже неактуален</i>"
# Кнопка выхода была под сообщениями рассказчика до v0.115.3 — рассказчика
# больше нет, обработка оставлена для уже разосланных сообщений.
EXIT_ALERT = "Хорошо."

# Снимки кабинета (Этап 49.2): рисует служба llm на mycraft, будит его и
# ~30 с грузит CPU — поэтому не больше одного снимка в чате одновременно.
# Суточный потолок на чат; 0 — без лимита (снят по просьбе пользователя 2026-09-30).
PHOTO_DAILY_LIMIT = 0
PHOTO_PURPOSE = "photo"
PHOTO_FEATURES_IN_FRAME = 4
PHOTO_CAPTION = "Кабинет"
# Кадры по ходу сцены (Ведущий, поле photo): переход стадии и финал снимаются
# всегда, прочие — не чаще раза в PHOTO_SCENE_GAP_TURNS ходов.
PHOTO_SCENE_GAP_TURNS = 3
PHOTO_SCENE_CAPTION = "В кабинете"

OPT_IN_TEXT = "Интерактивы в этом чате включены."
OPT_OUT_TEXT = "Интерактивы в этом чате выключены. Включить — /interactives on."


def parse_callback(data: str | None) -> tuple[str, str] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[1] not in REGISTRY:
        return None
    return parts[1], parts[2]


def _cb(scenario: str, button: str) -> str:
    return f"{CALLBACK_PREFIX}:{scenario}:{button}"


def _keyboard(scenario: str, rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=_cb(scenario, btn)) for text, btn in row]
            for row in rows
        ]
    )


def offer_keyboard(scenario: str) -> InlineKeyboardMarkup:
    return _keyboard(
        scenario,
        [
            [("Да", BTN_PLAY), ("Нет", BTN_NEVER)],
        ],
    )


def swap_keyboard(scenario: str) -> InlineKeyboardMarkup:
    return _keyboard(scenario, [[("Заменить", BTN_SWAP), ("Оставить старое", BTN_KEEP)]])


def toggle_keyboard(scenario: str, button: str, label: str) -> InlineKeyboardMarkup:
    return _keyboard(scenario, [[(label, button), ("Оставить как есть", BTN_TOGGLE_KEEP)]])


def finale_allowed(scenario: Scenario, run: Run) -> bool:
    """Финал — только с последней стадии и не раньше минимума ходов
    (считая текущий, уже отыгранный ход)."""
    return run.stage >= scenario.last_stage and run.turns_total + 1 >= (
        scenario.min_turns_before_finale
    )


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
    if run.finale:
        run.last_effect = effect or run.last_effect
        return effect

    wanted = decision.stage if decision is not None else run.stage
    target = max(run.stage, min(wanted, run.stage + 1, scenario.last_stage))
    if (
        target == run.stage
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
    if allowed and (wants_finale or run.turns_on_stage >= scenario.stage_soft_cap):
        run.finale = True
        run.finale_fault = (
            decision.finale_fault
            if decision is not None and decision.finale_fault
            else choose(scenario.fallback_faults)
        )
        if not effect:
            effect = f"Внутри передатчика обнаруживается страшное: {run.finale_fault}."
    run.last_effect = effect or run.last_effect
    return effect


def build_scene_note(scenario: Scenario, run: Run, place: str | None = None) -> str:
    parts = [scenario.scene_frame]
    if place:
        parts.append(f"Где ты: {place}")
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


@dataclass
class _Queued:
    forms: list[tuple[str, str]] = field(default_factory=list)  # (сценарий, форма)


class Interactives:
    """Сервис интерактивов бота (один на процесс, см. app.py)."""

    def __init__(
        self,
        store: Store,
        notifier: Any,
        settings: Settings,
        get_node_link: Callable[[], ServiceLink | None],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        choose: Callable[[tuple[str, ...]], str] = random.choice,
        transylvania: Transylvania | None = None,
    ) -> None:
        self._transylvania = transylvania or Transylvania()
        self._photo_busy: set[int] = set()
        self._photo_tasks: set[asyncio.Task] = set()
        self._store = store
        self._state = InteractiveStore(store)
        self._notifier = notifier
        self._settings = settings
        self._get_node_link = get_node_link
        self._now = now
        self._choose = choose
        self._queued: dict[int, _Queued] = {}

    # --- эффект «чистая речь» ---

    async def speech_clear(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return await self._state.get_effect(radio.EFFECT_SPEECH_CLEAR, user_id) == "1"

    def _pinned(self, chat_id: int) -> bool:
        return chat_id in self._settings.llm.speech_therapy_pinned_chat_ids

    # --- ход /ai ---

    async def before_turn(
        self, chat_id: int, user_id: int | None, user_text: str, *, is_private: bool
    ) -> TurnPlan:
        plan = TurnPlan(
            chat_id=chat_id,
            user_id=user_id,
            user_text=user_text,
            speech_clear=await self.speech_clear(user_id),
            speech_clear_now=functools.partial(self.speech_clear, user_id),
        )
        if not is_private or user_id is None or self._pinned(chat_id):
            return plan
        if await self._state.is_opted_out(chat_id):
            return plan
        scenario = radio.RADIO
        run = await self._state.load_run(chat_id, scenario.id)
        run = await self._expire_offer(run)
        triggered = bool(scenario.trigger_re.search(user_text))

        if run is not None and run.status == STATUS_IDLE and triggered:
            run.status = STATUS_ACTIVE  # согласие уже было — продолжаем молча
            await self._state.save_run(run)
        if run is not None and run.status == STATUS_ACTIVE:
            plan.scenario = scenario.id
            plan.scene = True
            cab = await cabinet_mod.load(self._store, user_id)
            outside = await self._transylvania.outside(self._now())
            place = f"{cab.describe_ru()} Сейчас {outside.ru()}."
            plan.note = build_scene_note(scenario, run, place)
            plan.force_swap_form = run.finale and not run.finale_form_sent
            return plan
        if triggered and await self._may_offer(scenario, run, user_id):
            await self._offer(scenario, run, chat_id, user_id, user_text)
            plan.scenario = scenario.id
            plan.offered = True
        return plan

    async def _may_offer(self, scenario: Scenario, run: Run | None, user_id: int) -> bool:
        if await self._state.is_completed(scenario.id, user_id):
            return False  # дальше только переключатель через swap_radio
        if run is None:
            return True
        if run.status in (STATUS_OFFERED, STATUS_ACTIVE, STATUS_DONE):
            return False
        if run.status == STATUS_DECLINED:
            until = parse_iso(run.declined_until)
            return until is None or until <= self._now()
        return True  # paused / idle без триггера сюда не доходит

    async def _offer(
        self,
        scenario: Scenario,
        run: Run | None,
        chat_id: int,
        user_id: int,
        user_text: str | None,
    ) -> None:
        if run is None:
            run = Run(scenario=scenario.id, chat_id=chat_id, user_id=user_id)
        run.status = STATUS_OFFERED
        run.offered_at = iso(self._now())
        if user_text:
            run.log("Гость", user_text)
        await self._state.save_run(run)
        self._queue(chat_id, scenario.id, FORM_OFFER)

    async def _expire_offer(self, run: Run | None) -> Run | None:
        if run is None or run.status != STATUS_OFFERED:
            return run
        offered = parse_iso(run.offered_at)
        if offered is not None and offered + OFFER_TTL <= self._now():
            run.status = STATUS_DECLINED
            run.declined_until = iso(self._now() + DECLINE_COOLDOWN)
            await self._state.save_run(run)
        return run

    async def after_turn(
        self,
        plan: TurnPlan,
        reply: str,
        *,
        dialogue_id: int | None,
        message_thread_id: int | None = None,
    ) -> None:
        if plan.scenario is None:
            return
        scenario = REGISTRY[plan.scenario]
        run = await self._state.load_run(plan.chat_id, scenario.id)
        if run is None:
            return
        if plan.offered:
            run.log("Альфред", reply)
            await self._state.save_run(run)
            return
        if not plan.scene or run.status != STATUS_ACTIVE:
            return
        run.log("Гость", plan.user_text)
        run.log("Альфред", reply)
        # Прошлое событие Альфред уже пересказал этим ответом.
        run.pending_effect = None
        cab = await cabinet_mod.load(self._store, run.user_id)
        decision = await self._ask_director(scenario, run, cab)
        stage_before, finale_before = run.stage, run.finale
        if decision is not None and decision.cabinet_add:
            # Перечитываем перед записью: снимок в фоне мог дописать своё.
            cab = await cabinet_mod.load(self._store, run.user_id)
            if cab.add(list(decision.cabinet_add)):
                await cabinet_mod.save(self._store, cab)
        effect = apply_decision(scenario, run, decision, choose=self._choose)
        if effect:
            run.log("Событие", effect)
            run.pending_effect = effect
        focus = scene_photo_focus(
            run, decision, key_moment=run.stage != stage_before or run.finale != finale_before
        )
        if (
            focus is not None
            and run.status == STATUS_ACTIVE
            and not await self._photo_limit_reached(run.chat_id)
        ):
            started = await self._start_photo(
                run.chat_id,
                run.user_id,
                focus=focus,
                caption=PHOTO_SCENE_CAPTION,
                happening=run.last_effect,
                outside=await self._transylvania.outside(self._now()),
                message_thread_id=message_thread_id,
                trigger_message_id=None,
            )
            if started:
                run.photo_turn = run.turns_total
        await self._state.save_run(run)

    async def _ask_director(
        self, scenario: Scenario, run: Run, cab: Cabinet
    ) -> DirectorDecision | None:
        node_link = self._get_node_link()
        if node_link is None:
            return None
        outside = await self._transylvania.outside(self._now())
        return await ask_director(
            node_link,
            Address(node=LLM_NODE, service=LLM_SERVICE),
            self._settings.llm.request_timeout_s,
            scenario,
            run,
            finale_allowed=finale_allowed(scenario, run),
            place=cab.describe_ru(),
            outside=outside.ru(),
            need_features=0 if cab.features else cabinet_mod.FIRST_FEATURES,
        )

    # --- формы ---

    def _queue(self, chat_id: int, scenario: str, form: str) -> None:
        queued = self._queued.setdefault(chat_id, _Queued())
        if (scenario, form) not in queued.forms:
            queued.forms.append((scenario, form))

    async def flush_forms(
        self,
        chat_id: int,
        plan: TurnPlan | None = None,
        *,
        dialogue_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> None:
        if plan is not None and plan.force_swap_form and plan.scenario is not None:
            run = await self._state.load_run(chat_id, plan.scenario)
            if run is not None and run.status == STATUS_ACTIVE and run.finale:
                self._queue(chat_id, plan.scenario, FORM_SWAP)
        queued = self._queued.pop(chat_id, None)
        if queued is None:
            return
        for scenario_id, form in queued.forms:
            text, markup = self._render_form(scenario_id, form)
            message_id = await self._notifier.send_direct(
                chat_id, text, reply_markup=markup, message_thread_id=message_thread_id
            )
            if message_id is None:
                log.warning(
                    "interactives: форма %s/%s не ушла (chat=%s)", scenario_id, form, chat_id
                )
                continue
            if form == FORM_SWAP:
                run = await self._state.load_run(chat_id, scenario_id)
                if run is not None:
                    run.finale_form_sent = True
                    await self._state.save_run(run)
            await self._store.record_ai_turn(
                chat_id,
                message_id,
                dialogue_id if dialogue_id is not None else message_id,
                "assistant",
                _plain(text),
                self._now(),
            )

    def discard_forms(self, chat_id: int) -> None:
        self._queued.pop(chat_id, None)

    @staticmethod
    def _render_form(scenario_id: str, form: str) -> tuple[str, InlineKeyboardMarkup]:
        scenario = REGISTRY[scenario_id]
        if form == FORM_OFFER:
            return scenario.offer_text, offer_keyboard(scenario_id)
        if form == FORM_SWAP:
            return radio.SWAP_FORM_TEXT, swap_keyboard(scenario_id)
        if form == FORM_RETURN:
            return radio.RETURN_FORM_TEXT, toggle_keyboard(scenario_id, BTN_RETURN_OLD, "Вернуть")
        return radio.REINSTALL_FORM_TEXT, toggle_keyboard(scenario_id, BTN_INSTALL_NEW, "Поставить")

    # --- тул swap_radio ---

    async def tool_swap_radio(
        self, chat_id: int | None, user_id: int | None, *, is_private: bool
    ) -> str:
        if chat_id is None or user_id is None:
            return radio.TOOL_UNAVAILABLE
        if self._pinned(chat_id):
            return radio.TOOL_PINNED
        scenario = radio.RADIO
        if await self._state.is_completed(scenario.id, user_id):
            if await self.speech_clear(user_id):
                self._queue(chat_id, scenario.id, FORM_RETURN)
                return radio.TOOL_RETURN_FORM
            self._queue(chat_id, scenario.id, FORM_REINSTALL)
            return radio.TOOL_REINSTALL_FORM
        if not is_private or await self._state.is_opted_out(chat_id):
            return radio.TOOL_OPTED_OUT
        run = await self._state.load_run(chat_id, scenario.id)
        run = await self._expire_offer(run)
        if run is not None and run.status == STATUS_ACTIVE and run.finale:
            self._queue(chat_id, scenario.id, FORM_SWAP)
            return radio.TOOL_SWAP_FORM
        if run is not None and run.status in (STATUS_ACTIVE, STATUS_OFFERED):
            return radio.TOOL_NOT_YET
        if run is not None and run.status == STATUS_IDLE:
            run.status = STATUS_ACTIVE
            await self._state.save_run(run)
            return radio.TOOL_NOT_YET
        if await self._may_offer(scenario, run, user_id):
            await self._offer(scenario, run, chat_id, user_id, None)
            return radio.TOOL_OFFER
        return radio.TOOL_NOT_YET

    # --- тул take_photo: снимок кабинета (Этап 49.2) ---

    async def tool_take_photo(
        self,
        chat_id: int | None,
        user_id: int | None,
        args: dict[str, Any],
        *,
        message_thread_id: int | None = None,
        trigger_message_id: int | None = None,
    ) -> str:
        """Альфред снимает то, что видит у себя в кабинете. Снимок рисуется в
        фоне (~30 с) и приходит сам — ответ Альфреда его не ждёт («сейчас
        сниму»). Кабинет у каждого гостя свой (cabinet.py); без особенностей
        первые придумывает Ведущий. Общий вид без изменений — повторный
        показ прежнего снимка, mycraft не будим."""
        if chat_id is None or user_id is None or not hasattr(self._notifier, "send_photo_ex"):
            return cabinet_mod.TOOL_PHOTO_UNAVAILABLE
        if chat_id in self._photo_busy:
            return cabinet_mod.TOOL_PHOTO_BUSY
        focus = args.get("focus") if isinstance(args.get("focus"), str) else ""
        focus = " ".join(focus.split())
        caption = args.get("caption") if isinstance(args.get("caption"), str) else ""
        caption = caption.strip()[:80] or PHOTO_CAPTION
        now = self._now()
        outside = await self._transylvania.outside(now)
        cab = await cabinet_mod.load(self._store, user_id)
        run = await self._state.load_run(chat_id, radio.SCENARIO_ID)
        happening = run.last_effect if run is not None and run.status == STATUS_ACTIVE else None
        reuse_key = cab.state_key(outside.key) if cab.features else None
        if not focus and not happening and reuse_key in cab.photos:
            image = await self._store.image_by_id(cab.photos[reuse_key])
            if image is not None and image["telegram_file_id"]:
                sent = await self._notifier.send_photo_ex(
                    chat_id,
                    image["telegram_file_id"],
                    caption=caption,
                    message_thread_id=message_thread_id,
                    reply_to_message_id=trigger_message_id,
                )
                if sent is not None:
                    where = cab.describe_ru()
                    return cabinet_mod.TOOL_PHOTO_SENT.format(where=where, now=outside.ru())
        if await self._photo_limit_reached(chat_id):
            return cabinet_mod.TOOL_PHOTO_LIMIT
        started = await self._start_photo(
            chat_id,
            user_id,
            focus=focus,
            caption=caption,
            happening=happening,
            outside=outside,
            message_thread_id=message_thread_id,
            trigger_message_id=trigger_message_id,
        )
        if not started:
            return cabinet_mod.TOOL_PHOTO_UNAVAILABLE
        return cabinet_mod.TOOL_PHOTO_STARTED.format(where=cab.describe_ru(), now=outside.ru())

    async def _photo_limit_reached(self, chat_id: int) -> bool:
        if not PHOTO_DAILY_LIMIT:
            return False
        since = self._now() - timedelta(days=1)
        taken = await self._store.count_images_since(chat_id, since, PHOTO_PURPOSE)
        return taken >= PHOTO_DAILY_LIMIT

    async def _start_photo(
        self,
        chat_id: int,
        user_id: int,
        *,
        focus: str,
        caption: str,
        happening: str | None,
        outside: Outside,
        message_thread_id: int | None,
        trigger_message_id: int | None,
    ) -> bool:
        """Снимок в фоне. False — не начат: уже идёт другой или нет связи."""
        if chat_id in self._photo_busy or not hasattr(self._notifier, "send_photo_ex"):
            return False
        if self._get_node_link() is None:
            return False
        self._photo_busy.add(chat_id)
        task = asyncio.create_task(
            self._photo_job(
                chat_id,
                user_id,
                focus=focus,
                caption=caption,
                happening=happening,
                outside=outside,
                message_thread_id=message_thread_id,
                trigger_message_id=trigger_message_id,
            )
        )
        self._photo_tasks.add(task)
        task.add_done_callback(self._photo_tasks.discard)
        return True

    async def _photo_job(
        self,
        chat_id: int,
        user_id: int,
        *,
        focus: str,
        caption: str,
        happening: str | None,
        outside: Outside,
        message_thread_id: int | None,
        trigger_message_id: int | None,
    ) -> None:
        try:
            await self._photo(
                chat_id,
                user_id,
                focus=focus,
                caption=caption,
                happening=happening,
                outside=outside,
                message_thread_id=message_thread_id,
                trigger_message_id=trigger_message_id,
            )
        except Exception:
            log.exception("interactives: снимок кабинета не удался (chat=%s)", chat_id)
        finally:
            self._photo_busy.discard(chat_id)

    async def _photo(
        self,
        chat_id: int,
        user_id: int,
        *,
        focus: str,
        caption: str,
        happening: str | None,
        outside: Outside,
        message_thread_id: int | None,
        trigger_message_id: int | None,
    ) -> None:
        node_link = self._get_node_link()
        if node_link is None:
            return
        dst = Address(node=LLM_NODE, service=LLM_SERVICE)
        cfg = self._settings.llm
        cab = await cabinet_mod.load(self._store, user_id)
        if not cab.features:
            new = await ask_features(
                node_link,
                dst,
                cfg.request_timeout_s,
                chat_id=chat_id,
                place=cab.describe_ru(),
                outside=outside.ru(),
                count=cabinet_mod.FIRST_FEATURES,
            )
            if cab.add(list(new)):
                await cabinet_mod.save(self._store, cab)
        description, context = photo_description(cab, outside, focus, happening)
        try:
            result = await node_link.command(
                image_tools.ACTION_GENERATE_IMAGE,
                {
                    "description": description,
                    "mode": "scene" if focus else "free",
                    "context": context,
                    "chat_id": chat_id,
                },
                dst=dst,
                timeout=cfg.imagegen_request_timeout_s,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
            log.warning("interactives: снимок не нарисован (chat=%s): %s", chat_id, exc)
            return
        png = base64.b64decode(result["png_b64"])
        state = cab.state_key(outside.key)
        image_id = await self._store.add_image(
            chat_id=chat_id,
            author=None,
            prompt_ru=f"{caption}: {focus or cab.describe_ru()}",
            prompt_en=str(result.get("prompt") or description),
            caption=caption,
            width=int(result["width"]),
            height=int(result["height"]),
            colors=cfg.imagegen_colors,
            png=png,
            now=self._now(),
            purpose=PHOTO_PURPOSE,
            params=json.dumps(
                {
                    "location": cabinet_mod.LOCATION,
                    "user_id": user_id,
                    "state": state,
                    "focus": focus,
                    "seed": result.get("seed"),
                },
                ensure_ascii=False,
            ),
        )
        sent = await self._notifier.send_photo_ex(
            chat_id,
            image_tools.upscale_png(png, cfg.imagegen_display_px),
            caption=caption,
            message_thread_id=message_thread_id,
            reply_to_message_id=trigger_message_id,
        )
        if sent is None:
            log.warning("interactives: снимок #%s не ушёл в чат %s", image_id, chat_id)
            return
        await self._store.set_image_sent(image_id, sent[1], sent[0])
        if not focus and not happening:
            cab = await cabinet_mod.load(self._store, user_id)
            cab.remember_photo(cab.state_key(outside.key), image_id)
            await cabinet_mod.save(self._store, cab)

    # --- кнопки ---

    async def handle_click(
        self,
        chat_id: int,
        user_id: int,
        scenario_id: str,
        button: str,
        *,
        message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> tuple[str, str | None, bool]:
        """Нажатие кнопки. Возвращает (текст для callback.answer, новый текст
        формы или None — не править, убрать ли клавиатуру).

        ``message_id``/``message_thread_id`` — сообщение с формой: реплика
        Альфреда после кнопки уходит в тот же тред и тот же диалог (живой баг
        2026-09-28: без них ответ улетал в общий топик лички)."""
        scenario = REGISTRY[scenario_id]
        run = await self._state.load_run(chat_id, scenario_id)
        where = await self._where(chat_id, message_id, message_thread_id)
        if button in (BTN_PLAY, BTN_LATER, BTN_NEVER):
            return await self._click_offer(run, user_id, button, where)
        if button == BTN_EXIT:
            if run is None or run.user_id != user_id:
                return "Эта кнопка не для вас.", None, False
            if run.status != STATUS_ACTIVE:
                return "Сценка уже закончилась.", None, True
            run.status = STATUS_PAUSED
            await self._state.save_run(run)
            return EXIT_ALERT, None, True
        if button in (BTN_SWAP, BTN_KEEP):
            if run is None or run.user_id != user_id:
                return "Эта форма не для вас.", None, False
            if run.status != STATUS_ACTIVE or not run.finale:
                return "Уже решено.", None, True
            if button == BTN_KEEP:
                return "Хорошо.", radio.SWAP_KEPT_TEXT, True
            await self._set_clear(chat_id, user_id, True)
            await self._state.mark_completed(scenario.id, user_id)
            run.status = STATUS_DONE
            await self._state.save_run(run)
            await self._speak(chat_id, radio.AFTER_SWAP_DIRECTIVE, where)
            return "Готово.", radio.SWAP_ACCEPTED_TEXT, True
        if button in (BTN_RETURN_OLD, BTN_INSTALL_NEW, BTN_TOGGLE_KEEP):
            if not await self._state.is_completed(scenario.id, user_id):
                return "Эта форма не для вас.", None, False
            if button == BTN_TOGGLE_KEEP:
                return "Оставили как есть.", None, True
            target = button == BTN_INSTALL_NEW
            if await self.speech_clear(user_id) == target:
                return "Уже так.", None, True
            await self._set_clear(chat_id, user_id, target)
            await self._speak(
                chat_id,
                radio.AFTER_SWAP_DIRECTIVE if target else radio.AFTER_RETURN_DIRECTIVE,
                where,
            )
            done_text = radio.REINSTALL_ACCEPTED_TEXT if target else radio.RETURN_ACCEPTED_TEXT
            return "Готово.", done_text, True
        return "Неизвестная кнопка.", None, False

    async def _where(
        self, chat_id: int, message_id: int | None, message_thread_id: int | None
    ) -> dict[str, Any]:
        """meta задачи tasks: куда доставить реплику Альфреда после кнопки
        (bot/node_events.py::_handle_task_result, как у tool_remind)."""
        dialogue_id = None
        if message_id is not None:
            turn = await self._store.ai_turn(chat_id, message_id)
            if turn is not None:
                dialogue_id = turn.get("dialogue_id")
        return {
            "dialogue_id": dialogue_id,
            "trigger_message_id": message_id,
            "message_thread_id": message_thread_id,
        }

    async def _click_offer(
        self, run: Run | None, user_id: int, button: str, where: dict[str, Any]
    ) -> tuple[str, str | None, bool]:
        if run is None or run.user_id != user_id:
            return "Эта форма не для вас.", None, False
        if run.status != STATUS_OFFERED:
            return "Уже решено.", None, True
        offer_text = REGISTRY[run.scenario].offer_text
        run = await self._expire_offer(run)
        if run is None or run.status != STATUS_OFFERED:
            return "Вопрос уже неактуален.", offer_text + OFFER_EXPIRED_SUFFIX, True
        if button == BTN_PLAY:
            run.status = STATUS_ACTIVE
            await self._state.save_run(run)
            transcript = "\n".join(run.transcript[-NOTE_TRANSCRIPT_LINES:]) or "—"
            await self._speak(
                run.chat_id, radio.AFTER_AGREE_DIRECTIVE.format(transcript=transcript), where
            )
            return "Хорошо.", offer_text + OFFER_YES_SUFFIX, True
        run.status = STATUS_DECLINED
        run.declined_until = iso(self._now() + DECLINE_COOLDOWN)
        await self._state.save_run(run)
        if button == BTN_NEVER:
            await self._state.set_opted_out(run.chat_id, True)
        # BTN_LATER — только у форм, разосланных до v0.115.2 («Не сейчас»).
        return "Хорошо.", offer_text + OFFER_NO_SUFFIX, True

    async def _set_clear(self, chat_id: int, user_id: int, clear: bool) -> None:
        """Источник правды — БД бота, пишется сразу. Служба llm — зеркало:
        пуш best-effort, а если mycraft спит, зеркало починит первый же
        живой запрос гостя (он несёт speech_clear, llm/service.py::
        _speech_target)."""
        await self._state.set_effect(radio.EFFECT_SPEECH_CLEAR, user_id, "1" if clear else "0")
        node_link = self._get_node_link()
        if node_link is None:
            return
        try:
            await node_link.command(
                ACTION_SET_SPEECH_CLEAR,
                {"user_id": user_id, "clear": clear},
                dst=Address(node=LLM_NODE, service=LLM_SERVICE),
                timeout=SET_SPEECH_TIMEOUT_S,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
            log.info("interactives: зеркало речи не обновлено сразу (chat=%s): %s", chat_id, exc)

    async def _speak(self, chat_id: int, directive: str, where: dict[str, Any]) -> None:
        """Реплика Альфреда после кнопки — задачей службы tasks (как речь
        форм Этапа 45). Best-effort: не вышло — форма уже сказала главное."""
        node_link = self._get_node_link()
        if node_link is None:
            return
        # Импорт здесь: bot/tools.py сам импортирует этот пакет (тул swap_radio).
        from sa_home_bot.bot import tools as ai_tools

        try:
            await ai_tools.schedule_agent_dialogue(
                node_link,
                chat_id,
                [wrap_system_directive(directive)],
                reminder_reason(self._settings.llm),
                self._settings.llm.request_timeout_s,
                meta_extra=where,
            )
        except (ServiceUnavailableError, ProtoError) as exc:
            log.info("interactives: реплика после смены не поставлена (chat=%s): %s", chat_id, exc)

    # --- запрет в переписке ---

    async def set_opted_out(self, chat_id: int, opted_out: bool) -> None:
        await self._state.set_opted_out(chat_id, opted_out)

    async def is_opted_out(self, chat_id: int) -> bool:
        return await self._state.is_opted_out(chat_id)


def scene_photo_focus(
    run: Run, decision: DirectorDecision | None, *, key_moment: bool
) -> str | None:
    """Нужен ли кадр после хода сцены и что в нём крупно ("" — общий вид).
    Переход стадии и финал снимаются всегда; по желанию Ведущего — не чаще
    раза в PHOTO_SCENE_GAP_TURNS ходов."""
    wanted = decision.photo if decision is not None else None
    if key_moment:
        return wanted or ""
    if wanted is None:
        return None
    if run.photo_turn is not None and run.turns_total - run.photo_turn < PHOTO_SCENE_GAP_TURNS:
        return None
    return wanted


def photo_description(
    cab: Cabinet, outside: Outside, focus: str, happening: str | None
) -> tuple[str, str]:
    """Описание снимка для художника-промптера (llm/image_prompt.py) и
    контекст сцены. Общий вид — особенности гостя первыми (самые свежие:
    промпт ограничен 77 токенами), потом канон и свет; крупный план —
    предмет, а кабинет со светом — контекстом."""
    features = cab.visible_features()[-PHOTO_FEATURES_IN_FRAME:]
    light = outside.en()
    if focus:
        context = f"{cabinet_mod.CANON_EN}; {'; '.join(features)}; {light}"
        if happening:
            context += f"; just happened: {happening}"
        return focus, context
    parts = []
    if features:
        parts.append("Clearly visible: " + "; ".join(features) + ".")
    parts.append(f"Room: {cabinet_mod.CANON_EN}.")
    parts.append(f"Light: {light}.")
    if happening:
        parts.append(f"Just happened: {happening}")
    return " ".join(parts), ""


def _plain(text_html: str) -> str:
    return html.unescape(_TAG_RE.sub("", text_html))
