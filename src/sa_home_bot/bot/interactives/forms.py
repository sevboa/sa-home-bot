from __future__ import annotations

import json
import logging
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from sa_home_bot.bot.interactives import items as items_mod
from sa_home_bot.bot.interactives import radio
from sa_home_bot.bot.interactives.base import (
    STATUS_ACTIVE,
    STATUS_DECLINED,
    STATUS_DONE,
    STATUS_IDLE,
    STATUS_OFFERED,
    STATUS_PAUSED,
    iso,
)
from sa_home_bot.bot.interactives.core import EngineCore, _Queued
from sa_home_bot.bot.interactives.keyboards import (
    BTN_EXIT,
    BTN_INSTALL_NEW,
    BTN_KEEP,
    BTN_LATER,
    BTN_NEVER,
    BTN_PLAY,
    BTN_RETURN_OLD,
    BTN_SWAP,
    BTN_TOGGLE_KEEP,
    EXIT_ALERT,
    FORM_ITEM,
    FORM_OFFER,
    FORM_REINSTALL,
    FORM_RETURN,
    FORM_SWAP,
    LIVE_BUTTONS_KEY,
    LIVE_FORM,
    LIVE_MENU,
    _plain,
    item_caption,
    item_form_keyboard,
    item_keyboard,
    offer_keyboard,
    swap_keyboard,
    toggle_keyboard,
)
from sa_home_bot.bot.interactives.registry import REGISTRY
from sa_home_bot.bot.interactives.scene import TurnPlan
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import reminder_reason
from sa_home_bot.llm.prompt import wrap_system_directive
from sa_home_bot.proto.addresses import LLM_ADDRESS
from sa_home_bot.proto.messages import ProtoError

log = logging.getLogger(__name__)

ACTION_SET_SPEECH_CLEAR = "set_speech_clear"


SET_SPEECH_TIMEOUT_S = 15.0


class FormsMixin(EngineCore):
    def _queue(self, chat_id: int, scenario: str, form: str, user_id: int | None) -> None:
        queued = self._queued.setdefault(chat_id, _Queued())
        queued.user_id = user_id if user_id is not None else queued.user_id
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
        # Кадры катакомб (59.4) — тоже после речи Альфреда.
        await self.flush_frames(chat_id)
        if plan is not None and plan.force_swap_form and plan.scenario is not None:
            run = await self._state.load_run(chat_id, plan.scenario)
            if run is not None and run.status == STATUS_ACTIVE and run.finale:
                self._queue(chat_id, plan.scenario, FORM_SWAP, plan.user_id)
        queued = self._queued.pop(chat_id, None)
        if queued is None:
            return
        outgoing: list[tuple[str, InlineKeyboardMarkup, dict[str, Any]]] = [
            (*self._render_form(scenario_id, form), {"scenario": scenario_id, "form": form})
            for scenario_id, form in queued.forms
        ]
        for item_id, code in queued.items:
            item = await self._store.item_by_id(item_id)
            kind = items_mod.KINDS.get(item["type"]) if item is not None else None
            action = kind.action(code) if kind is not None else None
            if action is not None:
                outgoing.append(
                    (action.form, item_form_keyboard(item_id, action), {"form": FORM_ITEM})
                )
        for text, markup, extra in outgoing:
            message_id = await self._notifier.send_direct(
                chat_id, text, reply_markup=markup, message_thread_id=message_thread_id
            )
            if message_id is None:
                log.warning("interactives: форма %s не ушла (chat=%s)", extra, chat_id)
                continue
            await self._remember_buttons(chat_id, message_id, queued.user_id, LIVE_FORM, **extra)
            scenario_id, form = extra.get("scenario", ""), extra["form"]
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
        self.discard_frames(chat_id)
        self._queued.pop(chat_id, None)

    async def _live_buttons(self, chat_id: int) -> list[dict[str, Any]]:
        raw = await self._store.get_state(LIVE_BUTTONS_KEY.format(chat_id=chat_id))
        try:
            entries = json.loads(raw) if raw else []
        except ValueError:
            return []
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    async def _save_live_buttons(self, chat_id: int, entries: list[dict[str, Any]]) -> None:
        await self._store.set_state(
            LIVE_BUTTONS_KEY.format(chat_id=chat_id), json.dumps(entries, ensure_ascii=False)
        )

    async def _remember_buttons(
        self, chat_id: int, message_id: int, user_id: int | None, kind: str, **extra: Any
    ) -> None:
        entries = [e for e in await self._live_buttons(chat_id) if e.get("m") != message_id]
        entries.append({"m": message_id, "u": user_id, "k": kind, **extra})
        await self._save_live_buttons(chat_id, entries)

    async def _forget_buttons(self, chat_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        entries = await self._live_buttons(chat_id)
        left = [e for e in entries if e.get("m") != message_id]
        if len(left) != len(entries):
            await self._save_live_buttons(chat_id, left)

    async def dismiss_buttons(
        self, chat_id: int, user_id: int | None, *, keep: set[int] | frozenset[int] = frozenset()
    ) -> int:
        """Гость написал, не нажав кнопку, — «проигнорировано гостем»: у форм
        кнопки снимаются (решение остаётся прежним; согласие на сцену можно
        будет спросить снова), раскрытый перечень действий сворачивается в
        карточку. Чужое сообщение в общем чате чужих форм не трогает.
        ``keep`` — сообщения, которые не гасить (живое предложение канарейки).
        Возвращает, сколько сообщений погашено."""
        entries = await self._live_buttons(chat_id)
        if not entries:
            return 0
        mine = [e for e in entries if e.get("u") in (None, user_id) and e.get("m") not in keep]
        if not mine:
            return 0
        await self._save_live_buttons(chat_id, [e for e in entries if e not in mine])
        for entry in mine:
            try:
                await self._dismiss_one(chat_id, entry)
            except Exception:  # noqa: BLE001 — косметика, ход гостя важнее
                log.info(
                    "interactives: кнопки не сняты (chat=%s, %s)", chat_id, entry, exc_info=True
                )
        return len(mine)

    async def _dismiss_one(self, chat_id: int, entry: dict[str, Any]) -> None:
        message_id = int(entry["m"])
        if entry.get("k") == LIVE_MENU:
            item = await self._store.item_by_id(int(entry.get("item") or 0))
            kind = items_mod.KINDS.get(item["type"]) if item is not None else None
            if item is None or kind is None:
                await self._notifier.edit_markup(chat_id, message_id, None)
                return
            owner = int(item["owner_user_id"])
            if entry.get("inv"):
                text, markup = await self.inventory(owner)
                await self._notifier.edit_caption(
                    chat_id, message_id, text, reply_markup=markup, photo=False
                )
                return
            place = await self.item_place(owner, item)
            await self._notifier.edit_caption(
                chat_id,
                message_id,
                item_caption(kind, item, place == items_mod.PLACE_DESK),
                reply_markup=item_keyboard(kind, int(item["id"])),
                photo=bool(entry.get("photo")),
            )
            return
        await self._notifier.edit_markup(chat_id, message_id, None)
        if entry.get("form") == FORM_OFFER and entry.get("scenario") in REGISTRY:
            # Согласие без ответа — как истёкшее, но без кулдауна: заговорит
            # гость о радиостанции снова — спросим снова.
            run = await self._state.load_run(chat_id, str(entry["scenario"]))
            if run is not None and run.status == STATUS_OFFERED:
                run.status = STATUS_DECLINED
                run.declined_until = iso(self._now())
                await self._state.save_run(run)

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

    async def tool_swap_radio(
        self, chat_id: int | None, user_id: int | None, *, is_private: bool
    ) -> str:
        if chat_id is None or user_id is None:
            return radio.TOOL_UNAVAILABLE
        if self._pinned(chat_id):
            return radio.TOOL_PINNED
        scenario = radio.RADIO
        if await self._state.is_completed(scenario.id, user_id):
            # Прошедшим сцену до Этапа 49.3 — старое радио предметом.
            await self._owned_item(user_id, items_mod.RADIO)
            if await self.speech_clear(user_id):
                self._queue(chat_id, scenario.id, FORM_RETURN, user_id)
                return radio.TOOL_RETURN_FORM
            self._queue(chat_id, scenario.id, FORM_REINSTALL, user_id)
            return radio.TOOL_REINSTALL_FORM
        if not is_private or await self._state.is_opted_out(chat_id):
            return radio.TOOL_OPTED_OUT
        run = await self._state.load_run(chat_id, scenario.id)
        run = await self._expire_offer(run)
        if run is not None and run.status == STATUS_ACTIVE and run.finale:
            self._queue(chat_id, scenario.id, FORM_SWAP, user_id)
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
        result = await self._handle_click(
            chat_id,
            user_id,
            scenario_id,
            button,
            message_id=message_id,
            message_thread_id=message_thread_id,
        )
        if result[1] is not None or result[2]:
            await self._forget_buttons(chat_id, message_id)  # форма закрыта нажатием
        return result

    async def _handle_click(
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
        if scenario.canary and not self.canary_ok(user_id):
            # Канареечной формы у прочих гостей нет; нажать могут только
            # подделкой callback_data.
            return "Эта форма не для вас.", None, False
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
            await self._end_scene_in_cabinet(user_id)
            await self._speak(chat_id, radio.AFTER_SWAP_DIRECTIVE, where)
            if scenario.item_kind in items_mod.KINDS:
                self._spawn(
                    self._grant_item(chat_id, user_id, run, where.get("message_thread_id")),
                    "выдача предмета",
                )
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
                dst=LLM_ADDRESS,
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
