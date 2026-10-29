from __future__ import annotations

import asyncio
import html
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sa_home_bot.bot import image_tools
from sa_home_bot.bot.alfred_voice import ALFRED_PREFIX_HTML
from sa_home_bot.bot.interactives import cabinet as cabinet_mod
from sa_home_bot.bot.interactives import frame_heroes, places, radio, underground
from sa_home_bot.bot.interactives import items as items_mod
from sa_home_bot.bot.interactives.cabinet import Cabinet
from sa_home_bot.bot.interactives.core import EngineCore
from sa_home_bot.bot.interactives.llm_link import LLM_CALL_ERRORS, ask_chat
from sa_home_bot.bot.interactives.photo_text import (
    MOOD_PRESETS,
    PHOTO_PURPOSE,
    PORTRAIT_PURPOSE,
    WINDOW_VIEW_RE,
    _image_params,
    is_general_view,
    photo_description,
    photo_expect,
    photo_line_directive,
    photo_negative,
    photo_state_key,
    photo_subject,
    portrait_description,
    retake_emphasis,
    selfie_description,
)
from sa_home_bot.bot.interactives.places import Room
from sa_home_bot.bot.interactives.transylvania import PHASE_NIGHT, Outside
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.llm.prompt import wrap_system_directive
from sa_home_bot.proto.addresses import LLM_ADDRESS
from sa_home_bot.proto.messages import Address, ProtoError

log = logging.getLogger(__name__)

PHOTO_DAILY_LIMIT = 0


ACTION_CHAT_PROGRESS = "chat_progress"


PHOTO_PHASE_POLL_S = 1.0


PHOTO_PHASE_STATUS = {
    "compose": cabinet_mod.PHOTO_STATUS_AIMING,
    "draw": cabinet_mod.PHOTO_STATUS_DEVELOPING,
}


class PhotosMixin(EngineCore):
    async def tool_take_photo(
        self,
        chat_id: int | None,
        user_id: int | None,
        args: dict[str, Any],
        *,
        message_thread_id: int | None = None,
        trigger_message_id: int | None = None,
        dialogue_id: int | None = None,
    ) -> str:
        if chat_id is None or user_id is None or not self._can_send_photos:
            return cabinet_mod.TOOL_PHOTO_UNAVAILABLE
        focus = args.get("focus") if isinstance(args.get("focus"), str) else ""
        focus = " ".join(focus.split())
        caption = args.get("caption") if isinstance(args.get("caption"), str) else ""
        caption = " ".join(caption.split())[:80]
        selfie = args.get("selfie") is True or str(args.get("selfie")).lower() == "true"
        if self.canary_ok(user_id):
            refusal = await self._photo_elsewhere(chat_id, user_id, f"{focus} {caption}")
            if refusal is not None:
                return refusal
        if chat_id in self._photo_busy:
            return await self._join_photo(chat_id, user_id, trigger_message_id, dialogue_id)
        if cabinet_mod.SELFIE_FOCUS_RE.match(focus):
            selfie, focus = True, ""
        elif not focus and cabinet_mod.SELFIE_FOCUS_RE.match(caption):
            selfie = True
        cab = await self._room(chat_id, user_id)
        if is_general_view(focus, cab) or (focus and cab.is_general(focus)):
            focus = ""
        elif not focus and caption and not selfie and not is_general_view(caption, cab):
            focus = caption
        caption = caption or (cabinet_mod.SELFIE_CAPTION if selfie else cab.caption)
        under = await self.underground_photo(
            chat_id,
            user_id,
            cab,
            focus,
            selfie=selfie,
            message_thread_id=message_thread_id,
            trigger_message_id=trigger_message_id,
        )
        if under is not None:
            return under
        if (
            not selfie
            and cab.is_study
            and await self._wants_stored_item(user_id, f"{focus} {caption}")
        ):
            await self.tool_manor_items(
                chat_id,
                user_id,
                show=items_mod.RADIO.type,
                message_thread_id=message_thread_id,
                trigger_message_id=trigger_message_id,
            )
            return radio.TOOL_PHOTO_STORED_ITEM
        expect = photo_expect(args.get("expect")) or ([focus] if focus else [])
        now = self._now()
        outside = await self._transylvania.outside(now)
        run = await self._scene_run(chat_id, user_id)
        in_scene = run is not None
        if not in_scene and cab.end_scene():
            await self._save_room(cab)
        happening = run.last_effect if run is not None else None
        mood = run.mood if run is not None else None
        reuse_key = None
        if cab.features and cab.is_study:
            shot = await self._item_shot(chat_id, user_id, "", draw=False)
            reuse_key = photo_state_key(cab, outside, shot, await self.photo_size(user_id))
        if not selfie and not focus and not happening and not expect and reuse_key in cab.photos:
            image = await self._store.image_by_id(cab.photos[reuse_key])
            if image is not None and image["telegram_file_id"] and image["chat_id"] != chat_id:
                sent = await self._notifier.send_photo_ex(
                    chat_id,
                    image["telegram_file_id"],
                    caption=caption,
                    message_thread_id=message_thread_id,
                    reply_to_message_id=trigger_message_id,
                )
                if sent is not None:
                    seen = _image_params(image).get("seen")
                    where = await self._where_ru(cab, user_id, in_scene=in_scene)
                    result = cabinet_mod.TOOL_PHOTO_SENT.format(where=where, now=outside.ru())
                    if isinstance(seen, str) and seen:
                        await self._save_last_photo(chat_id, int(image["id"]), seen, [], [])
                        result += cabinet_mod.TOOL_PHOTO_SENT_SEEN.format(description=seen)
                    return result
        if await self._photo_limit_reached(chat_id):
            return cabinet_mod.TOOL_PHOTO_LIMIT
        started = await self._start_photo(
            chat_id,
            user_id,
            focus=focus,
            caption=caption,
            happening=happening,
            mood=mood,
            outside=outside,
            message_thread_id=message_thread_id,
            trigger_message_id=trigger_message_id,
            expect=expect,
            dialogue_id=dialogue_id,
            describe=await self._where_ru(cab, user_id, in_scene=in_scene, tools=False),
            selfie=selfie,
        )
        if not started:
            return cabinet_mod.TOOL_PHOTO_UNAVAILABLE
        return cabinet_mod.TOOL_PHOTO_STARTED

    async def _photo_elsewhere(self, chat_id: int, user_id: int, text: str) -> str | None:
        cab = await self._room(chat_id, user_id)
        here = cab.in_ru
        name = places.foreign_place(text, cab.place)
        if name is not None:
            return places.TOOL_PHOTO_ELSEWHERE.format(name=name, here=here)
        if not cab.windowed and WINDOW_VIEW_RE.search(text):
            return places.TOOL_PHOTO_NO_WINDOW.format(here=here)
        return None

    async def _join_photo(
        self,
        chat_id: int,
        user_id: int,
        trigger_message_id: int | None,
        dialogue_id: int | None,
    ) -> str:
        task = self._photo_jobs.get(chat_id)
        if (
            task is None
            or task.done()
            or chat_id in self._photo_described
            or chat_id in self._photo_join
        ):
            return cabinet_mod.TOOL_PHOTO_BUSY
        cab = await self._room(chat_id, user_id)
        in_scene = await self._scene_run(chat_id, user_id) is not None
        self._photo_join[chat_id] = {
            "describe": await self._where_ru(cab, user_id, in_scene=in_scene, tools=False),
            "dialogue_id": dialogue_id,
            "trigger_message_id": trigger_message_id,
        }
        return cabinet_mod.TOOL_PHOTO_STARTED

    async def _wants_stored_item(self, user_id: int, text: str) -> bool:
        kind = items_mod.RADIO
        if kind.stored_re is None or not kind.focus_re.search(text):
            return False
        if not kind.stored_re.search(text) or not await self.speech_clear(user_id):
            return False
        return bool(await self._store.items_of(user_id, kind.type))

    async def _where_ru(
        self, cab: Cabinet, user_id: int, *, in_scene: bool, tools: bool = True
    ) -> str:
        where = cab.describe_ru()
        if isinstance(cab, Room):
            return where + await self._room_extra(cab, user_id)
        if in_scene or not await self._state.is_completed(radio.SCENARIO_ID, user_id):
            return where
        if await self.speech_clear(user_id):
            hint = radio.RADIO_STATE_NEW_TOOL_HINT if tools else ""
            return f"{where} {radio.RADIO_STATE_NEW}{hint}"
        owned = await self._store.items_of(user_id, items_mod.RADIO_TYPE)
        traits = items_mod.RADIO.traits_ru(owned[0]["traits"] or []) if owned else []
        return f"{where} " + radio.RADIO_STATE_OLD.format(
            traits=radio.RADIO_STATE_TRAITS.format(traits="; ".join(traits)) if traits else ""
        )

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
        mood: str | None = None,
        alfred: str | None = None,
        outside: Outside,
        message_thread_id: int | None,
        trigger_message_id: int | None,
        expect: list[str] | None = None,
        dialogue_id: int | None = None,
        describe: str | None = None,
        selfie: bool = False,
    ) -> bool:
        if chat_id in self._photo_busy or not self._can_send_photos:
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
                mood=mood,
                alfred=alfred,
                outside=outside,
                message_thread_id=message_thread_id,
                trigger_message_id=trigger_message_id,
                expect=expect,
                dialogue_id=dialogue_id,
                describe=describe,
                selfie=selfie,
            )
        )
        self._track(task)
        self._photo_jobs[chat_id] = task
        task.add_done_callback(lambda _t: self._photo_jobs.pop(chat_id, None))
        if describe is not None:
            self._photo_described.add(chat_id)
        return True

    async def wait_photo(
        self, chat_id: int, on_status: Callable[[str], Awaitable[None]] | None = None
    ) -> None:
        task = self._photo_jobs.get(chat_id) or self._card_jobs.get(chat_id)
        if task is None or task.done():
            return
        if on_status is not None:
            self._photo_status[chat_id] = on_status
        cfg = self._settings.llm
        try:
            await asyncio.wait(
                {task}, timeout=cfg.imagegen_request_timeout_s + cfg.request_timeout_s + 60
            )
        finally:
            if self._photo_status.get(chat_id) is on_status:
                self._photo_status.pop(chat_id, None)

    async def _show_photo_status(self, chat_id: int, text: str) -> None:
        on_status = self._photo_status.get(chat_id)
        if on_status is None:
            return
        try:
            await on_status(text)
        except Exception:
            log.debug("interactives: статус снимка не показан (chat=%s)", chat_id, exc_info=True)

    async def _follow_photo_phases(
        self, node_link: ServiceLink, dst: Address, chat_id: int, request_id: str
    ) -> None:
        shown = None
        while True:
            await asyncio.sleep(PHOTO_PHASE_POLL_S)
            try:
                state = await node_link.command(
                    ACTION_CHAT_PROGRESS, {"request_id": request_id}, dst=dst, timeout=10
                )
            except LLM_CALL_ERRORS:
                continue
            text = PHOTO_PHASE_STATUS.get(str(state.get("partial") or ""))
            if text is not None and text != shown:
                shown = text
                await self._show_photo_status(chat_id, text)
            if state.get("done"):
                return

    async def _photo_job(
        self,
        chat_id: int,
        user_id: int,
        *,
        focus: str,
        caption: str,
        happening: str | None,
        mood: str | None = None,
        alfred: str | None = None,
        outside: Outside,
        message_thread_id: int | None,
        trigger_message_id: int | None,
        expect: list[str] | None = None,
        dialogue_id: int | None = None,
        describe: str | None = None,
        selfie: bool = False,
    ) -> None:
        delivered = False
        if describe is not None:
            await self._mark_photo_pending(chat_id, message_thread_id, trigger_message_id)
        try:
            delivered = await self._photo(
                chat_id,
                user_id,
                focus=focus,
                caption=caption,
                happening=happening,
                mood=mood,
                alfred=alfred,
                outside=outside,
                message_thread_id=message_thread_id,
                trigger_message_id=trigger_message_id,
                expect=expect,
                dialogue_id=dialogue_id,
                describe=describe,
                selfie=selfie,
            )
        except Exception:
            log.exception("interactives: снимок кабинета не удался (chat=%s)", chat_id)
        finally:
            self._photo_busy.discard(chat_id)
            self._photo_described.discard(chat_id)
            join = self._photo_join.pop(chat_id, None)
            if not delivered and (describe is not None or join is not None):
                reply_to = (join or {}).get("trigger_message_id") or trigger_message_id
                await self._photo_lost(chat_id, message_thread_id, reply_to)
            await self._clear_photo_pending(chat_id)

    def start_greeting_portrait(
        self,
        chat_id: int,
        user_id: int,
        *,
        message_thread_id: int | None = None,
        trigger_message_id: int | None = None,
    ) -> bool:
        if chat_id in self._portrait_busy or not self._can_send_photos:
            return False
        if self._get_node_link() is None:
            return False
        self._portrait_busy.add(chat_id)
        task = asyncio.create_task(
            self._greeting_portrait(chat_id, user_id, message_thread_id, trigger_message_id)
        )
        self._track(task)
        task.add_done_callback(lambda _t: self._portrait_busy.discard(chat_id))
        return True

    async def _greeting_portrait(
        self,
        chat_id: int,
        user_id: int,
        message_thread_id: int | None,
        trigger_message_id: int | None,
    ) -> None:
        node_link = self._get_node_link()
        if node_link is None:
            return
        dst = LLM_ADDRESS
        cfg = self._settings.llm
        try:
            now = self._now()
            outside = await self._transylvania.outside(now)
            cab = await cabinet_mod.load(self._store, user_id)
            await self._ensure_first_features(cab, chat_id, outside)
            description = portrait_description(cab, outside)
            portrait_request: dict[str, Any] = {
                "description": description,
                "mode": "free",
                "chat_id": chat_id,
                "negative_extra": cabinet_mod.PHOTO_NEGATIVE_EN,
                "loras": [list(image_tools.ALFRED_LORA)],
                "light": outside.en(),
            }
            px = await self.photo_size(user_id)
            if px:
                portrait_request["size"] = px
            result = await node_link.command(
                image_tools.ACTION_GENERATE_IMAGE,
                portrait_request,
                dst=dst,
                timeout=cfg.imagegen_request_timeout_s,
            )
            png, image_id = await image_tools.record_generated(
                self._store,
                result,
                chat_id=chat_id,
                author=None,
                prompt_ru=f"{cabinet_mod.PORTRAIT_CAPTION}: {outside.ru()}",
                fallback_prompt_en=description,
                caption=cabinet_mod.PORTRAIT_CAPTION,
                colors=cfg.imagegen_colors,
                now=now,
                purpose=PORTRAIT_PURPOSE,
                params=json.dumps(
                    {
                        "location": cabinet_mod.LOCATION,
                        "user_id": user_id,
                        "state": photo_state_key(cab, outside, None),
                        "seed": result.get("seed"),
                    },
                    ensure_ascii=False,
                ),
            )
            sent = await image_tools.deliver_generated(
                self._notifier,
                self._store,
                image_id,
                chat_id,
                image_tools.upscale_png(png, cfg.imagegen_display_px),
                message_thread_id=message_thread_id,
                reply_to_message_id=trigger_message_id,
            )
        except LLM_CALL_ERRORS as exc:
            log.warning(
                "interactives: портрет к приветствию не нарисован (chat=%s): %s", chat_id, exc
            )
            return
        except Exception:
            log.exception("interactives: портрет к приветствию не удался (chat=%s)", chat_id)
            return
        if sent is None:
            log.warning("interactives: портрет #%s не ушёл в чат %s", image_id, chat_id)

    async def _photo_lost(
        self, chat_id: int, message_thread_id: int | None, reply_to: int | None
    ) -> None:
        log.warning("interactives: обещанный снимок не дошёл (chat=%s)", chat_id)
        try:
            await self._notifier.send_direct(
                chat_id,
                self._choose(cabinet_mod.PHOTO_LOST_TEXTS),
                reply_to_message_id=reply_to,
                message_thread_id=message_thread_id,
            )
        except Exception:
            log.exception("interactives: не сказал о пропавшем снимке (chat=%s)", chat_id)

    async def _photo_pending(self) -> dict[str, Any]:
        raw = await self._store.get_state(cabinet_mod.PHOTO_PENDING_KEY)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    async def _mark_photo_pending(
        self, chat_id: int, message_thread_id: int | None, reply_to: int | None
    ) -> None:
        pending = await self._photo_pending()
        pending[str(chat_id)] = {"thread": message_thread_id, "reply_to": reply_to}
        await self._store.set_state(cabinet_mod.PHOTO_PENDING_KEY, json.dumps(pending))

    async def _clear_photo_pending(self, chat_id: int) -> None:
        pending = await self._photo_pending()
        if pending.pop(str(chat_id), None) is not None:
            await self._store.set_state(cabinet_mod.PHOTO_PENDING_KEY, json.dumps(pending))

    async def recover(self) -> None:
        pending = await self._photo_pending()
        if not pending:
            return
        await self._store.set_state(cabinet_mod.PHOTO_PENDING_KEY, "{}")
        for chat, where in pending.items():
            where = where if isinstance(where, dict) else {}
            await self._photo_lost(int(chat), where.get("thread"), where.get("reply_to"))

    async def _photo(
        self,
        chat_id: int,
        user_id: int,
        *,
        focus: str,
        caption: str,
        happening: str | None,
        mood: str | None = None,
        alfred: str | None = None,
        outside: Outside,
        message_thread_id: int | None,
        trigger_message_id: int | None,
        expect: list[str] | None = None,
        dialogue_id: int | None = None,
        describe: str | None = None,
        selfie: bool = False,
        frame: underground.Frame | None = None,
    ) -> bool:
        node_link = self._get_node_link()
        if node_link is None:
            return False
        dst = LLM_ADDRESS
        cfg = self._settings.llm
        cab = frame.room if frame is not None else await self._room(chat_id, user_id)
        if is_general_view(focus, cab):
            focus = ""
        await self._ensure_first_features(cab, chat_id, outside)
        shot = (
            None
            if selfie or not cab.is_study
            else await self._item_shot(chat_id, user_id, focus, draw=True)
        )
        hero_frame = frame is not None and bool(frame.request_en) and not focus and not selfie
        if selfie:
            description, context = selfie_description(cab, outside, focus, happening), ""
        elif hero_frame:
            description, context = frame.request_en, ""
        else:
            description, context = photo_description(
                cab,
                outside,
                focus,
                happening,
                item=shot.kind if shot else None,
                item_place=shot.place if shot else None,
                alfred=alfred,
                state=None if frame is not None else await self._scene_facts(chat_id, user_id),
            )
        request: dict[str, Any] = {
            "description": description,
            "mode": "scene" if focus and not selfie else "free",
            "context": context,
            "chat_id": chat_id,
            "keep_key": f"snap-{chat_id}-{uuid.uuid4().hex[:12]}",
            "expect": list(expect or []),
            "negative_extra": photo_negative(cab),
        }
        if frame is not None:
            del request["keep_key"]
        px = await self.photo_size(user_id)
        if px:
            request["size"] = px
        emphasize = retake_emphasis(await self._last_photo(chat_id), list(expect or []))
        if shot is not None:
            emphasize = [e for e in emphasize if not shot.kind.focus_re.search(e)]
        if emphasize:
            request["emphasize"] = emphasize
            log.info("interactives: пересъёмка, упор на %s (chat=%s)", emphasize, chat_id)
        loras: list[list[Any]] = [list(image_tools.ALFRED_LORA)] if selfie else []
        preset = MOOD_PRESETS.get(mood or "")
        if preset is not None:
            model, lora, weight = preset
            request["model"] = model
            loras.append([lora, weight])
        if alfred in cabinet_mod.SCENE_ALFRED_EN and not selfie:
            loras.append(list(image_tools.ALFRED_LORA))
        if loras:
            request["loras"] = loras
        window = bool(WINDOW_VIEW_RE.search(focus))
        closeup = (
            not selfie
            and not window
            and bool(focus or (shot is not None and shot.place == "closeup"))
        )
        request["light"] = cab.light_en(outside, closeup=closeup)
        if hero_frame:
            request["light"] = ""
            request["hero_weight"] = frame_heroes.HERO_WEIGHT
            request["place_weight"] = frame_heroes.PLACE_WEIGHT
            request["negative_extra"] += f", {frame_heroes.FRAME_NEGATIVE_EN}"
        if cab.windowed and outside.light == PHASE_NIGHT and not closeup:
            request["light_move"] = True
        if shot is not None:
            request["paste"] = {"key": shot.key, "place": shot.place, "hint": shot.kind.paste_hint}
        request["request_id"] = uuid.uuid4().hex
        phases = asyncio.create_task(
            self._follow_photo_phases(node_link, dst, chat_id, request["request_id"])
        )
        try:
            result = await node_link.command(
                image_tools.ACTION_GENERATE_IMAGE,
                request,
                dst=dst,
                timeout=cfg.imagegen_request_timeout_s,
            )
        except LLM_CALL_ERRORS as exc:
            log.warning("interactives: снимок не нарисован (chat=%s): %s", chat_id, exc)
            if shot is not None and isinstance(exc, ProtoError):
                await self._forget_item_cut(chat_id, user_id, shot)
            return False
        finally:
            phases.cancel()
        inspection = result.get("inspect") if isinstance(result.get("inspect"), dict) else None
        seen = str(inspection.get("description") or "") if inspection else ""
        missing = [m for m in (inspection or {}).get("missing") or [] if isinstance(m, str)]
        state = photo_state_key(cab, outside, shot, px)
        png, image_id = await image_tools.record_generated(
            self._store,
            result,
            chat_id=chat_id,
            author=None,
            prompt_ru=f"{caption}: {focus or cab.describe_ru()}",
            fallback_prompt_en=description,
            caption=caption,
            colors=cfg.imagegen_colors,
            now=self._now(),
            purpose=PHOTO_PURPOSE,
            params=json.dumps(
                {
                    "location": cab.place,
                    "user_id": user_id,
                    "state": state,
                    "focus": focus,
                    "selfie": selfie,
                    "mood": mood,
                    "alfred": alfred,
                    "seed": result.get("seed"),
                    "expect": list(expect or []),
                    "seen": seen,
                    "missing": missing,
                },
                ensure_ascii=False,
            ),
        )
        misses = 0
        if seen:
            misses = await self._save_last_photo(
                chat_id, image_id, seen, list(expect or []), missing
            )
        join = self._photo_join.pop(chat_id, None) if describe is None else None
        if join is not None:
            describe = join["describe"]
            dialogue_id = join["dialogue_id"]
            trigger_message_id = join["trigger_message_id"] or trigger_message_id
        line = ""
        if describe is not None:
            await self._show_photo_status(chat_id, cabinet_mod.PHOTO_STATUS_DEVELOPING)
            line = await self._photo_line(
                chat_id,
                user_id,
                photo_line_directive(
                    cab.localize_ru(photo_subject(focus, selfie=selfie)) or cab.subject_ru,
                    seen or focus or cab.describe_ru(),
                    describe,
                    missing if seen else [],
                    misses,
                ),
            )
        sent = await image_tools.deliver_generated(
            self._notifier,
            self._store,
            image_id,
            chat_id,
            image_tools.upscale_png(png, cfg.imagegen_display_px),
            caption=ALFRED_PREFIX_HTML + html.escape(line) if line else caption,
            message_thread_id=message_thread_id,
            reply_to_message_id=trigger_message_id,
        )
        if sent is None:
            log.warning("interactives: снимок #%s не ушёл в чат %s", image_id, chat_id)
            return False
        if frame is not None:
            await self.remember_frame(user_id, frame, image_id, sent[1])
        if line and dialogue_id is not None:
            await self._store.record_ai_turn(
                chat_id, sent[0], dialogue_id, "assistant", line, self._now()
            )
        if seen and missing:
            if describe is None:
                await self._react_to_miss(
                    chat_id,
                    seen,
                    missing,
                    misses,
                    photo_message_id=sent[0],
                    message_thread_id=message_thread_id,
                    trigger_message_id=trigger_message_id,
                    dialogue_id=dialogue_id,
                )
            return True
        if not focus and not happening and not selfie and not alfred and cab.is_study:
            cab = await cabinet_mod.load(self._store, user_id)
            cab.remember_photo(photo_state_key(cab, outside, shot, px), image_id)
            await cabinet_mod.save(self._store, cab)
        return True

    async def _photo_line(self, chat_id: int, user_id: int, directive: str) -> str:
        node_link = self._get_node_link()
        if node_link is None:
            return ""
        args: dict[str, Any] = {
            "messages": [wrap_system_directive(directive)],
            "tools": [],
            "reason": "off",
            "chat_id": chat_id,
            "user_id": user_id,
            "speech_clear": await self.speech_clear(user_id),
        }
        raw = await ask_chat(
            node_link,
            self._settings.llm.request_timeout_s,
            args,
            failure="подпись к снимку не вышла",
        )
        line = (raw or "").strip()
        return line[: cabinet_mod.PHOTO_LINE_MAX]
