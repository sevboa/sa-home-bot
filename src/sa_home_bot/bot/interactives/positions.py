from __future__ import annotations

from sa_home_bot.bot.interactives import cabinet as cabinet_mod
from sa_home_bot.bot.interactives import places, underground
from sa_home_bot.bot.interactives.base import STATUS_ACTIVE, STATUS_IDLE, Run, Scenario, iso
from sa_home_bot.bot.interactives.cabinet import Cabinet
from sa_home_bot.bot.interactives.core import EngineCore
from sa_home_bot.bot.interactives.director import ask_features
from sa_home_bot.bot.interactives.places import Room
from sa_home_bot.bot.interactives.scene import TurnPlan
from sa_home_bot.bot.interactives.transylvania import Outside


class PositionsMixin(EngineCore):
    async def _room(self, chat_id: int, user_id: int) -> Cabinet:
        if not self.canary_ok(user_id):
            return await cabinet_mod.load(self._store, user_id)
        at = await places.get_at(self._store, user_id)
        if at is None or at.in_cabinet:
            return await cabinet_mod.load(self._store, user_id)
        if at.place == underground.PLACE_CATACOMBS:
            return await self._catacombs_room(user_id, at)
        if at.place in places.PLACES:
            return await places.load_room(self._store, user_id, at.place)
        if places.is_temp(at.place):
            for scenario in self.scenarios_for(user_id):
                run = await self._state.load_run(chat_id, scenario.id)
                if run is not None and run.world:
                    room = places.temp_room(run.world, user_id, at.place)
                    if room is not None:
                        return room
        return await cabinet_mod.load(self._store, user_id)

    async def _save_room(self, room: Cabinet, run: Run | None = None) -> None:
        if isinstance(room, Room):
            if room.temp:
                if run is not None:
                    places.store_temp_room(run.world, room)
            else:
                await places.save_room(self._store, room)
        else:
            await cabinet_mod.save(self._store, room)

    async def _move_to(self, user_id: int, place: str) -> None:
        at = places.new_at(place, self._now())
        await places.set_at(self._store, user_id, at)

    def _from_phrase(self, at: places.At) -> str:
        if at.place == underground.PLACE_CATACOMBS:
            return underground.CATACOMBS_CANON.from_ru
        canon = places.PLACES.get(at.place)
        if canon is not None:
            return canon.from_ru
        return "оттуда, где искал ключ" if places.is_temp(at.place) else places.RETURN_FROM_UNKNOWN

    async def _go_home(self, user_id: int, *, announce: bool) -> None:
        at = await places.get_at(self._store, user_id)
        if at is None or at.in_cabinet:
            return
        extra = {"returned_from": self._from_phrase(at)} if announce else {}
        home = places.new_at(places.PLACE_CABINET, self._now(), **extra)
        await places.set_at(self._store, user_id, home)

    async def _tend_position(self, user_id: int) -> str | None:
        at = await places.get_at(self._store, user_id)
        if at is None:
            return None
        now = self._now()
        if at.returned_from:
            await places.set_at(self._store, user_id, places.new_at(places.PLACE_CABINET, now))
            return places.RETURN_NOTE.format(from_ru=at.returned_from)
        if at.in_cabinet:
            return None
        if places.is_idle(at, now, self._settings.llm.interactives_return_idle_h):
            for scenario in self.scenarios_for(user_id):
                if not scenario.canary:
                    continue
                for chat_id in await self._canary_chats(user_id, scenario):
                    run = await self._state.load_run(chat_id, scenario.id)
                    if run is not None and run.status == STATUS_ACTIVE:
                        run.status = STATUS_IDLE
                        await self._state.save_run(run)
            phrase = self._from_phrase(at)
            await places.set_at(self._store, user_id, places.new_at(places.PLACE_CABINET, now))
            return places.RETURN_NOTE.format(from_ru=phrase)
        at.touched = iso(now)
        await places.set_at(self._store, user_id, at)
        return None

    async def _canary_chats(self, user_id: int, scenario: Scenario) -> list[int]:
        chats = []
        for key in await self._store.state_keys("interactive_run:"):
            parts = key.split(":")
            if len(parts) == 3 and parts[2] == scenario.id and parts[1].lstrip("-").isdigit():
                run = await self._state.load_run(int(parts[1]), scenario.id)
                if run is not None and run.user_id == user_id:
                    chats.append(int(parts[1]))
        return chats

    async def _room_extra(self, room: Cabinet, user_id: int) -> str:
        if (
            isinstance(room, Room)
            and room.canon.id == places.PLACE_CELLAR
            and await self._state.get_effect(places.EFFECT_CATACOMBS_OPEN, user_id) == "1"
        ):
            return places.CELLAR_HATCH_HINT + await self._travel_hint(user_id)
        return await self._underground_extra(room, user_id)

    def _where_rule(self, cab: Cabinet) -> str:
        window = places.WINDOW_YES if cab.windowed else places.WINDOW_NO
        return f"{window} {places.MOVE_RULE}"

    async def _add_where_note(self, plan: TurnPlan, chat_id: int, user_id: int) -> None:
        cab = await self._room(chat_id, user_id)
        if isinstance(cab, Room):
            detail = " " + cab.describe_ru() + await self._room_extra(cab, user_id)
            if cab.canon.group != places.GROUP_CATACOMBS:
                detail += places.EXIT_HOME
            note = places.WHERE_NOTE.format(here=cab.canon.in_ru, detail=detail)
        else:
            note = places.WHERE_NOTE.format(
                here=places.CABINET_CANON.in_ru, detail=await self._cabinet_exits(user_id)
            )
        note += f"\n{self._where_rule(cab)}"
        plan.note = f"{note}\n\n{plan.note}" if plan.note else note

    async def _cabinet_exits(self, user_id: int) -> str:
        unlocked, opened = await self._flags(user_id)
        targets = []
        if unlocked:
            targets.append("в подвал — cellar")
        if opened:
            targets.append("в катакомбы — catacombs")
        return places.EXIT_CABINET_TO.format(targets=", ".join(targets)) if targets else ""

    async def _end_scene_in_room(self, run: Run) -> None:
        room = await self._room(run.chat_id, run.user_id)
        if room.end_scene():
            await self._save_room(room, run)

    async def _end_scene_in_cabinet(self, user_id: int) -> None:
        cab = await cabinet_mod.load(self._store, user_id)
        if cab.end_scene():
            await cabinet_mod.save(self._store, cab)

    async def _apply_move(self, scenario: Scenario, run: Run, move: dict[str, str]) -> None:
        if "place" in move:
            allowed = scenario.stage_places[min(run.stage, scenario.last_stage)]
            if move["place"] in (allowed, places.PLACE_CABINET) and move["place"] in places.PLACES:
                await self._move_to(run.user_id, move["place"])
            return
        spec = places.temp_spec(move)
        if spec is None:
            return
        tid = places.put_temp_room(run.world, spec)
        await self._move_to(run.user_id, tid)

    async def _ensure_first_features(self, cab: Cabinet, chat_id: int, outside: Outside) -> None:
        if cab.features or not cab.is_study:
            return
        node_link = self._get_node_link()
        if node_link is None:
            return
        new = await ask_features(
            node_link,
            self._settings.llm.request_timeout_s,
            chat_id=chat_id,
            place=cab.describe_ru(),
            outside=outside.ru(),
            count=cabinet_mod.FIRST_FEATURES,
        )
        if cab.add(list(new)):
            await cabinet_mod.save(self._store, cab)
