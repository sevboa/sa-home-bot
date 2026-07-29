"""Дебаг-канал вызовов инструментов Альфреда: короткая строка + «развернуть».

Сообщение «🔧 Alfred вызвал инструмент: torrents» (bot/lifecycle.py::
notify_tool_call) отвечает на вопрос «обращался ли он в тул», но не на «с
чем обращался и что получил» — а именно это нужно, когда модель ищет и не
находит. Полный текст в сообщение не влезает по смыслу (дебаг-канал шумит на
каждой реплике) и по размеру (выдача поиска — килобайты), поэтому он
прячется за кнопкой: нажали «развернуть» — то же сообщение переписывается с
входом и выходом, нажали «скрыть» — возвращается короткое.

Сами вход/выход живут в памяти процесса, а не в callback_data (там 64 байта
на всё) и не в БД: это отладочный след разговора, переживать рестарт бота ему
незачем — тот же приём и те же соображения, что у PendingTorrents
(bot/torrent_pending.py). Потерялся после рестарта — кнопка честно скажет.
"""

from __future__ import annotations

import html
import json
import uuid
from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CALLBACK_PREFIX = "tdbg"
SHOW = "show"
HIDE = "hide"

EXPAND_TEXT = "🔍 Развернуть"
COLLAPSE_TEXT = "🙈 Скрыть"
LOST_TEXT = "Подробности потерялись (бота перезапускали)."

_MAX_RECORDS = 256
# Telegram режет сообщение на 4096 символах, а редактировать по частям
# нельзя — режем сами, каждую половину отдельно, чтобы длинный вход не
# съедал место под выход.
_MAX_PART_CHARS = 1400


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    result: str


class ToolCalls:
    """token → вызов. Ограниченный словарь с вытеснением самого старого."""

    def __init__(self, maxsize: int = _MAX_RECORDS) -> None:
        self._maxsize = maxsize
        self._items: dict[str, ToolCall] = {}

    def add(self, call: ToolCall) -> str:
        token = uuid.uuid4().hex[:8]
        self._items[token] = call
        if len(self._items) > self._maxsize:
            self._items.pop(next(iter(self._items)))
        return token

    def get(self, token: str) -> ToolCall | None:
        return self._items.get(token)


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
    # quote=False — внутри <pre> кавычки экранировать не нужно, а «&quot;»
    # в каждой строке JSON читается заметно хуже самого JSON.
    return (
        f"{render_short(call.name)}\n\n"
        f"<b>Вход:</b>\n<pre>{html.escape(args, quote=False)}</pre>\n"
        f"<b>Выход:</b>\n<pre>{html.escape(result, quote=False)}</pre>"
    )


def keyboard(token: str, *, expanded: bool) -> InlineKeyboardMarkup:
    action, title = (HIDE, COLLAPSE_TEXT) if expanded else (SHOW, EXPAND_TEXT)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=title, callback_data=f"{CALLBACK_PREFIX}:{action}:{token}")]
        ]
    )
