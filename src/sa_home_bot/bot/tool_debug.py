from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from sa_home_bot.bot.ui.callback_data import pack
from sa_home_bot.bot.ui.keyboards import single_button
from sa_home_bot.utils.pending import PendingStore

CALLBACK_PREFIX = "tdbg"
SHOW = "show"
HIDE = "hide"

EXPAND_TEXT = "🔍 Развернуть"
COLLAPSE_TEXT = "🙈 Скрыть"
LOST_TEXT = "Подробности потерялись (бота перезапускали)."

_MAX_RECORDS = 256
_MAX_PART_CHARS = 1400


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    result: str


class ToolCalls(PendingStore[ToolCall]):
    def __init__(self, maxsize: int = _MAX_RECORDS) -> None:
        super().__init__(maxsize)


def render_short(name: str) -> str:
    return f"🔧 Alfred вызвал инструмент: {name}"


def _clip(text: str) -> str:
    if len(text) <= _MAX_PART_CHARS:
        return text
    return text[:_MAX_PART_CHARS] + f"… (обрезано, всего {len(text)} символов)"


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def render_full(call: ToolCall) -> str:
    args = _clip(_as_text(call.args))
    result = _clip(_as_text(call.result))
    return (
        f"{render_short(call.name)}\n\n"
        f"<b>Вход:</b>\n<pre>{html.escape(args, quote=False)}</pre>\n"
        f"<b>Выход:</b>\n<pre>{html.escape(result, quote=False)}</pre>"
    )


def keyboard(token: str, *, expanded: bool) -> InlineKeyboardMarkup:
    action, title = (HIDE, COLLAPSE_TEXT) if expanded else (SHOW, EXPAND_TEXT)
    return single_button(title, pack(CALLBACK_PREFIX, action, token))
