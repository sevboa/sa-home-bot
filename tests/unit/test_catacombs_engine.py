"""Катакомбы в разговоре (Этап 59.4–59.5): сценарий-прогулка, тулы walk/go,
описание от Ведущего, запомненные кадры, особые места, быстрые переходы,
возврат после простоя, канарейка (гость вне списка не видит ничего)."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from PIL import Image

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.handlers.interactives import cmd_interactives
from sa_home_bot.bot.interactives import acts, engine, places, underground
from sa_home_bot.bot.interactives import catacombs as cat
from sa_home_bot.bot.interactives.base import (
    STATUS_ACTIVE,
    STATUS_DONE,
    STATUS_IDLE,
    InteractiveStore,
    Run,
)
from sa_home_bot.bot.interactives.catacombs import CatacombsState, Maze, Node, Passage
from sa_home_bot.bot.interactives.engine import Interactives
from sa_home_bot.bot.interactives.transylvania import Transylvania
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.models import Subscription

CANARY = 1101
GUEST = 1102


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "brown").save(buf, format="PNG")
    return buf.getvalue()


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.photos: list[tuple[int, object, str | None]] = []

    async def send_direct(self, chat_id, text, reply_markup=None, **_kw):
        self.sent.append((chat_id, text))
        return 1000 + len(self.sent)

    async def send_photo_ex(self, chat_id, photo, *, caption=None, **_kw):
        self.photos.append((chat_id, photo, caption))
        return 500 + len(self.photos), f"file-{len(self.photos)}"


class FakeLink:
    def __init__(self) -> None:
        self.director_replies: list[str] = []
        self.director_inputs: list[str] = []
        self.director_systems: list[str] = []
        self.describe_inputs: list[str] = []
        self.describe_fail = False
        self.generated: list[dict] = []

    async def command(self, action, args, dst=None, timeout=None):
        if action == "chat" and args.get("role") == "director":
            if args.get("system") == underground.DESCRIBE_SYSTEM:
                self.describe_inputs.append(args["messages"][0]["content"])
                if self.describe_fail:
                    raise TimeoutError
                n = len(self.describe_inputs)
                return {
                    "response": json.dumps(
                        {
                            "passage": {
                                "describe": f"Коридор {n} с рыжими потёками",
                                "spots": ["ящик у стены"],
                            },
                            "node": {"describe": f"Перекрёсток {n}, на полу кости", "spots": []},
                        },
                        ensure_ascii=False,
                    )
                }
            self.director_systems.append(args["system"])
            self.director_inputs.append(args["messages"][0]["content"])
            return {"response": self.director_replies.pop(0) if self.director_replies else "{}"}
        if action == "chat":
            return {"response": "Готово, сэр."}
        if action == "generate_image":
            self.generated.append(args)
            return {
                "png_b64": base64.b64encode(_png()).decode(),
                "width": 8,
                "height": 8,
                "prompt": "p",
                "seed": 7,
            }
        if action == "chat_progress":
            return {"partial": "draw", "done": True}
        raise AssertionError(action)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Dice:
    def __init__(self, *values: float) -> None:
        self.values = list(values) or [0.99]

    def __call__(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


async def _clear() -> int:
    return 0


def _make(store, *, canary=(CANARY,), dice=None, clock=None):
    notifier, link = FakeNotifier(), FakeLink()
    svc = Interactives(
        store,
        notifier,
        Settings(llm=LlmConfig(model="m", interactives_canary_user_ids=list(canary))),
        lambda: link,
        now=clock or Clock(),
        choose=lambda options: options[0],
        rng=dice or Dice(0.99),
        transylvania=Transylvania(fetch=_clear),
    )
    svc.spoken = []
    return svc, notifier, link


async def _open(svc, user=CANARY):
    await svc._state.set_effect(places.EFFECT_CELLAR_UNLOCKED, user, "1")
    await svc._state.set_effect(places.EFFECT_CATACOMBS_OPEN, user, "1")


async def _frames(svc, chat=CANARY):
    await svc.flush_frames(chat)
    await asyncio.gather(*list(svc._photo_tasks))


async def _seed_maze(store, user=CANARY, seed=7) -> Maze:
    maze = Maze.new(user, seed)
    await cat.save(store, maze)
    return maze


async def _descend(svc, store, *, frames=True, seed=7):
    await _open(svc)
    await _seed_maze(store, seed=seed)
    text = await svc.tool_go(CANARY, CANARY, "catacombs")
    if frames:
        await _frames(svc)
    return text


async def _vision(store):
    maze = await cat.load(store, CANARY)
    at = await places.get_at(store, CANARY)
    return maze, underground.state_of(at, maze)


async def _first_exit(store) -> str:
    maze, state = await _vision(store)
    return cat.look(state, maze).exits[0].rel


async def _step(svc, store, direction=None, *, frames=True):
    direction = direction or await _first_exit(store)
    text = await svc.tool_walk(CANARY, CANARY, direction)
    if frames:
        await _frames(svc)
    return text


def _maze_with_darkroom(user=CANARY) -> tuple[Maze, str]:
    """Вход, проход длиной 2 вперёд от входа и тупик с проявочной."""
    maze = Maze.new(user, 7)
    side = maze.entry_heading
    far = cat._step((0, 0), side, 2)
    passage = Passage(a=(0, 0), a_side=side, b=far, b_side=cat.OPPOSITE[side])
    maze.passages[passage.id] = passage
    maze.nodes[far] = Node(
        x=far[0], y=far[1], exits={cat.OPPOSITE[side]: passage.id}, special="darkroom"
    )
    maze.nodes[(0, 0)].exits[side] = passage.id
    maze.special_nodes["darkroom"] = far
    maze._reindex()
    return maze, side


# --- сценарий и реестр ---


def test_scenario_is_canary_manual_and_never_triggered_by_words():
    scenario = underground.CATACOMBS
    assert scenario.canary and scenario.trigger == "manual"
    assert engine.REGISTRY["catacombs"] is scenario
    assert not scenario.trigger_re.search("в катакомбы спустимся? катакомбы!")
    assert scenario.last_stage == 0 and scenario.min_turns_before_finale > 10**6
    assert "никогда не говори" in scenario.scene_frame


def test_special_rooms_registered_by_catalog():
    for room in cat.SPECIAL_ROOMS:
        canon = places.PLACES[room.id]
        assert canon is underground.SPECIAL_CANONS[room.id]
        assert canon.group == places.GROUP_CATACOMBS
        assert canon.name_ru == room.name
        assert canon.canon_ru and canon.canon_en and canon.light_en == places.UNDERGROUND_LIGHT_EN
    dark = places.PLACES["darkroom"]
    for word in ("красный", "ванноч", "верёвк", "увеличител"):
        assert word in dark.canon_ru.lower()
    assert dark.specials == (places.DARKROOM_CAMERA,)  # 59.6: первый обыск — фотоаппарат
    assert underground.PLACE_CATACOMBS not in places.PLACES


def test_resolve_target_by_id_and_name():
    assert underground.resolve_target("catacombs") == "catacombs"
    assert underground.resolve_target("в катакомбы") == "catacombs"
    assert underground.resolve_target("в подвал") == "cellar"
    assert underground.resolve_target("cellar") == "cellar"
    assert underground.resolve_target("в кабинет") == "cabinet"
    assert underground.resolve_target("darkroom") == "darkroom"
    assert underground.resolve_target("старая проявочная") == "darkroom"
    assert underground.resolve_target("в винный погреб") == "wine_cellar"
    assert underground.resolve_target("wine_cellar") == "wine_cellar"
    assert underground.resolve_target("в космос") is None
    assert underground.resolve_target(None) is None


def test_normalize_direction():
    assert underground.normalize_direction("Left") == "left"
    assert underground.normalize_direction("налево") == "left"
    assert underground.normalize_direction("наверх") == "up"
    assert underground.normalize_direction("вбок") is None
    assert underground.normalize_direction(5) is None


# --- канарейка ---


def _ctx(svc, user_id):
    return ai_tools.ToolContext(
        chat_id=user_id,
        dialogue_id=None,
        trigger_message_id=None,
        settings=Settings(llm=LlmConfig(model="m")),
        interactives=svc,
        user_id=user_id,
    )


async def test_walk_and_go_declarations_reach_only_canaries(store):
    svc, _, _ = _make(store)
    kit = ai_tools.tools_for(Subscription(name="me", chat_id=1, allowed_commands=frozenset({"*"})))
    assert {"walk", "go", "search"} <= kit.handlers.keys()
    assert ai_tools.CANARY_TOOLS == {"search", "walk", "go", "act"}
    guest_kit = ai_tools.for_context(kit, _ctx(svc, GUEST))
    for name in ("walk", "go", "search"):
        assert name not in guest_kit.handlers
        assert all(d["function"]["name"] != name for d in guest_kit.declarations)
    assert ai_tools.for_context(kit, _ctx(svc, CANARY)) is kit


async def test_forged_walk_and_go_by_non_canary_do_nothing(store):
    svc, notifier, link = _make(store)
    kit = ai_tools.tools_for(Subscription(name="me", chat_id=1, allowed_commands=frozenset({"*"})))
    before = {k: await store.get_state(k) for k in await store.state_keys("")}
    # Подделанный вызов: ни от обычного гостя, ни от канарейки вне личного чата.
    assert await kit.handlers["walk"](_ctx(svc, GUEST), {"direction": "left"}) == (
        underground.TOOL_WALK_UNAVAILABLE
    )
    assert await kit.handlers["go"](_ctx(svc, GUEST), {"place": "catacombs"}) == (
        underground.TOOL_GO_UNAVAILABLE
    )
    assert await svc.tool_walk(GUEST, GUEST, "left") == underground.TOOL_WALK_UNAVAILABLE
    assert await svc.tool_go(GUEST, GUEST, "catacombs") == underground.TOOL_GO_UNAVAILABLE
    assert await svc.tool_walk(-100, CANARY, "left") == underground.TOOL_WALK_UNAVAILABLE
    assert await svc.tool_go(-100, CANARY, "catacombs") == underground.TOOL_GO_UNAVAILABLE
    assert {k: await store.get_state(k) for k in await store.state_keys("")} == before == {}
    assert notifier.sent == [] and notifier.photos == [] and link.generated == []


async def test_catacombs_never_offered_or_continued_for_others(store):
    svc, notifier, _ = _make(store)
    await _open(svc, GUEST)
    await InteractiveStore(store).save_run(
        Run(scenario="catacombs", chat_id=GUEST, user_id=GUEST, status=STATUS_ACTIVE)
    )
    assert [s.id for s in svc.scenarios_for(GUEST)] == ["radio"]
    before = {k: await store.get_state(k) for k in await store.state_keys("")}
    plan = await svc.before_turn(GUEST, GUEST, "Спустись в катакомбы", is_private=True)
    assert plan.scenario is None and plan.note is None and not plan.scene
    assert {k: await store.get_state(k) for k in await store.state_keys("")} == before
    # А канарейке слова в катакомбы ничего не запускают: формы нет, запускает тул.
    plan = await svc.before_turn(CANARY, CANARY, "Спустись в катакомбы", is_private=True)
    assert not plan.offered and notifier.sent == []


# --- go: быстрые переходы ---


async def test_go_refuses_until_unlocked_and_unknown(store):
    svc, _, _ = _make(store)
    assert await svc.tool_go(CANARY, CANARY, "cellar") == underground.TOOL_GO_CELLAR_LOCKED
    assert await svc.tool_go(CANARY, CANARY, "catacombs") == underground.TOOL_GO_NO_WAY
    assert await svc.tool_go(CANARY, CANARY, "darkroom") == underground.TOOL_GO_NO_WAY
    # Незнакомое место — действие для Ведущего; из кабинета вне сцены его нет.
    assert await svc.tool_go(CANARY, CANARY, "в космос") == acts.TOOL_ACT_CABINET
    assert await places.get_at(store, CANARY) is None
    # Ключ есть, лаза нет: в подвал можно, в катакомбы нет.
    await svc._state.set_effect(places.EFFECT_CELLAR_UNLOCKED, CANARY, "1")
    assert "спустился в подвал" in await svc.tool_go(CANARY, CANARY, "cellar")
    assert (await places.get_at(store, CANARY)).place == places.PLACE_CELLAR
    assert await svc.tool_go(CANARY, CANARY, "catacombs") == underground.TOOL_GO_NO_WAY


async def test_go_catacombs_starts_walk_at_the_hatch(store):
    svc, notifier, link = _make(store)
    text = await _descend(svc, store, frames=False)
    assert "к самому началу" in text and "катакомбы" in text.lower()
    at = await places.get_at(store, CANARY)
    assert at.place == "catacombs" and at.node["node"] == [0, 0]
    maze = await cat.load(store, CANARY)
    assert maze.nodes[(0, 0)].describe  # вход описан Ведущим при первом спуске
    run = await InteractiveStore(store).load_run(CANARY, "catacombs")
    assert run.status == STATUS_ACTIVE
    # Кадр входа ушёл гостю только после реплики (flush), и только один.
    assert notifier.photos == []
    await _frames(svc)
    assert len(notifier.photos) == 1 and len(link.generated) == 1
    entrance = (await cat.load(store, CANARY)).nodes[(0, 0)]
    assert entrance.image_id and entrance.file_id == "file-1"
    assert entrance.image_key == cat.node_frame_key(entrance)


async def test_go_again_to_the_hatch_resets_position_and_shows_no_frames(store):
    svc, notifier, link = _make(store)
    await _descend(svc, store)
    await _step(svc, store)  # куда-то зашли
    maze, state = await _vision(store)
    assert state.node != (0, 0) or state.passage
    shots = len(notifier.photos)
    text = await svc.tool_go(CANARY, CANARY, "catacombs")
    await _frames(svc)
    assert "к самому началу" in text
    maze, state = await _vision(store)
    assert state.node == (0, 0) and state.passage is None and state.room is None
    assert state.heading == maze.entry_heading
    assert len(notifier.photos) == shots  # «знакомой дорогой», без кадров


async def test_go_to_cellar_and_cabinet_end_the_walk(store):
    svc, _, _ = _make(store)
    await _descend(svc, store)
    assert (await InteractiveStore(store).load_run(CANARY, "catacombs")).status == STATUS_ACTIVE
    text = await svc.tool_go(CANARY, CANARY, "cellar")
    assert "в подвал" in text
    assert (await places.get_at(store, CANARY)).place == places.PLACE_CELLAR
    assert (await InteractiveStore(store).load_run(CANARY, "catacombs")).status == STATUS_DONE
    # Из подвала — в катакомбы и сразу домой.
    await svc.tool_go(CANARY, CANARY, "catacombs")
    assert await svc.tool_go(CANARY, CANARY, "cabinet") == underground.TOOL_GO_HOME
    assert (await places.get_at(store, CANARY)).in_cabinet
    assert (await InteractiveStore(store).load_run(CANARY, "catacombs")).status == STATUS_DONE


async def test_go_is_busy_during_another_scene_but_closes_finished_cellar(store):
    svc, _, _ = _make(store)
    await _open(svc)
    radio_run = Run(scenario="radio", chat_id=CANARY, user_id=CANARY, status=STATUS_ACTIVE)
    await InteractiveStore(store).save_run(radio_run)
    assert await svc.tool_go(CANARY, CANARY, "catacombs") == underground.TOOL_GO_BUSY
    radio_run.status = STATUS_DONE
    await InteractiveStore(store).save_run(radio_run)
    cellar_run = Run(
        scenario="cellar", chat_id=CANARY, user_id=CANARY, status=STATUS_ACTIVE, finale=True
    )
    await InteractiveStore(store).save_run(cellar_run)
    await _seed_maze(store)
    assert "к самому началу" in await svc.tool_go(CANARY, CANARY, "catacombs")
    assert (await InteractiveStore(store).load_run(CANARY, "cellar")).status == STATUS_DONE


async def test_cellar_hint_offers_catacombs_and_found_rooms_by_name(store):
    svc, _, _ = _make(store)
    await _open(svc)
    await _seed_maze(store)
    await svc.tool_go(CANARY, CANARY, "cellar")
    room = await svc._room(CANARY, CANARY)
    where = await svc._where_ru(room, CANARY, in_scene=False)
    assert "лаз в катакомбы" in where and "go" in where
    assert "проявочная" not in where  # ещё не найдена
    maze, _ = _maze_with_darkroom()
    maze.found.append("darkroom")
    await cat.save(store, maze)
    where = await svc._where_ru(room, CANARY, in_scene=False)
    assert "старая проявочная (darkroom)" in where


# --- walk: ход, описание, кадры ---


async def test_walk_not_in_catacombs_and_bad_direction(store):
    svc, _, _ = _make(store)
    assert await svc.tool_walk(CANARY, CANARY, "left") == underground.TOOL_WALK_NOT_HERE
    await _descend(svc, store)
    assert await svc.tool_walk(CANARY, CANARY, "вбок") == underground.TOOL_WALK_BAD_DIRECTION


async def test_walk_builds_describes_once_and_saves_position(store):
    svc, _, link = _make(store)
    await _descend(svc, store)
    link.describe_inputs.clear()
    maze_before, _ = await _vision(store)
    text = await _step(svc, store, frames=False)
    assert len(link.describe_inputs) == 1  # проход и узел — одним вопросом
    assert "passage" in link.describe_inputs[0] and "node" in link.describe_inputs[0]
    maze, state = await _vision(store)
    assert len(maze.passages) == 1 and state.node != (0, 0)
    passage = next(iter(maze.passages.values()))
    here = maze.nodes[state.node]
    assert passage.describe.startswith("Коридор") and here.describe.startswith("Перекрёсток")
    assert [s.name for s in passage.spots] == ["ящик у стены"]
    # Результат тула — что увидел Альфред (look) и как об этом сказать.
    assert "Перекрёсток" in text and "ящик у стены" not in text.split("Перекрёсток")[0]
    assert "ещё не хоженый проход" in text or "назад" in text
    at = await places.get_at(store, CANARY)
    assert at.node["node"] == list(state.node) and at.heading == state.heading
    assert maze_before.seed == maze.seed
    # Описание не перетирается: вернуться и пройти снова — Ведущего не зовём.
    link.describe_inputs.clear()
    await svc.tool_walk(CANARY, CANARY, "back")
    await svc.tool_walk(CANARY, CANARY, "back")  # развернулись у входа — снова туда
    assert link.describe_inputs == []
    maze2, _ = await _vision(store)
    assert maze2.passages[passage.id].describe == passage.describe


async def test_walk_director_failure_uses_fallback_descriptions(store):
    svc, _, link = _make(store)
    await _descend(svc, store)
    link.describe_fail = True
    text = await _step(svc, store, frames=False)
    maze, state = await _vision(store)
    passage = next(iter(maze.passages.values()))
    assert passage.describe == underground.FALLBACK_PASSAGE[0]
    assert maze.nodes[state.node].describe == underground.FALLBACK_NODE[0]
    assert underground.FALLBACK_NODE[0] in text


async def test_walk_without_node_link_still_works(store):
    svc, _, _ = _make(store)
    await _descend(svc, store)
    svc._get_node_link = lambda: None
    text = await _step(svc, store, frames=False)
    assert underground.FALLBACK_NODE[0] in text


async def test_frames_come_after_the_reply_and_are_remembered(store):
    svc, notifier, link = _make(store)
    await _descend(svc, store)
    shots, drawn = len(notifier.photos), len(link.generated)
    text = await svc.tool_walk(CANARY, CANARY, await _first_exit(store))
    assert "придёт" in text
    assert len(notifier.photos) == shots  # до flush ничего не ушло
    await _frames(svc)
    # Новый проход — один кадр: узел, куда пришли; коридор отдельно не рисуется.
    assert len(notifier.photos) == shots + 1 and len(link.generated) == drawn + 1
    maze, state = await _vision(store)
    passage = next(iter(maze.passages.values()))
    node = maze.nodes[state.node]
    assert not passage.image_id
    assert node.image_id and node.file_id and node.image_key == cat.node_frame_key(node)
    assert node.file_id == "file-2"
    request = link.generated[-1]
    assert request["light"] == places.UNDERGROUND_LIGHT_EN
    assert "Alfred" not in request["description"] and "loras" not in request
    assert "keep_key" not in request and "paste" not in request
    assert "light_move" not in request


async def test_known_places_are_shown_by_file_id_without_generation(store):
    svc, notifier, link = _make(store)
    await _descend(svc, store)
    await _step(svc, store)
    maze, state = await _vision(store)
    node_file = maze.nodes[state.node].file_id
    drawn, shots = len(link.generated), len(notifier.photos)
    await svc.tool_walk(CANARY, CANARY, "back")
    await _frames(svc)
    await svc.tool_walk(CANARY, CANARY, "back")
    await _frames(svc)
    assert len(link.generated) == drawn  # mycraft не будим
    assert len(notifier.photos) == shots + 2
    # Повторный показ узла — ровно тот же file_id.
    assert notifier.photos[-1][1] == node_file


async def test_node_is_redrawn_only_when_its_exits_change(store):
    svc, notifier, link = _make(store)
    await _descend(svc, store)
    await _step(svc, store)
    maze, state = await _vision(store)
    node = maze.nodes[state.node]
    assert not cat.node_needs_frame(node)
    node.exits["N" if "N" not in node.exits else "S"] = None  # выход открылся
    await cat.save(store, maze)
    drawn = len(link.generated)
    await svc.tool_walk(CANARY, CANARY, "back")
    await _frames(svc)
    await svc.tool_walk(CANARY, CANARY, "back")
    await _frames(svc)
    assert len(link.generated) == drawn + 1  # перерисован один узел
    maze, state = await _vision(store)
    node = maze.nodes[state.node]
    assert not cat.node_needs_frame(node) and node.image_key == cat.node_frame_key(node)


async def test_frames_per_turn_capped_and_skipped_when_camera_busy(store):
    svc, notifier, link = _make(store)
    await _descend(svc, store)
    svc._photo_busy.add(CANARY)
    await svc.tool_walk(CANARY, CANARY, await _first_exit(store))
    await svc.flush_frames(CANARY)
    await asyncio.gather(*list(svc._photo_tasks))
    maze, state = await _vision(store)
    assert all(p.image_id is None for p in maze.passages.values())  # ничего не запомнено
    svc._photo_busy.discard(CANARY)
    # discard_forms (ход отменён) сбрасывает очередь кадров.
    await svc.tool_walk(CANARY, CANARY, "back")
    svc.discard_forms(CANARY)
    drawn = len(link.generated)
    await _frames(svc)
    assert len(link.generated) == drawn


async def test_remember_frame_ignored_after_reset(store):
    svc, notifier, link = _make(store)
    await _descend(svc, store)
    await svc.tool_walk(CANARY, CANARY, await _first_exit(store))
    await svc.flush_frames(CANARY)
    await svc.debug_reset(CANARY, CANARY, "catacombs")
    await asyncio.gather(*list(svc._photo_tasks))
    assert not await store.get_state(f"catacombs:{CANARY}")


def test_plan_frames_one_node_frame_per_step():
    maze = Maze.new(1, 7)
    state = CatacombsState.at_entrance(maze)
    res = cat.walk(state, maze, "forward")
    # Новый проход — всё равно один кадр: узел, куда пришли (живая находка
    # 2026-10-15: кадр коридора перед ним выглядел вторым ответом).
    frames = underground.plan_frames(1, maze, res)
    assert [f.kind for f in frames] == ["node"]
    assert "Alfred" not in frames[0].room.canon.canon_en
    res.node.image_id, res.node.file_id = 6, "nf"
    res.node.image_key = cat.node_frame_key(res.node)
    assert underground.plan_frames(1, maze, res)[0].file_id == "nf"


def test_frame_canon_en_structure_and_spots():
    maze = Maze.new(1, 7)
    node = maze.nodes[(0, 0)]
    node.describe = "Сырой свод"
    node.spots = [cat.Spot("ниша с черепами")]
    en = underground.frame_canon_en(maze, node)
    assert "stone shaft" in en and "Сырой свод" in en and "ниша с черепами" in en
    state = CatacombsState.at_entrance(maze)
    res = cat.walk(state, maze, "forward")
    assert "narrow stone catacomb corridor" in underground.frame_canon_en(maze, res.passage)
    assert "lever" in underground.frame_canon_en(maze, res.passage, wall=True)


# --- сцена: заметка, Ведущий, простой ---


async def test_scene_note_and_director_input_after_walk(store):
    svc, _, link = _make(store)
    await _descend(svc, store)
    plan = await svc.before_turn(CANARY, CANARY, "Ну что там?", is_private=True)
    assert plan.scene is False or plan.scenario == "catacombs"
    assert plan.scenario == "catacombs" and plan.scene
    assert underground.CATACOMBS.scene_frame in plan.note
    assert "Где ты: Ты в катакомбах" in plan.note and "за окном" not in plan.note.lower()
    await svc.tool_walk(CANARY, CANARY, await _first_exit(store))
    await svc.after_turn(plan, "Иду, сэр.", dialogue_id=77)
    assert link.director_systems[-1] == underground.CATACOMBS.director_prompt
    last = link.director_inputs[-1]
    assert "Шаг (определён кодом" in last and "new_passage" in last
    run = await InteractiveStore(store).load_run(CANARY, "catacombs")
    assert run.status == STATUS_ACTIVE and run.turns_total == 1
    # Через ход старый шаг Ведущему уже не уходит.
    plan = await svc.before_turn(CANARY, CANARY, "И?", is_private=True)
    await svc.after_turn(plan, "Тихо.", dialogue_id=77)
    assert "Шаг (определён" not in link.director_inputs[-1]


async def test_director_idle_takes_alfred_home_from_catacombs(store):
    svc, _, link = _make(store)
    await _descend(svc, store)
    plan = await svc.before_turn(CANARY, CANARY, "Кстати, анекдот", is_private=True)
    link.director_replies = [json.dumps({"active": False})]
    await svc.after_turn(plan, "Ха.", dialogue_id=77)
    at = await places.get_at(store, CANARY)
    assert at.in_cabinet and at.returned_from == "из катакомб"
    assert (await InteractiveStore(store).load_run(CANARY, "catacombs")).status == STATUS_IDLE
    plan = await svc.before_turn(CANARY, CANARY, "Ну?", is_private=True)
    assert "вернулся из катакомб к себе в кабинет" in plan.note


async def test_guest_silence_returns_alfred_from_catacombs(store):
    clock = Clock()
    svc, _, _ = _make(store, clock=clock)
    await _descend(svc, store)
    clock.now += timedelta(hours=1, minutes=50)
    plan = await svc.before_turn(CANARY, CANARY, "Я тут", is_private=True)
    assert plan.scene  # ещё внизу
    clock.now += timedelta(hours=2, minutes=1)
    plan = await svc.before_turn(CANARY, CANARY, "Я снова тут", is_private=True)
    assert not plan.scene
    assert "вернулся из катакомб к себе в кабинет" in plan.note
    assert (await places.get_at(store, CANARY)).in_cabinet
    assert (await InteractiveStore(store).load_run(CANARY, "catacombs")).status == STATUS_IDLE
    # Заново спуститься можно: прогулка начинается свежей.
    await svc.tool_go(CANARY, CANARY, "catacombs")
    assert (await InteractiveStore(store).load_run(CANARY, "catacombs")).status == STATUS_ACTIVE


async def test_walk_up_goes_to_the_cellar_and_ends_the_walk(store):
    svc, _, _ = _make(store)
    await _descend(svc, store)
    await _step(svc, store)
    text = await svc.tool_walk(CANARY, CANARY, "up")
    assert "в подвал" in text.lower() and "лаз в катакомбы" in text
    assert (await places.get_at(store, CANARY)).place == places.PLACE_CELLAR
    assert (await InteractiveStore(store).load_run(CANARY, "catacombs")).status == STATUS_DONE
    plan = await svc.before_turn(CANARY, CANARY, "Привет", is_private=True)
    assert not plan.scene


# --- обыск и снимки в лабиринте ---


async def test_search_in_corridor_uses_spots_and_finds_nothing_special(store):
    svc, _, _ = _make(store, dice=Dice(0.99))
    await _descend(svc, store)
    await _step(svc, store)
    await svc.tool_walk(CANARY, CANARY, "back")  # снова у входа
    # Место поиска есть у прохода только на самом проходе — идём туда и встаём
    # перед стеной нельзя, поэтому обыскиваем то, что назвал Альфред.
    out = await svc.tool_search(CANARY, CANARY, "ящик у стены")
    assert "Ты обыскал" in out and "Ничего интересного" in out  # rng 0.99 — пусто
    assert underground.TOOL_FRAME_NOTE in out
    # Картинка обысканного места ждёт конца реплики.
    assert [f.kind for f in svc._ug().pending[CANARY].frames] == ["spot"]
    maze, state = await _vision(store)
    node = maze.nodes[state.node]
    assert [s.name for s in node.spots] == ["ящик у стены"] and node.spots[0].searched
    again = await svc.tool_search(CANARY, CANARY, "в ящике у стены")
    assert places.SEARCH_REPEAT_TEXT in again
    # Названное вне списка обыскивается как новое место.
    svc._rng = Dice(0.0)
    out = await svc.tool_search(CANARY, CANARY, "кучу костей")
    assert "Ничего ценного, лишь мелочь" in out
    maze, state = await _vision(store)
    assert {s.name for s in maze.nodes[state.node].spots} == {"ящик у стены", "кучу костей"}
    # Идущей прогулке итог виден Ведущему.
    run = await InteractiveStore(store).load_run(CANARY, "catacombs")
    assert "кучу костей" in run.world["search_outcome"]


async def test_search_frame_is_drawn_once_and_remembered(store):
    svc, notifier, link = _make(store)
    await _descend(svc, store)
    await _step(svc, store)
    await _frames(svc)
    drawn, shots = len(link.generated), len(notifier.photos)
    await svc.tool_search(CANARY, CANARY, "ящик у стены")
    await _frames(svc)
    assert len(link.generated) == drawn + 1 and len(notifier.photos) == shots + 1
    assert notifier.photos[-1][2] == "Ящик у стены"
    maze, state = await _vision(store)
    spot = maze.nodes[state.node].spots[-1]
    assert spot.image_id and spot.file_id
    # Повторный обыск — та же картинка по file_id, без генерации.
    await svc.tool_search(CANARY, CANARY, "ящик у стены")
    await _frames(svc)
    assert len(link.generated) == drawn + 1
    assert notifier.photos[-1][1] == spot.file_id


async def test_take_photo_in_corridor_shows_remembered_frame_and_spot_closeup(store):
    svc, notifier, link = _make(store)
    await _descend(svc, store)
    await _step(svc, store)
    maze, state = await _vision(store)
    node_file = maze.nodes[state.node].file_id
    drawn = len(link.generated)
    out = await svc.tool_take_photo(CANARY, CANARY, {})
    assert out == underground.TOOL_FRAME_STARTED  # не TOOL_PHOTO_STARTED: хода это не закончит
    await _frames(svc)
    assert len(link.generated) == drawn and notifier.photos[-1][1] == node_file
    # Крупный план места поиска на проходе: рисуется один раз и запоминается.
    await svc.tool_walk(CANARY, CANARY, "back")
    await _frames(svc)
    maze, state = await _vision(store)
    passage = next(iter(maze.passages.values()))
    # место поиска проходной (на узле его нет): берём место прохода
    node = maze.nodes[state.node]
    node.spots = [cat.Spot("ящик у стены")]
    await cat.save(store, maze)
    drawn = len(link.generated)
    assert await svc.tool_take_photo(CANARY, CANARY, {"focus": "ящик"}) == (
        underground.TOOL_FRAME_STARTED
    )
    await _frames(svc)
    assert len(link.generated) == drawn + 1
    assert link.generated[-1]["mode"] == "scene"
    maze, _ = await _vision(store)
    spot = maze.nodes[state.node].spots[0]
    assert spot.image_id and spot.file_id
    await svc.tool_take_photo(CANARY, CANARY, {"focus": "ящик"})
    await _frames(svc)
    assert len(link.generated) == drawn + 1 and notifier.photos[-1][1] == spot.file_id
    assert passage.id in maze.passages


async def test_selfie_in_corridor_uses_the_ordinary_path(store):
    svc, notifier, link = _make(store)
    await _descend(svc, store)
    out = await svc.tool_take_photo(CANARY, CANARY, {"selfie": True})
    assert out != underground.TOOL_FRAME_STARTED
    await asyncio.gather(*list(svc._photo_tasks))
    request = link.generated[-1]
    assert request["light"] == places.UNDERGROUND_LIGHT_EN and request["loras"]
    assert "catacomb" in request["description"]


# --- особые места ---


async def _at_darkroom_door(svc, store):
    await _open(svc)
    maze, side = _maze_with_darkroom()
    await cat.save(store, maze)
    await svc.tool_go(CANARY, CANARY, "catacombs")
    await _frames(svc)
    return maze, side


async def test_special_room_found_entered_searched_and_left(store):
    svc, notifier, link = _make(store, dice=Dice(0.99))
    await _at_darkroom_door(svc, store)
    text = await svc.tool_walk(CANARY, CANARY, "forward")
    await _frames(svc)
    maze, state = await _vision(store)
    assert maze.found == ["darkroom"] and "старая проявочная" in text
    shots = len(notifier.photos)
    door_image = maze.nodes[state.node].image_id  # кадр узла-двери нарисован при подходе
    text = await svc.tool_walk(CANARY, CANARY, "enter")
    assert "красный фонарь" in text and "search" in text
    maze, state = await _vision(store)
    assert state.room == "darkroom"
    room = await svc._room(CANARY, CANARY)
    assert room.canon.id == "darkroom" and not room.temp
    # Снимок комнаты — каждый раз новый и ни к чему не привязан.
    await _frames(svc)
    assert len(notifier.photos) == shots + 1
    assert "red safelight" in link.generated[-1]["description"]
    maze, _ = await _vision(store)
    assert maze.nodes[state.node].image_id == door_image  # снимок комнаты в лабиринт не пишется
    # Обыск в особом месте — обычный путь: итог решил код, Ведущий обставляет
    # его синхронно (Этап 59.7), обысканное помечено.
    link.director_replies = [json.dumps({"result": "В ванночке плёнка.", "stage": 0})]
    out = await svc.tool_search(CANARY, CANARY, "ванночки")
    assert "В ванночке плёнка." in out
    assert "фотоаппарат" in link.director_inputs[-1]
    assert places.normalize_where("ванночки") in await places.load_searched(
        store, CANARY, "darkroom"
    )
    link.director_replies = [json.dumps({"result": "Всё то же.", "stage": 0})]
    await svc.tool_search(CANARY, CANARY, "ванночки")
    # повторный обыск ничего не даёт
    assert places.SEARCH_REPEAT_TEXT in link.director_inputs[-1]
    # Выйти нельзя иначе как назад.
    assert "один путь" in await svc.tool_walk(CANARY, CANARY, "forward")
    left = await svc.tool_walk(CANARY, CANARY, "back")
    assert "вышел" in left
    assert (await _vision(store))[1].room is None


async def test_go_to_special_room_by_name_faces_the_door(store):
    svc, notifier, _ = _make(store)
    await _open(svc)
    assert await svc.tool_go(CANARY, CANARY, "darkroom") == underground.TOOL_GO_NOT_FOUND
    maze, side = _maze_with_darkroom()
    await cat.save(store, maze)
    assert await svc.tool_go(CANARY, CANARY, "старая проявочная") == underground.TOOL_GO_NOT_FOUND
    maze.found.append("darkroom")
    await cat.save(store, maze)
    shots = len(notifier.photos)
    text = await svc.tool_go(CANARY, CANARY, "darkroom")
    await _frames(svc)
    assert "старая проявочная" in text and "красный фонарь" in text
    assert len(notifier.photos) == shots  # без кадров по пути
    maze, state = await _vision(store)
    assert state.room == "darkroom" and state.node == maze.special_nodes["darkroom"]
    assert cat.to_relative(state.heading, side) == "forward"  # выход за спиной
    assert (await svc._room(CANARY, CANARY)).canon.id == "darkroom"
    # Выход из комнаты — в тот же тупик; «в катакомбы» всегда к лазу.
    assert "вышел" in await svc.tool_walk(CANARY, CANARY, "back")
    await svc.tool_go(CANARY, CANARY, "catacombs")
    maze, state = await _vision(store)
    assert state.node == (0, 0) and state.room is None
    # Из подвала — тоже по имени.
    await svc.tool_go(CANARY, CANARY, "cellar")
    assert "старая проявочная" in (await svc.tool_go(CANARY, CANARY, "проявочная"))
    assert (await places.get_at(store, CANARY)).place == "catacombs"


async def test_special_room_photo_is_not_remembered(store):
    svc, notifier, link = _make(store)
    await _at_darkroom_door(svc, store)
    await svc.tool_walk(CANARY, CANARY, "forward")
    await svc.tool_walk(CANARY, CANARY, "enter")
    await _frames(svc)
    drawn = len(link.generated)
    assert await svc.tool_take_photo(CANARY, CANARY, {})
    await asyncio.gather(*list(svc._photo_tasks))
    assert len(link.generated) == drawn + 1  # каждый раз заново
    assert await svc.tool_take_photo(CANARY, CANARY, {})
    await asyncio.gather(*list(svc._photo_tasks))
    assert len(link.generated) == drawn + 2


# --- отладка и сброс ---


class FakeMessage:
    def __init__(self, user_id):
        self.chat = type("Chat", (), {"id": user_id})()
        self.from_user = type("User", (), {"id": user_id})()
        self.answers: list[str] = []

    async def answer(self, text, **_kw):
        self.answers.append(text)


class FakeCommand:
    def __init__(self, args):
        self.args = args


async def test_debug_where_shows_node_heading_and_explored_count(store):
    svc, _, _ = _make(store)
    await _descend(svc, store)
    await _step(svc, store)
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("where"), svc)
    raw, _, extra = message.answers[0].partition("\n")
    assert json.loads(raw)["place"] == "catacombs"
    maze, state = await _vision(store)
    assert f"узел ({state.node[0]},{state.node[1]}), heading {state.heading}" in extra
    assert f"разведано узлов {len(maze.nodes)}" in extra
    # Вне катакомб ответ — прежний чистый JSON.
    await svc.tool_walk(CANARY, CANARY, "up")
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("where"), svc)
    assert json.loads(message.answers[0])["place"] == "cellar"


async def test_reset_catacombs_clears_position_maze_run_and_special_rooms(store):
    svc, _, _ = _make(store)
    await _at_darkroom_door(svc, store)
    await svc.tool_walk(CANARY, CANARY, "forward")
    await svc.tool_walk(CANARY, CANARY, "enter")
    await svc.tool_search(CANARY, CANARY, "ванночки")
    await places.save_room(store, await places.load_room(store, CANARY, "darkroom"))
    await svc._state.set_effect(places.EFFECT_CATACOMBS_OPEN, CANARY, "1")
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("reset catacombs"), svc)
    keys = await store.state_keys("")
    assert not [k for k in keys if k.startswith(("alfred_at", "catacombs:", "location"))]
    assert not [k for k in keys if k.startswith("interactive_run") and k.endswith("catacombs")]
    assert not [k for k in keys if "darkroom" in k]
    # Флаг открытия лаза принадлежит подвалу и остаётся.
    assert f"user_effect:catacombs_open:{CANARY}" in keys
    assert (await svc._room(CANARY, CANARY)).place == "cabinet"


# --- подключение к ходу /ai ---


async def test_flush_forms_sends_frames_after_the_reply(store):
    svc, notifier, link = _make(store)
    await _descend(svc, store)
    shots = len(notifier.photos)
    plan = await svc.before_turn(CANARY, CANARY, "Налево", is_private=True)
    await svc.tool_walk(CANARY, CANARY, await _first_exit(store))
    await svc.after_turn(plan, "Иду.", dialogue_id=77)
    assert len(notifier.photos) == shots  # реплика ещё не ушла — кадров нет
    await svc.flush_forms(CANARY, plan, dialogue_id=77)
    await asyncio.gather(*list(svc._photo_tasks))
    assert len(notifier.photos) == shots + 1  # один кадр на шаг


async def test_canary_handlers_walk_through_the_toolkit(store):
    svc, _, _ = _make(store)
    await _descend(svc, store)
    kit = ai_tools.tools_for(Subscription(name="me", chat_id=1, allowed_commands=frozenset({"*"})))
    ctx = _ctx(svc, CANARY)
    out = await kit.handlers["walk"](ctx, {"direction": await _first_exit(store)})
    assert "Ты пошёл" in out
    assert "Знакомой дорогой" in await kit.handlers["go"](ctx, {"place": "cellar"})
