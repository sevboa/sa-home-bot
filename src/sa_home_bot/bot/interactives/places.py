"""Места Альфреда (Этап 59.1, IMPLEMENTATION_PLAN.md): не один кабинет, а
несколько комнат, и Альфред стоит в одной из них.

Два вида мест (решение владельца 2026-10-10):

- **комната** — кабинет, подвал, места поиска ключа, особые места катакомб.
  Устроена как кабинет (cabinet.py): канон в коде, особенности гостя от
  Ведущего, следы сцены, снимок каждый раз рисуется заново. Кабинет — одна из
  комнат: его ключ ``location:cabinet:<гость>`` и формат хранения прежние
  (существующие данные читаются как были), поэтому для кабинета остаётся
  сам ``cabinet.Cabinet``; ``Room`` — его наследник для прочих комнат с ключом
  ``location:<id>:<гость>``;
- **лабиринт** — катакомбы (catacombs.py, 59.4, отдельный модуль): здесь
  только поля ``node``/``heading`` положения Альфреда.

Положение Альфреда — ``app_state`` ``alfred_at:<гость>``: ``{place, node?,
heading?, since}``. «Где ты», take_photo и кадры Ведущего берут место
отсюда. Канарейка (59.C): для гостя вне ``llm.interactives_canary_user_ids``
положение игнорируется — Альфред всегда в кабинете, и ничего не пишется.

Временные места (поиск ключа: кладовая, кухня, библиотека…) в хранилище не
попадают: канон придумывает Ведущий на ходу, комната живёт в ``Run.world``
сценария (``world["rooms"]``), а в ``alfred_at.place`` — её временный id.

Обыск (тул ``search``): что найдено, решает код по таблице находок места
(``RoomCanon.specials`` — особая находка, иначе пусто или мелочь для
антуража); обысканное помечается, повторный обыск там ничего не даёт.
"""

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
# Временные места поиска — id вида «t1», «t2» (Run.world["rooms"]).
TEMP_PREFIX = "t"

GROUP_CABINET = "cabinet"
GROUP_CELLAR = "cellar"
GROUP_CATACOMBS = "catacombs"

# Темп кадра подземелья: свет — фонарь Альфреда, без времени суток и погоды
# (план 59.0: кэш картинок поэтому стабилен).
UNDERGROUND_LIGHT_EN = "dim lantern light, deep shadows"

TEMP_NAME_MAX = 40
TEMP_CANON_RU_MAX = 220
TEMP_CANON_EN_MAX = 120
TEMP_ROOMS_KEEP = 8


@dataclass(frozen=True)
class Find:
    """Особая находка места: срабатывает, когда обыскивают то, что назвал
    Альфред (``pattern`` по тексту ``where``). Один раз на гостя в месте —
    обысканное помечается."""

    pattern: re.Pattern[str]
    text: str


@dataclass(frozen=True)
class RoomCanon:
    """Канон комнаты — общий для всех гостей (особенности у каждого свои)."""

    id: str
    name_ru: str  # «подвал» — как её называет Альфред
    in_ru: str  # «в подвале»
    canon_ru: str
    canon_en: str  # для снимка: коротко, 77 токенов CLIP
    caption: str  # подпись общего снимка
    scene_caption: str  # подпись кадра сцены
    stem: str  # корень названия для «общий вид подвала»
    in_en: str  # «in a dark stone cellar» — для селфи
    group: str = GROUP_CELLAR
    windowed: bool = False
    light_en: str | None = UNDERGROUND_LIGHT_EN
    # Мелочь для антуража и её шанс; пусто — «ничего» (план: «почти всегда
    # пусто или мелочь»).
    trinkets: tuple[str, ...] = ()
    trinket_chance: float = 0.35
    specials: tuple[Find, ...] = ()
    # Склонения для Ведущего и Альфреда: «подвала» и «из подвала».
    of_ru: str = "этого места"
    from_ru: str = "оттуда, где был"


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
    trinkets=(
        "пустую бутылку без этикетки",
        "треснувшую глиняную кружку",
        "моток гнилой верёвки",
        "ржавую подкову",
    ),
)

# Реестр комнат с каноном в коде. Особые места катакомб (59.5) регистрирует
# catacombs.py через ``register``.
PLACES: dict[str, RoomCanon] = {
    canon.id: canon for canon in (CABINET_CANON, CELLAR_DOOR_CANON, CELLAR_CANON)
}


def register(canon: RoomCanon) -> None:
    PLACES[canon.id] = canon


# --- положение Альфреда ---

# Заметка Альфреду на следующий ход после самовозврата в кабинет (простой
# гостя дольше порога или Ведущий вернул idle): о возвращении он скажет сам.
RETURN_NOTE = (
    "Пока собеседник молчал, ты вернулся {from_ru} к себе в кабинет и снова за "
    "своим столом. Скажи об этом сам одной фразой, в образе дворецкого, и "
    "продолжай разговор."
)
RETURN_FROM_UNKNOWN = "из подземелья"
# «Где ты» в подвале, когда лаз уже найден (Этап 59.2).
CELLAR_HATCH_HINT = (
    " Отсюда есть лаз в катакомбы — ты сам можешь предложить собеседнику спуститься."
)


@dataclass
class At:
    """``alfred_at:<гость>``. ``node``/``heading`` — лабиринт катакомб (59.4),
    движок их не трогает. ``touched`` — когда гость последний раз говорил с
    Альфредом, пока тот вне кабинета (для возврата после простоя).
    ``returned_from`` — Альфред только что поднялся в кабинет сам и обязан
    сказать об этом следующим ходом."""

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
    """Гость молчит дольше порога — Альфред вне кабинета пора вернуть."""
    last = parse_iso(at.touched) or parse_iso(at.since)
    return last is not None and now - last >= timedelta(hours=hours)


def new_at(place: str, now: datetime, **extra: Any) -> At:
    stamp = iso(now)
    return At(place=place, since=stamp, touched=stamp, **extra)


# --- комнаты ---


@dataclass
class Room(Cabinet):
    """Комната, кроме кабинета. Особенности/следы/снимки — как у кабинета и в
    том же JSON, ключ ``location:<id>:<гость>``; временная комната (``temp``)
    в хранилище не попадает — она в ``Run.world``."""

    canon: RoomCanon = field(default=CELLAR_CANON)
    temp: bool = False

    @property
    def place(self) -> str:
        return self.canon.id

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
    # Формат хранения — ровно кабинетский (cabinet.save): id и канон комнаты в
    # JSON не пишем — id в ключе, канон в коде.
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


# --- временные комнаты (Run.world["rooms"]) ---


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].rstrip(" .,;")


def temp_spec(raw: Any) -> dict[str, str] | None:
    """Описание временного места от Ведущего (``move_to``) → чистый словарь.
    Нужны хотя бы название и канон; иначе None — перемещения нет."""
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
    """Id временной комнаты по названию: повторный заход — та же комната."""
    rooms: dict[str, Any] = world.setdefault("rooms", {})
    for tid, data in rooms.items():
        if str(data.get("name", "")).casefold() == name.casefold():
            return tid
    tid = f"{TEMP_PREFIX}{len(rooms) + 1}"
    while tid in rooms:
        tid += "_"
    return tid


def put_temp_room(world: dict[str, Any], spec: dict[str, str]) -> str:
    """Записать (или обновить канон) временную комнату в мир сцены, вернуть id."""
    tid = temp_id_for(world, spec["name"])
    rooms: dict[str, Any] = world["rooms"]
    entry = rooms.setdefault(tid, {"features": [], "scene": [], "searched": []})
    entry.update(spec)
    # Лимит: старые уходят первыми (текущую оставляем).
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
    """Вернуть изменения комнаты (особенности, следы) в мир сцены."""
    data = (world.get("rooms") or {}).get(room.canon.id)
    if isinstance(data, dict):
        data["features"] = list(room.features)
        data["scene"] = list(room.scene)


# --- обыск ---

_PREPOSITIONS = frozenset({"в", "во", "на", "под", "за", "у", "около", "возле", "над", "из"})
_NOT_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_where(where: str) -> str:
    """«В ящике стола!» и «ящик стола» — одно и то же место."""
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
    text: str  # что нашёл Альфред — словами для него


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
) -> SearchResult:
    """Итог обыска ``where`` в комнате по таблице находок. ``searched``
    (нормализованные названия обысканного) дописывается — повторный обыск
    того же ничего не даёт."""
    norm = normalize_where(where) or "здесь"
    if norm in searched:
        return SearchResult(SEARCH_KIND_REPEAT, SEARCH_REPEAT_TEXT)
    searched.append(norm)
    for find in canon.specials:
        if find.pattern.search(where):
            return SearchResult(SEARCH_KIND_SPECIAL, find.text)
    if canon.trinkets and rng() < canon.trinket_chance:
        item = choose(canon.trinkets)
        return SearchResult(SEARCH_KIND_TRINKET, SEARCH_TRINKET_TEXT.format(item=item))
    return SearchResult(SEARCH_KIND_EMPTY, SEARCH_EMPTY_TEXT)


# --- сброс прогресса (отладка, /interactives reset) ---

RESET_SCOPES = ("cellar", "catacombs", "all")

# Флаги гостя (user_effect:<ключ>:<гость>).
EFFECT_CELLAR_UNLOCKED = "cellar_unlocked"
EFFECT_CATACOMBS_OPEN = "catacombs_open"
CATACOMBS_KEY = "catacombs:{user_id}"
# Сколько обычных ходов подряд было в чате без сцены (спонтанный триггер 59.2).
PLAIN_TURNS_KEY = "interactives_plain:{chat_id}"


async def reset_progress(
    store: Store, user_id: int, scope: str, chat_id: int | None = None
) -> list[str]:
    """Стереть прогресс гостя по Этапу 59: ``cellar`` — подвал (ход сцены,
    завершённость, флаги, положение, комнаты подвала), ``catacombs`` —
    катакомбы (лабиринт, их комнаты, сцена), ``all`` — всё и сразу. Кабинет и
    его данные не трогаются никогда. Возвращает стёртые ключи (для отчёта)."""
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
            # Комната, которой нет в реестре, — особое место катакомб, ещё не
            # зарегистрированное в этом процессе.
            group = canon.group if canon is not None else GROUP_CATACOMBS
            if group in groups:
                await drop(key)
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


# --- тул search (engine.Interactives.tool_search) ---

SEARCH_WHERE_MAX = 120
TOOL_SEARCH_UNAVAILABLE = "недоступно: порыскать можно только в живом разговоре"
TOOL_SEARCH_NO_WHERE = "Скажи, что именно ты обыскиваешь: ящик, полку, бочку, угол…"
# В сцене итог обыска решил код и ушёл Ведущему; Альфред узнает его
# следующим ходом, как узнаёт всё, что происходит вокруг него.
TOOL_SEARCH_SCENE = (
    "Ты принялся обыскивать «{where}». Что ты там обнаружишь, выяснится "
    "через мгновение — сейчас коротко скажи, что шаришь там, и не выдумывай "
    "никаких находок."
)
# Вне сцены Ведущего нет — итог отдаётся сразу.
TOOL_SEARCH_FREE = (
    "Ты обыскал «{where}». {found} Расскажи об этом собеседнику своими "
    "словами, ничего не добавляя от себя."
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
