from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands
from sa_home_bot.bot.pagination import clamp_offset, nav_row  # noqa: F401
from sa_home_bot.runtime import format_duration

TORRENTS_PAGE_SIZE = 8
_MAX_NAME_LEN = 38

UNAVAILABLE_TEXT = "⚠️ Служба «закачки» недоступна — попробуйте позже."

SPEED_PRESETS: tuple[int, ...] = (0, 1, 2, 5)

LAMP_DOWNLOADING = "🟡"
LAMP_SEEDING = "🟢"
LAMP_WAITING = "🟠"
LAMP_ERROR = "🔴"
LAMP_STOPPED_DONE = "🟤"
LAMP_STOPPED = "⚪"
LAMP_OTHER = "⚫"

_DOWNLOADING_STATES = frozenset(
    {"downloading", "forcedDL", "allocating", "checkingDL", "checkingResumeData"}
)
_SEEDING_STATES = frozenset({"uploading", "forcedUP", "checkingUP", "stalledUP"})
_WAITING_STATES = frozenset({"stalledDL", "queuedDL", "queuedUP", "metaDL"})
_STOPPED_STATES = frozenset({"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"})
_ERROR_STATES = frozenset({"error", "missingFiles"})

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
    "stalledUP": "раздаёт (нет запросов)",
    "queuedDL": "в очереди",
    "queuedUP": "в очереди на раздачу",
    "pausedDL": "остановлена",
    "pausedUP": "остановлена",
    "stoppedDL": "остановлена",
    "stoppedUP": "остановлена",
    "error": "ошибка",
    "missingFiles": "не найдены файлы",
}


def _speed_line(state: str, dlspeed_bytes_s: int, upspeed_bytes_s: int) -> str | None:
    if state in _DOWNLOADING_STATES:
        return f"↓{_speed_text(dlspeed_bytes_s)}"
    if state in _SEEDING_STATES:
        return f"↑{_speed_text(upspeed_bytes_s)}"
    return None


def _lamp(state: str, progress_pct: int) -> str:
    if state in _ERROR_STATES:
        return LAMP_ERROR
    if state in _DOWNLOADING_STATES:
        return LAMP_DOWNLOADING
    if state in _SEEDING_STATES:
        return LAMP_SEEDING
    if state in _WAITING_STATES:
        return LAMP_WAITING
    if state in _STOPPED_STATES:
        return LAMP_STOPPED_DONE if progress_pct >= 100 else LAMP_STOPPED
    return LAMP_OTHER


def _state_label(state: str) -> str:
    return _STATE_LABELS.get(state, state or "?")


_RANK_PROBLEM = 0
_RANK_DOWNLOADING = 1
_RANK_PAUSED_INCOMPLETE = 2
_RANK_SEEDING = 3
_RANK_QUEUED_UPLOAD = 4
_RANK_PAUSED_COMPLETE = 5
_RANK_OTHER = 6


def _sort_rank(state: str, progress_pct: int) -> int:
    if state in _ERROR_STATES or state == "stalledDL":
        return _RANK_PROBLEM
    if state in _DOWNLOADING_STATES or state in ("metaDL", "queuedDL"):
        return _RANK_DOWNLOADING
    if state in _STOPPED_STATES:
        return _RANK_PAUSED_COMPLETE if progress_pct >= 100 else _RANK_PAUSED_INCOMPLETE
    if state in _SEEDING_STATES:
        return _RANK_SEEDING
    if state == "queuedUP":
        return _RANK_QUEUED_UPLOAD
    return _RANK_OTHER


def _sort_key(t: dict[str, Any]) -> tuple[int, int]:
    rank = _sort_rank(t.get("state", ""), int(t.get("progress_pct", 0)))
    return rank, -int(t.get("added_on", 0) or 0)


def _short_name(name: str, limit: int = _MAX_NAME_LEN) -> str:
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _speed_text(bytes_s: int) -> str:
    if bytes_s <= 0:
        return "0 КБ/с"
    mb = bytes_s / 1024**2
    if mb >= 1:
        return f"{mb:.1f} МБ/с"
    return f"{bytes_s / 1024:.0f} КБ/с"


def _moon_phase(progress_pct: int) -> str:
    if progress_pct <= 0:
        return "🌑"
    if progress_pct >= 100:
        return "🌕"
    if progress_pct < 33:
        return "🌒"
    if progress_pct <= 66:
        return "🌓"
    return "🌔"


_NARROW_CHARS = frozenset(" .,:;!'\"-·…|")
_WIDE_CHAR_THRESHOLD = 0x2000


def _char_width(ch: str) -> float:
    if ch in _NARROW_CHARS:
        return 0.5
    if ch.isdigit():
        return 0.55
    if ch.isupper():
        return 0.72
    if ch.islower():
        return 0.5
    if ord(ch) > _WIDE_CHAR_THRESHOLD:
        return 1.8
    return 0.6


def _text_width(s: str) -> float:
    return sum(_char_width(c) for c in s)


_SAFETY_MARGIN = 0.85
_LIST_LINE_WIDTH_BUDGET = (
    _text_width("🔵 Marvels.Daredevil.S01.1080p.L.N.B.J — 100% · ↓0") * _SAFETY_MARGIN
)
_ELLIPSIS_WIDTH = _text_width("…")


def _fit_name(name: str, budget: float) -> str:
    if _text_width(name) <= budget:
        return name
    budget -= _ELLIPSIS_WIDTH
    out: list[str] = []
    width = 0.0
    for ch in name:
        w = _char_width(ch)
        if width + w > budget:
            break
        out.append(ch)
        width += w
    return "".join(out).rstrip() + "…"


def _speed_limit_text(mbps: int) -> str:
    return "без ограничения" if mbps <= 0 else f"{mbps} МБ/с"


def _speed_buttons(current_mbps: int, offset: int) -> list[InlineKeyboardButton]:
    buttons = []
    for mbps in SPEED_PRESETS:
        label = "🚫 Без огр." if mbps == 0 else f"{mbps} МБ/с"
        if mbps == current_mbps:
            label = f"✅ {label}"
        buttons.append(
            InlineKeyboardButton(
                text=label,
                callback_data=commands.panel_callback(commands.TORRENT_SPEED_CODE, mbps, offset),
            )
        )
    return buttons


def build_list_view(result: dict[str, Any], offset: int) -> tuple[str, InlineKeyboardMarkup]:
    torrents: Sequence[dict[str, Any]] = sorted(result.get("torrents", ()), key=_sort_key)
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
        state = t.get("state", "")
        progress = int(t.get("progress_pct", 0))
        prefix = f"{_lamp(state, progress)} {_moon_phase(progress)} "
        speed = _speed_line(
            state, int(t.get("dlspeed_bytes_s", 0)), int(t.get("upspeed_bytes_s", 0))
        )
        suffix = f" — {speed}" if speed else ""
        budget = _LIST_LINE_WIDTH_BUDGET - _text_width(prefix) - _text_width(suffix)
        name = _fit_name(t.get("name", "?"), budget)
        lines.append(f"{prefix}{escape(name)}{suffix}")

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{_lamp(t.get('state', ''), int(t.get('progress_pct', 0)))} "
                f"{_moon_phase(int(t.get('progress_pct', 0)))} "
                f"{_short_name(t.get('name', '?'))}",
                callback_data=commands.panel_callback(
                    commands.TORRENT_CARD_CODE, t.get("hash", "")
                ),
            )
        ]
        for t in page
    ]
    nav = nav_row(
        offset,
        TORRENTS_PAGE_SIZE,
        len(torrents),
        lambda o: commands.panel_callback(commands.TORRENTS_LIST_CODE, o),
    )
    if nav:
        buttons.append(nav)
    buttons.append(_speed_buttons(limit_mbps, offset))
    buttons.append(
        [
            InlineKeyboardButton(
                text="🧲 qBittorrent",
                callback_data=commands.panel_callback(commands.TORRENT_APP_CODE),
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data=commands.panel_callback(commands.TORRENTS_LIST_CODE, offset),
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def build_app_view(text: str) -> tuple[str, InlineKeyboardMarkup]:
    buttons = [
        [
            InlineKeyboardButton(
                text="🔄 Обновить", callback_data=commands.panel_callback(commands.TORRENT_APP_CODE)
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=commands.panel_callback(commands.TORRENTS_LIST_CODE, 0),
            )
        ],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def build_card_view(torrent: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    state = torrent.get("state", "")
    thash = torrent.get("hash", "")
    eta_s = torrent.get("eta_s")
    progress = int(torrent.get("progress_pct", 0))
    lines = [
        f"🧲 <b>{escape(torrent.get('name', '?'))}</b>",
        "",
        f"Статус: {_lamp(state, progress)} {_state_label(state)}",
        f"Прогресс: {_moon_phase(progress)} {progress}%",
    ]
    speed = _speed_line(
        state, int(torrent.get("dlspeed_bytes_s", 0)), int(torrent.get("upspeed_bytes_s", 0))
    )
    if speed:
        lines.append(f"Скорость: {speed}")
    lines.append(f"Пиры: {torrent.get('seeds', 0)} сидов, {torrent.get('peers', 0)} личей")
    lines.append(f"Осталось: {format_duration(eta_s) if eta_s is not None else 'неизвестно'}")
    toggle_text = "▶️ Запустить" if torrent.get("paused") else "⏸ Остановить"
    buttons = [
        [
            InlineKeyboardButton(
                text=toggle_text,
                callback_data=commands.panel_callback(commands.TORRENT_TOGGLE_CODE, thash),
            )
        ],
        [
            InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data=commands.panel_callback(commands.TORRENT_CARD_CODE, thash),
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=commands.panel_callback(commands.TORRENTS_LIST_CODE, 0),
            )
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)
