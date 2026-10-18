from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sa_home_bot.bot.interactives import cabinet as cabinet_mod
from sa_home_bot.bot.interactives.base import iso, parse_iso
from sa_home_bot.bot.interactives.cabinet import Cabinet

if TYPE_CHECKING:
    from sa_home_bot.db.store import Store

PLACE_CABINET = cabinet_mod.LOCATION
PLACE_CELLAR_DOOR = "cellar_door"
PLACE_CELLAR = "cellar"
TEMP_PREFIX = "t"

GROUP_CABINET = "cabinet"
GROUP_CELLAR = "cellar"
GROUP_CATACOMBS = "catacombs"

UNDERGROUND_LIGHT_EN = "dim lantern light, deep shadows"
NO_OUTSIDE_KEY = "lantern"
NO_WINDOW_NEGATIVE_EN = "window, daylight, sunlight, sky"

TEMP_NAME_MAX = 40
TEMP_CANON_RU_MAX = 220
TEMP_CANON_EN_MAX = 120
TEMP_ROOMS_KEEP = 8


@dataclass(frozen=True)
class Find:
    pattern: re.Pattern[str] | None
    text: str
    item: str | None = None
    first: bool = False

    def matches(self, where: str, *, first_search: bool) -> bool:
        if self.first and not first_search:
            return False
        return self.pattern is None or bool(self.pattern.search(where))


@dataclass(frozen=True)
class RoomCanon:
    id: str
    name_ru: str
    in_ru: str
    canon_ru: str
    canon_en: str
    caption: str
    scene_caption: str
    stem: str
    in_en: str
    group: str = GROUP_CELLAR
    windowed: bool = False
    light_en: str | None = UNDERGROUND_LIGHT_EN
    trinkets: tuple[str, ...] = ()
    trinket_chance: float = 0.35
    specials: tuple[Find, ...] = ()
    of_ru: str = "этого места"
    from_ru: str = "оттуда, где был"
    names: tuple[str, ...] = ()


CABINET_CANON = RoomCanon(
    id=PLACE_CABINET,
    name_ru="кабинет",
    in_ru="в кабинете",
    canon_ru=cabinet_mod.CANON_RU,
    canon_en=cabinet_mod.CANON_EN,
    caption="Кабинет",
    scene_caption="В кабинете",
    stem="кабинет",
    in_en="in his study",
    of_ru="кабинета",
    group=GROUP_CABINET,
    windowed=True,
    light_en=None,
    names=(r"кабинет\w*",),
    trinkets=(
        "выцветшую записку без подписи",
        "огрызок сургучной палочки",
        "потускневшую запонку",
        "пожелтевший пустой конверт",
        "стёртую медную монету",
    ),
)

CELLAR_DOOR_CANON = RoomCanon(
    id=PLACE_CELLAR_DOOR,
    name_ru="лестница к двери подвала",
    in_ru="у двери подвала",
    canon_ru=(
        "Узкая каменная лестница вниз и тяжёлая дверь подвала, заколоченная "
        "досками крест-накрест; на двери ржавый замок, рядом на крюке фонарь."
    ),
    canon_en="narrow stone stairwell, old heavy door boarded up with planks, rusty lock",
    caption="У двери подвала",
    scene_caption="У двери подвала",
    stem="двер",
    in_en="at an old boarded-up cellar door",
    of_ru="лестницы у двери подвала",
    from_ru="от двери подвала",
    names=(
        r"двер\w*\s+(?:в\s+)?(?:подвал|погреб)\w*",
        r"(?:лестниц|пролёт|пролет)\w*(?:\s+(?:в|к|на)\s+(?:подвал|погреб)\w*)?",
    ),
    trinkets=(
        "ржавый гвоздь из доски",
        "клочок паутины с высохшей мухой",
        "обломок кирпича",
    ),
)

CELLAR_CANON = RoomCanon(
    id=PLACE_CELLAR,
    name_ru="подвал",
    in_ru="в подвале",
    canon_ru=(
        "Подвал замка: низкий каменный свод, вдоль стен бочки и пыльные "
        "полки, паутина, при свете фонаря тени."
    ),
    canon_en="dark stone cellar, vaulted ceiling, old barrels, dusty shelves",
    caption="Подвал",
    scene_caption="В подвале",
    stem="подвал",
    in_en="in a dark stone cellar",
    of_ru="подвала",
    from_ru="из подвала",
    names=(r"(?:подвал|погреб)\w*",),
    trinkets=(
        "пустую бутылку без этикетки",
        "треснувшую глиняную кружку",
        "моток гнилой верёвки",
        "ржавую подкову",
    ),
)

PLACES: dict[str, RoomCanon] = {
    canon.id: canon for canon in (CABINET_CANON, CELLAR_DOOR_CANON, CELLAR_CANON)
}


def register(canon: RoomCanon) -> None:
    PLACES[canon.id] = canon
    NAMEABLE[canon.id] = canon


NAMEABLE: dict[str, RoomCanon] = dict(PLACES)


def register_nameable(canon: RoomCanon) -> None:
    NAMEABLE[canon.id] = canon


_HATCH_RE = r"\bлаз(?:а|у|ом|е|ы)?\b"
_HATCH_IDS = frozenset({PLACE_CELLAR, "catacombs"})
_HATCH_NAME = "лаз в катакомбы"


def foreign_place(text: str, here: str) -> str | None:
    low = text.casefold()
    parts: list[tuple[frozenset[str], str]] = []
    patterns: list[str] = []
    for canon in NAMEABLE.values():
        for fragment in canon.names:
            patterns.append(f"(?P<g{len(parts)}>{fragment})")
            parts.append((frozenset({canon.id}), canon.name_ru))
    patterns.append(f"(?P<g{len(parts)}>{_HATCH_RE})")
    parts.append((_HATCH_IDS, _HATCH_NAME))
    for match in re.finditer("|".join(patterns), low):
        ids, name = parts[int(match.lastgroup[1:])]  # type: ignore[index]
        if here not in ids:
            return name
    return None


RETURN_NOTE = (
    "Пока собеседник молчал, ты вернулся {from_ru} к себе в кабинет и снова за "
    "своим столом. Скажи об этом сам одной фразой, в образе дворецкого, и "
    "продолжай разговор."
)
RETURN_FROM_UNKNOWN = "из подземелья"
CELLAR_HATCH_HINT = (
    " Отсюда есть лаз в катакомбы — ты сам можешь предложить собеседнику спуститься."
)


@dataclass
class At:
    place: str = PLACE_CABINET
    since: str | None = None
    node: Any = None
    heading: str | None = None
    touched: str | None = None
    returned_from: str | None = None

    def to_json(self) -> str:
        data: dict[str, Any] = {"place": self.place, "since": self.since}
        for name in ("node", "heading", "touched", "returned_from"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> At | None:
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("place"), str):
            return None
        return cls(
            place=data["place"],
            since=data.get("since"),
            node=data.get("node"),
            heading=data.get("heading"),
            touched=data.get("touched"),
            returned_from=data.get("returned_from"),
        )

    @property
    def in_cabinet(self) -> bool:
        return self.place == PLACE_CABINET


def at_key(user_id: int) -> str:
    return f"alfred_at:{user_id}"


async def get_at(store: Store, user_id: int) -> At | None:
    raw = await store.get_state(at_key(user_id))
    return At.from_json(raw) if raw else None


async def set_at(store: Store, user_id: int, at: At) -> None:
    await store.set_state(at_key(user_id), at.to_json())


def is_idle(at: At, now: datetime, hours: float) -> bool:
    last = parse_iso(at.touched) or parse_iso(at.since)
    return last is not None and now - last >= timedelta(hours=hours)


def new_at(place: str, now: datetime, **extra: Any) -> At:
    stamp = iso(now)
    return At(place=place, since=stamp, touched=stamp, **extra)


@dataclass
class Room(Cabinet):
    canon: RoomCanon = field(default=CELLAR_CANON)
    temp: bool = False

    @property
    def place(self) -> str:
        return self.canon.id

    @property
    def is_study(self) -> bool:
        return False

    @property
    def name_ru(self) -> str:
        return self.canon.name_ru

    @property
    def in_ru(self) -> str:
        return self.canon.in_ru

    @property
    def canon_ru(self) -> str:
        return self.canon.canon_ru

    @property
    def canon_en(self) -> str:
        return self.canon.canon_en

    @property
    def windowed(self) -> bool:
        return self.canon.windowed

    @property
    def caption(self) -> str:
        return self.canon.caption

    @property
    def scene_caption(self) -> str:
        return self.canon.scene_caption

    @property
    def subject_ru(self) -> str:
        return self.canon.name_ru

    @property
    def pose_room_en(self) -> str:
        return self.canon.canon_en

    def light_en(self, outside: Any, *, closeup: bool = False) -> str:
        if self.canon.light_en is None:
            return outside.en(closeup=closeup)
        return self.canon.light_en

    def localize_selfie(self, text: str) -> str:
        return text.replace("in his study", self.canon.in_en).replace(
            "dark gothic study", self.canon.canon_en
        )

    def localize_ru(self, text: str) -> str:
        return text.replace("в кабинете", self.canon.in_ru).replace(
            "свой кабинет", self.canon.name_ru
        )

    def is_general(self, focus: str) -> bool:
        low = focus.casefold()
        return self.canon.stem in low and len(low.split()) <= 3

    def state_key(self, outside_key: str) -> str:
        if not self.canon.windowed:
            outside_key = NO_OUTSIDE_KEY
        return f"{self.canon.id}:{super().state_key(outside_key)}"


def _key(place: str, user_id: int) -> str:
    return f"location:{place}:{user_id}"


def searched_key(place: str, user_id: int) -> str:
    return f"location_searched:{place}:{user_id}"


async def load_room(store: Store, user_id: int, place: str) -> Room:
    canon = PLACES[place]
    raw = await store.get_state(_key(place, user_id))
    if raw:
        try:
            data: dict[str, Any] = json.loads(raw)
            return Room(
                user_id=user_id,
                features=[str(f) for f in data.get("features") or []],
                scene=[str(f) for f in data.get("scene") or []],
                photos={str(k): int(v) for k, v in (data.get("photos") or {}).items()},
                canon=canon,
            )
        except (ValueError, TypeError, AttributeError):
            pass
    return Room(user_id=user_id, canon=canon)


async def save_room(store: Store, room: Room) -> None:
    data = {
        "user_id": room.user_id,
        "features": room.features,
        "scene": room.scene,
        "photos": room.photos,
    }
    await store.set_state(_key(room.canon.id, room.user_id), json.dumps(data, ensure_ascii=False))


async def load_searched(store: Store, user_id: int, place: str) -> list[str]:
    raw = await store.get_state(searched_key(place, user_id))
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


async def save_searched(store: Store, user_id: int, place: str, searched: list[str]) -> None:
    await store.set_state(searched_key(place, user_id), json.dumps(searched, ensure_ascii=False))


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].rstrip(" .,;")


def temp_spec(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    name = _clip(raw.get("name"), TEMP_NAME_MAX)
    canon_ru = _clip(raw.get("canon_ru"), TEMP_CANON_RU_MAX)
    canon_en = _clip(raw.get("canon_en"), TEMP_CANON_EN_MAX)
    if not name or not canon_ru or not canon_en:
        return None
    where = _clip(raw.get("where"), TEMP_NAME_MAX + 10) or f"в месте «{name}»"
    return {"name": name, "where": where, "canon_ru": canon_ru, "canon_en": canon_en}


def temp_id_for(world: dict[str, Any], name: str) -> str:
    rooms: dict[str, Any] = world.setdefault("rooms", {})
    for tid, data in rooms.items():
        if str(data.get("name", "")).casefold() == name.casefold():
            return tid
    tid = f"{TEMP_PREFIX}{len(rooms) + 1}"
    while tid in rooms:
        tid += "_"
    return tid


def put_temp_room(world: dict[str, Any], spec: dict[str, str]) -> str:
    tid = temp_id_for(world, spec["name"])
    rooms: dict[str, Any] = world["rooms"]
    entry = rooms.setdefault(tid, {"features": [], "scene": [], "searched": []})
    entry.update(spec)
    while len(rooms) > TEMP_ROOMS_KEEP:
        oldest = next(k for k in rooms if k != tid)
        del rooms[oldest]
    return tid


def is_temp(place: str) -> bool:
    return place not in PLACES and place.startswith(TEMP_PREFIX)


def temp_room(world: dict[str, Any], user_id: int, tid: str) -> Room | None:
    data = (world.get("rooms") or {}).get(tid)
    if not isinstance(data, dict):
        return None
    name = str(data.get("name") or tid)
    canon = RoomCanon(
        id=tid,
        name_ru=name,
        in_ru=str(data.get("where") or f"в месте «{name}»"),
        canon_ru=str(data.get("canon_ru") or name),
        canon_en=str(data.get("canon_en") or name),
        caption=name.capitalize(),
        scene_caption=str(data.get("where") or name).capitalize(),
        stem=name.casefold().split()[0][:6] if name.split() else name.casefold(),
        in_en="in the room",
        trinkets=CELLAR_CANON.trinkets,
        of_ru=f"места «{name}»",
        from_ru="оттуда, где искал ключ",
    )
    return Room(
        user_id=user_id,
        features=[str(f) for f in data.get("features") or []],
        scene=[str(f) for f in data.get("scene") or []],
        canon=canon,
        temp=True,
    )


def store_temp_room(world: dict[str, Any], room: Room) -> None:
    data = (world.get("rooms") or {}).get(room.canon.id)
    if isinstance(data, dict):
        data["features"] = list(room.features)
        data["scene"] = list(room.scene)


_PREPOSITIONS = frozenset({"в", "во", "на", "под", "за", "у", "около", "возле", "над", "из"})
_NOT_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_where(where: str) -> str:
    words = [w for w in _NOT_WORD.sub(" ", where.casefold()).split() if w]
    while words and words[0] in _PREPOSITIONS:
        words.pop(0)
    return " ".join(words)


SEARCH_KIND_REPEAT = "repeat"
SEARCH_KIND_SPECIAL = "special"
SEARCH_KIND_TRINKET = "trinket"
SEARCH_KIND_EMPTY = "empty"


@dataclass(frozen=True)
class SearchResult:
    kind: str
    text: str
    item: str | None = None


SEARCH_REPEAT_TEXT = "Это место ты уже обыскал — ничего нового там нет."
SEARCH_EMPTY_TEXT = "Ничего интересного: пыль, паутина, старый хлам."
SEARCH_TRINKET_TEXT = "Ничего ценного, лишь мелочь: {item}."


def search_room(
    canon: RoomCanon,
    where: str,
    searched: list[str],
    *,
    rng: Callable[[], float],
    choose: Callable[[tuple[str, ...]], str],
    have_items: frozenset[str] = frozenset(),
) -> SearchResult:
    norm = normalize_where(where) or "здесь"
    if norm in searched:
        return SearchResult(SEARCH_KIND_REPEAT, SEARCH_REPEAT_TEXT)
    first_search = not searched
    searched.append(norm)
    for find in canon.specials:
        if find.item is not None and find.item in have_items:
            continue
        if find.matches(where, first_search=first_search):
            return SearchResult(SEARCH_KIND_SPECIAL, find.text, find.item)
    if canon.trinkets and rng() < canon.trinket_chance:
        item = choose(canon.trinkets)
        return SearchResult(SEARCH_KIND_TRINKET, SEARCH_TRINKET_TEXT.format(item=item))
    return SearchResult(SEARCH_KIND_EMPTY, SEARCH_EMPTY_TEXT)


DARKROOM_CAMERA_TEXT = (
    "На полке среди склянок ты находишь старый фотоаппарат — и сразу узнаёшь "
    "его: ты сам оставил его здесь когда-то и успел позабыть."
)
DARKROOM_CAMERA = Find(
    pattern=None,
    text=DARKROOM_CAMERA_TEXT,
    item="camera",
    first=True,
)


MOVE_RULE = (
    "Перейти в другое место ты можешь только инструментом (go или walk): "
    "словами ты никуда не уходишь, а «спустился» или «поднялся» говоришь лишь "
    "после того, как инструмент это сделал. Снимок и обыск — только того "
    "места, где ты стоишь сейчас."
)
WINDOW_YES = "Здесь есть окно."
WINDOW_NO = "Окон здесь нет, света снаружи нет — только твой фонарь."
WHERE_NOTE = "Где ты сейчас: {here}.{detail}"
EXIT_HOME = " Домой, в кабинет, — go(cabinet)."
EXIT_CABINET_TO = " Отсюда знакомой дорогой (go): {targets}."

TOOL_PHOTO_ELSEWHERE = (
    "Снимка не будет: место «{name}» отсюда не снять — ты сейчас {here}. "
    "Снимать можно только то, что вокруг тебя. Чтобы снять то место, сначала "
    "перейди туда инструментом go (по катакомбам — walk), потом снимай. "
    "Скажи об этом собеседнику своими словами."
)
TOOL_PHOTO_NO_WINDOW = (
    "Снимка не будет: окон здесь нет — ты сейчас {here}, вида из окна отсюда "
    "не снять. Скажи об этом собеседнику своими словами."
)
TOOL_SEARCH_ELSEWHERE = (
    "Обыскать место «{name}» отсюда нельзя: ты сейчас {here}. Порыскать можно "
    "только там, где стоишь. Чтобы порыскать там, сначала перейди туда "
    "инструментом go (по катакомбам — walk). Скажи об этом собеседнику "
    "своими словами."
)


RESET_SCOPES = ("cellar", "catacombs", "all")

EFFECT_CELLAR_UNLOCKED = "cellar_unlocked"
EFFECT_CATACOMBS_OPEN = "catacombs_open"
CATACOMBS_KEY = "catacombs:{user_id}"
PLAIN_TURNS_KEY = "interactives_plain:{chat_id}"


async def reset_progress(
    store: Store, user_id: int, scope: str, chat_id: int | None = None
) -> list[str]:
    scenarios = {
        "cellar": ("cellar",),
        "catacombs": ("catacombs",),
        "all": ("cellar", "catacombs"),
    }[scope]
    groups = {
        "cellar": {GROUP_CELLAR},
        "catacombs": {GROUP_CATACOMBS},
        "all": {GROUP_CELLAR, GROUP_CATACOMBS},
    }[scope]
    removed: list[str] = []

    async def drop(key: str) -> None:
        if await store.get_state(key) is not None:
            await store.delete_state(key)
            removed.append(key)

    for scenario in scenarios:
        await drop(f"interactive_done:{scenario}:{user_id}")
        for key in await store.state_keys("interactive_run:"):
            if key.endswith(f":{scenario}") and await _run_owner(store, key) == user_id:
                await drop(key)
    if "cellar" in scenarios:
        for effect in (EFFECT_CELLAR_UNLOCKED, EFFECT_CATACOMBS_OPEN):
            await drop(f"user_effect:{effect}:{user_id}")
    if "catacombs" in scenarios:
        await drop(CATACOMBS_KEY.format(user_id=user_id))
    for prefix in ("location:", "location_searched:"):
        for key in await store.state_keys(prefix):
            parts = key.split(":")
            if len(parts) != 3 or parts[2] != str(user_id) or parts[1] == PLACE_CABINET:
                continue
            canon = PLACES.get(parts[1])
            group = canon.group if canon is not None else GROUP_CATACOMBS
            if group in groups:
                await drop(key)
    stage_items = {"cellar": (("wine", "tipsy"),), "catacombs": (("camera", "camera"),)}
    for scenario in scenarios:
        for item_type, effect in stage_items[scenario]:
            for item in await store.items_of(user_id, item_type):
                await store.consume_item(int(item["id"]))
                removed.append(f"item:{item_type}:{item['id']}")
            await drop(f"user_effect:{effect}:{user_id}")
    if chat_id is not None:
        await drop(PLAIN_TURNS_KEY.format(chat_id=chat_id))
    await drop(at_key(user_id))
    return removed


async def _run_owner(store: Store, key: str) -> int | None:
    raw = await store.get_state(key)
    try:
        data = json.loads(raw) if raw else {}
        return int(data.get("user_id"))
    except (ValueError, TypeError, AttributeError):
        return None


SEARCH_WHERE_MAX = 120
TOOL_SEARCH_UNAVAILABLE = "недоступно: порыскать можно только в живом разговоре"
TOOL_SEARCH_NO_WHERE = "Скажи, что именно ты обыскиваешь: ящик, полку, бочку, угол…"
TOOL_SEARCH_FREE = (
    "Ты обыскал «{where}». {found} Расскажи об этом собеседнику своими "
    "словами, ничего не добавляя от себя."
)

SEARCH_ITEM_DIRECTOR = (
    "Альфред обыскал «{where}». {text} Он забирает находку с собой: это вещь "
    "«{name}». Опиши, как она выглядит (поле found_item)."
)
SEARCH_ITEM_TAKEN = (
    " Ты забираешь находку: {name}{note}. Теперь она среди особенных вещей поместья."
)

SEARCH_DECLARATION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search",
        "description": (
            "Порыскать в месте, где ты сейчас находишься: обыскать ящик, полку, "
            "бочку, шкаф, угол. Вызывай, когда собеседник просит что-то "
            "поискать или ты сам решил что-то найти. Что именно найдено, "
            "решает мир, а не ты: не выдумывай находку до результата."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "where": {
                    "type": "string",
                    "description": (
                        "что именно обыскиваешь, например «ящик письменного "
                        "стола», «дальняя бочка», «полка у входа»"
                    ),
                }
            },
            "required": ["where"],
        },
    },
}
