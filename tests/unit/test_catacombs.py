"""Топология катакомб (Этап 59.4–59.5): детерминизм, инварианты на длинных
блужданиях, рычаги, укорачивание, особые места, JSON."""

from __future__ import annotations

import random

import pytest

from sa_home_bot.bot.interactives import catacombs as cat
from sa_home_bot.bot.interactives.catacombs import (
    SPECIAL_ROOMS,
    CatacombsState,
    Maze,
    Outcome,
    walk,
)

SEEDS = (1, 7, 2024, 987654321)


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get_state(self, key: str) -> str | None:
        return self.data.get(key)

    async def set_state(self, key: str, value: str) -> None:
        self.data[key] = value


def explore_step(maze: Maze, state: CatacombsState, rng: random.Random) -> cat.WalkResult:
    """Один шаг «блуждающего гостя»: в основном по выходам, иногда глупости."""
    roll = rng.random()
    if roll < 0.01:
        return walk(state, maze, "up")
    if roll < 0.04:
        return walk(state, maze, rng.choice(cat.WALK_DIRECTIONS))
    vision = cat.look(state, maze)
    if vision.kind == "room":
        return walk(state, maze, "back")
    if vision.kind == "wall":
        if vision.lever and rng.random() < 0.8:
            return walk(state, maze, "lever")
        return walk(state, maze, rng.choice(["forward", "back"]))
    if vision.room and rng.random() < 0.3:
        return walk(state, maze, "enter")
    unexplored = [e for e in vision.exits if not e.explored]
    pool = unexplored if unexplored and rng.random() < 0.7 else vision.exits
    return walk(state, maze, rng.choice(pool).rel)


def check_invariants(maze: Maze) -> None:
    open_exits = {
        (n.cell, s) for n in maze.nodes.values() for s, pid in n.exits.items() if pid is None
    }
    assert open_exits, "лабиринт захлопнулся: нет неразведанных выходов"
    assert open_exits == maze._open

    interior: dict[cat.Cell, str] = {}
    for pid, p in maze.passages.items():
        assert pid == p.id
        assert 1 <= p.length <= 3
        assert p.a in maze.nodes and p.b in maze.nodes
        assert p.b == cat._step(p.a, p.a_side, p.length)
        assert p.b_side == cat.OPPOSITE[p.a_side]
        assert maze.nodes[p.a].exits[p.a_side] == pid
        far = maze.nodes[p.b].exits.get(p.b_side, "нет")
        if p.sealed:
            assert far == "нет", "за стеной с рычагом у узла не должно быть выхода"
        else:
            assert far == pid
        for cell in p.interior():
            assert cell not in maze.nodes, "проход пересёк узел"
            assert cell not in interior, "проходы пересеклись"
            interior[cell] = pid
    assert set(interior) == maze._interior
    # каждый выход со ссылкой указывает на существующий проход
    for n in maze.nodes.values():
        for s, pid in n.exits.items():
            if pid is not None:
                p = maze.passages[pid]
                assert (p.a, p.a_side) == (n.cell, s) or (p.b, p.b_side) == (n.cell, s)
    # неразведанный выход не замурован: его первая клетка не внутри прохода
    for cell, side in maze._open:
        assert cat._step(cell, side) not in maze._interior


def wander(
    seed: int, steps: int, *, check_every: int = 25
) -> tuple[Maze, CatacombsState, list[cat.WalkResult]]:
    maze = Maze.new(1, seed)
    state = CatacombsState.at_entrance(maze)
    rng = random.Random(seed)
    results = []
    for i in range(steps):
        results.append(explore_step(maze, state, rng))
        assert maze.unexplored > 0
        if i % check_every == 0:
            check_invariants(maze)
    check_invariants(maze)
    return maze, state, results


# --- детерминизм ---


def test_same_seed_same_maze():
    a, _, _ = wander(42, 400)
    b, _, _ = wander(42, 400)
    assert a.to_json() == b.to_json()


def test_different_seeds_differ():
    a, _, _ = wander(1, 200)
    b, _, _ = wander(2, 200)
    assert a.to_json() != b.to_json()


def test_node_exits_depend_only_on_seed_and_cell():
    # Вход (0, 0) — функция одного зерна.
    assert Maze.new(1, 5).nodes[(0, 0)].exits == Maze.new(99, 5).nodes[(0, 0)].exits
    assert Maze.new(1, 5).entry_heading == Maze.new(1, 5).entry_heading


# --- инварианты ---


@pytest.mark.parametrize("seed", SEEDS)
def test_long_wander_keeps_invariants(seed):
    maze, _, _ = wander(seed, 3000)
    assert len(maze.nodes) > 40


def test_new_maze_has_entrance_and_open_exits():
    maze = Maze.new(1, 3)
    assert set(maze.nodes) == {(0, 0)}
    assert 2 <= len(maze.nodes[(0, 0)].exits) <= 3
    assert maze.entry_heading in maze.nodes[(0, 0)].exits
    check_invariants(maze)


def test_walking_known_passage_does_not_generate():
    maze = Maze.new(1, 11)
    state = CatacombsState.at_entrance(maze)
    first = walk(state, maze, "forward")
    assert first.new_passage and first.outcome in (Outcome.NEW_PASSAGE, Outcome.CONNECTED)
    snapshot = maze.to_json()
    back = walk(state, maze, "back")
    assert back.outcome == Outcome.MOVED and not back.new_passage
    again = walk(state, maze, "back")  # снова вперёд по тому же проходу (развернулись)
    assert again.outcome == Outcome.MOVED
    assert maze.to_json() == snapshot
    assert state.node == first.node.cell


# --- рычаг ---


def find_lever_wall(seed: int, steps: int = 4000):
    maze = Maze.new(1, seed)
    state = CatacombsState.at_entrance(maze)
    rng = random.Random(seed)
    for _ in range(steps):
        res = explore_step(maze, state, rng)
        if res.outcome == Outcome.LEVER_WALL and res.new_passage:
            return maze, state, res
    return None


def test_lever_appears_and_opens_exit():
    found = None
    for seed in SEEDS:
        found = find_lever_wall(seed)
        if found:
            break
    assert found, "за 4 сида по 4000 шагов не встретилась стена с рычагом"
    maze, state, res = found
    passage, old = res.passage, res.linked_node
    assert passage.sealed and state.passage == passage.id
    assert passage.b_side not in old.exits
    key_before = cat.node_frame_key(old)
    vision = cat.look(state, maze)
    assert vision.kind == "wall" and vision.lever

    # вперёд нельзя, пока рычаг не потянут
    assert walk(state, maze, "forward").outcome == Outcome.BLOCKED_BY_LEVER
    assert walk(state, maze, "left").outcome == Outcome.NO_EXIT

    pulled = walk(state, maze, "lever")
    assert pulled.outcome == Outcome.LEVER_PULLED
    assert not passage.sealed
    assert old.exits[passage.b_side] == passage.id
    assert cat.node_frame_key(old) != key_before
    check_invariants(maze)

    assert walk(state, maze, "lever").outcome == Outcome.NO_LEVER
    through = walk(state, maze, "forward")
    assert through.outcome == Outcome.MOVED and state.node == old.cell and state.passage is None
    # а обратно через открытый проход — к исходному узлу
    back = walk(state, maze, "back")
    assert back.outcome == Outcome.MOVED and state.node == passage.a


def test_lever_wall_back_returns_to_origin():
    found = None
    for seed in SEEDS:
        found = find_lever_wall(seed)
        if found:
            break
    assert found
    maze, state, res = found
    origin = res.passage.a
    heading = state.heading
    back = walk(state, maze, "back")
    assert back.outcome == Outcome.MOVED
    assert state.node == origin and state.passage is None
    assert state.heading == cat.OPPOSITE[heading]
    assert res.passage.sealed  # рычаг не тронут


def test_lever_without_wall():
    maze = Maze.new(1, 3)
    state = CatacombsState.at_entrance(maze)
    assert walk(state, maze, "lever").outcome == Outcome.NO_LEVER


# --- укорачивание ---


def test_passages_get_shortened_without_crossing():
    shortened = 0
    for seed in SEEDS:
        maze = Maze.new(1, seed)
        state = CatacombsState.at_entrance(maze)
        rng = random.Random(seed)
        for _ in range(2500):
            before = set(maze.passages)
            node = maze.nodes[state.node]
            res = explore_step(maze, state, rng)
            for pid in set(maze.passages) - before:
                p = maze.passages[pid]
                want = cat._rng(seed, "len", p.a[0], p.a[1], p.a_side).choices(
                    cat.PASSAGE_LEN_CHOICES, cat.PASSAGE_LEN_WEIGHTS
                )[0]
                assert p.length <= want
                if p.length < want:
                    shortened += 1
            del node, res
        check_invariants(maze)
    assert shortened > 0


def test_shorten_to_last_free_cell_manual():
    """Ручная сборка: поперёк пути лежит чужой проход, новый укорачивается."""
    maze = Maze.new(1, 5)
    # убираем стартовые выходы и строим свою картину
    entrance = maze.nodes[(0, 0)]
    for s in list(entrance.exits):
        del entrance.exits[s]
    maze._open.clear()
    # горизонтальный проход (-1,2)—(1,2), клетка (0,2) внутри; (0,1) свободна
    left = cat.Node(x=-1, y=2)
    right = cat.Node(x=1, y=2)
    maze.nodes[left.cell] = left
    maze.nodes[right.cell] = right
    p = cat.Passage(a=left.cell, a_side="E", b=right.cell, b_side="W")
    maze.passages[p.id] = p
    left.exits["E"] = p.id
    right.exits["W"] = p.id
    maze._interior.add((0, 2))
    # выход из входа на север: путь (0,1) свободна, (0,2) занята -> узел в (0,1)
    maze._set_exit(entrance, "N", None)
    state = CatacombsState(node=(0, 0), heading="N")
    for want_seed in range(200):  # длина из ГСЧ может быть 1 — перебираем зёрна
        maze.seed = want_seed
        if (
            cat._rng(want_seed, "len", 0, 0, "N").choices(
                cat.PASSAGE_LEN_CHOICES, cat.PASSAGE_LEN_WEIGHTS
            )[0]
            >= 2
        ):
            break
    res = walk(state, maze, "forward")
    assert res.outcome == Outcome.NEW_PASSAGE
    assert res.passage.length == 1 and state.node == (0, 1)
    assert (0, 2) not in maze.nodes
    check_invariants(maze)


# --- относительные направления ---


def test_relative_directions_table():
    assert cat.to_absolute("N", "forward") == "N"
    assert cat.to_absolute("N", "left") == "W"
    assert cat.to_absolute("N", "right") == "E"
    assert cat.to_absolute("N", "back") == "S"
    assert cat.to_absolute("E", "left") == "N"
    assert cat.to_absolute("E", "right") == "S"
    assert cat.to_absolute("S", "left") == "E"
    assert cat.to_absolute("W", "forward") == "W"
    for h in cat.SIDES:
        for rel in cat.RELATIVE:
            assert cat.to_relative(h, cat.to_absolute(h, rel)) == rel


def test_walk_uses_heading():
    maze = Maze.new(1, 77)
    state = CatacombsState.at_entrance(maze)
    node = maze.nodes[(0, 0)]
    for rel in ("left", "right", "forward", "back"):
        st = CatacombsState(node=(0, 0), heading="E")
        side = cat.to_absolute("E", rel)
        m = Maze.from_json(maze.to_json())
        res = walk(st, m, rel)
        if side in node.exits:
            assert res.abs_side == side and res.moved or res.outcome == Outcome.LEVER_WALL
        else:
            assert res.outcome == Outcome.NO_EXIT and res.abs_side == side
    del state


def test_heading_after_move_is_travel_side():
    maze = Maze.new(1, 8)
    state = CatacombsState.at_entrance(maze)
    res = walk(state, maze, "forward")
    assert res.abs_side == maze.entry_heading
    if res.moved:
        assert state.heading == maze.entry_heading


def test_look_reports_relative_exits():
    maze = Maze.new(1, 8)
    state = CatacombsState.at_entrance(maze)
    vision = cat.look(state, maze)
    assert vision.kind == "node" and vision.can_up
    assert {e.side for e in vision.exits} == set(maze.nodes[(0, 0)].exits)
    for e in vision.exits:
        assert e.rel == cat.to_relative(state.heading, e.side) and not e.explored
    assert "прямо" in vision.text_ru().lower()
    walk(state, maze, "forward")
    # после перехода разведанный путь назад виден как explored
    back = [e for e in cat.look(state, maze).exits if e.rel == "back"]
    if back:
        assert back[0].explored and back[0].passage_id


# --- особые места ---


def test_special_rooms_depth_and_catalog_order():
    seen_any = False
    for seed in SEEDS:
        maze = Maze.new(1, seed)
        state = CatacombsState.at_entrance(maze)
        rng = random.Random(seed)
        for _ in range(3000):
            res = explore_step(maze, state, rng)
            if res.room and res.room_found_now:
                seen_any = True
                node = res.node
                assert node.special == res.room.id
                assert (maze.depth(node.cell) or 0) >= cat.SPECIAL_MIN_DEPTH
                assert len(node.exits) == 1, "узел особого места — тупик"
        ids = list(maze.special_nodes)
        assert ids == [r.id for r in SPECIAL_ROOMS[: len(ids)]]
        assert maze.found == [i for i in ids if i in maze.found]
        # найденные — в порядке каталога: нельзя найти вторую, не расставив первую
        assert [r.id for r in cat.found_rooms(maze)] == maze.found
    assert seen_any
    assert SPECIAL_ROOMS[0].id == "darkroom"


def test_enter_and_leave_room():
    for seed in SEEDS:
        maze = Maze.new(1, seed)
        state = CatacombsState.at_entrance(maze)
        rng = random.Random(seed)
        for _ in range(4000):
            res = explore_step(maze, state, rng)
            if res.room and res.moved and state.room is None:
                break
        else:
            continue
        break
    else:
        pytest.fail("особое место не нашлось")

    # вернёмся в узел особого места «знакомой дорогой» и зайдём
    room_id = maze.found[0]
    state = CatacombsState.at_entrance(maze)
    assert cat.go_to_room(state, maze, room_id)
    assert state.room == room_id
    state.room = None
    state.node = maze.special_nodes[room_id]
    entered = walk(state, maze, "enter")
    assert (
        entered.outcome == Outcome.ENTERED_ROOM
        and entered.room.id == room_id
        and state.room == room_id
    )
    assert walk(state, maze, "forward").outcome == Outcome.NO_EXIT
    assert cat.look(state, maze).kind == "room"
    left = walk(state, maze, "back")
    assert left.outcome == Outcome.LEFT_ROOM and state.room is None
    up = walk(state, maze, "up")
    assert up.outcome == Outcome.ASCENDED and state.node == (0, 0)


def test_enter_without_room():
    maze = Maze.new(1, 3)
    state = CatacombsState.at_entrance(maze)
    assert walk(state, maze, "enter").outcome == Outcome.NO_ROOM


def test_go_to_unfound_room_fails():
    maze = Maze.new(1, 3)
    state = CatacombsState.at_entrance(maze)
    assert not cat.go_to_room(state, maze, "darkroom")
    assert not cat.go_to_room(state, maze, "nonexistent")


def test_up_and_reset_position():
    maze = Maze.new(1, 9)
    state = CatacombsState.at_entrance(maze)
    for _ in range(5):
        walk(state, maze, "forward")
    cat.reset_position(state, maze)
    assert state.node == (0, 0) and state.heading == maze.entry_heading
    assert state.passage is None and state.room is None
    walk(state, maze, "forward")
    res = walk(state, maze, "up")
    assert res.outcome == Outcome.ASCENDED and state.node == (0, 0)


def test_unknown_direction_raises():
    maze = Maze.new(1, 9)
    with pytest.raises(ValueError):
        walk(CatacombsState.at_entrance(maze), maze, "sideways")


# --- JSON и хранение ---


def test_json_roundtrip_and_continuation():
    maze, state, _ = wander(5, 600)
    # дописываем описания, картинку и рычаг-состояние
    node = maze.nodes[state.node]
    cat.set_description(node, "Сырой коридор, на стене факел.", ["ящик", "ниша"])
    cat.set_image(node, 7, "AgAD")
    raw = maze.to_json()
    clone = Maze.from_json(raw)
    assert clone == maze
    assert clone.to_json() == raw
    check_invariants(clone)
    st2 = CatacombsState.from_dict(state.to_dict())
    assert st2 == state
    # дальнейшее блуждание в оригинале и копии идёт одинаково
    ra, rb = random.Random(1), random.Random(1)
    for _ in range(300):
        explore_step(maze, state, ra)
        explore_step(clone, st2, rb)
    assert clone.to_json() == maze.to_json() and st2 == state


async def test_load_save_roundtrip():
    store = FakeStore()
    fresh = await cat.load(store, 5)
    assert fresh.user_id == 5 and len(fresh.nodes) == 1
    state = CatacombsState.at_entrance(fresh)
    walk(state, fresh, "forward")
    await cat.save(store, fresh)
    assert "catacombs:5" in store.data
    again = await cat.load(store, 5)
    assert again == fresh
    store.data["catacombs:5"] = "{мусор"
    broken = await cat.load(store, 5)
    assert len(broken.nodes) == 1


# --- описание и кадры ---


def test_description_is_remembered_once_and_frame_key_tracks_exits():
    found = None
    for seed in SEEDS:
        found = find_lever_wall(seed)
        if found:
            break
    assert found
    maze, state, res = found
    old = res.linked_node
    assert cat.needs_describe(old) and cat.node_needs_frame(old)
    cat.set_description(old, "Развилка, на полу кости.", ["саркофаг"])
    cat.set_description(old, "другое", ["ещё"])  # повторно не перетирает
    assert old.describe == "Развилка, на полу кости." and [s.name for s in old.spots] == [
        "саркофаг"
    ]
    cat.set_image(old, 1, "fid")
    assert not cat.node_needs_frame(old)
    walk(state, maze, "lever")
    assert cat.node_needs_frame(old), "открыт новый выход — кадр узла надо перерисовать"
    cat.set_image(old, 2, "fid2")
    assert not cat.node_needs_frame(old)

    passage = res.passage
    assert cat.passage_needs_frame(passage)
    cat.set_image(passage, 3)
    assert not cat.passage_needs_frame(passage)


def test_frame_key_includes_exit_set():
    n = cat.Node(x=1, y=2, exits={"N": None, "E": "x"})
    k1 = cat.node_frame_key(n)
    n.exits["S"] = None
    assert cat.node_frame_key(n) != k1


def test_walk_result_to_dict_is_plain():
    maze = Maze.new(1, 4)
    state = CatacombsState.at_entrance(maze)
    d = walk(state, maze, "forward").to_dict()
    assert d["direction"] == "forward" and isinstance(d["outcome"], str)
