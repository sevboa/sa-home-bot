"""Прогулка по катакомбам, особые места и быстрые переходы (Этап 59.4–59.5,
IMPLEMENTATION_PLAN.md). Топологию лабиринта держит catacombs.py (чистый код),
комнаты и положение Альфреда — places.py; здесь они подключаются к разговору.

Что здесь живёт:

- **сценарий-прогулка** ``catacombs`` (``CATACOMBS``): не лестница стадий, а
  ``Run``, который живёт, пока Альфред под землёй. Формы согласия у него нет
  (``trigger="manual"``): Альфред сам предлагает спуститься, войдя в подвал, а
  согласие гостя — обычная реплика; дальше модель зовёт тул ``go`` (решение
  2026-10-10: тул, а не распознавание слов в before_turn, — модели так проще,
  а гость формулирует как угодно). Прогулка завершается, когда Альфред
  поднимается (walk up), уходит быстрым переходом или его возвращает простой
  (engine._tend_position);
- **тулы** ``walk`` и ``go`` (канарейка, tools.CANARY_TOOLS). ``walk`` ходит по
  ``catacombs.walk``; описание нового прохода или узла Ведущий присылает
  синхронно, прямо в ходе тула (``describe_new``): иначе Альфред отвечал бы
  «иду налево» вслепую, а о том, что увидел, рассказывал бы только на
  следующей реплике гостя. Запоминается один раз (``set_description``). Сбой
  Ведущего — запасное описание, ход не блокируется;
- **кадры**: у узла и прохода своя картинка (``image_id`` + ``file_id``),
  повторный показ — тот же ``file_id`` без генерации (mycraft не будим), узел
  рисуется заново, только если ``node_needs_frame``. Кадры коридоров без
  Альфреда, находки в промпт не попадают (они решаются позже, при обыске). Кадры
  уходят гостю ПОСЛЕ реплики Альфреда (engine.flush_forms): иначе готовая
  картинка по ``file_id`` обогнала бы его речь;
- **особые места** (59.5): ``RoomCanon`` группы катакомб по каталогу
  ``catacombs.SPECIAL_ROOMS``, зарегистрированные в ``places.PLACES``. Альфред
  внутри — ``alfred_at.place == "catacombs"`` и ``CatacombsState.room``; снимок
  комнаты каждый раз новый, в ней можно порыскать (обычный ``search``);
- **быстрые переходы** ``go``: из любого места — в подвал, в катакомбы (всегда
  к лазу, ``reset_position``) и в найденное особое место (``go_to_room``).
  Одна реплика «знакомой дорогой», кадров по пути нет.

Миксин ``UndergroundMixin`` подмешивается в engine.Interactives: так правок в
engine.py — несколько строк, а вся логика катакомб читается в одном файле.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from sa_home_bot.bot.interactives import catacombs, cellar, places
from sa_home_bot.bot.interactives.base import (
    STATUS_ACTIVE,
    STATUS_DONE,
    Run,
    Scenario,
    iso,
)
from sa_home_bot.bot.interactives.catacombs import (
    CatacombsState,
    Maze,
    Node,
    Outcome,
    Passage,
    WalkResult,
)
from sa_home_bot.bot.interactives.director import ask_json
from sa_home_bot.bot.interactives.places import Room, RoomCanon
from sa_home_bot.proto.messages import Address

if TYPE_CHECKING:
    from sa_home_bot.bot.interactives.cabinet import Cabinet

log = logging.getLogger(__name__)

SCENARIO_ID = "catacombs"
PLACE_CATACOMBS = "catacombs"

# --- особые места (59.5) ---
#
# Канон каждого особого места — в коде, особенности гостя дописывает Ведущий
# (cabinet_add), как у кабинета. Снимок каждый раз новый и не запоминается; в
# комнате можно порыскать (search). Таблица находок ``specials`` у всех пока
# пустая: «почти всегда пусто или мелочь», особые вещи — отдельным шагом.

DARKROOM_CANON = RoomCanon(
    id="darkroom",
    name_ru="старая проявочная",
    in_ru="в проявочной",
    canon_ru=(
        "Старая проявочная: тесная каменная комната, над столом тускло горит "
        "красный фонарь, на столе ванночки с мутными растворами, под потолком "
        "натянуты верёвки с прищепками и обрывками плёнок, в углу увеличитель."
    ),
    canon_en=(
        "old photographic darkroom, dim red safelight glowing, developing trays, "
        "film strips hanging on ropes, photo enlarger"
    ),
    caption="Проявочная",
    scene_caption="В проявочной",
    stem="проявоч",
    in_en="in an old photographic darkroom",
    group=places.GROUP_CATACOMBS,
    trinkets=(
        "обрывок засвеченной плёнки",
        "пустой пузырёк из-под проявителя",
        "бельевую прищепку",
        "выцветший снимок, на котором ничего не разобрать",
    ),
    # 59.6: фотоаппарат подключается после слияния. Первый обыск проявочной —
    # всегда фотоаппарат: сюда добавится ``Find`` (или особая ветка в
    # places.search_room), а Альфред вспомнит, что оставил его здесь сам.
    specials=(),
    of_ru="проявочной",
    from_ru="из проявочной",
)

LABORATORY_CANON = RoomCanon(
    id="laboratory",
    name_ru="заброшенная лаборатория",
    in_ru="в лаборатории",
    canon_ru=(
        "Заброшенная лаборатория алхимика: каменный стол в потёках, склянки и "
        "реторты, треснувшие колбы, ржавые весы, закопчённые полки вдоль стен."
    ),
    canon_en=(
        "abandoned alchemist laboratory, stone table with glass flasks and retorts, "
        "cracked beakers, soot-stained shelves"
    ),
    caption="Лаборатория",
    scene_caption="В лаборатории",
    stem="лаборатор",
    in_en="in an abandoned alchemist laboratory",
    group=places.GROUP_CATACOMBS,
    trinkets=(
        "осколок мутного стекла",
        "тусклую медную гирьку",
        "сухой пучок неизвестных трав",
        "обугленную записную книжку без единой читаемой страницы",
    ),
    of_ru="лаборатории",
    from_ru="из лаборатории",
)

CAVE_CANON = RoomCanon(
    id="cave",
    name_ru="пещера",
    in_ru="в пещере",
    canon_ru=(
        "Пещера: естественный свод, с потолка свисают сталактиты, стены мокрые "
        "и блестят, на неровном полу лужи, в глубине слышна капель."
    ),
    canon_en=(
        "natural limestone cave, stalactites hanging from the ceiling, wet glistening rock walls, "
        "puddles on uneven floor"
    ),
    caption="Пещера",
    scene_caption="В пещере",
    stem="пещер",
    in_en="in a damp natural cave",
    group=places.GROUP_CATACOMBS,
    trinkets=(
        "обломок сталактита",
        "гладкий речной камешек",
        "кость какого-то мелкого зверька",
    ),
    of_ru="пещеры",
    from_ru="из пещеры",
)

WINE_CELLAR_CANON = RoomCanon(
    id="wine_cellar",
    name_ru="винный погреб",
    in_ru="в винном погребе",
    canon_ru=(
        "Старый винный погреб: кирпичные своды, вдоль стен деревянные стеллажи "
        "с пыльными бутылками, дубовые бочки с обручами, холодно и сухо."
    ),
    canon_en=(
        "old brick wine cellar, wooden racks of dusty wine bottles, oak barrels, vaulted ceiling"
    ),
    caption="Винный погреб",
    scene_caption="В винном погребе",
    stem="винн",
    in_en="in an old wine cellar",
    group=places.GROUP_CATACOMBS,
    trinkets=(
        "пробку от давно выпитой бутылки",
        "пустую бутылку с выцветшей этикеткой",
        "ржавый штопор",
    ),
    of_ru="винного погреба",
    from_ru="из винного погреба",
)

SPECIAL_CANONS: dict[str, RoomCanon] = {
    canon.id: canon for canon in (DARKROOM_CANON, LABORATORY_CANON, CAVE_CANON, WINE_CELLAR_CANON)
}

# Катакомбы как место: комната-«оболочка» лабиринта. Канон текущего узла или
# прохода собирается на ходу (``labyrinth_canon``), в хранилище комнаты нет —
# всё живёт в ``catacombs:<гость>``. В ``places.PLACES`` не регистрируется:
# ``engine._room`` обрабатывает ``alfred_at.place == "catacombs"`` отдельно.
CATACOMBS_CANON = RoomCanon(
    id=PLACE_CATACOMBS,
    name_ru="катакомбы",
    in_ru="в катакомбах",
    canon_ru="Катакомбы под замком: сырые каменные коридоры, темнота, фонарь в руке.",
    canon_en="narrow stone catacomb corridor, rough walls, low arched ceiling",
    caption="Катакомбы",
    scene_caption="В катакомбах",
    stem="катаком",
    in_en="in the catacombs",
    group=places.GROUP_CATACOMBS,
    # Обыск в лабиринте: «почти всегда пусто или мелочь» (план 59.4).
    trinkets=(
        "обломок кости",
        "потемневшую монетку",
        "огарок свечи",
        "ржавый гвоздь",
        "обрывок истлевшей ткани",
    ),
    trinket_chance=0.2,
    of_ru="катакомб",
    from_ru="из катакомб",
)

for _canon in SPECIAL_CANONS.values():
    places.register(_canon)


def special_canon(room_id: str) -> RoomCanon | None:
    return SPECIAL_CANONS.get(room_id)


# --- сценарий-прогулка ---

# Формы согласия нет, слова гостя прогулку не запускают (триггер — тул go).
_NEVER = re.compile(r"(?!)")

_SCENE_FRAME = (
    "Ты спустился в катакомбы под замком с фонарём в руке. Где ты стоишь "
    "сейчас, что видно вокруг и куда можно пойти, сказано ниже в «Где ты». "
    "Ходи только инструментом walk: left, right, forward, back (от того, куда "
    "ты смотришь), lever — потянуть рычаг в стене, перед которой стоишь, "
    "enter — войти в особое место у тебя на пути, up — подняться в подвал. "
    "Идёшь туда, куда просит собеседник; на развилке спрашивай его, куда "
    "свернуть. Коридоры, выходы, рычаги, места и находки НЕ выдумывай: всё, "
    "что есть вокруг, сказано в «Где ты» и в ответах инструментов. "
    "Порыскать где-нибудь — инструмент search, быстро вернуться в подвал или "
    "в кабинет — go. "
    "Для собеседника это обычный разговор с тобой: никогда не говори, что это "
    "игра, квест, сценка, сюжет, уровень или локация, не упоминай карту и "
    "никаких подсказок."
)

_STEP_HINT = (
    "Ты бредёшь по катакомбам при свете фонаря. Рассказывай, что видишь, "
    "коротко и по делу, сдержанно, с сухим юмором дворецкого. Когда идти "
    "дальше — решает собеседник; если он не говорит, предложи варианты из "
    "«Где ты»."
)

_DIRECTOR_PROMPT = (
    "Ты — Ведущий неспешной истории в Telegram-чате. Ты режиссёр и "
    "рассказчик, не персонаж. Участники: гость (живой человек) и Альфред — "
    "ИИ-дворецкий старого замка в Трансильвании. Альфред бродит по катакомбам "
    "под замком с фонарём. Тон: сумрак, сырость, лёгкая жуть и сухой юмор; "
    "ничего кровавого.\n\n"
    "Топологию (проходы, выходы, рычаги, особые места) и находки определяет "
    "КОД, а не ты: в блоке «Шаг» сказано, что произошло, ты ничего из этого не "
    "меняешь и не придумываешь новых выходов, дверей и находок.\n\n"
    "Твои задачи на каждый ход:\n"
    "1. effect — коротко (1–2 предложения) опиши, что окружает Альфреда или "
    "происходит рядом: капель, тень, далёкий скрежет, гулкое эхо, — от лица "
    "рассказчика, без обращения к гостю; Альфред сам пересказывает это на "
    "следующем ходу. Если Альфред ничего не делал — null. Не давай разгадок, "
    "не добавляй предметов, которые можно найти.\n"
    "2. stage всегда 0, finale всегда false.\n"
    "3. directive — одна скрытая подсказка Альфреду на следующий ход (не "
    "раскрывай механику).\n"
    "4. photo — кадр события с Альфредом спиной или боком; только в по-настоящему "
    "значимый момент, обычно null. Кадры самих коридоров рисует код.\n"
    "5. active=false — только если гость явно ушёл в другую тему надолго; "
    "тогда Альфред сам поднимется в кабинет.\n\n"
    "Свет под землёй — только фонарь Альфреда, без времени суток и погоды. Не "
    "называй гостя по имени, не пиши за Альфреда и за гостя, не упоминай, что "
    "это игра, квест, локация или карта. Пиши по-русски. Отвечай строго одним "
    "JSON-объектом."
)


def extra_input(run: Run) -> str:
    """Блок ко входу Ведущего: где Альфред, последний шаг (решение кода),
    исход обыска."""
    lines = []
    here = run.world.get("here")
    if here:
        lines.append(f"Альфред сейчас: {here}.")
    step = run.world.get("step")
    if step and run.world.get("step_turn") == run.turns_total:
        lines.append("Шаг (определён кодом, НЕ меняй): " + str(step))
    outcome = run.world.get("search_outcome")
    if outcome:
        lines.append("Исход обыска (определён кодом, НЕ меняй): " + str(outcome))
    return "\n".join(lines) or "Альфред только что спустился."


CATACOMBS = Scenario(
    id=SCENARIO_ID,
    title="Катакомбы",
    offer_text="",
    trigger_re=_NEVER,
    scene_frame=_SCENE_FRAME,
    ladder=(_STEP_HINT,),
    finale_directive="",
    director_prompt=_DIRECTOR_PROMPT,
    fallback_faults=(),
    # Одна «стадия» и никакого финала: прогулка кончается уходом наверх.
    min_turns_before_finale=10**9,
    min_turns_on_stage=1,
    stage_soft_cap=99,
    canary=True,
    trigger="manual",
    auto_stages=False,
    auto_finale=False,
    extra_input=extra_input,
)

# --- тексты тулов ---

TOOL_WALK_UNAVAILABLE = "недоступно: ходить по катакомбам можно только в личном разговоре"
TOOL_WALK_BAD_DIRECTION = (
    "Не понял, куда идти. Направления: left, right, forward, back, lever "
    "(потянуть рычаг), enter (войти в особое место), up (наверх, в подвал)."
)
TOOL_WALK_NOT_HERE = (
    "Ты сейчас не в катакомбах, идти некуда. Спуститься знакомой дорогой — "
    "инструмент go (place=catacombs), если собеседник согласен."
)
TOOL_GO_UNAVAILABLE = "недоступно: перейти можно только в личном разговоре"
TOOL_GO_UNKNOWN = (
    "Не знаю такого места. Доступно: cabinet (кабинет), cellar (подвал, если он "
    "открыт), catacombs (катакомбы, к лазу) и найденные особые места по id."
)
TOOL_GO_BUSY = "Сейчас ты занят другим делом и никуда не пойдёшь — закончи его сначала."
TOOL_GO_CELLAR_LOCKED = "Подвал заперт — ключа у тебя нет, пройти туда ты не можешь."
TOOL_GO_NO_WAY = "Ты не знаешь дороги туда: путь в катакомбы тебе ещё не открылся."
TOOL_GO_NOT_FOUND = "Такого места ты ещё не находил, дороги туда не знаешь."
TOOL_GO_HOME = "Ты поднялся в свой кабинет и снова у себя за столом."
TOOL_GO_CELLAR = (
    "Знакомой дорогой ты спустился в подвал. Скажи об этом одной репликой, без "
    "подробностей пути. Вот где ты теперь: {where}"
)
TOOL_GO_CATACOMBS = (
    "Знакомой дорогой ты дошёл до лаза и спустился в катакомбы, к самому началу. "
    "Скажи об этом одной репликой, без подробностей пути. Вот что вокруг: {where}"
    "{frame_note}"
)
TOOL_GO_ROOM = (
    "Знакомой дорогой ты дошёл до места «{name}» и вошёл внутрь. Скажи об этом "
    "одной репликой, без подробностей пути. Вот что вокруг: {where}"
)
TOOL_FRAME_NOTE = " Картинка этого места придёт собеседнику сама, её описывать не нужно."
TOOL_FRAME_STARTED = (
    "Снимок придёт собеседнику сам. Скажи пару слов о том, что снимаешь, "
    "ничего не выдумывая сверх сказанного ниже в «Где ты»."
)
TOOL_WALK_CLOSING = (
    " Скажи собеседнику коротко, что видишь и куда отсюда можно пойти; ничего не "
    "выдумывай сверх сказанного."
)
TOOL_WALK_ROOM_HINT = " Здесь можно порыскать (инструмент search), выйти обратно — walk(back)."
TOOL_WALK_ASCEND = (
    "Ты поднялся по ступеням наверх, в подвал. Скажи об этом одной репликой. "
    "Вот где ты теперь: {where}"
)

WALK_DECLARATION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "walk",
        "description": (
            "Идти по катакомбам под замком (только когда ты в них спустился). "
            "left, right, forward, back — налево, направо, прямо, назад от того, "
            "куда ты сейчас смотришь; lever — потянуть рычаг в стене, перед "
            "которой ты стоишь; enter — войти в особое место, если вход прямо "
            "здесь; up — подняться наверх, в подвал. Что вокруг, сказано в «Где "
            "ты» и в ответе этого инструмента — ничего не выдумывай. Вызывай, "
            "когда собеседник говорит, куда идти, или ты сам решил пойти."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": list(catacombs.WALK_DIRECTIONS),
                    "description": "куда идти или что сделать",
                }
            },
            "required": ["direction"],
        },
    },
}

GO_DECLARATION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "go",
        "description": (
            "Знакомой дорогой перейти в другое место одним шагом, без блуждания: "
            "cabinet — к себе в кабинет; cellar — в подвал (если он уже открыт); "
            "catacombs — в катакомбы, всегда к лазу в начале; либо id найденного "
            "особого места (например darkroom), оно названо в «Где ты» в подвале. "
            "Вызывай, когда собеседник просит «в подвал», «в катакомбы», «в "
            "проявочную» и когда он согласился спуститься. Бродить по самим "
            "катакомбам — инструмент walk, а не этот."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "description": "cabinet | cellar | catacombs | id найденного особого места",
                }
            },
            "required": ["place"],
        },
    },
}

# --- направления ---

_DIRECTION_ALIASES = {
    "налево": "left",
    "влево": "left",
    "направо": "right",
    "вправо": "right",
    "прямо": "forward",
    "вперёд": "forward",
    "вперед": "forward",
    "назад": "back",
    "рычаг": "lever",
    "потянуть рычаг": "lever",
    "войти": "enter",
    "внутрь": "enter",
    "вход": "enter",
    "наверх": "up",
    "вверх": "up",
    "в подвал": "up",
}


def normalize_direction(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.casefold().split()).strip(" .!")
    if text in catacombs.WALK_DIRECTIONS:
        return text
    return _DIRECTION_ALIASES.get(text)


def resolve_target(raw: Any) -> str | None:
    """Куда просят перейти: ``cabinet`` / ``cellar`` / ``catacombs`` / id
    особого места (в том числе по названию: «проявочная», «винный погреб»)."""
    if not isinstance(raw, str):
        return None
    text = places.normalize_where(raw)
    if not text:
        return None
    if text in ("cabinet", "кабинет", "домой") or text.startswith("кабинет"):
        return "cabinet"
    if text == "cellar" or text.startswith("подвал"):
        return "cellar"
    if text == "catacombs" or "катаком" in text:
        return "catacombs"
    for room in catacombs.SPECIAL_ROOMS:
        canon = SPECIAL_CANONS.get(room.id)
        if text == room.id or room.id.replace("_", " ") == text:
            return room.id
        if canon is not None and canon.stem in text:
            return room.id
    return None


# --- положение Альфреда под землёй (alfred_at) ---


def state_of(at: places.At | None, maze: Maze) -> CatacombsState:
    """``CatacombsState`` из ``alfred_at`` (поле ``node`` хранит ``to_dict``).
    Битое или устаревшее положение (узла нет в лабиринте) — вход."""
    if at is not None and isinstance(at.node, dict):
        try:
            state = CatacombsState.from_dict(at.node)
        except (TypeError, ValueError, IndexError, KeyError):
            return CatacombsState.at_entrance(maze)
        if state.node not in maze.nodes:
            return CatacombsState.at_entrance(maze)
        if state.passage and state.passage not in maze.passages:
            state.passage = None
        if state.room and (
            state.room not in places.PLACES
            or maze.special_nodes.get(state.room) != state.node
            or state.room not in maze.found
        ):
            state.room = None
        return state
    return CatacombsState.at_entrance(maze)


def with_state(at: places.At, state: CatacombsState) -> places.At:
    at.node = state.to_dict()
    at.heading = state.heading
    return at


def current_place(maze: Maze, state: CatacombsState) -> Node | Passage:
    """Где «стоит» Альфред: в проходе у стены с рычагом или в узле."""
    if state.passage and state.passage in maze.passages:
        return maze.passages[state.passage]
    return maze.nodes[state.node]


# --- английский вид места для кадра ---

_COUNT_EN = {1: "one", 2: "two", 3: "three", 4: "four"}


def node_kind(maze: Maze, node: Node) -> str:
    """entrance | special | dead_end | bend | straight | junction."""
    if node.cell == (0, 0):
        return "entrance"
    if node.special:
        return "special"
    sides = list(node.exits)
    if len(sides) <= 1:
        return "dead_end"
    if len(sides) == 2:
        return "straight" if catacombs.OPPOSITE[sides[0]] == sides[1] else "bend"
    return "junction"


_NODE_KIND_RU = {
    "entrance": "вход в катакомбы",
    "special": "тупик с входом",
    "dead_end": "тупик",
    "bend": "поворот",
    "straight": "коридор",
    "junction": "перекрёсток",
}


def node_base_en(maze: Maze, node: Node) -> str:
    kind = node_kind(maze, node)
    n = len(node.exits)
    if kind == "entrance":
        return (
            "bottom of a stone shaft in the catacombs, a rope hanging down from a hatch above, "
            "dark stone passages leading away"
        )
    if kind == "special":
        return "dead end of a stone catacomb corridor with a low old wooden door in the wall"
    if kind == "dead_end":
        return "dead end of a stone catacomb corridor, bare stone wall ahead"
    if kind == "bend":
        return "stone catacomb corridor turning a sharp corner"
    if kind == "straight":
        return "stone catacomb corridor continuing straight on into darkness"
    return f"stone catacomb junction with {_COUNT_EN.get(n, str(n))} dark passages leading away"


def passage_base_en(passage: Passage, *, wall: bool = False) -> str:
    length = {1: "short ", 3: "long "}.get(passage.length, "")
    base = f"{length}narrow stone catacomb corridor, rough walls, low arched ceiling"
    if wall:
        base += ", ending in a blank stone wall with a big iron lever"
    return base


def frame_canon_en(maze: Maze, place: Node | Passage, *, wall: bool = False) -> str:
    """Канон кадра места для промптера: структура места (код) → описание
    Ведущего (русское, промптер переведёт) → места поиска на виду. Находок нет
    и быть не может: они решаются только при обыске."""
    if isinstance(place, Node):
        base = node_base_en(maze, place)
    else:
        base = passage_base_en(place, wall=wall)
    parts = [base]
    if place.describe:
        parts.append(place.describe)
    spots = [s.name for s in place.spots]
    if spots:
        parts.append("visible: " + ", ".join(spots))
    return "; ".join(parts)


def frame_caption(maze: Maze, place: Node | Passage, *, wall: bool = False) -> str:
    if isinstance(place, Node):
        kind = node_kind(maze, place)
        if kind == "special":
            return "Катакомбы: " + _special_door_ru(place)
        return "Катакомбы: " + _NODE_KIND_RU[kind]
    return "Катакомбы: упор в стену с рычагом" if wall else "Катакомбы: коридор"


def _special_door_ru(node: Node) -> str:
    room = catacombs.special_room(node.special) if node.special else None
    return f"дверь, {room.name}" if room else "тупик"


UNDERGROUND_INTRO = "Ты в катакомбах под замком, при свете фонаря. "


def position_ru(maze: Maze, state: CatacombsState) -> str:
    """«Где ты» в лабиринте: что видно отсюда (look → text_ru) вместе с
    описанием узла или прохода."""
    return UNDERGROUND_INTRO + catacombs.look(state, maze).text_ru()


def labyrinth_canon(maze: Maze, state: CatacombsState) -> RoomCanon:
    place = current_place(maze, state)
    return replace(
        CATACOMBS_CANON,
        canon_ru=position_ru(maze, state),
        canon_en=frame_canon_en(maze, place, wall=isinstance(place, Passage) and place.sealed),
    )


def labyrinth_room(user_id: int, maze: Maze, state: CatacombsState) -> Room:
    # temp=True: в хранилище комната не пишется (``Interactives._save_room``
    # для временных кладёт в мир сцены, а там для неё записи нет — no-op).
    return Room(user_id=user_id, canon=labyrinth_canon(maze, state), temp=True)


# --- кадры ---

FRAMES_PER_TURN = 2


@dataclass
class Frame:
    """Один кадр к показу гостю. ``file_id`` — запомненная картинка: ушла бы
    прежней, без генерации. ``owner`` — "node:x,y" / "passage:<id>" (чей кадр
    запоминаем), ``spot`` — крупный план места поиска."""

    kind: str  # node | passage | spot | room
    owner: str
    key: str
    caption: str
    room: Room
    focus: str = ""
    spot: str | None = None
    file_id: str | None = None

    @property
    def remembered(self) -> bool:
        return self.kind != "room"

    @property
    def dedupe(self) -> tuple[str, str | None]:
        return (self.owner, self.spot)


@dataclass
class FramePlan:
    user_id: int
    frames: list[Frame]
    message_thread_id: int | None = None
    reply_to: int | None = None


def _cell_str(cell: tuple[int, int]) -> str:
    return f"{cell[0]},{cell[1]}"


def node_frame(user_id: int, maze: Maze, node: Node) -> Frame:
    cached = None if catacombs.node_needs_frame(node) else node.file_id
    state = CatacombsState(node=node.cell, heading=maze.entry_heading)
    return Frame(
        kind="node",
        owner="node:" + _cell_str(node.cell),
        key=catacombs.node_frame_key(node),
        caption=frame_caption(maze, node),
        room=labyrinth_room(user_id, maze, state),
        file_id=cached,
    )


def passage_frame(user_id: int, maze: Maze, passage: Passage, *, wall: bool = False) -> Frame:
    cached = None if catacombs.passage_needs_frame(passage) else passage.file_id
    state = CatacombsState(
        node=passage.a, heading=passage.a_side, passage=passage.id if wall else None
    )
    room = labyrinth_room(user_id, maze, state)
    if not wall:
        # Проход без стены: канон — сам коридор, а не узел, где он начинается.
        canon = replace(
            room.canon,
            canon_en=frame_canon_en(maze, passage),
            canon_ru=UNDERGROUND_INTRO + (passage.describe or "Каменный коридор."),
        )
        room = Room(user_id=user_id, canon=canon, temp=True)
    return Frame(
        kind="passage",
        owner="passage:" + passage.id,
        key=catacombs.passage_frame_key(passage),
        caption=frame_caption(maze, passage, wall=wall),
        room=room,
        file_id=cached,
    )


def spot_frame(user_id: int, maze: Maze, owner: Node | Passage, spot: catacombs.Spot) -> Frame:
    owner_ref = (
        "node:" + _cell_str(owner.cell) if isinstance(owner, Node) else "passage:" + owner.id
    )
    wall = isinstance(owner, Passage) and owner.sealed
    base = frame_canon_en(maze, owner, wall=wall)
    canon = replace(
        CATACOMBS_CANON,
        canon_ru=UNDERGROUND_INTRO + (owner.describe or ""),
        canon_en=base,
    )
    return Frame(
        kind="spot",
        owner=owner_ref,
        key=f"spot:{owner_ref}:{spot.name}",
        caption=spot.name.capitalize(),
        room=Room(user_id=user_id, canon=canon, temp=True),
        focus=spot.name,
        spot=spot.name,
        file_id=spot.file_id if spot.image_id is not None else None,
    )


def plan_frames(user_id: int, maze: Maze, result: WalkResult) -> list[Frame]:
    """Какие кадры показать после шага (комнату особого места рисует вызывающий).
    Новый проход показываем кадром коридора, потом — кадр узла, куда пришли
    (запомненный уходит по file_id); повторные проходы — только узел."""
    outcome = result.outcome
    frames: list[Frame] = []
    if outcome in (Outcome.MOVED, Outcome.NEW_PASSAGE, Outcome.CONNECTED):
        if result.passage is not None and catacombs.passage_needs_frame(result.passage):
            frames.append(passage_frame(user_id, maze, result.passage))
        if result.node is not None:
            frames.append(node_frame(user_id, maze, result.node))
    elif outcome == Outcome.LEVER_WALL and result.passage is not None:
        frames.append(passage_frame(user_id, maze, result.passage, wall=True))
    elif outcome == Outcome.LEFT_ROOM and result.node is not None:
        frames.append(node_frame(user_id, maze, result.node))
    return frames


def match_spot(spots: list[catacombs.Spot], where: str) -> catacombs.Spot | None:
    """Место поиска по тому, как его назвал Альфред или гость: «ящик» и «ящик у
    стены» — одно и то же."""
    wanted = _stems(where)
    if not wanted:
        return None
    for spot in spots:
        name = _stems(spot.name)
        if name and (name <= wanted or wanted <= name):
            return spot
    return None


def _stems(text: str) -> set[str]:
    """Слова названия по первым четырём буквам: «в ящике у стены» и «ящик у
    стены» — одно и то же место (падежи не мешают)."""
    return {w[:4] for w in places.normalize_where(text).split() if len(w) >= 3}


# --- описание нового места (Ведущий) ---

DESCRIBE_SYSTEM = (
    "Ты — Ведущий: придумываешь вид мест в катакомбах под старым замком в "
    "Трансильвании, где бродит дворецкий Альфред. Свет — только его фонарь. "
    "Пиши по-русски. Отвечай строго одним JSON-объектом."
)
DESCRIBE_MAX = 220
SPOT_MAX = 40
SPOTS_PER_PLACE = 2
NEIGHBORS_SHOWN = 5

FALLBACK_PASSAGE = (
    "Низкий каменный коридор, на стенах влажные потёки и рыжие следы ржавчины.",
    "Узкий проход в серой кладке, в щелях между камнями белеет плесень.",
    "Сырой коридор с неровным полом, по стенам тянутся тёмные трещины.",
)
FALLBACK_NODE = (
    "Небольшая каменная площадка, под ногами крошка и пыль.",
    "Сводчатое место, где сходятся ходы; кладка в потёках, на полу мелкие кости.",
    "Тесный закуток с отсыревшими стенами и тяжёлым воздухом.",
)


def _cells_ru(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} клетка"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} клетки"
    return f"{n} клеток"


def describe_hint(maze: Maze, key: str, place: Node | Passage) -> str:
    """Что за место просим описать — структура, определённая кодом."""
    if isinstance(place, Passage):
        text = f"Проход — прямой коридор длиной {_cells_ru(place.length)}"
        if place.sealed:
            text += ", он упирается в глухую стену с рычагом (плита или решётка)"
        return text + "."
    kind = node_kind(maze, place)
    if kind == "entrance":
        return (
            "Узел — подножие шахты: здесь Альфред спустился из подвала по лазу, "
            "наверху виден лаз; отсюда расходятся ходы."
        )
    if kind == "special":
        room = catacombs.special_room(place.special) if place.special else None
        name = room.name if room else "особое место"
        return f"Узел — тупик, в стене дверь или проём, ведущий в «{name}»."
    if kind == "dead_end":
        return "Узел — тупик, дальше хода нет."
    if kind == "bend":
        return "Узел — поворот коридора."
    if kind == "straight":
        return "Узел — место посреди коридора, ход идёт дальше прямо."
    return f"Узел — перекрёсток, отсюда расходятся {len(place.exits)} хода."


def build_describe_input(
    maze: Maze, todo: list[tuple[str, Node | Passage]], neighbors: list[str]
) -> str:
    lines = ["Опиши новые места катакомб (только запрошенные ключи)."]
    for key, place in todo:
        title = "passage" if key == "passage" else "node"
        lines.append(f'- "{title}": {describe_hint(maze, key, place)}')
    if neighbors:
        lines.append(
            "Рядом уже описаны (у каждого места свои приметы, не повторяйся): "
            + " | ".join(neighbors)
        )
    lines.append(
        "describe — 1–2 коротких фразы по-русски, только то, что ВИДНО при свете "
        "фонаря: цвет и вид камня или кирпича, кости, вода, плесень, факел в "
        "держателе, надписи, трещины. Без запахов и звуков, без слов про выходы и "
        "направления (их определяет код), без находок. spots — 0–2 места, где можно "
        "порыскать и которые видны с первого взгляда: 1–3 слова («ящик у стены», "
        "«ниша с черепами»). Для тупика, перекрёстка и коридора места поиска "
        "необязательны."
    )
    keys = ", ".join(f'"{"passage" if k == "passage" else "node"}"' for k, _ in todo)
    lines.append(
        f'Ответ — ОДИН JSON-объект с ключами {keys}; каждый: {{"describe": str, "spots": [str]}}.'
    )
    return "\n".join(lines)


def parse_described(data: dict | None, key: str) -> tuple[str, list[str]] | None:
    block = (data or {}).get("passage" if key == "passage" else "node")
    if not isinstance(block, dict):
        return None
    describe = " ".join(str(block.get("describe") or "").split())[:DESCRIBE_MAX].strip()
    if not describe:
        return None
    raw_spots = block.get("spots")
    if isinstance(raw_spots, str):
        raw_spots = [raw_spots]
    spots: list[str] = []
    for raw in raw_spots if isinstance(raw_spots, list) else []:
        spot = " ".join(str(raw).split())[:SPOT_MAX].strip(" .,;")
        if spot and spot.casefold() not in {s.casefold() for s in spots}:
            spots.append(spot)
    return describe, spots[:SPOTS_PER_PLACE]


def neighbor_descriptions(maze: Maze) -> list[str]:
    """Описанные места лабиринта (последние) — чтобы Ведущий не повторялся."""
    seen = [p.describe for p in maze.passages.values() if p.describe]
    seen += [n.describe for n in maze.nodes.values() if n.describe]
    return seen[-NEIGHBORS_SHOWN:]


# --- текст шага для модели ---


def outcome_ru(result: WalkResult, *, in_room: bool = False) -> str:
    way = catacombs.RELATIVE_RU.get(result.direction, "")
    o = result.outcome
    if o == Outcome.MOVED:
        return f"Ты пошёл {way} по знакомому проходу."
    if o == Outcome.NEW_PASSAGE:
        return f"Ты пошёл {way} по ещё не хоженому проходу и дошёл до нового места."
    if o == Outcome.CONNECTED:
        return f"Ты пошёл {way} по новому проходу и вышел в уже знакомое место."
    if o == Outcome.LEVER_WALL:
        return (
            f"Ты пошёл {way}, и проход упёрся в глухую каменную стену с рычагом. "
            "Ты стоишь перед ней: рычаг можно потянуть (lever) или вернуться (back)."
        )
    if o == Outcome.LEVER_PULLED:
        return (
            "Ты потянул рычаг: что-то лязгнуло, стена ушла в сторону, проход открыт. "
            "Дальше можно идти прямо (forward)."
        )
    if o == Outcome.ENTERED_ROOM:
        name = result.room.name if result.room else "особое место"
        return f"Ты вошёл: {name}."
    if o == Outcome.LEFT_ROOM:
        name = result.room.name if result.room else "комнаты"
        return f"Ты вышел из места «{name}» обратно в коридор."
    if o == Outcome.NO_EXIT:
        if in_room:
            return "Из комнаты один путь — назад (back)."
        return "Туда прохода нет: там глухая стена."
    if o == Outcome.BLOCKED_BY_LEVER:
        return "Прямо не пройти: путь закрывает стена. В ней рычаг (lever)."
    if o == Outcome.NO_LEVER:
        return "Рычага здесь нет."
    if o == Outcome.NO_ROOM:
        return "Входа в особое место здесь нет."
    return "Ты остался на месте."


def step_summary(result: WalkResult) -> str:
    """Шаг для Ведущего: фраза по-русски и плоская сводка ``to_dict``."""
    return f"{outcome_ru(result)} ({result.to_dict()})"


# --- миксин для Interactives ---


@dataclass
class _Runtime:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[int, FramePlan] = field(default_factory=dict)


class UndergroundMixin:
    """Катакомбы в ``engine.Interactives``. Все ``self._*`` — атрибуты и методы
    движка (хранилище, часы, комнаты, снимок)."""

    # --- служебное ---

    def _ug(self) -> _Runtime:
        runtime = self.__dict__.get("_ug_runtime")
        if runtime is None:
            runtime = self.__dict__["_ug_runtime"] = _Runtime()
        return runtime

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
            from sa_home_bot.bot.interactives import engine as eng

            data = await ask_json(
                link,
                Address(node=eng.LLM_NODE, service=eng.LLM_SERVICE),
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
        async with self._ug().lock:
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
            return TOOL_GO_UNKNOWN
        busy = await self._scene_run(chat_id, user_id)
        if busy is not None and busy.scenario != SCENARIO_ID:
            if busy.scenario == cellar.SCENARIO_ID and busy.finale:
                # Сказка про подвал дошла до финала — она закончена.
                busy.status = STATUS_DONE
                await self._state.save_run(busy)
            else:
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
        async with self._ug().lock:
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
        pending = self._ug().pending
        plan = pending.get(chat_id)
        if plan is None:
            plan = pending[chat_id] = FramePlan(user_id, [], message_thread_id, reply_to)
        for frame in frames:
            plan.frames = [f for f in plan.frames if f.dedupe != frame.dedupe]
            plan.frames.append(frame)
        del plan.frames[:-FRAMES_PER_TURN]

    def discard_frames(self, chat_id: int) -> None:
        self._ug().pending.pop(chat_id, None)

    async def flush_frames(self, chat_id: int) -> None:
        plan = self._ug().pending.pop(chat_id, None)
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
        async with self._ug().lock:
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
                owner.image_id, owner.file_id = image_id, file_id
                if isinstance(owner, Node):
                    # Ключ — на момент кадра: за время рисования узел мог
                    # получить выход, тогда он снова попросит кадр.
                    owner.image_key = frame.key
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
        async with self._ug().lock:
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
        run = await self._scene_run(chat_id, user_id)
        if run is not None and run.scenario == SCENARIO_ID:
            run.world["search_outcome"] = f"Альфред обыскал «{where}». {result.text}"
            await self._state.save_run(run)
        return places.TOOL_SEARCH_FREE.format(where=where, found=result.text)

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
        async with self._ug().lock:
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
