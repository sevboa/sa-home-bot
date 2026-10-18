from __future__ import annotations

import html
import re
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot.interactives import items as items_mod
from sa_home_bot.bot.interactives import radio
from sa_home_bot.bot.interactives.registry import REGISTRY

# callback_data «ia:<сценарий>:<кнопка>» — всё остальное в app_state.
CALLBACK_PREFIX = "ia"


BTN_PLAY = "p"


BTN_LATER = "l"


BTN_NEVER = "n"


BTN_EXIT = "x"


BTN_SWAP = "s"


BTN_KEEP = "k"


BTN_RETURN_OLD = "r"


BTN_INSTALL_NEW = "i"


BTN_TOGGLE_KEEP = "o"


FORM_OFFER = "offer"


FORM_SWAP = "swap"


FORM_RETURN = "return"


FORM_REINSTALL = "reinstall"


FORM_ITEM = "item"  # форма тула item_action (вещь поместья)


def offer_suffix(label: str) -> str:
    return f"\n<i>— {label}</i>"


OFFER_EXPIRED_SUFFIX = offer_suffix("Вопрос уже неактуален")


# Кнопка выхода была под сообщениями рассказчика до v0.115.3 — рассказчика
# больше нет, обработка оставлена для уже разосланных сообщений.
EXIT_ALERT = "Хорошо."


# Сюжетные предметы (Этап 49.3, items.py). Карточка предмета — фото с
# кнопкой «it:<id предмета>:<кнопка>»; портрет рисует служба llm
# (item_portrait), в кадры он вставляется пикселями (generate_image, paste).
ITEM_CALLBACK_PREFIX = "it"


# Кнопка несёт намерение, а не «переключить»: речь можно сменить и формами
# swap_radio, тогда надпись на старой карточке устарела бы.
ITEM_BTN_CARD = "c"  # кнопка описи: принести карточку вещи


ITEM_BTN_ACTIONS = "a"  # карточка → перечень действий


ITEM_BTN_BACK = "b"  # перечень действий → карточка


ITEM_BTN_KEEP = "k"  # форма тула: «Оставить как есть» (+ код действия)


# Откуда нажата кнопка — префикс кода: из описи /items (перечень вернётся в
# опись) или из формы тула item_action (форма закрывается итогом). Коды
# действий вида поэтому не начинаются с этих букв.
ITEM_FROM_INVENTORY = "i"


ITEM_FROM_FORM = "f"


# Живые кнопки (пользователь 2026-10-02): формы сцены и смены радиостанции
# и раскрытый перечень действий карточки. Следующее сообщение гостя — «гость
# проигнорировал»: у форм кнопки снимаются, перечень сворачивается обратно
# в карточку. Иначе старая форма оставалась нажимаемой рядом с новой.
LIVE_BUTTONS_KEY = "live_buttons:{chat_id}"


LIVE_FORM = "form"


LIVE_MENU = "menu"


OPT_IN_TEXT = "Интерактивы в этом чате включены."


OPT_OUT_TEXT = "Интерактивы в этом чате выключены. Включить — /interactives on."


def parse_callback(data: str | None) -> tuple[str, str] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[1] not in REGISTRY:
        return None
    return parts[1], parts[2]


def _cb(scenario: str, button: str) -> str:
    return f"{CALLBACK_PREFIX}:{scenario}:{button}"


def _keyboard(scenario: str, rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=_cb(scenario, btn)) for text, btn in row]
            for row in rows
        ]
    )


def offer_keyboard(scenario: str) -> InlineKeyboardMarkup:
    yes, no = REGISTRY[scenario].offer_buttons if scenario in REGISTRY else ("Да", "Нет")
    return _keyboard(
        scenario,
        [
            [(yes, BTN_PLAY), (no, BTN_NEVER)],
        ],
    )


def swap_keyboard(scenario: str) -> InlineKeyboardMarkup:
    return _keyboard(scenario, [[("Заменить", BTN_SWAP), ("Оставить старое", BTN_KEEP)]])


def toggle_keyboard(scenario: str, button: str, label: str) -> InlineKeyboardMarkup:
    return _keyboard(scenario, [[(label, button), ("Оставить как есть", BTN_TOGGLE_KEEP)]])


def _item_button(item_id: int, label: str, button: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=label, callback_data=f"{ITEM_CALLBACK_PREFIX}:{item_id}:{button}")
    ]


def item_keyboard(kind: items_mod.ItemKind, item_id: int) -> InlineKeyboardMarkup | None:
    """Под карточкой — только «Действия», если вид объявил действия."""
    if not kind.actions:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[_item_button(item_id, radio.ITEM_ACTIONS_LABEL, ITEM_BTN_ACTIONS)]
    )


def item_actions_keyboard(
    kind: items_mod.ItemKind, item_id: int, place: str, origin: str = ""
) -> InlineKeyboardMarkup:
    """Перечень действий (заменяет карточку или опись) и «Назад». Кнопка
    несёт намерение, а не «переключить»: состояние можно сменить и формой.
    ``origin`` — откуда открыт перечень (``ITEM_FROM_INVENTORY`` — из описи
    /items: «Назад» и действие возвращают опись)."""
    rows = [
        _item_button(item_id, action.label, origin + action.code)
        for action in kind.available(place)
    ]
    rows.append(_item_button(item_id, radio.ITEM_BACK_LABEL, origin + ITEM_BTN_BACK))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_actions_caption(kind: items_mod.ItemKind, place: str) -> str:
    return (kind.menu_caption or radio.ITEM_ACTIONS_CAPTION).format(
        name=html.escape(kind.name), where=kind.menu_where(place)
    )


_NO_BUTTONS = InlineKeyboardMarkup(inline_keyboard=[])


def item_form_keyboard(item_id: int, action: items_mod.ItemAction) -> InlineKeyboardMarkup:
    """Форма тула item_action: согласие и «Оставить как есть»."""
    keep = ITEM_FROM_FORM + ITEM_BTN_KEEP + action.code
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _item_button(item_id, action.confirm, ITEM_FROM_FORM + action.code)
            + _item_button(item_id, radio.ITEM_FORM_KEEP, keep)
        ]
    )


def inventory_keyboard(items: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
    """Опись: по строке на вещь — принести карточку и, если вид объявил
    действия, «Действия» прямо из описи (особенные вещи — всегда)."""
    rows = []
    for item in items:
        kind = items_mod.KINDS.get(item["type"])
        if kind is None:
            continue
        row = _item_button(int(item["id"]), f"{kind.icon} {kind.name}", ITEM_BTN_CARD)
        if kind.actions:
            row += _item_button(
                int(item["id"]), radio.ITEM_ACTIONS_LABEL, ITEM_FROM_INVENTORY + ITEM_BTN_ACTIONS
            )
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def inventory_text(entries: list[tuple[items_mod.ItemKind, dict[str, Any], str]]) -> str:
    """Опись вещей гостя: (вид, вещь, место) → текст в голосе Альфреда."""
    if not entries:
        return items_mod.INVENTORY_EMPTY
    blocks = [items_mod.INVENTORY_TITLE]
    for kind, item, place in entries:
        line = items_mod.INVENTORY_LINE.format(
            icon=kind.icon,
            name=html.escape(kind.name),
            where=kind.where_ru(place),
        )
        traits = kind.traits_ru(item.get("traits") or [])
        if traits:
            line += "\n" + items_mod.INVENTORY_TRAITS.format(traits=html.escape("; ".join(traits)))
        blocks.append(line)
    blocks.append(items_mod.INVENTORY_HINT)
    return "\n\n".join(blocks)


def parse_item_callback(data: str | None) -> tuple[int, str] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != ITEM_CALLBACK_PREFIX or not parts[1].isdigit():
        return None
    return int(parts[1]), parts[2]


def item_caption(kind: items_mod.ItemKind, item: dict[str, Any], installed: bool) -> str:
    if item.get("status") == "consumed" and kind.consumed_text:
        return f"<b>{html.escape(kind.name)}</b>\n{kind.consumed_text}"
    traits = kind.traits_ru(item.get("traits") or [])
    note = item.get("note")
    place = items_mod.PLACE_DESK if installed else items_mod.PLACE_STOREROOM
    return (kind.card_caption or radio.ITEM_CARD_CAPTION).format(
        name=html.escape(kind.name),
        traits=radio.ITEM_CARD_TRAITS.format(traits=html.escape("; ".join(traits)))
        if traits
        else "",
        fault=(kind.note_card or radio.ITEM_CARD_FAULT).format(fault=html.escape(note.rstrip(". ")))
        if note
        else "",
        where=kind.card_where(place),
    )


_TAG_RE = re.compile(r"<[^>]+>")


def _plain(text_html: str) -> str:
    return html.unescape(_TAG_RE.sub("", text_html))
