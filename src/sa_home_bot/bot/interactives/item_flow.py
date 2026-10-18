from __future__ import annotations

import base64
import json
import logging
from datetime import timedelta
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from sa_home_bot.bot import image_tools
from sa_home_bot.bot.interactives import cabinet as cabinet_mod
from sa_home_bot.bot.interactives import items as items_mod
from sa_home_bot.bot.interactives import radio
from sa_home_bot.bot.interactives.base import STATUS_ACTIVE, Run, iso
from sa_home_bot.bot.interactives.core import EngineCore, _Queued
from sa_home_bot.bot.interactives.keyboards import (
    _NO_BUTTONS,
    ITEM_BTN_ACTIONS,
    ITEM_BTN_BACK,
    ITEM_BTN_CARD,
    ITEM_BTN_KEEP,
    ITEM_FROM_FORM,
    ITEM_FROM_INVENTORY,
    LIVE_MENU,
    _plain,
    inventory_keyboard,
    inventory_text,
    item_actions_caption,
    item_actions_keyboard,
    item_caption,
    item_keyboard,
)
from sa_home_bot.bot.interactives.llm_link import LLM_CALL_ERRORS
from sa_home_bot.bot.interactives.photo_text import ITEM_PENDING_TAG, _image_params, _ItemShot
from sa_home_bot.bot.interactives.registry import REGISTRY
from sa_home_bot.proto.addresses import LLM_ADDRESS

log = logging.getLogger(__name__)

ITEM_PURPOSE = "item"


ACTION_ITEM_PORTRAIT = "item_portrait"


ITEMS_SHOWN_MAX = 3


NEW_RADIO_CUT_KEY = "radio-new"


NEW_RADIO_SEED = 33


NEW_RADIO_READY_KEY = "item_cut_ready:radio-new"


class ItemFlowMixin(EngineCore):
    async def _portrait(
        self, chat_id: int, key: str, kind: items_mod.ItemKind, traits: list[str], seed: int
    ) -> dict[str, Any] | None:
        node_link = self._get_node_link()
        if node_link is None:
            return None
        try:
            result = await node_link.command(
                ACTION_ITEM_PORTRAIT,
                {
                    "prompt": kind.portrait_prompt(traits),
                    "key": key,
                    "seed": seed,
                    "checks": list(kind.checks),
                    "chat_id": chat_id,
                    **({"restyle": restyle} if (restyle := kind.restyle(traits)) else {}),
                },
                dst=LLM_ADDRESS,
                timeout=self._settings.llm.imagegen_request_timeout_s * 2,
            )
        except LLM_CALL_ERRORS as exc:
            log.warning("interactives: портрет %s не нарисован (chat=%s): %s", key, chat_id, exc)
            return None
        if not isinstance(result, dict) or not result.get("png_b64"):
            return None
        log.info(
            "interactives: портрет %s, зерно %s, черты %s, нет %s",
            key,
            result.get("seed"),
            traits,
            result.get("missing"),
        )
        return result

    async def _save_portrait(
        self,
        chat_id: int,
        kind: items_mod.ItemKind,
        key: str,
        traits: list[str],
        result: dict[str, Any],
    ) -> int:
        png = base64.b64decode(result["png_b64"])
        return await self._store.add_image(
            chat_id=chat_id,
            author=None,
            prompt_ru=" ".join([kind.name, *kind.traits_ru(traits)]),
            prompt_en=str(result.get("full_prompt") or kind.portrait_prompt(traits)),
            caption=kind.name,
            width=int(result.get("width") or 0),
            height=int(result.get("height") or 0),
            colors=self._settings.llm.imagegen_colors,
            png=png,
            now=self._now(),
            purpose=ITEM_PURPOSE,
            params=json.dumps(
                {
                    "cut_key": key,
                    "seed": result.get("seed"),
                    "traits": traits,
                    "seen": result.get("seen"),
                    "missing": result.get("missing"),
                },
                ensure_ascii=False,
            ),
        )

    async def _owned_item(self, user_id: int, kind: items_mod.ItemKind) -> dict[str, Any] | None:
        owned = await self._store.items_of(user_id, kind.type)
        if owned:
            return owned[0]
        if kind.type != items_mod.RADIO_TYPE or not await self._state.is_completed(
            radio.SCENARIO_ID, user_id
        ):
            return None
        item_id = await self._store.add_item(
            type=kind.type,
            owner_user_id=user_id,
            traits=[],
            image_id=None,
            origin="radio_scene_before_items",
            now=self._now(),
        )
        log.info("interactives: гостю %s выдано радио задним числом (#%s)", user_id, item_id)
        return await self._store.item_by_id(item_id)

    async def _item_cut(
        self, chat_id: int, item: dict[str, Any], kind: items_mod.ItemKind, *, draw: bool
    ) -> tuple[str, int] | None:
        if item.get("image_id"):
            image = await self._store.image_by_id(int(item["image_id"]))
            key = _image_params(image).get("cut_key") if image else None
            if isinstance(key, str) and key:
                return key, int(item["image_id"])
        if not draw:
            return None
        key = items_mod.new_cut_key(kind, int(item["owner_user_id"]))
        result = await self._portrait(chat_id, key, kind, item["traits"], items_mod.new_seed())
        if result is None:
            return None
        image_id = await self._save_portrait(chat_id, kind, key, item["traits"], result)
        await self._store.set_item_image(int(item["id"]), image_id, item["traits"])
        item["image_id"] = image_id
        return key, image_id

    async def _item_shot(
        self, chat_id: int, user_id: int, focus: str, *, draw: bool
    ) -> _ItemShot | None:
        kind = items_mod.RADIO
        if focus and not kind.focus_re.search(focus):
            return None
        place = "closeup" if focus else "desk"
        run = await self._state.load_run(chat_id, radio.SCENARIO_ID)
        if run is not None and run.status == STATUS_ACTIVE and run.item_seed is not None:
            drawn = list(kind.drawn_key(run.item_traits))
            if run.item_key is None or run.item_drawn != drawn:
                if not draw:
                    return _ItemShot(kind, "", place, ITEM_PENDING_TAG)
                key = run.item_key or items_mod.new_cut_key(kind, user_id)
                result = await self._portrait(chat_id, key, kind, run.item_traits, run.item_seed)
                if result is None:
                    return None
                fresh = await self._state.load_run(chat_id, radio.SCENARIO_ID) or run
                fresh.item_key, fresh.item_drawn = key, drawn
                if isinstance(result.get("seed"), int):
                    fresh.item_seed = result["seed"]
                await self._state.save_run(fresh)
                run = fresh
            return _ItemShot(kind, str(run.item_key), place, f"{run.item_key}:{','.join(drawn)}")
        item = await self._owned_item(user_id, kind)
        if item is None:
            return None
        if await self.speech_clear(user_id):
            if not await self._store.get_state(NEW_RADIO_READY_KEY):
                if not draw:
                    return _ItemShot(kind, "", place, ITEM_PENDING_TAG)
                result = await self._portrait(chat_id, NEW_RADIO_CUT_KEY, kind, [], NEW_RADIO_SEED)
                if result is None:
                    return None
                await self._store.set_state(NEW_RADIO_READY_KEY, iso(self._now()))
            return _ItemShot(kind, NEW_RADIO_CUT_KEY, place, NEW_RADIO_CUT_KEY)
        cut = await self._item_cut(chat_id, item, kind, draw=draw)
        if cut is None:
            return None if draw else _ItemShot(kind, "", place, ITEM_PENDING_TAG)
        key, image_id = cut
        return _ItemShot(kind, key, place, f"item{item['id']}:{image_id}")

    async def _forget_item_cut(self, chat_id: int, user_id: int, shot: _ItemShot) -> None:
        if shot.key == NEW_RADIO_CUT_KEY:
            await self._store.set_state(NEW_RADIO_READY_KEY, "")
            return
        run = await self._state.load_run(chat_id, radio.SCENARIO_ID)
        if run is not None and run.item_key == shot.key:
            run.item_drawn = ["-"]
            await self._state.save_run(run)

    async def _grant_item(
        self, chat_id: int, user_id: int, run: Run, message_thread_id: int | None
    ) -> None:
        kind = items_mod.KINDS[REGISTRY[run.scenario].item_kind or ""]
        if await self._store.items_of(user_id, kind.type):
            return
        key = run.item_key or items_mod.new_cut_key(kind, user_id)
        seed = run.item_seed if run.item_seed is not None else items_mod.new_seed()
        result = await self._portrait(chat_id, key, kind, run.item_traits, seed)
        image_id = None
        if result is not None:
            image_id = await self._save_portrait(chat_id, kind, key, run.item_traits, result)
        item_id = await self._store.add_item(
            type=kind.type,
            owner_user_id=user_id,
            traits=list(run.item_traits),
            image_id=image_id,
            origin="radio_scene",
            now=self._now(),
            note=run.finale_fault,
        )
        item = await self._store.item_by_id(item_id)
        if item is not None:
            await self._send_item_card(chat_id, user_id, item, message_thread_id=message_thread_id)

    async def _send_item_card(
        self,
        chat_id: int,
        user_id: int,
        item: dict[str, Any],
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        kind = items_mod.KINDS.get(item["type"])
        if kind is None:
            return None
        installed = await self.item_place(user_id, item) == items_mod.PLACE_DESK
        caption = item_caption(kind, item, installed)
        markup = item_keyboard(kind, int(item["id"]))
        image = await self._store.image_by_id(int(item["image_id"])) if item["image_id"] else None
        if image is None or not self._can_send_photos:
            return await self._notifier.send_direct(
                chat_id, caption, reply_markup=markup, message_thread_id=message_thread_id
            )
        photo: bytes | str = image["telegram_file_id"] or image_tools.upscale_png(
            image["png"], self._settings.llm.imagegen_display_px
        )
        sent = await self._notifier.send_photo_ex(
            chat_id,
            photo,
            caption=caption,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            reply_markup=markup,
        )
        if sent is None:
            return None
        if not image["telegram_file_id"]:
            await self._store.set_image_sent(int(image["id"]), sent[1], sent[0])
        return sent[0]

    async def tool_manor_items(
        self,
        chat_id: int | None,
        user_id: int | None,
        *,
        show: str = "",
        message_thread_id: int | None = None,
        trigger_message_id: int | None = None,
        dialogue_id: int | None = None,
    ) -> str:
        if chat_id is None or user_id is None:
            return radio.TOOL_ITEMS_UNAVAILABLE
        await self._owned_item(user_id, items_mod.RADIO)
        owned = await self._owned_items(user_id)
        if not owned:
            return radio.TOOL_ITEMS_NONE
        lines = await self.manor_lines(user_id, owned)
        if not show.strip():
            return radio.TOOL_ITEMS_LIST.format(lines=lines)
        kind = items_mod.find_kind(show, self._visible_kinds(user_id))
        chosen = [it for it in owned if kind is not None and it["type"] == kind.type]
        if not chosen:
            return radio.TOOL_ITEMS_UNKNOWN.format(lines=lines)
        names, drawing = [], []
        for it in chosen[:ITEMS_SHOWN_MAX]:
            name = items_mod.KINDS[it["type"]].name
            if await self._show_card(
                chat_id,
                user_id,
                it,
                message_thread_id,
                reply_to_message_id=trigger_message_id,
                dialogue_id=dialogue_id,
            ):
                names.append(name)
            else:
                drawing.append(name)
        if drawing and not names:
            return radio.TOOL_ITEMS_DRAWING.format(items=", ".join(drawing))
        return radio.TOOL_ITEMS_SENT.format(items=", ".join(names + drawing))

    async def manor_lines(self, user_id: int, owned: list[dict[str, Any]]) -> str:
        lines = []
        for item in owned:
            kind = items_mod.KINDS[item["type"]]
            place = await self.item_place(user_id, item)
            traits = kind.traits_ru(item.get("traits") or [])
            note = (item.get("note") or "").rstrip(". ")
            actions = "; ".join(
                radio.TOOL_ITEMS_ACTION.format(what=action.tool_ru, name=action.name)
                for action in kind.available(place)
            )
            lines.append(
                radio.TOOL_ITEMS_LINE.format(
                    name=kind.name,
                    where=kind.where_ru(place),
                    traits=radio.TOOL_ITEMS_TRAITS.format(traits="; ".join(traits))
                    if traits
                    else "",
                    fault=(kind.note_tool or radio.TOOL_ITEMS_FAULT).format(fault=note)
                    if note
                    else "",
                    actions=actions or radio.TOOL_ITEMS_NO_ACTIONS,
                )
            )
        return "\n".join(lines)

    async def tool_item_action(
        self, chat_id: int | None, user_id: int | None, *, item: str, action: str
    ) -> str:
        if chat_id is None or user_id is None:
            return radio.TOOL_ACTION_UNAVAILABLE
        await self._owned_item(user_id, items_mod.RADIO)
        owned = await self._owned_items(user_id)
        if not owned:
            return radio.TOOL_ITEMS_NONE
        lines = await self.manor_lines(user_id, owned)
        kind = items_mod.find_kind(item, self._visible_kinds(user_id))
        chosen = next((it for it in owned if kind is not None and it["type"] == kind.type), None)
        if chosen is None or kind is None:
            return radio.TOOL_ITEMS_UNKNOWN.format(lines=lines)
        act = kind.action(action)
        if act is None:
            return radio.TOOL_ACTION_UNKNOWN.format(lines=lines)
        if act.pinned_locked and self._pinned(chat_id):
            return radio.TOOL_PINNED
        place = await self.item_place(user_id, chosen)
        if act not in kind.available(place):
            return radio.TOOL_ACTION_ALREADY.format(where=kind.where_ru(place))
        queued = self._queued.setdefault(chat_id, _Queued())
        queued.user_id = user_id
        if (int(chosen["id"]), act.code) not in queued.items:
            queued.items.append((int(chosen["id"]), act.code))
        return radio.TOOL_ACTION_FORM.format(form=_plain(act.form))

    async def item_place(self, user_id: int, item: dict[str, Any]) -> str:
        if item["type"] == items_mod.RADIO_TYPE and not await self.speech_clear(user_id):
            return items_mod.PLACE_DESK
        if item["type"] == items_mod.CAMERA_TYPE and await self.camera_on(user_id):
            return items_mod.PLACE_DESK
        return items_mod.PLACE_STOREROOM

    def _visible_kinds(self, user_id: int | None) -> list[items_mod.ItemKind]:
        canary = self.canary_ok(user_id)
        return [k for k in items_mod.KINDS.values() if canary or not k.canary]

    async def _owned_items(self, user_id: int) -> list[dict[str, Any]]:
        visible = {k.type for k in self._visible_kinds(user_id)}
        return [it for it in await self._store.items_of(user_id) if it["type"] in visible]

    async def inventory(self, user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
        await self._owned_item(user_id, items_mod.RADIO)
        owned = await self._owned_items(user_id)
        entries = [
            (items_mod.KINDS[it["type"]], it, await self.item_place(user_id, it)) for it in owned
        ]
        return inventory_text(entries), inventory_keyboard(owned)

    async def _show_card(
        self,
        chat_id: int,
        user_id: int,
        item: dict[str, Any],
        message_thread_id: int | None,
        *,
        reply_to_message_id: int | None = None,
        dialogue_id: int | None = None,
    ) -> bool:
        kind = items_mod.KINDS[item["type"]]
        if item["image_id"]:
            await self._send_item_card(
                chat_id,
                user_id,
                item,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
            )
            return True
        task = self._spawn(
            self._draw_and_send_card(
                chat_id, user_id, item, kind, message_thread_id, dialogue_id=dialogue_id
            ),
            "карточка предмета",
        )
        if dialogue_id is not None:
            self._card_jobs[chat_id] = task
            task.add_done_callback(
                lambda t: (
                    self._card_jobs.pop(chat_id, None)
                    if self._card_jobs.get(chat_id) is t
                    else None
                )
            )
        return False

    async def _draw_and_send_card(
        self,
        chat_id: int,
        user_id: int,
        item: dict[str, Any],
        kind: items_mod.ItemKind,
        message_thread_id: int | None,
        *,
        dialogue_id: int | None = None,
    ) -> None:
        await self._item_cut(chat_id, item, kind, draw=True)
        message_id = await self._send_item_card(
            chat_id, user_id, item, message_thread_id=message_thread_id
        )
        if message_id is None or dialogue_id is None:
            return
        installed = await self.item_place(user_id, item) == items_mod.PLACE_DESK
        await self._store.record_ai_turn(
            chat_id,
            message_id,
            dialogue_id,
            "assistant",
            _plain(item_caption(kind, item, installed)),
            self._now(),
        )

    async def handle_item_click(
        self,
        chat_id: int,
        user_id: int,
        item_id: int,
        button: str,
        *,
        message_id: int | None = None,
        message_thread_id: int | None = None,
        photo: bool = False,
    ) -> tuple[str, InlineKeyboardMarkup | None, str | None]:
        item = await self._store.item_by_id(item_id)
        if item is None or int(item["owner_user_id"]) != user_id:
            return radio.ITEM_NOT_YOURS, None, None
        kind = items_mod.KINDS.get(item["type"])
        if kind is not None and kind.canary and not self.canary_ok(user_id):
            return radio.ITEM_NOT_YOURS, None, None
        if item.get("status") == "consumed" and kind is not None:
            if button.startswith(ITEM_FROM_INVENTORY):
                text, markup = await self.inventory(user_id)
                return kind.consumed_text or "Уже нет.", markup or _NO_BUTTONS, text
            await self._forget_buttons(chat_id, message_id)
            return (
                kind.consumed_text or "Уже нет.",
                _NO_BUTTONS,
                item_caption(kind, item, False) if kind.consumed_text else None,
            )
        if button == ITEM_BTN_CARD and kind is not None:
            shown = await self._show_card(chat_id, user_id, item, message_thread_id)
            return (radio.ITEM_CARD_SHOWN if shown else radio.ITEM_CARD_DRAWING), None, None
        if kind is None or not kind.actions:
            return "Неизвестная кнопка.", None, None
        origin = ""
        if len(button) > 1 and button[0] in (ITEM_FROM_INVENTORY, ITEM_FROM_FORM):
            origin, button = button[0], button[1:]
        place = await self.item_place(user_id, item)
        if button == ITEM_BTN_ACTIONS and origin != ITEM_FROM_FORM:
            if message_id is not None:
                await self._remember_buttons(
                    chat_id,
                    message_id,
                    user_id,
                    LIVE_MENU,
                    item=item_id,
                    photo=photo,
                    inv=origin == ITEM_FROM_INVENTORY,
                )
            return (
                "",
                item_actions_keyboard(kind, item_id, place, origin),
                item_actions_caption(kind, place),
            )
        if button == ITEM_BTN_BACK and origin != ITEM_FROM_FORM:
            await self._forget_buttons(chat_id, message_id)
            return ("", *await self._item_view(user_id, kind, item, origin, place))
        if origin == ITEM_FROM_FORM and button.startswith(ITEM_BTN_KEEP):
            kept = kind.action(button[len(ITEM_BTN_KEEP) :])
            await self._forget_buttons(chat_id, message_id)
            text = radio.ITEM_FORM_KEPT.format(form=kept.form) if kept is not None else None
            return "Оставили как есть.", _NO_BUTTONS, text
        action = kind.action(button)
        if action is None:
            return "Неизвестная кнопка.", None, None
        if action.pinned_locked and self._pinned(chat_id):
            return radio.ITEM_PINNED, None, None
        await self._forget_buttons(chat_id, message_id)
        answer = "Уже так."
        if action in kind.available(place):
            await getattr(self, self._ITEM_EFFECTS[action.effect])(chat_id, user_id, item, action)
            if action.event:
                await self._store.add_item_event(
                    item_id,
                    action.event,
                    user_id=user_id,
                    chat_id=chat_id,
                    data=None,
                    now=self._now(),
                )
            if action.consumable:
                await self._store.consume_item(item_id)
                item["status"] = "consumed"
            if action.directive:
                where = await self._where(chat_id, message_id, message_thread_id)
                await self._speak(chat_id, action.directive, where)
            if action.snapshot:
                await self._action_selfie(chat_id, user_id, action, message_thread_id)
            answer = action.done
            place = await self.item_place(user_id, item)
        if origin == ITEM_FROM_FORM:
            return (
                answer,
                _NO_BUTTONS,
                radio.ITEM_FORM_DONE.format(form=action.form, confirm=action.confirm),
            )
        if item.get("status") == "consumed" and origin != ITEM_FROM_INVENTORY:
            return answer, _NO_BUTTONS, item_caption(kind, item, False)
        return (answer, *await self._item_view(user_id, kind, item, origin, place))

    async def _action_selfie(
        self,
        chat_id: int,
        user_id: int,
        action: items_mod.ItemAction,
        message_thread_id: int | None,
    ) -> None:
        started = await self._start_photo(
            chat_id,
            user_id,
            focus=action.snapshot,
            caption=action.snapshot_caption or cabinet_mod.SELFIE_CAPTION,
            happening=None,
            outside=await self._transylvania.outside(self._now()),
            message_thread_id=message_thread_id,
            trigger_message_id=None,
            selfie=True,
        )
        if not started:
            log.info("interactives: снимок по действию %s не начат (chat=%s)", action.name, chat_id)

    async def _item_view(
        self,
        user_id: int,
        kind: items_mod.ItemKind,
        item: dict[str, Any],
        origin: str,
        place: str,
    ) -> tuple[InlineKeyboardMarkup, str]:
        if origin == ITEM_FROM_INVENTORY:
            text, markup = await self.inventory(user_id)
            return markup or _NO_BUTTONS, text
        card = item_keyboard(kind, int(item["id"])) or _NO_BUTTONS
        return card, item_caption(kind, item, place == items_mod.PLACE_DESK)

    _ITEM_EFFECTS = {"radio_desk": "_effect_radio_desk", "user_effect": "_effect_user_effect"}

    async def _effect_user_effect(
        self, chat_id: int, user_id: int, item: dict[str, Any], action: items_mod.ItemAction
    ) -> None:
        if action.arg.startswith("-"):
            await self._state.clear_effect(action.arg[1:], user_id)
            return
        effect = items_mod.EFFECTS[action.arg]
        expires = self._now() + timedelta(hours=effect.hours) if effect.hours is not None else None
        await self._state.set_timed_effect(effect.key, user_id, expires)

    async def _effect_radio_desk(
        self, chat_id: int, user_id: int, item: dict[str, Any], action: items_mod.ItemAction
    ) -> None:
        await self._set_clear(chat_id, user_id, action.arg == "remove")
