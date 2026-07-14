"""Панель /status: inline-клавиатура действий + общие построители текстов.

Билдеры вызываются и из message-хендлеров (ввод команды вручную), и из
callback-хендлеров (нажатие кнопки), поэтому логика собрана здесь в одном
месте. Все данные (здоровье/диски/статистика/история отключений) приходят
от службы monitor по протоколу (ServiceLink; ``dst`` адресует монитор любой
ноды — «спроси любого»); недоступный монитор — честный текст об этом, а не
исключение в чат.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands
from sa_home_bot.bot.monitor_state import (
    parse_disk_summary,
    parse_health_state,
    parse_outage,
)
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.domain.models import KIND_CPU
from sa_home_bot.domain.render import (
    render_downtime,
    render_stats,
    render_status_full,
    render_status_summary,
)
from sa_home_bot.proto.messages import (
    ERR_UNKNOWN_ACTION,
    ERR_UNKNOWN_DST,
    ActionSpec,
    Address,
    ProtoError,
)
from sa_home_bot.subscriptions.models import Subscription

# Подписи кнопок-представлений (см. commands.STATUS_ACTIONS).
_LABELS: dict[str, str] = {
    "full": "🔎 Подробно",
    "stats": "📈 Статистика",
    "downtime": "⏻ Отключения",
}

# Постранично по 10 отключений (действие downtime службы monitor).
DOWNTIME_PAGE_SIZE = 10

MONITOR_DOWN_TEXT = (
    "⚠️ Служба мониторинга недоступна — бот не может получить данные датчиков. "
    "Проверьте, запущен ли монитор."
)

# Монитор ноды старой версии (без действия downtime) или нода без монитора.
DOWNTIME_UNSUPPORTED_TEXT = (
    "⚠️ Монитор этой ноды не поддерживает историю отключений — обновите ноду."
)

MONITOR_SERVICE = "monitor"


def monitor_dst(node_id: str | None) -> Address:
    """Адрес монитора ноды (node_id=None — своя) для «спроси любого»."""
    return Address(node=node_id, service=MONITOR_SERVICE)


def build_status_keyboard(
    subscription: Subscription | None,
    monitor_actions: Sequence[ActionSpec] = (),
    node_id: str | None = None,
) -> InlineKeyboardMarkup | None:
    """Кнопки мониторинга карточки ноды: представления бота + действия монитора.

    Представления (подробно/статистика/отключения) — свои у бота, права по
    имени команды. Действия — динамические из describe монитора, права
    `действие@monitor` (или голое имя действия — совместимость).
    node_id — карточка пира: те же кнопки, но callback несёт адрес ноды
    (пустой node_id — без сегмента, старые кнопки в чатах остаются валидны).
    """
    if subscription is None:
        return None
    suffix = f":{node_id}" if node_id else ""
    buttons = [
        InlineKeyboardButton(
            text=_LABELS[code],
            callback_data=f"{commands.CALLBACK_PREFIX}:{code}{suffix}",
        )
        for code, cmd in commands.STATUS_ACTIONS.items()
        if subscription.allows_command(cmd.name)
    ]
    for action in monitor_actions:
        if action.params:  # действия с параметрами в /status не выносим
            continue
        if subscription.allows_action(action.id, MONITOR_SERVICE):
            buttons.append(
                InlineKeyboardButton(
                    text=action.title,
                    callback_data=commands.action_callback(
                        action.id, node_id=node_id, service=MONITOR_SERVICE
                    ),
                )
            )
    if not buttons:
        return None
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_summary_text(link: ServiceLink, dst: Address | None = None) -> str:
    try:
        state = await link.get_state(dst=dst)
    except (ServiceUnavailableError, ProtoError):
        return MONITOR_DOWN_TEXT
    states = [parse_health_state(h) for h in state.get("health", [])]
    cpu_states = [s for s in states if s.kind == KIND_CPU]
    disks = [parse_disk_summary(d) for d in state.get("disks", [])]
    uptime_s = state.get("uptime_s")
    thresholds = state.get("thresholds", {})
    cpu_th = thresholds.get("cpu", {})
    disk_th = thresholds.get("disk", {})
    text = render_status_summary(
        datetime.now(tz=UTC),
        timedelta(seconds=uptime_s) if uptime_s is not None else None,
        cpu_states,
        disks,
        parse_outage(state.get("last_outage")),
        cpu_warn_c=cpu_th.get("warn_c", 0.0),
        cpu_crit_c=cpu_th.get("crit_c", 0.0),
        disk_warn_c=disk_th.get("warn_c", 0.0),
        disk_crit_c=disk_th.get("crit_c", 0.0),
    )
    problems = state.get("requirements") or []
    if problems:
        text += "\n" + "\n".join(f"⚠️ {p['hint']}" for p in problems)
    return text


async def build_full_text(link: ServiceLink, dst: Address | None = None) -> str:
    try:
        state = await link.get_state(dst=dst)
    except (ServiceUnavailableError, ProtoError):
        return MONITOR_DOWN_TEXT
    return render_status_full([parse_health_state(h) for h in state.get("health", [])])


async def build_stats_text(link: ServiceLink, dst: Address | None = None) -> str:
    try:
        state = await link.get_state(dst=dst)
    except (ServiceUnavailableError, ProtoError):
        return MONITOR_DOWN_TEXT
    return render_stats(state.get("job_counts", {}), state.get("recent_runs", []))


def _downtime_callback(offset: int, node_id: str | None = None) -> str:
    data = f"{commands.CALLBACK_PREFIX}:{commands.DOWNTIME_PAGE_CODE}:{offset}"
    if node_id:
        data += f":{node_id}"
    return data


def _downtime_keyboard(
    offset: int, has_next: bool, node_id: str | None = None
) -> InlineKeyboardMarkup | None:
    """Кнопки «Предыдущие 10» / «Следующие 10» под страницей /downtime."""
    buttons = []
    if offset > 0:
        prev_offset = max(0, offset - DOWNTIME_PAGE_SIZE)
        buttons.append(
            InlineKeyboardButton(
                text="⬅️ Предыдущие 10",
                callback_data=_downtime_callback(prev_offset, node_id),
            )
        )
    if has_next:
        next_offset = offset + DOWNTIME_PAGE_SIZE
        buttons.append(
            InlineKeyboardButton(
                text="➡️ Следующие 10",
                callback_data=_downtime_callback(next_offset, node_id),
            )
        )
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


async def build_downtime_page(
    link: ServiceLink, offset: int = 0, node_id: str | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Страница истории отключений ноды — командой downtime её монитора.

    node_id=None — своя нода. Монитор без действия downtime (старая версия)
    или нода без монитора — честный текст, а не исключение.
    """
    try:
        result = await link.command(
            "downtime",
            {"offset": offset, "limit": DOWNTIME_PAGE_SIZE},
            dst=monitor_dst(node_id),
        )
    except ServiceUnavailableError:
        return MONITOR_DOWN_TEXT, None
    except ProtoError as exc:
        if exc.code in (ERR_UNKNOWN_ACTION, ERR_UNKNOWN_DST):
            return DOWNTIME_UNSUPPORTED_TEXT, None
        return MONITOR_DOWN_TEXT, None
    events = [
        e
        for e in (parse_outage(raw) for raw in result.get("events", []))
        if e is not None
    ]
    text = render_downtime(events, offset)
    keyboard = _downtime_keyboard(offset, bool(result.get("has_next")), node_id)
    return text, keyboard


