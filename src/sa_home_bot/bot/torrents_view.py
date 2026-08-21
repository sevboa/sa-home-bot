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

LAMP_DOWNLOADING = "🟡"  # активная скачка
LAMP_SEEDING = "🟢"  # докачана и раздаёт
LAMP_WAITING = "🟠"  # ждёт/в очереди/получает метаданные
LAMP_ERROR = "🔴"
LAMP_STOPPED_DONE = "🟤"  # остановлена, но докачана полностью
LAMP_STOPPED = "⚪"  # остановлена, докачана не до конца
LAMP_OTHER = "⚫"  # прочее/неизвестное состояние (moving, unknown)

# Сырые строки состояния qBittorrent — префиксов два у "стоп"-группы
# (qBittorrent 5 переименовал paused*/pausedUP в stopped*), то же
# соглашение, что уже применено к полю "paused" в torrents/service.py.
# metaDL (получение метаданных magnet-ссылки) — не передача данных, лежит в
# "ожидании", а не в активной скачке.
_DOWNLOADING_STATES = frozenset(
    {"downloading", "forcedDL", "allocating", "checkingDL", "checkingResumeData"}
)
_SEEDING_STATES = frozenset({"uploading", "forcedUP", "checkingUP"})
_WAITING_STATES = frozenset({"stalledDL", "stalledUP", "queuedDL", "queuedUP", "metaDL"})
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
    "stalledUP": "ждёт получателей",
    "queuedDL": "в очереди",
    "queuedUP": "в очереди",
    "pausedDL": "остановлена",
    "pausedUP": "остановлена",
    "stoppedDL": "остановлена",
    "stoppedUP": "остановлена",
    "error": "ошибка",
    "missingFiles": "не найдены файлы",
}


def _speed_line(state: str, dlspeed_bytes_s: int, upspeed_bytes_s: int) -> str | None:
    """Скорость по направлению, которое реально сейчас идёт — download при
    скачке, upload при раздаче (None — в остальных статусах: у остановленной/
    в очереди/ошибочной раздачи скорость всегда 0 и ничего не говорит)."""
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


def _short_name(name: str, limit: int = _MAX_NAME_LEN) -> str:
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _speed_text(bytes_s: int) -> str:
    if bytes_s <= 0:
        return "0 КБ/с"
    mb = bytes_s / 1024**2
    if mb >= 1:
        return f"{mb:.1f} МБ/с"
    return f"{bytes_s / 1024:.0f} КБ/с"


# Прогресс в строке списка — луной, а не числом: компактнее (освобождает
# бюджет ширины под имя раздачи) и читается на глаз без арифметики. Точный
# процент остаётся на карточке — там уже есть место для обоих. Границы —
# решение пользователя: 0 и 100 — свои отдельные фазы (только-только начата
# / гарантированно докачана), середина делится пополам на 33.
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


# --- Ширина строки в пропорциональном шрифте (эвристика) --------------------
#
# Точных метрик шрифта клиента Telegram нет и не может быть — Android/iOS/
# desktop/web рисуют разными системными шрифтами. Категориальная оценка
# ширины символа в условных единицах (1.0 ≈ обычная строчная латинская
# буква) даёт результат того же порядка точности, что и рендер через Pillow
# каким-нибудь системным .ttf, но без файла шрифта и без риска, что он
# однажды пропадёт с ноды. Кириллица идёт тем же правилом, что и латиница —
# str.isupper()/islower() юникод-aware, отдельная ветка не нужна.
_NARROW_CHARS = frozenset(" .,:;!'\"-·…|")
_WIDE_CHAR_THRESHOLD = 0x2000  # эмодзи, стрелки и прочие широкие символы


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


# Бюджет ширины строки списка — калибровка по заданному пользователем
# примеру длины: «🔵 Marvels.Daredevil.S01.1080p.L.N.B.J — 100% · ↓0» (сам
# формат строки с тех пор изменился — луна переехала к лампе, — но общая
# длина ориентир остаётся тем же). SAFETY_MARGIN — запас на неточность
# эвристики (живая находка: без запаса часть строк всё равно переносилась на
# реальном клиенте) — лучше обрезать чуть короче, чем упустить перенос.
_SAFETY_MARGIN = 0.85
_LIST_LINE_WIDTH_BUDGET = (
    _text_width("🔵 Marvels.Daredevil.S01.1080p.L.N.B.J — 100% · ↓0") * _SAFETY_MARGIN
)
_ELLIPSIS_WIDTH = _text_width("…")


def _fit_name(name: str, budget: float) -> str:
    """Обрезать имя под остаток бюджета ширины строки (после лампы и
    суффикса — прогресс/скорость) — по ширине символов, а не по их числу:
    «Marvels» и «MMMMMMM» одной длины на глаз занимают разную ширину."""
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
        state = t.get("state", "")
        progress = int(t.get("progress_pct", 0))
        # Луна — сразу рядом с лампой статуса (оба про «в каком состоянии
        # раздача», а не про скорость), не в хвосте строки.
        prefix = f"{_lamp(state, progress)}{_moon_phase(progress)} "
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
                text=f"{_lamp(t.get('state', ''), int(t.get('progress_pct', 0)))}"
                f"{_moon_phase(int(t.get('progress_pct', 0)))} "
                f"{_short_name(t.get('name', '?'))}",
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
                text="🧲 qBittorrent", callback_data=_cb(commands.TORRENT_APP_CODE)
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить", callback_data=_cb(commands.TORRENTS_LIST_CODE, offset)
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def build_app_view(text: str) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка apps/qbittorrent внутри панели — только «Обновить»/«Назад»,
    без кнопок управления юнитом (start/stop/restart, которые обычно строит
    apps_view.run_app_skill): это не отдельная точка входа со своими правами
    apps, а справочный экран внутри /torrents (см. apps_view.HIDDEN_MENU_APP_IDS)."""
    buttons = [
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=_cb(commands.TORRENT_APP_CODE))],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=_cb(commands.TORRENTS_LIST_CODE, 0))],
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
    # Скорость по направлению, которое реально сейчас идёт (см. build_list_view):
    # у остановленной/в очереди/ошибочной раздачи она всегда 0.
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
