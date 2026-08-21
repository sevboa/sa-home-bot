"""Панель /torrents — «список закачек → карточка раздачи» в одном сообщении,
по образцу bot/guests_view.py/bot/node_view.py: список с пагинацией, кнопки
открывают вложенные экраны редактированием того же сообщения (edit_text),
«Назад» ведёт на список.

Модуль только рендерит (текст + клавиатура) из уже готовых данных — ответа
действия ``list`` службы torrents (с ``with_hash=True``, см. докстринг
torrents/service.py::_list_sync). Сетевые вызовы и разбор callback'ов —
bot/handlers/torrents_panel.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands
from sa_home_bot.bot.pagination import clamp_offset, nav_row  # noqa: F401 — реэкспорт
from sa_home_bot.runtime import format_duration

TORRENTS_PAGE_SIZE = 8
_MAX_NAME_LEN = 38

UNAVAILABLE_TEXT = "⚠️ Служба «закачки» недоступна — попробуйте позже."

# Пресеты общего лимита скорости (0 = без ограничения) — потолок 5 МБ/с
# (torrents/service.py::MAX_SPEED_LIMIT_MBPS), домашний канал слабый, выше
# пользователь ограничивать и не просил.
SPEED_PRESETS: tuple[int, ...] = (0, 1, 2, 5)

LAMP_DOWNLOADING = "🟢"
LAMP_SEEDING = "🔵"
LAMP_WAITING = "🟠"
LAMP_STOPPED = "🔴"
LAMP_OTHER = "⚪"

# Сырые строки состояния qBittorrent — префиксов два (qBittorrent 5
# переименовал paused*/pausedUP в stopped*), то же соглашение, что уже
# применено к полю "paused" в torrents/service.py.
_DOWNLOADING_STATES = frozenset(
    {"downloading", "forcedDL", "metaDL", "allocating", "checkingDL", "checkingResumeData"}
)
_SEEDING_STATES = frozenset({"uploading", "forcedUP", "checkingUP"})
_WAITING_STATES = frozenset({"stalledDL", "stalledUP", "queuedDL", "queuedUP"})
_STOPPED_STATES = frozenset({"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"})

_STATE_LABELS = {
    "downloading": "качается",
    "forcedDL": "качается (принудительно)",
    "metaDL": "получает метаданные",
    "allocating": "резервирует место",
    "checkingDL": "проверяется",
    "checkingResumeData": "проверяется",
    "uploading": "раздаёт",
    "forcedUP": "раздаёт (принудительно)",
    "checkingUP": "проверяется",
    "stalledDL": "ждёт источников",
    "stalledUP": "ждёт получателей",
    "queuedDL": "в очереди",
    "queuedUP": "в очереди",
    "pausedDL": "остановлена",
    "pausedUP": "остановлена",
    "stoppedDL": "остановлена",
    "stoppedUP": "остановлена",
}


def _lamp(state: str) -> str:
    if state in _DOWNLOADING_STATES:
        return LAMP_DOWNLOADING
    if state in _SEEDING_STATES:
        return LAMP_SEEDING
    if state in _WAITING_STATES:
        return LAMP_WAITING
    if state in _STOPPED_STATES:
        return LAMP_STOPPED
    return LAMP_OTHER


def _state_label(state: str) -> str:
    return _STATE_LABELS.get(state, state or "?")


def _short_name(name: str, limit: int = _MAX_NAME_LEN) -> str:
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _speed_text(bytes_s: int) -> str:
    if bytes_s <= 0:
        return "0 КБ/с"
    mb = bytes_s / 1024**2
    if mb >= 1:
        return f"{mb:.1f} МБ/с"
    return f"{bytes_s / 1024:.0f} КБ/с"


def _speed_limit_text(mbps: int) -> str:
    return "без ограничения" if mbps <= 0 else f"{mbps} МБ/с"


def _cb(code: str, *parts: object) -> str:
    return ":".join([commands.CALLBACK_PREFIX, code, *(str(p) for p in parts)])


def _speed_buttons(current_mbps: int, offset: int) -> list[InlineKeyboardButton]:
    buttons = []
    for mbps in SPEED_PRESETS:
        label = "🚫 Без огр." if mbps == 0 else f"{mbps} МБ/с"
        if mbps == current_mbps:
            label = f"✅ {label}"
        buttons.append(
            InlineKeyboardButton(
                text=label, callback_data=_cb(commands.TORRENT_SPEED_CODE, mbps, offset)
            )
        )
    return buttons


def build_list_view(result: dict[str, Any], offset: int) -> tuple[str, InlineKeyboardMarkup]:
    torrents: Sequence[dict[str, Any]] = result.get("torrents", ())
    limit_mbps = int(result.get("speed_limit_mbps") or 0)
    offset = clamp_offset(offset, TORRENTS_PAGE_SIZE, len(torrents))

    lines = [
        f"🧲 <b>Закачки</b> ({len(torrents)})",
        f"Лимит скорости: {_speed_limit_text(limit_mbps)}",
        "",
    ]
    if not torrents:
        lines.append("Пока ничего не качается.")
    page = torrents[offset : offset + TORRENTS_PAGE_SIZE]
    for t in page:
        lines.append(
            f"{_lamp(t.get('state', ''))} {escape(_short_name(t.get('name', '?')))} — "
            f"{t.get('progress_pct', 0)}% · ↓{_speed_text(int(t.get('dlspeed_bytes_s', 0)))}"
        )

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{_lamp(t.get('state', ''))} {_short_name(t.get('name', '?'))}",
                callback_data=_cb(commands.TORRENT_CARD_CODE, t.get("hash", "")),
            )
        ]
        for t in page
    ]
    nav = nav_row(
        offset, TORRENTS_PAGE_SIZE, len(torrents), lambda o: _cb(commands.TORRENTS_LIST_CODE, o)
    )
    if nav:
        buttons.append(nav)
    buttons.append(_speed_buttons(limit_mbps, offset))
    buttons.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить", callback_data=_cb(commands.TORRENTS_LIST_CODE, offset)
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def build_card_view(torrent: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    state = torrent.get("state", "")
    thash = torrent.get("hash", "")
    eta_s = torrent.get("eta_s")
    lines = [
        f"🧲 <b>{escape(torrent.get('name', '?'))}</b>",
        "",
        f"Статус: {_lamp(state)} {_state_label(state)}",
        f"Прогресс: {torrent.get('progress_pct', 0)}%",
        f"Скорость: ↓ {_speed_text(int(torrent.get('dlspeed_bytes_s', 0)))}",
        f"Пиры: {torrent.get('seeds', 0)} сидов, {torrent.get('peers', 0)} личей",
        f"Осталось: {format_duration(eta_s) if eta_s is not None else 'неизвестно'}",
    ]
    toggle_text = "▶️ Запустить" if torrent.get("paused") else "⏸ Остановить"
    buttons = [
        [
            InlineKeyboardButton(
                text=toggle_text, callback_data=_cb(commands.TORRENT_TOGGLE_CODE, thash)
            )
        ],
        [
            InlineKeyboardButton(
                text="🔄 Обновить", callback_data=_cb(commands.TORRENT_CARD_CODE, thash)
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data=_cb(commands.TORRENTS_LIST_CODE, 0)
            )
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)
