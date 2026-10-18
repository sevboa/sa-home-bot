from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from sa_home_bot.bot.interactives import cabinet as cabinet_mod
from sa_home_bot.bot.interactives.base import iso, parse_iso
from sa_home_bot.bot.interactives.core import EngineCore
from sa_home_bot.bot.interactives.transylvania import TZ as TRANSYLVANIA_TZ

log = logging.getLogger(__name__)


class PhotoReviewMixin(EngineCore):
    async def _save_last_photo(
        self,
        chat_id: int,
        image_id: int,
        seen: str,
        expect: list[str],
        missing: list[str],
    ) -> int:
        """Запомнить, что вышло на последнем снимке чата (только для Альфреда).
        Возвращает число промахов подряд по тому же ``expect``."""
        key = cabinet_mod.LAST_PHOTO_KEY.format(chat_id=chat_id)
        prev = await self._last_photo(chat_id, fresh_only=False)
        misses = 0
        if missing:
            same = prev is not None and prev.get("expect") == expect
            misses = (int(prev.get("misses") or 0) if same else 0) + 1
        await self._store.set_state(
            key,
            json.dumps(
                {
                    "image_id": image_id,
                    "at": iso(self._now()),
                    "seen": seen,
                    "expect": expect,
                    "missing": missing,
                    "misses": misses,
                },
                ensure_ascii=False,
            ),
        )
        return misses

    async def _last_photo(self, chat_id: int, *, fresh_only: bool = True) -> dict[str, Any] | None:
        raw = await self._store.get_state(cabinet_mod.LAST_PHOTO_KEY.format(chat_id=chat_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if fresh_only:
            at = parse_iso(data.get("at"))
            if at is None or at + timedelta(hours=cabinet_mod.LAST_PHOTO_TTL_H) <= self._now():
                return None
        return data

    async def photo_note(self, chat_id: int) -> str | None:
        """Заметка к ходу Альфреда: что на его последнем снимке."""
        data = await self._last_photo(chat_id)
        if data is None or not data.get("seen"):
            return None
        at = parse_iso(data.get("at"))
        when = at.astimezone(TRANSYLVANIA_TZ).strftime("%H:%M") if at else "недавно"
        note = cabinet_mod.PHOTO_SEEN_NOTE.format(at=when, description=data["seen"])
        missing = data.get("missing") or []
        if missing:
            note += cabinet_mod.PHOTO_SEEN_MISSING_NOTE.format(missing=", ".join(missing))
        return note

    async def _react_to_miss(
        self,
        chat_id: int,
        seen: str,
        missing: list[str],
        misses: int,
        *,
        photo_message_id: int | None,
        message_thread_id: int | None,
        trigger_message_id: int | None,
        dialogue_id: int | None,
    ) -> None:
        """На снимке нет обещанного — Альфред удивляется и предлагает
        переснять (ответом на снимок, в тот же диалог)."""
        template = cabinet_mod.PHOTO_MISS_DIRECTIVE
        if misses > 1:
            template = cabinet_mod.PHOTO_MISS_AGAIN_DIRECTIVE
        directive = template.format(missing=", ".join(missing), description=seen)
        where = await self._where(chat_id, trigger_message_id, message_thread_id)
        if dialogue_id is not None:
            where["dialogue_id"] = dialogue_id
        if photo_message_id is not None:
            where["trigger_message_id"] = photo_message_id
        log.info("interactives: на снимке нет %s (chat=%s, промах %d)", missing, chat_id, misses)
        await self._speak(chat_id, directive, where)
