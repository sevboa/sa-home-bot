from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from typing import Any

from sa_home_bot.bot.interactives import catacombs, cellar, places
from sa_home_bot.bot.interactives.base import STATUS_ACTIVE, STATUS_DONE, Run, iso
from sa_home_bot.bot.interactives.cabinet import Cabinet
from sa_home_bot.bot.interactives.catacombs import (
    CatacombsState,
    Maze,
    Node,
    Outcome,
    Passage,
    WalkResult,
)
from sa_home_bot.bot.interactives.core import EngineCore
from sa_home_bot.bot.interactives.director import ask_json
from sa_home_bot.bot.interactives.places import Room
from sa_home_bot.bot.interactives.underground import (
    CATACOMBS_CANON,
    DESCRIBE_SYSTEM,
    FALLBACK_NODE,
    FALLBACK_PASSAGE,
    FRAMES_PER_TURN,
    PLACE_CATACOMBS,
    SCENARIO_ID,
    TOOL_FRAME_NOTE,
    TOOL_FRAME_STARTED,
    TOOL_GO_BUSY,
    TOOL_GO_CATACOMBS,
    TOOL_GO_CELLAR,
    TOOL_GO_CELLAR_LOCKED,
    TOOL_GO_HOME,
    TOOL_GO_NO_WAY,
    TOOL_GO_NOT_FOUND,
    TOOL_GO_ROOM,
    TOOL_GO_UNAVAILABLE,
    TOOL_WALK_ASCEND,
    TOOL_WALK_BAD_DIRECTION,
    TOOL_WALK_CLOSING,
    TOOL_WALK_NOT_HERE,
    TOOL_WALK_ROOM_HINT,
    TOOL_WALK_UNAVAILABLE,
    Frame,
    FramePlan,
    build_describe_input,
    current_place,
    go_action,
    labyrinth_room,
    match_spot,
    neighbor_descriptions,
    node_frame,
    normalize_direction,
    outcome_ru,
    parse_described,
    passage_frame,
    plan_frames,
    resolve_target,
    spot_frame,
    state_of,
    step_summary,
    with_state,
)

log = logging.getLogger(__name__)


@dataclass
class _Runtime:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[int, FramePlan] = field(default_factory=dict)


class UndergroundMixin(EngineCore):
    """Катакомбы в ``engine.Interactives``. Все ``self._*`` — атрибуты и методы
    движка (хранилище, часы, комнаты, снимок)."""

    # --- служебное ---

    @functools.cached_property
    def _frames(self) -> _Runtime:
        return _Runtime()

    def _underground_ok(self, chat_id: int | None, user_id: int | None) -> bool:
        # Только канарейкам и только в личке: chat_id совпадает с user_id.
        return (
            chat_id is not None
            and user_id is not None
            and chat_id == user_id
            and self.canary_ok(user_id)
        )

    async def _catacombs_room(self, user_id: int, at: places.At) -> Cabinet:
        """Комната, где Альфред под землёй: особое место (если внутри) или
        оболочка лабиринта с видом текущего узла/прохода."""
        maze = await catacombs.load(self._store, user_id)
        state = state_of(at, maze)
        if state.room and state.room in places.PLACES:
            return await places.load_room(self._store, user_id, state.room)
        return labyrinth_room(user_id, maze, state)

    async def _flags(self, user_id: int) -> tuple[bool, bool]:
        """(подвал открыт ключом, путь в катакомбы открыт)."""
        state = self._state
        unlocked = await state.get_effect(places.EFFECT_CELLAR_UNLOCKED, user_id) == "1"
        opened = await state.get_effect(places.EFFECT_CATACOMBS_OPEN, user_id) == "1"
        return unlocked or opened, opened

    # --- прогулка как сцена ---

    async def _start_descent(self, chat_id: int, user_id: int) -> Run:
        """Прогулка начинается (или продолжается) — живой ``Run`` на чат."""
        run = await self._state.load_run(chat_id, SCENARIO_ID)
        if run is None or run.status != STATUS_ACTIVE:
            run = Run(scenario=SCENARIO_ID, chat_id=chat_id, user_id=user_id, status=STATUS_ACTIVE)
            await self._state.save_run(run)
        return run

    async def _end_descent(self, chat_id: int, user_id: int) -> None:
        """Альфред ушёл из-под земли — прогулка завершена."""
        run = await self._state.load_run(chat_id, SCENARIO_ID)
        if run is not None and run.status == STATUS_ACTIVE:
            run.status = STATUS_DONE
            await self._state.save_run(run)

    async def _note_step(self, chat_id: int, user_id: int, summary: str) -> None:
        run = await self._start_descent(chat_id, user_id)
        run.world["step"] = summary
        run.world["step_turn"] = run.turns_total
        await self._state.save_run(run)

    # --- описание новых мест ---

    async def describe_new(
        self, chat_id: int, maze: Maze, todo: list[tuple[str, Node | Passage]]
    ) -> None:
        """Описания для тех из ``todo`` (ключ ``passage``/``node`` и место),
        у кого их ещё нет: один синхронный вопрос Ведущему, ответ
        запоминается один раз. Нет связи или сбой — запасные описания."""
        todo = [(k, p) for k, p in todo if catacombs.needs_describe(p)]
        if not todo:
            return
        data: dict | None = None
        link = self._get_node_link()
        if link is not None:
            data = await ask_json(
                link,
                self._settings.llm.request_timeout_s,
                chat_id=chat_id,
                system=DESCRIBE_SYSTEM,
                content=build_describe_input(maze, todo, neighbor_descriptions(maze)),
            )
        for key, place in todo:
            parsed = parse_described(data, key)
            if parsed is None:
                options = FALLBACK_PASSAGE if key == "passage" else FALLBACK_NODE
                parsed = (self._choose(options), [])
            catacombs.set_description(place, parsed[0], parsed[1])

    # --- тул walk ---

    async def tool_walk(
        self,
        chat_id: int | None,
        user_id: int | None,
        direction: Any,
        *,
        message_thread_id: int | None = None,
        trigger_message_id: int | None = None,
    ) -> str:
        """Один шаг Альфреда по катакомбам. Топологию решает ``catacombs.walk``
        (код), описание нового места присылает Ведущий здесь же, синхронно;
        кадры уйдут гостю после реплики (``flush_frames``)."""
        if not self._underground_ok(chat_id, user_id):
            return TOOL_WALK_UNAVAILABLE
        assert chat_id is not None and user_id is not None
        way = normalize_direction(direction)
        if way is None:
            return TOOL_WALK_BAD_DIRECTION
        at = await places.get_at(self._store, user_id)
        if at is None or at.place != PLACE_CATACOMBS:
            return TOOL_WALK_NOT_HERE
        async with self._frames.lock:
            maze = await catacombs.load(self._store, user_id)
            state = state_of(at, maze)
            result = catacombs.walk(state, maze, way)
            todo: list[tuple[str, Node | Passage]] = []
            if result.outcome in (
                Outcome.MOVED,
                Outcome.NEW_PASSAGE,
                Outcome.CONNECTED,
                Outcome.LEVER_WALL,
            ):
                if result.passage is not None:
                    todo.append(("passage", result.passage))
                if result.node is not None and result.outcome != Outcome.LEVER_WALL:
                    todo.append(("node", result.node))
            await self.describe_new(chat_id, maze, todo)
            await catacombs.save(self._store, maze)
            if result.outcome == Outcome.ASCENDED:
                await self._move_to(user_id, places.PLACE_CELLAR)
            else:
                at.touched = iso(self._now())
                await places.set_at(self._store, user_id, with_state(at, state))
            frames = plan_frames(user_id, maze, result)
            if result.outcome == Outcome.ENTERED_ROOM and result.room is not None:
                room = await places.load_room(self._store, user_id, result.room.id)
                frames.append(
                    Frame(
                        kind="room",
                        owner="room:" + result.room.id,
                        key="",
                        caption=room.canon.caption,
                        room=room,
                    )
                )
            text = self._walk_text(maze, state, result)
        if result.outcome == Outcome.ASCENDED:
            await self._end_descent(chat_id, user_id)
            room = await self._room(chat_id, user_id)
            where = await self._where_ru(room, user_id, in_scene=False)
            return TOOL_WALK_ASCEND.format(where=where)
        await self._note_step(chat_id, user_id, step_summary(result))
        self._queue_frames(chat_id, user_id, frames, message_thread_id, trigger_message_id)
        if frames:
            text += TOOL_FRAME_NOTE
        return text

    def _walk_text(self, maze: Maze, state: CatacombsState, result: WalkResult) -> str:
        text = outcome_ru(result, in_room=bool(state.room))
        if state.room:
            canon = places.PLACES.get(state.room)
            if canon is not None and result.outcome == Outcome.ENTERED_ROOM:
                text += " " + canon.canon_ru
            return text + TOOL_WALK_ROOM_HINT
        return f"{text} {catacombs.look(state, maze).text_ru()}{TOOL_WALK_CLOSING}"

    # --- тул go ---

    async def tool_go(
        self,
        chat_id: int | None,
        user_id: int | None,
        place: Any,
        *,
        message_thread_id: int | None = None,
        trigger_message_id: int | None = None,
    ) -> str:
        """Быстрый переход «знакомой дорогой» (59.5): из любого места — в
        кабинет, подвал (если открыт ключом), катакомбы (всегда к лазу) и в
        найденное особое место. Одна реплика, кадров по пути нет (кроме кадра
        входа при первом спуске)."""
        if not self._underground_ok(chat_id, user_id):
            return TOOL_GO_UNAVAILABLE
        assert chat_id is not None and user_id is not None
        target = resolve_target(place)
        if target is None:
            # Кухня, двор, темница — обычные места поместья: дорогу туда
            # решает Ведущий (act), а не отказ «не знаю такого места» (живая
            # находка 2026-10-15: Альфред пересказывал его как «нет в списке»).
            return await self.tool_act(chat_id, user_id, go_action(place))
        busy = await self._scene_run(chat_id, user_id)
        if busy is not None and busy.scenario != SCENARIO_ID:
            if busy.scenario == cellar.SCENARIO_ID and busy.finale:
                # Сказка про подвал дошла до финала — она закончена.
                busy.status = STATUS_DONE
                await self._state.save_run(busy)
            elif busy.scenario == cellar.SCENARIO_ID and target != "cabinet":
                # Поиски ключа идут по всему поместью: к двери подвала и
                # куда угодно ещё ведёт Ведущий — он знает, заперто ли.
                return await self.tool_act(chat_id, user_id, go_action(place))
            elif busy.scenario != cellar.SCENARIO_ID:
                return TOOL_GO_BUSY
        unlocked, opened = await self._flags(user_id)
        if target == "cabinet":
            await self._end_descent(chat_id, user_id)
            await self._go_home(user_id, announce=False)
            return TOOL_GO_HOME
        if target == "cellar":
            if not unlocked:
                return TOOL_GO_CELLAR_LOCKED
            await self._end_descent(chat_id, user_id)
            await self._move_to(user_id, places.PLACE_CELLAR)
            room = await self._room(chat_id, user_id)
            where = await self._where_ru(room, user_id, in_scene=False)
            return TOOL_GO_CELLAR.format(where=where)
        if not opened:
            return TOOL_GO_NO_WAY
        async with self._frames.lock:
            maze = await catacombs.load(self._store, user_id)
            state = CatacombsState.at_entrance(maze)
            frames: list[Frame] = []
            if target == "catacombs":
                catacombs.reset_position(state, maze)
                entrance = maze.nodes[state.node]
                await self.describe_new(chat_id, maze, [("node", entrance)])
                if catacombs.node_needs_frame(entrance):
                    # Первый спуск (или вход перерисован): кадр входа.
                    frames.append(node_frame(user_id, maze, entrance))
            elif not catacombs.go_to_room(state, maze, target):
                return TOOL_GO_NOT_FOUND
            else:
                node = maze.nodes[state.node]
                sides = list(node.exits)
                if sides:
                    # Лицом к двери: единственный выход оказывается за спиной.
                    state.heading = catacombs.OPPOSITE[sides[0]]
            await catacombs.save(self._store, maze)
            at = places.new_at(
                PLACE_CATACOMBS,
                self._now(),
                node=state.to_dict(),
                heading=state.heading,
            )
            await places.set_at(self._store, user_id, at)
        await self._start_descent(chat_id, user_id)
        room = await self._room(chat_id, user_id)
        where = await self._where_ru(room, user_id, in_scene=True)
        if target == "catacombs":
            await self._note_step(chat_id, user_id, "Альфред спустился знакомой дорогой к лазу.")
            self._queue_frames(chat_id, user_id, frames, message_thread_id, trigger_message_id)
            return TOOL_GO_CATACOMBS.format(
                where=where, frame_note=TOOL_FRAME_NOTE if frames else ""
            )
        canon = places.PLACES[target]
        await self._note_step(
            chat_id, user_id, f"Альфред знакомой дорогой дошёл до места «{canon.name_ru}»."
        )
        return TOOL_GO_ROOM.format(name=canon.name_ru, where=where)

    # --- подсказки «Где ты» ---

    async def _travel_hint(self, user_id: int) -> str:
        """Войдя в подвал, Альфред сам предлагает быстрые переходы: катакомбы
        и по названию каждое найденное особое место (59.5)."""
        maze = await catacombs.load(self._store, user_id)
        rooms = catacombs.found_rooms(maze)
        text = " Знакомой дорогой (инструмент go) отсюда можно сразу спуститься в катакомбы, к лазу"
        if rooms:
            text += ", или в найденное: " + ", ".join(f"{r.name} ({r.id})" for r in rooms)
        return text + " — сам предложи это собеседнику."

    async def _underground_extra(self, room: Cabinet, user_id: int) -> str:
        """Приписка к «Где ты» для комнат вне подвала: в катакомбах и особых
        местах — как отсюда выйти."""
        if not isinstance(room, Room):
            return ""
        if room.canon.id == PLACE_CATACOMBS:
            return " Подняться в подвал — walk(up); быстро в кабинет — go(cabinet)."
        if room.canon.group == places.GROUP_CATACOMBS:
            return " Выйти обратно в коридор — walk(back); порыскать — search."
        return ""

    # --- кадры: очередь, отправка, запоминание ---

    def _queue_frames(
        self,
        chat_id: int,
        user_id: int,
        frames: list[Frame],
        message_thread_id: int | None,
        reply_to: int | None,
    ) -> None:
        """Кадры хода ждут конца реплики Альфреда (``flush_frames`` из
        flush_forms). За ход берём не больше ``FRAMES_PER_TURN`` последних."""
        if not frames:
            return
        pending = self._frames.pending
        plan = pending.get(chat_id)
        if plan is None:
            plan = pending[chat_id] = FramePlan(user_id, [], message_thread_id, reply_to)
        for frame in frames:
            plan.frames = [f for f in plan.frames if f.dedupe != frame.dedupe]
            plan.frames.append(frame)
        del plan.frames[:-FRAMES_PER_TURN]

    def discard_frames(self, chat_id: int) -> None:
        self._frames.pending.pop(chat_id, None)

    async def flush_frames(self, chat_id: int) -> None:
        plan = self._frames.pending.pop(chat_id, None)
        if plan is None or not plan.frames:
            return
        if not hasattr(self._notifier, "send_photo_ex"):
            return
        if chat_id in self._photo_busy:
            # Камера занята другим снимком: этот кадр не вышел, но и ничего не
            # запомнено — при следующем заходе он нарисуется заново.
            log.info("interactives: кадры катакомб пропущены, камера занята (chat=%s)", chat_id)
            return
        self._photo_busy.add(chat_id)
        task = asyncio.create_task(self._frames_job(chat_id, plan))
        self._photo_tasks.add(task)
        task.add_done_callback(self._photo_tasks.discard)

    async def _frames_job(self, chat_id: int, plan: FramePlan) -> None:
        try:
            outside = await self._transylvania.outside(self._now())
            for frame in plan.frames:
                if frame.file_id:
                    sent = await self._notifier.send_photo_ex(
                        chat_id,
                        frame.file_id,
                        caption=frame.caption,
                        message_thread_id=plan.message_thread_id,
                        reply_to_message_id=plan.reply_to,
                    )
                    if sent is not None:
                        continue
                    log.info("interactives: file_id кадра не ушёл, рисую заново (%s)", frame.owner)
                if self._get_node_link() is None:
                    continue
                await self._photo(
                    chat_id,
                    plan.user_id,
                    focus=frame.focus,
                    caption=frame.caption,
                    happening=None,
                    outside=outside,
                    message_thread_id=plan.message_thread_id,
                    trigger_message_id=plan.reply_to,
                    frame=frame,
                )
        except Exception:
            log.exception("interactives: кадры катакомб не вышли (chat=%s)", chat_id)
        finally:
            self._photo_busy.discard(chat_id)

    async def remember_frame(
        self, user_id: int, frame: Frame, image_id: int, file_id: str | None
    ) -> None:
        """Нарисованный кадр запоминается у узла/прохода/места поиска: повторный
        показ — тот же file_id. Лабиринт перечитываем: за время рисования он
        мог измениться (сделан шаг), а затирать чужие правки нельзя."""
        if not frame.remembered:
            return
        async with self._frames.lock:
            if not await self._store.get_state(places.CATACOMBS_KEY.format(user_id=user_id)):
                return  # лабиринт сброшен, пока кадр рисовался
            maze = await catacombs.load(self._store, user_id)
            kind, _, ref = frame.owner.partition(":")
            owner: Node | Passage | None
            if kind == "node":
                x, _, y = ref.partition(",")
                owner = maze.nodes.get((int(x), int(y)))
            else:
                owner = maze.passages.get(ref)
            if owner is None:
                return
            if frame.spot:
                spot = next((s for s in owner.spots if s.name == frame.spot), None)
                if spot is None:
                    return
                spot.image_id, spot.file_id = image_id, file_id
            else:
                catacombs.set_image(owner, image_id, file_id, image_key=frame.key)
            await catacombs.save(self._store, maze)

    # --- тул search и take_photo в лабиринте ---

    async def search_labyrinth(self, chat_id: int, user_id: int, where: str) -> str | None:
        """Обыск в коридоре или узле катакомб (не в особом месте). Обыскиваются
        места поиска узла/прохода, помеченные при описании; названное Альфредом
        вне списка тоже обыскивается (помечается как новое место). Итог —
        сразу, без Ведущего: находку решает код (``places.search_room``),
        катакомбная таблица — пусто или мелочь. None — не лабиринт, обычный
        путь ``tool_search``."""
        at = await places.get_at(self._store, user_id)
        if at is None or at.place != PLACE_CATACOMBS:
            return None
        async with self._frames.lock:
            maze = await catacombs.load(self._store, user_id)
            state = state_of(at, maze)
            if state.room:
                return None
            owner = current_place(maze, state)
            spot = match_spot(owner.spots, where)
            if spot is not None and spot.searched:
                result = places.SearchResult(places.SEARCH_KIND_REPEAT, places.SEARCH_REPEAT_TEXT)
            else:
                result = places.search_room(
                    CATACOMBS_CANON,
                    where,
                    [],
                    rng=self._rng,
                    choose=self._choose,
                )
                if spot is None:
                    spot = catacombs.Spot(name=where)
                    owner.spots.append(spot)
                spot.searched = True
                await catacombs.save(self._store, maze)
            # Картинка обысканного места — только по обыску (решение владельца
            # 2026-10-15), рисуется раз и запоминается у места поиска.
            frame = spot_frame(user_id, maze, owner, spot)
        self._queue_frames(chat_id, user_id, [frame], None, None)
        run = await self._scene_run(chat_id, user_id)
        if run is not None and run.scenario == SCENARIO_ID:
            run.world["search_outcome"] = f"Альфред обыскал «{where}». {result.text}"
            await self._state.save_run(run)
        return places.TOOL_SEARCH_FREE.format(where=where, found=result.text) + TOOL_FRAME_NOTE

    async def underground_photo(
        self,
        chat_id: int,
        user_id: int,
        cab: Cabinet,
        focus: str,
        *,
        selfie: bool,
        message_thread_id: int | None,
        trigger_message_id: int | None,
    ) -> str | None:
        """take_photo в лабиринте: общий вид — запомненный кадр узла/прохода,
        крупный план места поиска — запомненный кадр места (рисуется один раз).
        None — это что-то иное (селфи, произвольный план, особое место), снимок
        идёт обычным путём."""
        if selfie or not isinstance(cab, Room) or cab.canon.id != PLACE_CATACOMBS:
            return None
        at = await places.get_at(self._store, user_id)
        if at is None:
            return None
        async with self._frames.lock:
            maze = await catacombs.load(self._store, user_id)
            state = state_of(at, maze)
            owner = current_place(maze, state)
            if focus:
                spot = match_spot(owner.spots, focus)
                if spot is None:
                    return None
                frame = spot_frame(user_id, maze, owner, spot)
            elif isinstance(owner, Passage):
                frame = passage_frame(user_id, maze, owner, wall=owner.sealed)
            else:
                frame = node_frame(user_id, maze, owner)
        self._queue_frames(chat_id, user_id, [frame], message_thread_id, trigger_message_id)
        return TOOL_FRAME_STARTED

    # --- отладка ---

    async def underground_debug(self, user_id: int) -> str:
        """Строка к ``/interactives where``: узел, направление, число
        разведанных узлов и найденные особые места."""
        at = await places.get_at(self._store, user_id)
        if at is None or at.place != PLACE_CATACOMBS:
            return ""
        maze = await catacombs.load(self._store, user_id)
        state = state_of(at, maze)
        found = ", ".join(maze.found) or "нет"
        where = f"узел ({state.node[0]},{state.node[1]}), heading {state.heading}"
        if state.passage:
            where += f", в проходе {state.passage}"
        if state.room:
            where += f", внутри {state.room}"
        return (
            f"\nкатакомбы: {where}; разведано узлов {len(maze.nodes)}, "
            f"проходов {len(maze.passages)}, неразведанных выходов {maze.unexplored}; "
            f"особые места: {found}"
        )
