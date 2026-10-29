from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from sa_home_bot.bot.interactives import catacombs, frame_heroes, places
from sa_home_bot.bot.interactives.base import (
    Run,
    Scenario,
)
from sa_home_bot.bot.interactives.catacombs import (
    CatacombsState,
    Maze,
    Node,
    Outcome,
    Passage,
    WalkResult,
)
from sa_home_bot.bot.interactives.places import Room, RoomCanon

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

SCENARIO_ID = "catacombs"
PLACE_CATACOMBS = "catacombs"


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
    specials=(places.DARKROOM_CAMERA,),
    of_ru="проявочной",
    from_ru="из проявочной",
    names=(r"проявоч\w*",),
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
    names=(r"лаборатор\w*",),
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
    names=(r"пещер\w*",),
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
    names=(r"винн\w*\s+(?:погреб|подвал)\w*",),
)

SPECIAL_CANONS: dict[str, RoomCanon] = {
    canon.id: canon for canon in (DARKROOM_CANON, LABORATORY_CANON, CAVE_CANON, WINE_CELLAR_CANON)
}

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
    names=(r"(?:катаком|коридор|подземел|тоннел|туннел)\w*",),
)

places.register_nameable(CATACOMBS_CANON)
for _canon in SPECIAL_CANONS.values():
    places.register(_canon)


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
    "в кабинет — go. Любое другое физическое действие (взять, подвинуть, "
    "открыть, поднять, посветить) ты совершаешь только инструментом act — по "
    "одному за вызов, следующее после результата предыдущего. Намерение "
    "говорить можно, а исход — только из результата act: пока его нет, не "
    "пиши, что получилось и что ты нашёл. "
    "Для собеседника это обычный разговор с тобой: никогда не говори, что это "
    "игра, квест, сценка, сюжет, уровень или локация, не упоминай карту и "
    "никаких подсказок."
)

_ACT_PROMPT = (
    "Ты — Ведущий неспешной истории в Telegram-чате. Ты режиссёр и "
    "рассказчик, не персонаж. Участники: гость (живой человек) и Альфред — "
    "ИИ-дворецкий старого замка в Трансильвании. Альфред бродит по подвалу и "
    "катакомбам под замком с фонарём. Тон: сумрак, сырость, лёгкая жуть и "
    "сухой юмор; ничего кровавого.\n\n"
    "Сейчас Альфред совершает ОДНО физическое действие (взять, подвинуть, "
    "открыть, поднять, посветить…), и его исход определяешь ты. Топологию "
    "(проходы, выходы, рычаги, особые места) и находки определяет КОД: ты не "
    "создаёшь новых выходов, дверей, рычагов и найденных вещей; если в блоке "
    "«Исход определён кодом» сказано, что находит Альфред, ты не меняешь это, "
    "а придумываешь, как оно выглядит.\n\n"
    "Твои задачи:\n"
    "1. result — факт в 1–2 предложения от рассказчика: что получилось и что "
    "Альфред увидел, без обращения к гостю и без реплик Альфреда. Исход ТОЛЬКО "
    "этого действия. Действие может не удаться или обернуться мелкой "
    "неожиданностью (скрип, капля, паучок), но не обязано; ничего опасного и "
    "ничего ценного, чего нет в «Исходе». Не противоречь фактам мира и журналу.\n"
    "2. stage всегда 0, finale всегда false.\n"
    "3. facts_add / facts_remove — устойчивые факты мира после действия "
    "(«плита сдвинута», «ниша пуста»); ставшее неправдой снимай.\n\n"
    "Свет под землёй — только фонарь Альфреда, без времени суток и погоды. Не "
    "называй гостя по имени, не упоминай, что это игра, квест, локация или "
    "карта. Пиши по-русски. Отвечай строго одним JSON-объектом."
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
    "происходит рядом САМО: капель, тень, далёкий скрежет, гулкое эхо, — от "
    "лица рассказчика, без обращения к гостю; Альфред сам пересказывает это на "
    "следующем ходу. НЕ исход действий Альфреда (их исход он уже получил от "
    "инструмента). Если нечего сказать — null. Не давай разгадок, не добавляй "
    "предметов, которые можно найти.\n"
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
    min_turns_before_finale=10**9,
    min_turns_on_stage=1,
    stage_soft_cap=99,
    canary=True,
    trigger="manual",
    auto_stages=False,
    auto_finale=False,
    extra_input=extra_input,
    act_prompt=_ACT_PROMPT,
)


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
                    "description": (
                        "cabinet | cellar | catacombs | id найденного особого места | "
                        "любое другое место поместья словами (кухня, двор) — туда "
                        "дорогу решит Ведущий"
                    ),
                }
            },
            "required": ["place"],
        },
    },
}


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


def go_action(raw: Any) -> str:
    text = " ".join(str(raw or "").split())
    if resolve_target(text) == "cellar":
        return "пойти к двери подвала"
    return f"пойти: {text}" if text else "пойти дальше"


def resolve_target(raw: Any) -> str | None:
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


def state_of(at: places.At | None, maze: Maze) -> CatacombsState:
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
    if state.passage and state.passage in maze.passages:
        return maze.passages[state.passage]
    return maze.nodes[state.node]


_COUNT_EN = {1: "one", 2: "two", 3: "three", 4: "four"}


def node_kind(maze: Maze, node: Node) -> str:
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
    return UNDERGROUND_INTRO + catacombs.look(state, maze).text_ru()


def labyrinth_canon(maze: Maze, state: CatacombsState) -> RoomCanon:
    place = current_place(maze, state)
    return replace(
        CATACOMBS_CANON,
        canon_ru=position_ru(maze, state),
        canon_en=frame_canon_en(maze, place, wall=isinstance(place, Passage) and place.sealed),
    )


def labyrinth_room(user_id: int, maze: Maze, state: CatacombsState) -> Room:
    return Room(user_id=user_id, canon=labyrinth_canon(maze, state), temp=True)


FRAMES_PER_TURN = 1


@dataclass
class Frame:
    kind: str
    owner: str
    key: str
    caption: str
    room: Room
    focus: str = ""
    spot: str | None = None
    file_id: str | None = None
    request_en: str = ""

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


_HERO_VIEWS = {"entrance": "shaft", "special": "door", "dead_end": "dead_end"}


def hero_view(maze: Maze, place: Node | Passage, *, wall: bool = False) -> str:
    if isinstance(place, Passage):
        return "lever" if wall else "open"
    return _HERO_VIEWS.get(node_kind(maze, place), "open")


def _hero_salt(maze: Maze, owner: str) -> str:
    return f"{maze.seed}:{owner}"


def _neighbor_heroes(maze: Maze, place: Node | Passage) -> set[str]:
    if not isinstance(place, Node):
        return set()
    out: set[str] = set()
    for pid in place.exits.values():
        passage = maze.passages.get(pid) if pid else None
        if passage is None:
            continue
        other = maze.nodes.get(passage.other(place.cell))
        if other is not None:
            out.add(frame_heroes.own_hero(_hero_salt(maze, "node:" + _cell_str(other.cell))))
    return out


def frame_request_en(maze: Maze, place: Node | Passage, owner: str, *, wall: bool = False) -> str:
    special = place.special if isinstance(place, Node) else None
    hero = frame_heroes.place_hero(
        _hero_salt(maze, owner),
        hero_view(maze, place, wall=wall),
        special,
        _neighbor_heroes(maze, place),
    )
    return frame_heroes.frame_request(hero, place.describe or "")


def node_frame(user_id: int, maze: Maze, node: Node) -> Frame:
    cached = None if catacombs.node_needs_frame(node) else node.file_id
    state = CatacombsState(node=node.cell, heading=maze.entry_heading)
    owner = "node:" + _cell_str(node.cell)
    return Frame(
        kind="node",
        owner=owner,
        key=catacombs.node_frame_key(node),
        caption=frame_caption(maze, node),
        room=labyrinth_room(user_id, maze, state),
        file_id=cached,
        request_en=frame_request_en(maze, node, owner),
    )


def passage_frame(user_id: int, maze: Maze, passage: Passage, *, wall: bool = False) -> Frame:
    cached = None if catacombs.passage_needs_frame(passage) else passage.file_id
    state = CatacombsState(
        node=passage.a, heading=passage.a_side, passage=passage.id if wall else None
    )
    room = labyrinth_room(user_id, maze, state)
    if not wall:
        canon = replace(
            room.canon,
            canon_en=frame_canon_en(maze, passage),
            canon_ru=UNDERGROUND_INTRO + (passage.describe or "Каменный коридор."),
        )
        room = Room(user_id=user_id, canon=canon, temp=True)
    owner = "passage:" + passage.id
    return Frame(
        kind="passage",
        owner=owner,
        key=catacombs.passage_frame_key(passage),
        caption=frame_caption(maze, passage, wall=wall),
        room=room,
        file_id=cached,
        request_en=frame_request_en(maze, passage, owner, wall=wall),
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
    outcome = result.outcome
    frames: list[Frame] = []
    if outcome in (Outcome.MOVED, Outcome.NEW_PASSAGE, Outcome.CONNECTED):
        if result.node is not None:
            frames.append(node_frame(user_id, maze, result.node))
    elif outcome == Outcome.LEVER_WALL and result.passage is not None:
        frames.append(passage_frame(user_id, maze, result.passage, wall=True))
    elif outcome == Outcome.LEFT_ROOM and result.node is not None:
        frames.append(node_frame(user_id, maze, result.node))
    return frames


def match_spot(spots: list[catacombs.Spot], where: str) -> catacombs.Spot | None:
    wanted = _stems(where)
    if not wanted:
        return None
    for spot in spots:
        name = _stems(spot.name)
        if name and (name <= wanted or wanted <= name):
            return spot
    return None


def _stems(text: str) -> set[str]:
    return {w[:4] for w in places.normalize_where(text).split() if len(w) >= 3}


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
    seen = [p.describe for p in maze.passages.values() if p.describe]
    seen += [n.describe for n in maze.nodes.values() if n.describe]
    return seen[-NEIGHBORS_SHOWN:]


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
    return f"{outcome_ru(result)} ({result.to_dict()})"
