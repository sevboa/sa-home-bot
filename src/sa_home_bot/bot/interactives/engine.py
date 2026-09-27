"""Движок интерактивов (Этап 47): согласие → ходы сцены → финал → эффект.

Встраивание в ход /ai (bot/handlers/ai.py::_do_ask_and_reply):

1. ``before_turn`` — ДО запроса к модели: решает, идёт ли сцена, нужна ли
   форма согласия, и собирает скрытую заметку Альфреду (рамка сцены +
   подсказка стадии + директива Ведущего + журнал). Заодно отдаёт
   ``speech_clear`` гостя — он едет в каждый запрос к службе llm.
2. Альфред отвечает как обычно, ответ сразу уходит гостю.
3. ``after_turn`` — ПОСЛЕ ответа: журнал, вызов Ведущего, рамки темпа,
   сообщение рассказчика (🎬) с кнопкой выхода.
4. ``flush_forms`` — последним шагом хода: формы (согласие, смена
   передатчика), которые попросили тул или финал. Форма, посланная
   посреди генерации, обогнала бы речь Альфреда (тот же приём, что
   PendingActions.flush_drafts, Этап 45).

Согласие: сцена сначала ``offered`` и стартует только кнопкой «Играть».
«Не сейчас» — кулдаун, «Больше не предлагать» — запрет интерактивов в этой
переписке (снимается командой /interactives). Сцены — только в личке: в
общем чате рассказчик и подсказки мешали бы остальным. Эффект и
завершённость — на гостя глобально (base.py), так что переключатель после
завершения работает в любом чате.
"""

from __future__ import annotations

import html
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
from sa_home_bot.bot.interactives.director import DirectorDecision, ask_director
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

NARRATOR_PREFIX = "🎬 Ведущий:"
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

OFFER_PLAY_TEXT = (
    "🎬 <b>Сценка началась!</b>\n"
    "<i>Рассказывайте Альфреду, что не так со связью. Выйти — кнопкой под "
    "сообщениями Ведущего.</i>"
)
OFFER_LATER_TEXT = "🎬 <i>Хорошо, в другой раз.</i>"
OFFER_NEVER_TEXT = (
    "🚫 <i>Больше не буду предлагать сценки в этом чате. Вернуть — командой "
    "/interactives.</i>"
)
OFFER_EXPIRED_TEXT = "🎬 <i>Предложение устарело.</i>"
EXIT_ALERT = "Вы вышли из сценки. Вернуться можно, снова пожаловавшись на связь."

OPT_IN_TEXT = "🎬 Сценки в этом чате снова включены."
OPT_OUT_TEXT = "🚫 Сценки в этом чате выключены. Включить — /interactives on."


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
            [("▶️ Играть", BTN_PLAY), ("Не сейчас", BTN_LATER)],
            [("🚫 Больше не предлагать в этом чате", BTN_NEVER)],
        ],
    )


def exit_keyboard(scenario: str) -> InlineKeyboardMarkup:
    return _keyboard(scenario, [[("⏹ Выйти из сценки", BTN_EXIT)]])


def swap_keyboard(scenario: str) -> InlineKeyboardMarkup:
    return _keyboard(scenario, [[("📻 Заменить", BTN_SWAP), ("Оставить старый", BTN_KEEP)]])


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


def build_scene_note(scenario: Scenario, run: Run) -> str:
    parts = [scenario.scene_frame]
    if run.finale:
        parts.append(scenario.finale_directive.format(fault=run.finale_fault))
    else:
        parts.append("Сейчас: " + scenario.ladder[min(run.stage, scenario.last_stage)])
    if run.directive:
        parts.append("Подсказка на этот ход: " + run.directive)
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
    ) -> None:
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
            plan.note = build_scene_note(scenario, run)
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
        decision = await self._ask_director(scenario, run)
        effect = apply_decision(scenario, run, decision, choose=self._choose)
        if effect:
            run.log("Ведущий", effect)
        await self._state.save_run(run)
        if effect and run.status == STATUS_ACTIVE:
            await self._send_narrator(run, effect, dialogue_id, message_thread_id)

    async def _ask_director(self, scenario: Scenario, run: Run) -> DirectorDecision | None:
        node_link = self._get_node_link()
        if node_link is None:
            return None
        return await ask_director(
            node_link,
            Address(node=LLM_NODE, service=LLM_SERVICE),
            self._settings.llm.request_timeout_s,
            scenario,
            run,
            finale_allowed=finale_allowed(scenario, run),
        )

    async def _send_narrator(
        self, run: Run, effect: str, dialogue_id: int | None, message_thread_id: int | None
    ) -> None:
        text = f"🎬 <b>Ведущий:</b> <i>{html.escape(effect)}</i>"
        message_id = await self._notifier.send_direct(
            run.chat_id,
            text,
            reply_markup=exit_keyboard(run.scenario),
            message_thread_id=message_thread_id,
        )
        if message_id is None:
            return
        # Как формы Этапа 45: реплика рассказчика — ход треда, чтобы реплай
        # на неё продолжал тот же диалог.
        await self._store.record_ai_turn(
            run.chat_id,
            message_id,
            dialogue_id if dialogue_id is not None else message_id,
            "assistant",
            f"{NARRATOR_PREFIX} {effect}",
            self._now(),
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
            return radio.RETURN_FORM_TEXT, toggle_keyboard(
                scenario_id, BTN_RETURN_OLD, "📻 Вернуть старый"
            )
        return radio.REINSTALL_FORM_TEXT, toggle_keyboard(
            scenario_id, BTN_INSTALL_NEW, "📻 Поставить новый"
        )

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

    # --- кнопки ---

    async def handle_click(
        self, chat_id: int, user_id: int, scenario_id: str, button: str
    ) -> tuple[str, str | None, bool]:
        """Нажатие кнопки. Возвращает (текст для callback.answer, новый текст
        формы или None — не править, убрать ли клавиатуру)."""
        scenario = REGISTRY[scenario_id]
        run = await self._state.load_run(chat_id, scenario_id)
        if button in (BTN_PLAY, BTN_LATER, BTN_NEVER):
            return await self._click_offer(run, user_id, button)
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
                return "Оставили старый.", radio.SWAP_KEPT_TEXT, True
            await self._set_clear(chat_id, user_id, True)
            await self._state.mark_completed(scenario.id, user_id)
            run.status = STATUS_DONE
            await self._state.save_run(run)
            await self._speak(chat_id, radio.AFTER_SWAP_DIRECTIVE)
            return "Передатчик заменён!", radio.SWAP_ACCEPTED_TEXT, True
        if button in (BTN_RETURN_OLD, BTN_INSTALL_NEW, BTN_TOGGLE_KEEP):
            if not await self._state.is_completed(scenario.id, user_id):
                return "Эта форма не для вас.", None, False
            if button == BTN_TOGGLE_KEEP:
                return "Оставили как есть.", radio.TOGGLE_KEPT_TEXT, True
            target = button == BTN_INSTALL_NEW
            if await self.speech_clear(user_id) == target:
                return "Уже так.", None, True
            await self._set_clear(chat_id, user_id, target)
            await self._speak(
                chat_id, radio.AFTER_SWAP_DIRECTIVE if target else radio.AFTER_RETURN_DIRECTIVE
            )
            done_text = radio.REINSTALL_ACCEPTED_TEXT if target else radio.RETURN_ACCEPTED_TEXT
            return "Готово.", done_text, True
        return "Неизвестная кнопка.", None, False

    async def _click_offer(
        self, run: Run | None, user_id: int, button: str
    ) -> tuple[str, str | None, bool]:
        if run is None or run.user_id != user_id:
            return "Эта форма не для вас.", None, False
        if run.status != STATUS_OFFERED:
            return "Уже решено.", None, True
        run = await self._expire_offer(run)
        if run is None or run.status != STATUS_OFFERED:
            return "Срок предложения истёк.", OFFER_EXPIRED_TEXT, True
        if button == BTN_PLAY:
            run.status = STATUS_ACTIVE
            await self._state.save_run(run)
            return "Играем!", OFFER_PLAY_TEXT, True
        run.status = STATUS_DECLINED
        run.declined_until = iso(self._now() + DECLINE_COOLDOWN)
        await self._state.save_run(run)
        if button == BTN_NEVER:
            await self._state.set_opted_out(run.chat_id, True)
            return "Больше не предложу.", OFFER_NEVER_TEXT, True
        return "Хорошо.", OFFER_LATER_TEXT, True

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

    async def _speak(self, chat_id: int, directive: str) -> None:
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
            )
        except (ServiceUnavailableError, ProtoError) as exc:
            log.info("interactives: реплика после смены не поставлена (chat=%s): %s", chat_id, exc)

    # --- запрет в переписке ---

    async def set_opted_out(self, chat_id: int, opted_out: bool) -> None:
        await self._state.set_opted_out(chat_id, opted_out)

    async def is_opted_out(self, chat_id: int) -> bool:
        return await self._state.is_opted_out(chat_id)


def _plain(text_html: str) -> str:
    return html.unescape(_TAG_RE.sub("", text_html))
