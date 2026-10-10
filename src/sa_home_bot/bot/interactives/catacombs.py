"""Катакомбы Альфреда (Этап 59.4–59.5) — топология лабиринта. Только код:
ни модели, ни картинок, ни Telegram, ни движка. Модель потом лишь описывает
то, что решил этот модуль; топологию она не придумывает.

Устройство (решения плана 59.4):

- **Сетка.** Плоская, целые координаты, ось Y растёт на север (``N = (0, +1)``).
  Вход у лаза — ``(0, 0)``. **Узел** — перекрёсток, поворот, тупик или вход
  в особое место. **Проход** — прямой отрезок по одной оси между двумя узлами,
  длина 1–3 клетки (длина = расстояние между узлами; клетки строго между
  ними — «внутренность» прохода).
- **Выходы** узла — абсолютные стороны света ``N/E/S/W``. Выход со значением
  ``None`` — неразведанный: проход ещё не построен. Узел получает выходы при
  создании (первое посещение), от ГСЧ, заведённого из зерна гостя и координат
  узла: одно зерно и тот же порядок ходов дают тот же лабиринт.
- **Проход строится, когда Альфред в него входит** (``walk``). Выбирается длина
  1–3; клетки по пути проверяются одна за другой. Дошли до известного узла —
  соединяемся с ним (есть выход с этой стороны — просто соединяем; нет — проход
  упирается в стену с **рычагом**, ``sealed``, и ``lever`` открывает у старого
  узла выход). Наткнулись на чужой проход или на клетку, зарезервированную
  выходом другого узла, — проход **укорачивается** до последней свободной клетки,
  там возникает новый узел. Пересечений проходов не бывает.
- **Резерв клетки.** Первая клетка каждого неразведанного выхода считается
  занятой этим выходом: чужие проходы через неё не идут (исключение — проход
  по той же линии прямо в этот узел: он его соединит). Так выход никогда не
  оказывается «замурован» чужим проходом.
- **Лабиринт не захлопывается.** После каждого построения проверяется, что
  неразведанный выход остался хотя бы один. Если нет — он добавляется у
  ближайшего (по графу) узла со свободной стороной (``_ensure_open``).
- **Направления** хранятся абсолютными; Альфреду и гостю отдаются
  относительными (``forward|right|back|left``) от ``heading`` — см.
  ``to_relative``/``to_absolute``. После прохода по проходу ``heading`` — сторона
  движения; ``back`` — развернуться и идти туда, откуда пришёл.
- **Особые места** (``SPECIAL_ROOMS``) ставит код: не ближе
  ``SPECIAL_MIN_DEPTH`` узлов от входа (по графу), шанс на новом узле растёт
  с числом разведанных узлов, порядок — по каталогу. Узел особого места —
  тупик, ``enter`` входит в комнату. Саму комнату (канон, обыск) ведёт движок.
- **Описание и картинки запоминаются** на узле и на проходе (``describe``,
  ``spots``, ``image_id``, ``file_id``). Кадр узла рисуется заново только при
  смене набора выходов (рычаг открыл выход) — ключ кадра ``node_frame_key``
  включает выходы, ``node_needs_frame`` сравнивает его с ключом нарисованного.
  Сам кадр здесь не рисуется.

Состояние Альфреда под землёй — ``CatacombsState`` (узел, ``heading``, «в
проходе у рычага», «в комнате»); хранение — снаружи (``alfred_at``, 59.1),
здесь только ``to_dict``/``from_dict``. Лабиринт хранится в ``app_state``
``catacombs:<гость>`` (``load``/``save``, как cabinet.py).
"""

from __future__ import annotations

import json
import random
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sa_home_bot.db.store import Store

Cell = tuple[int, int]

STATE_PREFIX = "catacombs"
VERSION = 1

# --- стороны света и относительные направления ---

SIDES = ("N", "E", "S", "W")  # по часовой стрелке
VECTORS: dict[str, Cell] = {"N": (0, 1), "E": (1, 0), "S": (0, -1), "W": (-1, 0)}
OPPOSITE = {"N": "S", "E": "W", "S": "N", "W": "E"}
# Индекс = (сторона - heading) mod 4 по часовой.
RELATIVE = ("forward", "right", "back", "left")
RELATIVE_RU = {"forward": "прямо", "right": "направо", "back": "назад", "left": "налево"}
WALK_DIRECTIONS = (*RELATIVE, "lever", "enter", "up")

# --- параметры генерации ---

PASSAGE_LEN_CHOICES = (1, 2, 3)
PASSAGE_LEN_WEIGHTS = (3, 4, 3)
# Сколько выходов, кроме того, через который пришли, у нового узла (вес).
EXTRA_EXITS_CHOICES = (0, 1, 2, 3)
EXTRA_EXITS_WEIGHTS = (22, 42, 26, 10)
ENTRANCE_EXITS_CHOICES = (2, 3)
ENTRANCE_EXITS_WEIGHTS = (7, 3)

SPECIAL_MIN_DEPTH = 4
SPECIAL_CHANCE_PER_NODE = 0.04
SPECIAL_CHANCE_MAX = 0.5


@dataclass(frozen=True)
class SpecialRoom:
    id: str
    name: str


# Порядок — порядок появления. Проявочная первой (плана 59.5).
SPECIAL_ROOMS: tuple[SpecialRoom, ...] = (
    SpecialRoom("darkroom", "старая проявочная"),
    SpecialRoom("laboratory", "заброшенная лаборатория"),
    SpecialRoom("cave", "пещера"),
    SpecialRoom("wine_cellar", "винный погреб"),
)


def special_room(room_id: str) -> SpecialRoom | None:
    return next((r for r in SPECIAL_ROOMS if r.id == room_id), None)


def to_relative(heading: str, side: str) -> str:
    """Абсолютная сторона → относительная от ``heading``."""
    return RELATIVE[(SIDES.index(side) - SIDES.index(heading)) % 4]


def to_absolute(heading: str, relative: str) -> str:
    """Относительное направление (``forward|right|back|left``) → абсолютная сторона."""
    return SIDES[(SIDES.index(heading) + RELATIVE.index(relative)) % 4]


def _step(cell: Cell, side: str, k: int = 1) -> Cell:
    dx, dy = VECTORS[side]
    return (cell[0] + dx * k, cell[1] + dy * k)


def _rng(seed: int, tag: str, *parts: object) -> random.Random:
    # Строковое зерно Random детерминировано между процессами (в отличие от hash()).
    return random.Random(":".join(str(p) for p in (seed, tag, *parts)))


# --- данные ---


@dataclass
class Spot:
    """Место поиска (ящик, ниша, саркофаг…) на узле или проходе."""

    name: str
    searched: bool = False
    image_id: int | None = None
    file_id: str | None = None


@dataclass
class Described:
    """Всё, что Ведущий придумывает при первом посещении и что запоминается."""

    describe: str | None = None
    spots: list[Spot] = field(default_factory=list)
    image_id: int | None = None
    file_id: str | None = None


@dataclass(kw_only=True)
class Node(Described):
    x: int
    y: int
    # сторона -> id прохода; None — неразведанный выход
    exits: dict[str, str | None] = field(default_factory=dict)
    special: str | None = None
    # ключ кадра, под которым нарисована картинка (см. node_frame_key)
    image_key: str | None = None

    @property
    def cell(self) -> Cell:
        return (self.x, self.y)


@dataclass(kw_only=True)
class Passage(Described):
    a: Cell
    a_side: str  # сторона узла a, откуда проход начинается
    b: Cell
    b_side: str  # сторона узла b, куда проход приходит (= OPPOSITE[a_side])
    # Упирается в стену с рычагом у конца b: у узла b нет выхода b_side
    # до тех пор, пока рычаг не потянут.
    sealed: bool = False

    @property
    def id(self) -> str:
        return passage_id(self.a, self.a_side)

    @property
    def length(self) -> int:
        return abs(self.b[0] - self.a[0]) + abs(self.b[1] - self.a[1])

    def other(self, cell: Cell) -> Cell:
        return self.b if cell == self.a else self.a

    def interior(self) -> list[Cell]:
        return [_step(self.a, self.a_side, k) for k in range(1, self.length)]


def passage_id(a: Cell, a_side: str) -> str:
    return f"{a[0]},{a[1]}{a_side}"


def _cell_key(cell: Cell) -> str:
    return f"{cell[0]},{cell[1]}"


def _parse_cell(raw: str) -> Cell:
    x, y = raw.split(",")
    return (int(x), int(y))


def _place_to_dict(p: Described) -> dict[str, Any]:
    return {
        "describe": p.describe,
        "spots": [
            {"name": s.name, "searched": s.searched, "image_id": s.image_id, "file_id": s.file_id}
            for s in p.spots
        ],
        "image_id": p.image_id,
        "file_id": p.file_id,
    }


def _place_from_dict(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "describe": d.get("describe"),
        "spots": [
            Spot(
                name=str(s["name"]),
                searched=bool(s.get("searched")),
                image_id=s.get("image_id"),
                file_id=s.get("file_id"),
            )
            for s in d.get("spots") or []
        ],
        "image_id": d.get("image_id"),
        "file_id": d.get("file_id"),
    }


# --- результат хода и «что видно» ---


class Outcome(StrEnum):
    MOVED = "moved"  # прошли по уже известному проходу
    NEW_PASSAGE = "new_passage"  # построен новый проход, пришли в новый узел
    CONNECTED = "connected"  # построен новый проход в уже известный узел
    LEVER_WALL = "lever_wall"  # проход упёрся в стену с рычагом, Альфред в проходе
    LEVER_PULLED = "lever_pulled"  # рычаг потянут, выход открыт
    ENTERED_ROOM = "entered_room"  # вошли в особое место
    LEFT_ROOM = "left_room"  # вышли из особого места в его узел
    ASCENDED = "ascended"  # наверх, в подвал
    NO_EXIT = "no_exit"  # туда нельзя: стена
    BLOCKED_BY_LEVER = "blocked_by_lever"  # вперёд нельзя: стена с рычагом
    NO_LEVER = "no_lever"  # рычага здесь нет
    NO_ROOM = "no_room"  # входа в комнату здесь нет


@dataclass
class WalkResult:
    outcome: Outcome
    direction: str  # что просили (left|right|forward|back|lever|enter|up)
    abs_side: str | None = None  # куда реально шли, абсолютная сторона
    node: Node | None = None  # узел, где Альфред сейчас (или откуда стоит в проходе)
    passage: Passage | None = None  # пройденный / построенный / упёршийся проход
    linked_node: Node | None = None  # старый узел за стеной / с открытым рычагом выходом
    new_passage: bool = False
    new_node: bool = False
    room: SpecialRoom | None = None  # особое место узла (если пришли/вошли/вышли)
    room_found_now: bool = False  # особое место увидено впервые

    @property
    def moved(self) -> bool:
        return self.outcome in (Outcome.MOVED, Outcome.NEW_PASSAGE, Outcome.CONNECTED)

    def to_dict(self) -> dict[str, Any]:
        """Плоская сводка для директивы Ведущему."""
        return {
            "outcome": str(self.outcome),
            "direction": self.direction,
            "abs_side": self.abs_side,
            "node": list(self.node.cell) if self.node else None,
            "passage": self.passage.id if self.passage else None,
            "passage_length": self.passage.length if self.passage else None,
            "new_passage": self.new_passage,
            "new_node": self.new_node,
            "room": self.room.id if self.room else None,
            "room_found_now": self.room_found_now,
        }


@dataclass
class ExitView:
    rel: str
    side: str
    explored: bool
    passage_id: str | None = None
    describe: str | None = None  # описание прохода
    leads_to: str | None = None  # описание узла на том конце (если узел известен)
    leads_to_room: SpecialRoom | None = None


@dataclass
class Vision:
    """Что Альфред видит отсюда — для «Где ты». Только разведанное и ближнее."""

    kind: str  # node | wall (в проходе у стены с рычагом) | room
    heading: str
    exits: list[ExitView] = field(default_factory=list)
    lever: bool = False
    room: SpecialRoom | None = None  # у входа в особое место / внутри него
    spots: list[str] = field(default_factory=list)  # необысканные места поиска
    describe: str | None = None
    can_up: bool = True

    def text_ru(self) -> str:
        """Короткая сводка по-русски — для вставки в ответ тула."""
        if self.kind == "room":
            name = self.room.name if self.room else "особое место"
            return f"Альфред внутри: {name}. Выйти можно назад."
        parts: list[str] = []
        if self.describe:
            parts.append(self.describe)
        if self.kind == "wall":
            parts.append("Проход упирается в стену с рычагом." if self.lever else "Стена открыта.")
        for e in self.exits:
            way = RELATIVE_RU[e.rel]
            if not e.explored:
                parts.append(f"{way.capitalize()}: ещё не хоженый проход.")
                continue
            tail = e.describe or "проход"
            if e.leads_to_room:
                tail += f"; в конце — {e.leads_to_room.name}"
            parts.append(f"{way.capitalize()}: {tail}.")
        if self.kind == "node" and self.room:
            parts.append(f"Здесь вход: {self.room.name}.")
        if self.spots:
            parts.append("Можно порыскать: " + ", ".join(self.spots) + ".")
        return " ".join(parts)


@dataclass
class CatacombsState:
    """Где Альфред под землёй. ``passage`` — id прохода, в котором он стоит
    у стены с рычагом (``node`` — тогда узел, откуда он вошёл); ``room`` — id
    особого места, где он внутри."""

    node: Cell = (0, 0)
    heading: str = "N"
    passage: str | None = None
    room: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": list(self.node),
            "heading": self.heading,
            "passage": self.passage,
            "room": self.room,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CatacombsState:
        node = d.get("node") or (0, 0)
        heading = d.get("heading")
        return cls(
            node=(int(node[0]), int(node[1])),
            heading=heading if heading in SIDES else "N",
            passage=d.get("passage") or None,
            room=d.get("room") or None,
        )

    @classmethod
    def at_entrance(cls, maze: Maze) -> CatacombsState:
        return cls(node=(0, 0), heading=maze.entry_heading)


# --- лабиринт ---


@dataclass
class Maze:
    user_id: int
    seed: int
    nodes: dict[Cell, Node] = field(default_factory=dict)
    passages: dict[str, Passage] = field(default_factory=dict)
    entry_heading: str = "N"
    # id особых мест в порядке нахождения (узел увиден) / где стоят
    found: list[str] = field(default_factory=list)
    special_nodes: dict[str, Cell] = field(default_factory=dict)
    # Индексы, не сериализуются: неразведанные выходы и клетки внутри проходов.
    _open: set[tuple[Cell, str]] = field(default_factory=set, init=False, repr=False, compare=False)
    _interior: set[Cell] = field(default_factory=set, init=False, repr=False, compare=False)
    # (узел, сторона), где проход упёрся в стену с рычагом: выход там заводить нельзя.
    _walls: set[tuple[Cell, str]] = field(
        default_factory=set, init=False, repr=False, compare=False
    )

    # --- создание ---

    @classmethod
    def new(cls, user_id: int, seed: int | None = None) -> Maze:
        if seed is None:
            seed = random.SystemRandom().getrandbits(48)
        maze = cls(user_id=user_id, seed=seed)
        rng = _rng(seed, "entrance")
        node = Node(x=0, y=0)
        maze.nodes[(0, 0)] = node
        count = rng.choices(ENTRANCE_EXITS_CHOICES, ENTRANCE_EXITS_WEIGHTS)[0]
        sides = rng.sample(SIDES, count)
        for side in sides:
            maze._set_exit(node, side, None)
        maze.entry_heading = sides[0]
        return maze

    def _reindex(self) -> None:
        self._open = set()
        self._interior = set()
        self._walls = set()
        for node in self.nodes.values():
            for side, pid in node.exits.items():
                if pid is None:
                    self._open.add((node.cell, side))
        for p in self.passages.values():
            self._interior.update(p.interior())
            if p.sealed:
                self._walls.add((p.b, p.b_side))

    def _set_exit(self, node: Node, side: str, pid: str | None) -> None:
        node.exits[side] = pid
        if pid is None:
            self._open.add((node.cell, side))
        else:
            self._open.discard((node.cell, side))

    # --- доступ ---

    def node_at(self, cell: Cell) -> Node:
        return self.nodes[cell]

    def passage_of(self, node: Node, side: str) -> Passage | None:
        pid = node.exits.get(side)
        return self.passages.get(pid) if pid else None

    @property
    def unexplored(self) -> int:
        return len(self._open)

    def room_node(self, room_id: str) -> Node | None:
        cell = self.special_nodes.get(room_id)
        return self.nodes.get(cell) if cell else None

    # --- геометрия: что можно строить ---

    def _reservers(self, cell: Cell, ignore: tuple[Cell, str] | None = None) -> list[Cell]:
        """Узлы, чей неразведанный выход первой клеткой упирается в ``cell``."""
        out: list[Cell] = []
        for side in SIDES:
            owner = _step(cell, OPPOSITE[side])
            node = self.nodes.get(owner)
            if node is None or (owner, side) == ignore:
                continue
            if side in node.exits and node.exits[side] is None:
                out.append(owner)
        return out

    def _passable(self, cell: Cell, side: str, ignore: tuple[Cell, str] | None = None) -> bool:
        """Можно ли проходу, идущему в сторону ``side``, занять свободную клетку."""
        if cell in self._interior:
            return False
        ahead = _step(cell, side)
        return all(owner == ahead for owner in self._reservers(cell, ignore))

    def _side_ok(self, cell: Cell, side: str) -> bool:
        """Можно ли узлу в ``cell`` завести выход в сторону ``side``."""
        if (cell, side) in self._walls:
            return False
        first = _step(cell, side)
        if first in self.nodes:
            return True
        return self._passable(first, side)

    # --- граф ---

    def _neighbors(self, cell: Cell) -> list[Cell]:
        node = self.nodes[cell]
        out = []
        for side in SIDES:
            pid = node.exits.get(side)
            if pid and pid in self.passages:
                out.append(self.passages[pid].other(cell))
        return out

    def _bfs(self, start: Cell) -> dict[Cell, int]:
        dist = {start: 0}
        queue = deque([start])
        while queue:
            cur = queue.popleft()
            for nxt in self._neighbors(cur):
                if nxt not in dist:
                    dist[nxt] = dist[cur] + 1
                    queue.append(nxt)
        return dist

    def depth(self, cell: Cell) -> int | None:
        """Расстояние в узлах от входа по графу; None — не достижим."""
        return self._bfs((0, 0)).get(cell)

    # --- генерация ---

    def _assign_exits(self, node: Node, came_side: str, *, dead_end: bool) -> None:
        rng = _rng(self.seed, "exits", node.x, node.y)
        free = [
            s
            for s in SIDES
            if s != came_side and s not in node.exits and self._side_ok(node.cell, s)
        ]
        rng.shuffle(free)
        extra = rng.choices(EXTRA_EXITS_CHOICES, EXTRA_EXITS_WEIGHTS)[0]
        if dead_end:
            extra = 0
        for side in free[: min(extra, len(free))]:
            self._set_exit(node, side, None)

    def _maybe_special(self, node: Node) -> bool:
        if len(self.special_nodes) >= len(SPECIAL_ROOMS):
            return False
        rng = _rng(self.seed, "special", node.x, node.y)
        chance = min(SPECIAL_CHANCE_MAX, SPECIAL_CHANCE_PER_NODE * len(self.nodes))
        if rng.random() >= chance:
            return False
        d = self.depth(node.cell)
        if d is None or d < SPECIAL_MIN_DEPTH:
            return False
        room = SPECIAL_ROOMS[len(self.special_nodes)]
        node.special = room.id
        self.special_nodes[room.id] = node.cell
        return True

    def _ensure_open(self, near: Cell) -> None:
        """Инвариант: остаётся хотя бы один неразведанный выход."""
        if self._open:
            return
        dist = self._bfs(near)
        order = sorted(self.nodes, key=lambda c: (dist.get(c, 10**9), c[1], c[0]))
        for allow_special in (False, True):
            for cell in order:
                node = self.nodes[cell]
                if node.special and not allow_special:
                    continue
                sides = [s for s in SIDES if s not in node.exits and self._side_ok(cell, s)]
                if sides:
                    _rng(self.seed, "open", cell[0], cell[1]).shuffle(sides)
                    self._set_exit(node, sides[0], None)
                    return

    def _build(self, node: Node, side: str) -> tuple[Passage, Node, bool] | None:
        """Построить проход из неразведанного выхода. Возвращает (проход, узел
        на конце, новый ли узел) или None, если упереться некуда."""
        rng = _rng(self.seed, "len", node.x, node.y, side)
        want = rng.choices(PASSAGE_LEN_CHOICES, PASSAGE_LEN_WEIGHTS)[0]
        ignore = (node.cell, side)
        cells: list[Cell] = []
        hit: Cell | None = None
        for k in range(1, want + 1):
            cell = _step(node.cell, side, k)
            if cell in self.nodes:
                hit = cell
                break
            if not self._passable(cell, side, ignore):
                break
            cells.append(cell)
        new = False
        if hit is None:
            if not cells:
                return None
            hit = cells.pop()  # последняя свободная клетка становится узлом
            new = True
        back = OPPOSITE[side]
        passage = Passage(a=node.cell, a_side=side, b=hit, b_side=back)
        self.passages[passage.id] = passage
        self._interior.update(cells)
        self._set_exit(node, side, passage.id)
        if new:
            end = Node(x=hit[0], y=hit[1])
            self.nodes[hit] = end
            self._set_exit(end, back, passage.id)
            special = self._maybe_special(end)
            self._assign_exits(end, back, dead_end=special)
        else:
            end = self.nodes[hit]
            if back in end.exits and end.exits[back] is None:
                self._set_exit(end, back, passage.id)
            else:
                passage.sealed = True
                self._walls.add((passage.b, passage.b_side))
        self._ensure_open(hit)
        return passage, end, new

    # --- сериализация ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": VERSION,
            "user_id": self.user_id,
            "seed": self.seed,
            "entry_heading": self.entry_heading,
            "found": list(self.found),
            "special_nodes": {k: list(v) for k, v in self.special_nodes.items()},
            "nodes": [
                {
                    "x": n.x,
                    "y": n.y,
                    "exits": dict(n.exits),
                    "special": n.special,
                    "image_key": n.image_key,
                    **_place_to_dict(n),
                }
                for n in self.nodes.values()
            ],
            "passages": [
                {
                    "a": list(p.a),
                    "a_side": p.a_side,
                    "b": list(p.b),
                    "b_side": p.b_side,
                    "sealed": p.sealed,
                    **_place_to_dict(p),
                }
                for p in self.passages.values()
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Maze:
        maze = cls(user_id=int(d["user_id"]), seed=int(d["seed"]))
        maze.entry_heading = d.get("entry_heading") if d.get("entry_heading") in SIDES else "N"
        maze.found = [str(x) for x in d.get("found") or []]
        maze.special_nodes = {
            str(k): (int(v[0]), int(v[1])) for k, v in (d.get("special_nodes") or {}).items()
        }
        for raw in d.get("nodes") or []:
            node = Node(
                x=int(raw["x"]),
                y=int(raw["y"]),
                exits={
                    str(s): (str(p) if p else None) for s, p in (raw.get("exits") or {}).items()
                },
                special=raw.get("special"),
                image_key=raw.get("image_key"),
                **_place_from_dict(raw),
            )
            maze.nodes[node.cell] = node
        for raw in d.get("passages") or []:
            p = Passage(
                a=(int(raw["a"][0]), int(raw["a"][1])),
                a_side=str(raw["a_side"]),
                b=(int(raw["b"][0]), int(raw["b"][1])),
                b_side=str(raw["b_side"]),
                sealed=bool(raw.get("sealed")),
                **_place_from_dict(raw),
            )
            maze.passages[p.id] = p
        if (0, 0) not in maze.nodes:
            raise ValueError("в лабиринте нет входа (0, 0)")
        maze._reindex()
        return maze

    @classmethod
    def from_json(cls, raw: str) -> Maze:
        return cls.from_dict(json.loads(raw))


# --- хранение (как cabinet.load/save) ---


def _key(user_id: int) -> str:
    return f"{STATE_PREFIX}:{user_id}"


async def load(store: Store, user_id: int) -> Maze:
    """Лабиринт гостя. Нет записи (или она битая) — новый со случайным зерном;
    он существует, только пока его не сохранили (``save``): зерно в памяти."""
    raw = await store.get_state(_key(user_id))
    if raw:
        try:
            return Maze.from_json(raw)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            pass
    return Maze.new(user_id)


async def save(store: Store, maze: Maze) -> None:
    await store.set_state(_key(maze.user_id), maze.to_json())


async def reset(store: Store, user_id: int) -> None:
    """Стереть прогресс катакомб (отладка ``/interactives reset catacombs``)."""
    await store.set_state(_key(user_id), "")


# --- описание и картинки ---


def node_frame_key(node: Node) -> str:
    """Ключ кадра узла: координаты, особое место и набор выходов (разведанных
    и нет). Рычаг открыл выход — ключ другой, картинку надо перерисовать."""
    exits = "".join(s for s in SIDES if s in node.exits)
    return f"node:{node.x},{node.y}:{exits}:{node.special or '-'}"


def passage_frame_key(passage: Passage) -> str:
    return f"passage:{passage.id}"


def node_needs_frame(node: Node) -> bool:
    return node.image_id is None or node.image_key != node_frame_key(node)


def passage_needs_frame(passage: Passage) -> bool:
    return passage.image_id is None


def needs_describe(place: Described) -> bool:
    return not place.describe


def set_description(place: Described, describe: str, spots: list[str] | None = None) -> None:
    """Запомнить описание от Ведущего (один раз; повторно не перетирает)."""
    if place.describe:
        return
    place.describe = describe.strip()
    place.spots = [Spot(name=s.strip()) for s in (spots or []) if s and s.strip()]


def set_image(place: Described, image_id: int, file_id: str | None = None) -> None:
    place.image_id = image_id
    place.file_id = file_id
    if isinstance(place, Node):
        place.image_key = node_frame_key(place)


# --- навигация ---


def reset_position(state: CatacombsState, maze: Maze) -> CatacombsState:
    """«В катакомбы» — всегда к лазу, к ``(0, 0)``, а не туда, где были в
    прошлый раз."""
    state.node = (0, 0)
    state.heading = maze.entry_heading
    state.passage = None
    state.room = None
    return state


def found_rooms(maze: Maze) -> list[SpecialRoom]:
    """Найденные особые места в порядке нахождения (для быстрых переходов)."""
    return [r for rid in maze.found if (r := special_room(rid))]


def go_to_room(state: CatacombsState, maze: Maze, room_id: str) -> bool:
    """Быстрый переход «знакомой дорогой» в найденное особое место: Альфред
    сразу внутри. False — место не найдено."""
    node = maze.room_node(room_id)
    if node is None or room_id not in maze.found:
        return False
    state.node = node.cell
    state.passage = None
    state.room = room_id
    return True


def _mark_found(maze: Maze, node: Node) -> bool:
    if node.special and node.special not in maze.found:
        maze.found.append(node.special)
        return True
    return False


def _arrive(
    state: CatacombsState, maze: Maze, node: Node, side: str
) -> tuple[SpecialRoom | None, bool]:
    state.node = node.cell
    state.heading = side
    state.passage = None
    state.room = None
    return (special_room(node.special) if node.special else None), _mark_found(maze, node)


def walk(state: CatacombsState, maze: Maze, direction: str) -> WalkResult:
    """Один ход Альфреда. ``direction``: ``left|right|forward|back|lever|enter|up``.

    Меняет ``state`` и, если надо, ``maze`` (строит проход, тянет рычаг).
    Вызывающий сохраняет оба. ``heading`` в ``state`` обязан быть корректным."""
    if direction not in WALK_DIRECTIONS:
        raise ValueError(f"неизвестное направление: {direction!r}")
    node = maze.nodes[state.node]

    def res(outcome: Outcome, **kw: Any) -> WalkResult:
        return WalkResult(outcome=outcome, direction=direction, node=node, **kw)

    if direction == "up":
        reset_position(state, maze)
        return res(Outcome.ASCENDED)

    if state.room:
        if direction == "back":
            room = special_room(state.room)
            state.room = None
            return res(Outcome.LEFT_ROOM, room=room)
        return res(Outcome.NO_EXIT)

    if direction == "enter":
        if not node.special or state.passage:
            return res(Outcome.NO_ROOM)
        state.room = node.special
        return res(Outcome.ENTERED_ROOM, room=special_room(node.special))

    if state.passage:
        return _walk_at_wall(state, maze, node, direction, res)

    if direction == "lever":
        return res(Outcome.NO_LEVER)

    side = to_absolute(state.heading, direction)
    if side not in node.exits:
        return res(Outcome.NO_EXIT, abs_side=side)

    passage = maze.passage_of(node, side)
    if passage is not None:
        return _traverse(state, maze, node, passage, side, res, new=False)
    built = maze._build(node, side)
    if built is None:  # некуда упереться — выход оказался глухим, убираем
        del node.exits[side]
        maze._open.discard((node.cell, side))
        maze._ensure_open(node.cell)
        return res(Outcome.NO_EXIT, abs_side=side)
    passage, end, new_node = built
    return _traverse(state, maze, node, passage, side, res, new=True, new_node=new_node)


def _traverse(
    state: CatacombsState,
    maze: Maze,
    node: Node,
    passage: Passage,
    side: str,
    res: Any,
    *,
    new: bool,
    new_node: bool = False,
) -> WalkResult:
    end = maze.nodes[passage.other(node.cell)]
    if passage.sealed and passage.a == node.cell:
        # дошли до стены с рычагом и встали
        state.node = node.cell
        state.heading = side
        state.passage = passage.id
        state.room = None
        return res(
            Outcome.LEVER_WALL, abs_side=side, passage=passage, linked_node=end, new_passage=new
        )
    room, found_now = _arrive(state, maze, end, side)
    outcome = Outcome.MOVED
    if new:
        outcome = Outcome.NEW_PASSAGE if new_node else Outcome.CONNECTED
    result = res(
        outcome,
        abs_side=side,
        passage=passage,
        new_passage=new,
        new_node=new_node,
        room=room,
        room_found_now=found_now,
    )
    result.node = end
    return result


def _walk_at_wall(
    state: CatacombsState, maze: Maze, node: Node, direction: str, res: Any
) -> WalkResult:
    passage = maze.passages[state.passage] if state.passage in maze.passages else None
    if passage is None:  # битое состояние — возвращаем к узлу
        state.passage = None
        return res(Outcome.NO_EXIT)
    end = maze.nodes[passage.b]
    if direction == "lever":
        if not passage.sealed:
            return res(Outcome.NO_LEVER, passage=passage, linked_node=end)
        passage.sealed = False
        maze._walls.discard((passage.b, passage.b_side))
        maze._set_exit(end, passage.b_side, passage.id)
        return res(Outcome.LEVER_PULLED, abs_side=passage.a_side, passage=passage, linked_node=end)
    side = to_absolute(state.heading, direction)
    if direction == "forward":
        if passage.sealed:
            return res(Outcome.BLOCKED_BY_LEVER, abs_side=side, passage=passage, linked_node=end)
        return _traverse(state, maze, node, passage, side, res, new=False)
    if direction == "back":
        # развернуться и вернуться к узлу, откуда вошли
        back = OPPOSITE[state.heading]
        room, found_now = _arrive(state, maze, node, back)
        return WalkResult(
            outcome=Outcome.MOVED,
            direction=direction,
            abs_side=back,
            node=node,
            passage=passage,
            room=room,
            room_found_now=found_now,
        )
    return res(Outcome.NO_EXIT, abs_side=side)


# --- что видно отсюда ---


def look(state: CatacombsState, maze: Maze) -> Vision:
    """Что Альфред видит отсюда — только разведанное и ближнее, без карты."""
    node = maze.nodes[state.node]
    heading = state.heading

    def exit_view(side: str, pid: str | None) -> ExitView:
        rel = to_relative(heading, side)
        if pid is None or pid not in maze.passages:
            return ExitView(rel=rel, side=side, explored=False)
        p = maze.passages[pid]
        far = maze.nodes[p.other(node.cell)]
        return ExitView(
            rel=rel,
            side=side,
            explored=True,
            passage_id=pid,
            describe=p.describe,
            leads_to=far.describe,
            leads_to_room=special_room(far.special) if far.special else None,
        )

    if state.room:
        return Vision(
            kind="room", heading=heading, room=special_room(state.room), describe=node.describe
        )

    if state.passage and state.passage in maze.passages:
        p = maze.passages[state.passage]
        exits = [exit_view(OPPOSITE[heading], p.id)]
        if not p.sealed:
            exits.insert(0, exit_view(heading, p.id))
        return Vision(
            kind="wall",
            heading=heading,
            exits=exits,
            lever=p.sealed,
            spots=[s.name for s in p.spots if not s.searched],
            describe=p.describe,
        )

    exits = [exit_view(s, node.exits[s]) for s in SIDES if s in node.exits]
    order = {r: i for i, r in enumerate(RELATIVE)}
    exits.sort(key=lambda e: order[e.rel])
    return Vision(
        kind="node",
        heading=heading,
        exits=exits,
        room=special_room(node.special) if node.special else None,
        spots=[s.name for s in node.spots if not s.searched],
        describe=node.describe,
    )
