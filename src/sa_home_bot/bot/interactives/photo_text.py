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
from sa_home_bot.bot.interactives.transylvania import Outside

PHOTO_PURPOSE = "photo"


PORTRAIT_PURPOSE = "portrait"


PHOTO_FEATURES_IN_FRAME = 2


PHOTO_FACTS_IN_FRAME = 3


def _general_view_re(nouns: str) -> re.Pattern[str]:
    return re.compile(
        r"^(?:(?:мой|наш|твой|весь)\s+)?(?:общ\w+\s+(?:вид|план)\w*"
        r"|(?:(?:общ\w+\s+)?(?:вид|план|снимок|фото)\w*\s+)?(?:(?:на|всего|мо\w+|тво\w+)\s+)*"
        rf"(?:{nouns})\w*)",
        re.I,
    )


GENERAL_VIEW_RE = _general_view_re("кабинет|комнат")


GENERAL_VIEW_ROOM_RE = _general_view_re("комнат")


def is_general_view(text: str, cab: Cabinet) -> bool:
    return bool((GENERAL_VIEW_RE if cab.is_study else GENERAL_VIEW_ROOM_RE).match(text))


WINDOW_VIEW_RE = re.compile(
    r"\bиз\s+(?:\w+\s+){0,2}окн|\bза\s+(?:\w+\s+){0,2}окн|\bокн\w*\s+(?:и|с)\s+вид"
    r"|window\s+view|view\s+(?:from|through|out)",
    re.I,
)


PHOTO_SCENE_GAP_TURNS = 3


PHOTO_SCENE_CAPTION = "В кабинете"


MOOD_PRESETS: dict[str, tuple[str, str, float]] = {
    "rot": ("revanim", "rottech", 0.8),
    "eldritch": ("ghostmix", "eldritch", 0.8),
}


ITEM_CLOSEUP_RU = "Крупный план: пустая столешница старого письменного стола"


ITEM_PENDING_TAG = "pending"


@dataclass(frozen=True)
class _ItemShot:
    kind: items_mod.ItemKind
    key: str
    place: str
    tag: str


def photo_negative(cab: Cabinet) -> str:
    if cab.windowed:
        return cabinet_mod.PHOTO_NEGATIVE_EN
    return f"{cabinet_mod.PHOTO_NEGATIVE_EN}, {places.NO_WINDOW_NEGATIVE_EN}"


def photo_state_key(
    cab: Cabinet, outside: Outside, shot: _ItemShot | None, size: int | None = None
) -> str:
    key = cab.state_key(outside.key)
    if shot is not None and shot.place == "desk":
        key += f"#{shot.tag}"
    if size:
        key += f"#px{size}"
    return key


def photo_line_directive(
    focus: str, description: str, where: str, missing: list[str], misses: int
) -> str:
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
    wanted = decision.photo if decision is not None else None
    if key_moment:
        return wanted or ""
    if wanted is None:
        return None
    if run.photo_turn is not None and run.turns_total - run.photo_turn < PHOTO_SCENE_GAP_TURNS:
        return None
    return wanted


def retake_emphasis(prev: dict[str, Any] | None, expect: list[str]) -> list[str]:
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
    visible_features = cab.visible_features()
    if item is not None and item_place is not None:
        visible_features = [f for f in visible_features if not item.focus_re.search(f)]
        if happening and item.focus_re.search(happening):
            happening = None
        if item_place == "closeup":
            focus = ITEM_CLOSEUP_RU
    alfred_en = cabinet_mod.SCENE_ALFRED_EN.get(alfred or "")
    features = visible_features[-(1 if alfred_en else PHOTO_FEATURES_IN_FRAME) :]
    light = cab.light_en(outside, closeup=bool(focus))
    if focus:
        context = f"{cab.canon_en}; {light}"
        if happening:
            context += f"; just happened: {happening}"
        if alfred_en:
            focus = f"{focus}. Also in frame: {alfred_en}."
        return focus, context
    parts = []
    if happening:
        parts.append(f"Main subject, just happened: {happening}")
    if alfred_en:
        parts.append(f"In frame: {alfred_en}.")
    if features:
        parts.append("Must be clearly visible: " + "; ".join(features) + ".")
    if state:
        parts.append("Current state: " + "; ".join(state[-PHOTO_FACTS_IN_FRAME:]) + ".")
    parts.append(f"Room: {cab.canon_en}.")
    parts.append(f"Light: {light}.")
    return " ".join(parts), ""


def photo_subject(focus: str, *, selfie: bool) -> str:
    if not selfie:
        return focus
    if focus:
        return cabinet_mod.PHOTO_SUBJECT_SELF_DOING.format(focus=focus)
    return cabinet_mod.PHOTO_SUBJECT_SELF


def selfie_description(cab: Cabinet, outside: Outside, focus: str, happening: str | None) -> str:
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
    parts = [subject, f"Light: {cab.light_en(outside)}."]
    if pose:
        parts.append(f"Room: {cab.pose_room_en}.")
        return " ".join(parts)
    features = cab.visible_features()[-1:]
    if features:
        parts.append("Also visible: " + "; ".join(features) + ".")
    parts.append(f"Room: {cab.canon_en}.")
    return " ".join(parts)
