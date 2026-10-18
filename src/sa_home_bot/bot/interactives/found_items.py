from __future__ import annotations

import logging
from typing import Any

from sa_home_bot.bot.interactives import director as director_mod
from sa_home_bot.bot.interactives import items as items_mod
from sa_home_bot.bot.interactives.base import Run
from sa_home_bot.bot.interactives.core import EngineCore
from sa_home_bot.bot.interactives.director import DirectorDecision

log = logging.getLogger(__name__)


class FoundItemsMixin(EngineCore):
    async def _create_found_item(
        self, user_id: int, kind: items_mod.ItemKind, origin: str
    ) -> dict[str, Any] | None:
        if await self._store.items_of(user_id, kind.type):
            return None
        pool = list(kind.fallback_traits)
        traits: list[str] = []
        for _ in range(min(2, len(pool))):
            pick = self._choose(tuple(pool))
            pool.remove(pick)
            traits.append(pick)
        note = self._choose(kind.fallback_notes) if kind.fallback_notes else None
        item_id = await self._store.add_item(
            type=kind.type,
            owner_user_id=user_id,
            traits=traits,
            image_id=None,
            origin=origin,
            now=self._now(),
            note=note,
        )
        log.info(
            "interactives: гостю %s выдана вещь %s (#%s, %s)", user_id, kind.type, item_id, origin
        )
        return await self._store.item_by_id(item_id)

    @staticmethod
    def _ask_found_item(run: Run, item: dict[str, Any], kind: items_mod.ItemKind) -> None:
        run.world["found_item"] = {"id": int(item["id"]), "name": kind.name, "ask": kind.found_ask}

    async def _give_found_item(self, run: Run, kind: items_mod.ItemKind, *, origin: str) -> None:
        item = await self._create_found_item(run.user_id, kind, origin)
        if item is not None:
            self._ask_found_item(run, item, kind)

    async def _apply_found_item(
        self, run: Run, decision: DirectorDecision | director_mod.ActDecision | None
    ) -> None:
        found = run.world.pop("found_item", None)
        if not isinstance(found, dict) or decision is None or decision.found_item is None:
            return
        item = await self._store.item_by_id(int(found.get("id") or 0))
        if item is None or int(item["owner_user_id"]) != run.user_id:
            return
        await self._store.update_item(
            int(item["id"]),
            traits=decision.found_item["traits"] or None,
            note=decision.found_item["note"],
        )
