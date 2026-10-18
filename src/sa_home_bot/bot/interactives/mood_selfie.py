from __future__ import annotations

import asyncio
import base64
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
from sa_home_bot.bot.interactives.director import (
    ACTION_CHAT,
    ROLE_DIRECTOR,
    _json_object,
    ask_features,
)
from sa_home_bot.bot.interactives.photo_text import (
    ALFRED_PHOTO_PREFIX,
    MOOD_PRESETS,
    photo_negative,
    selfie_description,
)
from sa_home_bot.bot.interactives.places import Room
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.proto.addresses import LLM_ADDRESS
from sa_home_bot.proto.messages import ProtoError

log = logging.getLogger(__name__)


class MoodSelfieMixin(EngineCore):
    async def _mood_selfie_gated(self, chat_id: int, reply: str) -> bool:
        """Дешёвые гейты без GPU: связь и отправка фото есть, реплика влезает
        в подпись, в чате не чаще раза в SELFIE_MOOD_GAP_H часов и
        SELFIE_MOOD_DAILY в сутки. True — можно спрашивать классификатор."""
        if not hasattr(self._notifier, "send_photo_ex") or self._get_node_link() is None:
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
        """(эмоция, интенсивность 0..3, action) или None — сбой/мусор."""
        node_link = self._get_node_link()
        if node_link is None:
            return None
        args: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "content": cabinet_mod.SELFIE_MOOD_INPUT.format(
                        user=user_text.strip()[:600], reply=reply.strip()[:900]
                    ),
                }
            ],
            "role": ROLE_DIRECTOR,
            "system": cabinet_mod.SELFIE_MOOD_SYSTEM,
            "reason": "off",
            "chat_id": chat_id,
        }
        try:
            result = await node_link.command(
                ACTION_CHAT,
                args,
                dst=LLM_ADDRESS,
                timeout=self._settings.llm.request_timeout_s,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
            log.warning("interactives: оценка эмоции не вышла (chat=%s): %s", chat_id, exc)
            return None
        data = _json_object(result.get("response", "") if isinstance(result, dict) else "")
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
        """Спонтанное селфи к ответу /ai: реплика Альфреда (``reply``) уходит
        подписью к его фото одним сообщением. Возвращает message_id фото или
        None — селфи нет (гейты, «none», не выпал бросок, сбой) и ответ надо
        слать обычным текстом. Никогда не бросает: ответ не должен теряться."""
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
            if not cab.features and not isinstance(cab, Room):
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
            run = await self._scene_run(chat_id, user_id)
            happening = run.last_effect if run is not None else None
            mood = run.mood if run is not None else None
            face = cabinet_mod.SELFIE_EMOTION_FACE_EN[emotion]
            if action and self.canary_ok(user_id) and places.foreign_place(action, cab.place):
                action = ""  # селфи только здесь: чужое место в позе не рисуем (59.7)
            focus = "; ".join(p for p in (face, action) if p)
            description = selfie_description(cab, outside, focus, happening)
            request: dict[str, Any] = {
                "description": description,
                "mode": "free",
                "context": "",
                "chat_id": chat_id,
                "negative_extra": photo_negative(cab),
                # Мимика — главным: без этого промптер выкинет её ради обстановки.
                "emphasize": [e for e in (face or action,) if e],
            }
            px = await self.photo_size(user_id)  # старый фотоаппарат (59.6)
            if px:
                request["size"] = px
            loras: list[list[Any]] = [list(cabinet_mod.ALFRED_LORA)]
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
                png = base64.b64decode(result["png_b64"])
            except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
                log.warning("interactives: селфи к ответу не нарисовано (%s): %s", chat_id, exc)
                return None
            finally:
                phases.cancel()
            image_id = await self._store.add_image(
                chat_id=chat_id,
                author=None,
                prompt_ru=f"{cabinet_mod.SELFIE_MOOD_CAPTION}: {emotion}",
                prompt_en=str(result.get("prompt") or description),
                caption=cabinet_mod.SELFIE_MOOD_CAPTION,
                width=int(result["width"]),
                height=int(result["height"]),
                colors=cfg.imagegen_colors,
                png=png,
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
            sent = await self._notifier.send_photo_ex(
                chat_id,
                image_tools.upscale_png(png, cfg.imagegen_display_px),
                caption=ALFRED_PHOTO_PREFIX + html.escape(reply),
                message_thread_id=message_thread_id,
                reply_to_message_id=trigger_message_id,
            )
            if sent is None:
                log.warning("interactives: селфи #%s не ушло в чат %s", image_id, chat_id)
                return None
            await self._store.set_image_sent(image_id, sent[1], sent[0])
            return int(sent[0])
        finally:
            if self._photo_status.get(chat_id) is on_status:
                self._photo_status.pop(chat_id, None)
