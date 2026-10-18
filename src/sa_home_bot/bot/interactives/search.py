from __future__ import annotations

from sa_home_bot.bot.interactives import acts, cellar, places
from sa_home_bot.bot.interactives import items as items_mod
from sa_home_bot.bot.interactives.core import EngineCore
from sa_home_bot.bot.interactives.places import Room
from sa_home_bot.bot.interactives.registry import REGISTRY


class SearchMixin(EngineCore):
    async def tool_search(self, chat_id: int | None, user_id: int | None, where: str) -> str:
        """Альфред порыскал в месте, где стоит. Что найдено, решает код по
        таблице находок места (places.search_room); обысканное помечается.
        В идущей канареечной сцене итог (а на поиске ключа — и сам ключ,
        cellar.roll_key) решён кодом, а Ведущий обставляет его синхронно тем же
        запросом, что и тул act (Этап 59.7: поиск — частный случай действия);
        вне сцены итог отдаётся сразу. Только канарейкам."""
        if chat_id is None or user_id is None or not self.canary_ok(user_id):
            return places.TOOL_SEARCH_UNAVAILABLE
        where = " ".join(where.split())[: places.SEARCH_WHERE_MAX]
        if not where:
            return places.TOOL_SEARCH_NO_WHERE
        room = await self._room(chat_id, user_id)
        canon = room.canon if isinstance(room, Room) else places.CABINET_CANON
        # Обыскать можно только то место, где стоишь (59.7): чужое место не
        # обыскивается и не помечается обысканным.
        name = places.foreign_place(where, room.place)
        if name is not None:
            return places.TOOL_SEARCH_ELSEWHERE.format(name=name, here=canon.in_ru)
        # Коридоры и перекрёстки катакомб (59.4): места поиска узла/прохода,
        # итог сразу. Особые места катакомб идут обычным путём ниже.
        labyrinth = await self.search_labyrinth(chat_id, user_id, where)
        if labyrinth is not None:
            return labyrinth
        run = await self._scene_run(chat_id, user_id)
        if run is not None and not REGISTRY[run.scenario].canary:
            run = None  # сцена радио поиска не касается (прогресс радио не трогаем)
        if run is not None and not self._take_act(chat_id):
            return acts.TOOL_ACT_LIMIT
        temp = isinstance(room, Room) and room.temp
        if temp:
            entry = ((run.world.get("rooms") or {}) if run is not None else {}).get(canon.id)
            if entry is None:
                return places.TOOL_SEARCH_UNAVAILABLE
            searched: list[str] = entry.setdefault("searched", [])
        else:
            searched = await places.load_searched(self._store, user_id, canon.id)
        have = frozenset(it["type"] for it in await self._store.items_of(user_id))
        result = places.search_room(
            canon, where, searched, rng=self._rng, choose=self._choose, have_items=have
        )
        if not temp:
            await places.save_searched(self._store, user_id, canon.id, searched)
        found_kind = items_mod.KINDS.get(result.item or "")
        found_item = (
            await self._create_found_item(user_id, found_kind, f"find:{canon.id}")
            if found_kind is not None
            else None
        )
        if run is None:
            found = result.text
            if found_kind is not None and found_item is not None:
                # Ведущего нет — заметку (производитель, модель) даёт запасной
                # список вида.
                note = (found_item.get("note") or "").rstrip(". ")
                found += places.SEARCH_ITEM_TAKEN.format(
                    name=found_kind.name, note=f" ({note})" if note else ""
                )
            return places.TOOL_SEARCH_FREE.format(where=where, found=found)
        hunting = (
            run.scenario == cellar.SCENARIO_ID
            and run.stage == cellar.STAGE_KEY_HUNT
            and not run.finale
        )
        key = False
        if hunting and result.kind != places.SEARCH_KIND_REPEAT:
            key = cellar.roll_key(run, self._rng)
            if key:
                await self._state.set_effect(places.EFFECT_CELLAR_UNLOCKED, user_id, "1")
        outcome = cellar.search_outcome_text(
            where, result.kind, result.text, key=key, hunting=hunting
        )
        fallback = cellar.search_fallback_text(
            where, result.kind, result.text, key=key, hunting=hunting
        )
        if found_kind is not None and found_item is not None and not key:
            # Находка с вещью: итог для Ведущего — что найдено, а приметы и
            # заметку вещи он придумает в ответе (found_item).
            outcome = places.SEARCH_ITEM_DIRECTOR.format(
                where=where, text=result.text, name=found_kind.name
            )
            note = (found_item.get("note") or "").rstrip(". ")
            fallback = result.text + places.SEARCH_ITEM_TAKEN.format(
                name=found_kind.name, note=f" ({note})" if note else ""
            )
            self._ask_found_item(run, found_item, found_kind)
        return await self._act_in_scene(
            run, f"порыскать: {where}", outcome=outcome, fallback=fallback
        )
