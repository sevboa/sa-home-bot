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
from sa_home_bot.bot.interactives import cabinet as cabinet_mod
from sa_home_bot.bot.interactives import places
from sa_home_bot.bot.interactives.core import EngineCore
from sa_home_bot.bot.interactives.director import json_object
from sa_home_bot.bot.interactives.llm_link import LLM_CALL_ERRORS, ask_as_director
from sa_home_bot.bot.interactives.photo_text import (
    ALFRED_PHOTO_PREFIX,
    MOOD_PRESETS,
    photo_negative,
    selfie_description,
)
from sa_home_bot.proto.addresses import LLM_ADDRESS

log = logging.getLogger(__name__)


class MoodSelfieMixin(EngineCore):
    async def _mood_selfie_gated(self, chat_id: int, reply: str) -> bool:
        if not self._can_send_photos or self._get_node_link() is None:
            return False
        if chat_id in self._photo_busy or chat_id in self._photo_jobs:
            return False
        if not reply.strip() or len(reply) > cabinet_mod.PHOTO_LINE_MAX:
            return False
        if len(ALFRED_PHOTO_PREFIX + html.escape(reply)) > cabinet_mod.PHOTO_CAPTION_MAX:
            return False
        now = self._now()
        purpose = cabinet_mod.SELFIE_MOOD_PURPOSE
        gap = timedelta(hours=cabinet_mod.SELFIE_MOOD_GAP_H)
        if await self._store.count_images_since(chat_id, now - gap, purpose):
            return False
        day = await self._store.count_images_since(chat_id, now - timedelta(days=1), purpose)
        return day < cabinet_mod.SELFIE_MOOD_DAILY

    async def _classify_mood(
        self, chat_id: int, user_text: str, reply: str
    ) -> tuple[str, int, str] | None:
        node_link = self._get_node_link()
        if node_link is None:
            return None
        raw = await ask_as_director(
            node_link,
            self._settings.llm.request_timeout_s,
            chat_id=chat_id,
            system=cabinet_mod.SELFIE_MOOD_SYSTEM,
            content=cabinet_mod.SELFIE_MOOD_INPUT.format(
                user=user_text.strip()[:600], reply=reply.strip()[:900]
            ),
            failure="оценка эмоции не вышла",
            reason="off",
        )
        data = json_object(raw) if raw is not None else None
        if data is None:
            return None
        emotion = data.get("emotion")
        intensity = data.get("intensity")
        if emotion not in cabinet_mod.SELFIE_EMOTIONS or isinstance(intensity, bool):
            return None
        try:
            level = int(intensity)
        except (TypeError, ValueError):
            return None
        action = data.get("action") if isinstance(data.get("action"), str) else ""
        action = " ".join(action.split())[: cabinet_mod.SELFIE_ACTION_MAX].rstrip(" .")
        return str(emotion), level, action

    async def mood_selfie(
        self,
        chat_id: int,
        user_id: int | None,
        user_text: str,
        reply: str,
        *,
        message_thread_id: int | None = None,
        trigger_message_id: int | None = None,
        on_status: Callable[[str], Awaitable[None]] | None = None,
    ) -> int | None:
        if user_id is None:
            return None
        try:
            if not await self._mood_selfie_gated(chat_id, reply):
                return None
            mood = await self._classify_mood(chat_id, user_text, reply)
            if mood is None:
                return None
            emotion, intensity, action = mood
            chance = cabinet_mod.SELFIE_MOOD_CHANCE.get(min(intensity, 3), 0.0)
            if intensity <= 1 or self._rng() >= chance:
                return None
            self._photo_busy.add(chat_id)
            try:
                return await self._mood_selfie_shot(
                    chat_id,
                    user_id,
                    reply,
                    emotion,
                    intensity,
                    action,
                    message_thread_id=message_thread_id,
                    trigger_message_id=trigger_message_id,
                    on_status=on_status,
                )
            finally:
                self._photo_busy.discard(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("interactives: селфи к ответу не удалось (chat=%s)", chat_id)
            return None

    async def _mood_selfie_shot(
        self,
        chat_id: int,
        user_id: int,
        reply: str,
        emotion: str,
        intensity: int,
        action: str,
        *,
        message_thread_id: int | None,
        trigger_message_id: int | None,
        on_status: Callable[[str], Awaitable[None]] | None,
    ) -> int | None:
        node_link = self._get_node_link()
        if node_link is None:
            return None
        dst = LLM_ADDRESS
        cfg = self._settings.llm
        now = self._now()
        outside = await self._transylvania.outside(now)
        if on_status is not None:
            self._photo_status[chat_id] = on_status
        try:
            await self._show_photo_status(chat_id, cabinet_mod.PHOTO_STATUS_AIMING)
            cab = await self._room(chat_id, user_id)
            await self._ensure_first_features(cab, chat_id, outside)
            run = await self._scene_run(chat_id, user_id)
            happening = run.last_effect if run is not None else None
            mood = run.mood if run is not None else None
            face = cabinet_mod.SELFIE_EMOTION_FACE_EN[emotion]
            if action and self.canary_ok(user_id) and places.foreign_place(action, cab.place):
                action = ""
            focus = "; ".join(p for p in (face, action) if p)
            description = selfie_description(cab, outside, focus, happening)
            request: dict[str, Any] = {
                "description": description,
                "mode": "free",
                "context": "",
                "chat_id": chat_id,
                "negative_extra": photo_negative(cab),
                "emphasize": [e for e in (face or action,) if e],
            }
            px = await self.photo_size(user_id)
            if px:
                request["size"] = px
            loras: list[list[Any]] = [list(image_tools.ALFRED_LORA)]
            preset = MOOD_PRESETS.get(mood or "")
            if preset is not None:
                model, lora, weight = preset
                request["model"] = model
                loras.append([lora, weight])
            request["loras"] = loras
            request["light"] = cab.light_en(outside)
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
                log.warning("interactives: селфи к ответу не нарисовано (%s): %s", chat_id, exc)
                return None
            finally:
                phases.cancel()
            png, image_id = await image_tools.record_generated(
                self._store,
                result,
                chat_id=chat_id,
                author=None,
                prompt_ru=f"{cabinet_mod.SELFIE_MOOD_CAPTION}: {emotion}",
                fallback_prompt_en=description,
                caption=cabinet_mod.SELFIE_MOOD_CAPTION,
                colors=cfg.imagegen_colors,
                now=now,
                purpose=cabinet_mod.SELFIE_MOOD_PURPOSE,
                params=json.dumps(
                    {
                        "location": cab.place,
                        "user_id": user_id,
                        "selfie": True,
                        "emotion": emotion,
                        "intensity": intensity,
                        "action": action,
                        "mood": mood,
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
                caption=ALFRED_PHOTO_PREFIX + html.escape(reply),
                message_thread_id=message_thread_id,
                reply_to_message_id=trigger_message_id,
            )
            if sent is None:
                log.warning("interactives: селфи #%s не ушло в чат %s", image_id, chat_id)
                return None
            return int(sent[0])
        finally:
            if self._photo_status.get(chat_id) is on_status:
                self._photo_status.pop(chat_id, None)
