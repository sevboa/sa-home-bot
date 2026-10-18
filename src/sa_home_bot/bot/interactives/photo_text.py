from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sa_home_bot.bot.interactives import cabinet as cabinet_mod
from sa_home_bot.bot.interactives import items as items_mod
from sa_home_bot.bot.interactives import places
from sa_home_bot.bot.interactives.base import Run
from sa_home_bot.bot.interactives.cabinet import Cabinet
from sa_home_bot.bot.interactives.director import DirectorDecision
from sa_home_bot.bot.interactives.places import Room
from sa_home_bot.bot.interactives.transylvania import Outside

PHOTO_PURPOSE = "photo"


PORTRAIT_PURPOSE = "portrait"


# Больше двух особенностей промптер под 77 токенов CLIP не удерживает:
# живые снимки 2026-09-30 из четырёх сохранили по две.
PHOTO_FEATURES_IN_FRAME = 2


# Факты мира сцены в общем виде (Этап 59.7) — тоже под бюджет промпта.
PHOTO_FACTS_IN_FRAME = 3


def _general_view_re(nouns: str) -> re.Pattern[str]:
    return re.compile(
        r"^(?:(?:мой|наш|твой|весь)\s+)?(?:общ\w+\s+(?:вид|план)\w*"
        r"|(?:(?:общ\w+\s+)?(?:вид|план|снимок|фото)\w*\s+)?(?:(?:на|всего|мо\w+|тво\w+)\s+)*"
        rf"(?:{nouns})\w*)",
        re.I,
    )


# Подпись или focus про весь кабинет — общий вид, а не крупный план
# (tool_take_photo, _photo). Хвост не важен: «Вид кабинета после инцидента»,
# «кабинет с корпусом передатчика на полу» — тоже общий вид; крупным планом
# их снимали без радио на столе (живой прогон 2026-10-01).
GENERAL_VIEW_RE = _general_view_re("кабинет|комнат")


# Вне кабинета (Этап 59.7) «кабинет» — другое место, а не общий вид того, где
# Альфред стоит: «Мой кабинет» в подвале рисовал подвал.
GENERAL_VIEW_ROOM_RE = _general_view_re("комнат")


def is_general_view(text: str, cab: Cabinet) -> bool:
    """Подпись или focus про весь вид места, где стоит Альфред."""
    return bool((GENERAL_VIEW_ROOM_RE if isinstance(cab, Room) else GENERAL_VIEW_RE).match(text))


# Вид ИЗ окна («вид из окна», «окно с видом на горизонт», «окно и вид за ним»,
# «за окном») — не крупный план предмета, хотя focus непустой: ему нужен свет
# общего вида с окном и луной по фазе (стенд 2026-10-10: как «крупный» он получал
# _CLOSEUP_LIGHT — без окна и без луны). «Подоконник», «оконная рама с трещиной»
# сюда не попадают: это предметы, их снимают крупно.
WINDOW_VIEW_RE = re.compile(
    r"\bиз\s+(?:\w+\s+){0,2}окн|\bза\s+(?:\w+\s+){0,2}окн|\bокн\w*\s+(?:и|с)\s+вид"
    r"|window\s+view|view\s+(?:from|through|out)",
    re.I,
)


# Кадры по ходу сцены (Ведущий, поле photo): переход стадии и финал снимаются
# всегда, прочие — не чаще раза в PHOTO_SCENE_GAP_TURNS ходов.
PHOTO_SCENE_GAP_TURNS = 3


PHOTO_SCENE_CAPTION = "В кабинете"


# Настроение кадра (Ведущий, director.MOODS) → модель и LoRA службы рисования.
# Связки и вес — стенд тем 2026-09-30, одобрен пользователем; «обычно» — эталон C.
# «Жуть» на giger убрана (пользователь 2026-10-08: рисует непрошеного
# человекоподобного «чёрта» почти в каждом кадре и не нравится по виду).
MOOD_PRESETS: dict[str, tuple[str, str, float]] = {
    "rot": ("revanim", "rottech", 0.8),
    "eldritch": ("ghostmix", "eldritch", 0.8),
}


# Крупный план предмета: фон без предмета (его вставят) — иначе промптер
# нарисует второй, свой передатчик.
ITEM_CLOSEUP_RU = "Крупный план: пустая столешница старого письменного стола"


ITEM_PENDING_TAG = "pending"


@dataclass(frozen=True)
class _ItemShot:
    """Предмет в кадре: вырезка ``key`` на ноде llm, место ``place``,
    ``tag`` — версия облика (для ключа повторного показа общего вида)."""

    kind: items_mod.ItemKind
    key: str
    place: str
    tag: str


def photo_negative(cab: Cabinet) -> str:
    """Негатив рисунка. Комнате без окна — ещё и окно с дневным светом (59.7)."""
    if cab.windowed:
        return cabinet_mod.PHOTO_NEGATIVE_EN
    return f"{cabinet_mod.PHOTO_NEGATIVE_EN}, {places.NO_WINDOW_NEGATIVE_EN}"


def photo_state_key(
    cab: Cabinet, outside: Outside, shot: _ItemShot | None, size: int | None = None
) -> str:
    """Ключ повторного показа общего вида: обстановка и свет, а если на стол
    вставляется предмет — ещё и версия его облика (Этап 49.3). ``size`` —
    снимок другого размера (старый фотоаппарат, 59.6) не подменяется прежним
    64-пиксельным."""
    key = cab.state_key(outside.key)
    if shot is not None and shot.place == "desk":
        key += f"#{shot.tag}"
    if size:
        key += f"#px{size}"
    return key


def photo_line_directive(
    focus: str, description: str, where: str, missing: list[str], misses: int
) -> str:
    """Директива подписи к снимку: что вышло — или чего не вышло (промах)."""
    template = cabinet_mod.PHOTO_READY_DIRECTIVE
    if missing:
        template = cabinet_mod.PHOTO_READY_MISS_DIRECTIVE
        if misses > 1:
            template = cabinet_mod.PHOTO_READY_MISS_AGAIN_DIRECTIVE
    return template.format(
        subject=focus or cabinet_mod.PHOTO_SUBJECT_ROOM,
        description=description,
        where=where,
        missing=", ".join(missing),
    )


def photo_expect(raw: Any) -> list[str]:
    """``expect`` тула take_photo → 1-3 непустых пункта."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    items = [" ".join(item.split()) for item in raw if isinstance(item, str) and item.strip()]
    return items[:3]


def _image_params(image: dict[str, Any]) -> dict[str, Any]:
    try:
        params = json.loads(image.get("params") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return params if isinstance(params, dict) else {}


def scene_photo_focus(
    run: Run, decision: DirectorDecision | None, *, key_moment: bool
) -> str | None:
    """Нужен ли кадр после хода сцены и что в нём крупно ("" — общий вид).
    Переход стадии и финал снимаются всегда; по желанию Ведущего — не чаще
    раза в PHOTO_SCENE_GAP_TURNS ходов."""
    wanted = decision.photo if decision is not None else None
    if key_moment:
        return wanted or ""
    if wanted is None:
        return None
    if run.photo_turn is not None and run.turns_total - run.photo_turn < PHOTO_SCENE_GAP_TURNS:
        return None
    return wanted


def retake_emphasis(prev: dict[str, Any] | None, expect: list[str]) -> list[str]:
    """Что из ``expect`` не вышло на прошлом снимке чата — снимаем снова.
    Модель при пересъёмке может перефразировать пункт, поэтому сверка без
    регистра и по вхождению (как в llm/photo_check.parse)."""
    if not prev or not expect:
        return []
    missed = [m.lower() for m in prev.get("missing") or [] if isinstance(m, str) and m.strip()]
    return [
        item
        for item in expect
        if any(m == item.lower() or m in item.lower() or item.lower() in m for m in missed)
    ]


def photo_description(
    cab: Cabinet,
    outside: Outside,
    focus: str,
    happening: str | None,
    *,
    item: items_mod.ItemKind | None = None,
    item_place: str | None = None,
    alfred: str | None = None,
    state: list[str] | None = None,
) -> tuple[str, str]:
    """Описание снимка для художника-промптера (llm/image_prompt.py) и
    контекст сцены. Общий вид — особенности гостя первыми (самые свежие:
    промпт ограничен 77 токенами; случившееся в сцене — ещё раньше), потом
    канон и свет; крупный план —
    предмет, а кабинет со светом — контекстом.

    ``item_place`` (Этап 49.3) — в кадр вставят предмет ``item`` пикселями
    (``desk`` — на стол общего вида, ``closeup`` — крупно): словами его в
    описании быть не должно, иначе промптер нарисует второй, свой.

    ``state`` (Этап 59.7) — факты мира идущей сцены («доски сорваны»): в общем
    виде последние ``PHOTO_FACTS_IN_FRAME`` идут после особенностей."""
    visible_features = cab.visible_features()
    if item is not None and item_place is not None:
        visible_features = [f for f in visible_features if not item.focus_re.search(f)]
        if happening and item.focus_re.search(happening):
            happening = None
        if item_place == "closeup":
            focus = ITEM_CLOSEUP_RU
    # Альфред спиной/боком (кадр события сцены): его фраза идёт сразу после
    # главного, а места ей даём, убрав особенности кабинета до одной.
    alfred_en = cabinet_mod.SCENE_ALFRED_EN.get(alfred or "")
    features = visible_features[-(1 if alfred_en else PHOTO_FEATURES_IN_FRAME) :]
    light = cab.light_en(outside, closeup=bool(focus))
    if focus:
        # Крупный план — только предмет, комната и свет: особенности кабинета
        # в контексте промптер тащил в кадр (снимок картины с рунами и
        # туманом, живая находка 2026-09-30).
        context = f"{cab.canon_en}; {light}"
        if happening:
            context += f"; just happened: {happening}"
        if alfred_en:
            focus = f"{focus}. Also in frame: {alfred_en}."
        return focus, context
    # Порядок — по важности: промптер ставит первое главным, а хвост
    # срезается под 77 токенов CLIP.
    parts = []
    if happening:
        parts.append(f"Main subject, just happened: {happening}")
    if alfred_en:
        parts.append(f"In frame: {alfred_en}.")
    if features:
        parts.append("Must be clearly visible: " + "; ".join(features) + ".")
    if state:
        parts.append("Current state: " + "; ".join(state[-PHOTO_FACTS_IN_FRAME:]) + ".")
    # Композицию «стол в нижней трети» под вставку дописывает служба llm
    # (item_paste.DESK_COMPOSITION) — промптер её пересказывал и терял.
    parts.append(f"Room: {cab.canon_en}.")
    parts.append(f"Light: {light}.")
    return " ".join(parts), ""


def photo_subject(focus: str, *, selfie: bool) -> str:
    """Что снято — словами для подписи (photo_line_directive)."""
    if not selfie:
        return focus
    if focus:
        return cabinet_mod.PHOTO_SUBJECT_SELF_DOING.format(focus=focus)
    return cabinet_mod.PHOTO_SUBJECT_SELF


def selfie_description(cab: Cabinet, outside: Outside, focus: str, happening: str | None) -> str:
    """Снимок себя (take_photo selfie): Альфред и что он делает — первым,
    случившееся в сцене — следом, дальше как у портрета."""
    subject = (
        cabinet_mod.SELFIE_ACTION_EN.format(action=focus.rstrip(" ."))
        if focus
        else cabinet_mod.SELFIE_SUBJECT_EN
    )
    if happening:
        subject += f" Just happened around him: {happening}."
    pose = bool(focus) and bool(cabinet_mod.SELFIE_POSE_RE.search(focus))
    return portrait_description(cab, outside, subject=cab.localize_selfie(subject), pose=pose)


def portrait_description(
    cab: Cabinet,
    outside: Outside,
    *,
    subject: str = cabinet_mod.PORTRAIT_SUBJECT_EN,
    pose: bool = False,
) -> str:
    """Описание портрета к приветствию для промптера: сам Альфред первым,
    потом свет (время суток и погода — как у снимков кабинета), потом
    обстановка гостя. Особенностей — меньше, чем у снимка: место в 77
    токенах CLIP занимает Альфред. ``pose`` — селфи в ракурсе: голая
    комната без особенностей (cabinet.SELFIE_POSE_RE)."""
    parts = [subject, f"Light: {cab.light_en(outside)}."]
    if pose:
        parts.append(f"Room: {cab.pose_room_en}.")
        return " ".join(parts)
    features = cab.visible_features()[-1:]
    if features:
        parts.append("Also visible: " + "; ".join(features) + ".")
    parts.append(f"Room: {cab.canon_en}.")
    return " ".join(parts)


# Подпись снимка — реплика Альфреда (как bot/handlers/ai.py::ALFRED_PREFIX).
ALFRED_PHOTO_PREFIX = "<b>Альфред:</b> "
