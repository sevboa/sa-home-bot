"""«⚠️ Сообщить о проблеме» и «💬 Ответить» в /vpn (этап 57.6b).

Чистый модуль: тексты, клавиатуры, контекст для владельца и память ожиданий
reply. Сеть и отправка — в bot/vpn_help.py и bot/handlers/vpn.py.

Колбэки (``act:vpn:…``):
  report:m[<ключ>]      — экран причин (с карточки устройства — ключ устройства)
  report:f              — то же, но из «❓ Помощь»
  report:c|s|w[<ключ>]  — причина кнопкой: не подключается / медленно / сайты
  report:o[<ключ>]      — «Другое»: бот просит описание reply на своё сообщение
  reply:<chat_id>       — владельцу: «💬 Ответить» человеку

Права. ``report@vpn`` отдельным тумблером не выдаётся: он есть у каждого, у кого
есть ``usage@vpn`` (своя карточка /vpn), см. ``can_report``; middleware
(bot/middlewares.py) пропускает кнопку по тому же правилу. ``reply@vpn`` —
только у админов (``*``), как и любое «неперечисленное» умение службы.

Ожидания reply живут в памяти процесса: ``(chat_id, message_id запроса бота)``
→ кто нажал и зачем. После рестарта бота или через ``REPLY_TTL_S`` reply уже
не принимается («Время ответа вышло»).
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass
from datetime import tzinfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands
from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.domain import vpn_check
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

SERVICE = vpn_protocol.SERVICE_NAME
REPORT_ACTION = "report"
REPLY_ACTION = "reply"

MENU, MENU_FAQ = "m", "f"
CONNECT, SLOW, SITES, OTHER = "c", "s", "w", "o"

# код → (кнопка, причина для владельца)
REASONS: dict[str, tuple[str, str]] = {
    CONNECT: ("🔌 Не подключается", "не подключается"),
    SLOW: ("🐢 Работает медленно", "работает медленно"),
    SITES: ("🌐 Не открываются сайты", "не открываются сайты"),
}
OTHER_BUTTON = "✍️ Другое — опишу словами"
REPORT_BUTTON = "⚠️ Сообщить о проблеме"

MENU_TEXT = "Что случилось?"
OTHER_PROMPT = "Опишите проблему одним сообщением — ответьте на это сообщение."
SENT_TEXT = "✅ Передал владельцу. Ответ придёт сюда."
THROTTLED_TEXT = "Уже передал, ждите ответа."
NOBODY_TEXT = "Сейчас некому передать. Попробуйте написать владельцу напрямую."
EXPIRED_TEXT = "Время ответа вышло. Нажмите «⚠️ Сообщить о проблеме» ещё раз."
OWNER_SENT_TEXT = "✅ Отправил."
OWNER_FAILED_TEXT = "⚠️ Не удалось доставить ответ."
ANSWER_PREFIX = "💬 Ответ владельца: "
MAX_DESCRIPTION = 800

REPLY_TTL_S = 1800.0
_KEEP_S = 86400.0

KIND_REPORT, KIND_ANSWER = "report", "answer"


def can_report(subscription: Subscription | None) -> bool:
    """«Сообщить о проблеме» — у каждого, у кого есть VPN (usage@vpn), и у тех,
    кому ``report@vpn`` выдан явно."""
    return subscription is not None and (
        subscription.allows_action(REPORT_ACTION, SERVICE)
        or subscription.allows_action(vpn_protocol.ACTION_USAGE, SERVICE)
    )


def can_reply(subscription: Subscription | None) -> bool:
    return subscription is not None and subscription.allows_action(REPLY_ACTION, SERVICE)


# --- callback_data ---------------------------------------------------------


def report_cb(value: str = MENU, key: str = "") -> str:
    return commands.action_callback(REPORT_ACTION, f"{value}{key}", service=SERVICE)


def reply_cb(chat_id: int) -> str:
    return commands.action_callback(REPLY_ACTION, str(chat_id), service=SERVICE)


def report_button(key: str = "", *, faq: bool = False) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=REPORT_BUTTON, callback_data=report_cb(MENU_FAQ if faq else MENU, key)
    )


def parse_report(value: str | None) -> tuple[str, str]:
    """``<код><ключ>`` → (код, ключ); пусто или незнакомое — экран причин."""
    if not value:
        return MENU, ""
    code, key = value[:1], value[1:]
    if code == MENU_FAQ:
        return MENU_FAQ, ""
    if code in (MENU, CONNECT, SLOW, SITES, OTHER):
        return code, key
    return MENU, ""


# --- экран причин ----------------------------------------------------------


def menu_keyboard(key: str, *, back_cb: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=text, callback_data=report_cb(code, key))]
        for code, (text, _reason) in REASONS.items()
    ]
    rows.append([InlineKeyboardButton(text=OTHER_BUTTON, callback_data=report_cb(OTHER, key))])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def done_keyboard(home_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📶 На главную", callback_data=home_cb)]]
    )


def owner_keyboard(chat_id: int, guest_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💬 Ответить", callback_data=reply_cb(chat_id)),
                InlineKeyboardButton(text="👤 Карточка гостя", callback_data=guest_cb),
            ]
        ]
    )


def owner_prompt(name: str) -> str:
    return f"Ответ для {name} — ответьте на это сообщение."


# --- ожидания reply --------------------------------------------------------


@dataclass
class Pending:
    kind: str  # KIND_REPORT | KIND_ANSWER
    user_id: int  # кому разрешено ответить
    created: float
    device_key: str = ""
    target_chat: int = 0  # для KIND_ANSWER — кому уйдёт ответ


_pending: dict[tuple[int, int], Pending] = {}


def reset_pending() -> None:
    _pending.clear()


def new_pending(kind: str, user_id: int, **kw) -> Pending:
    return Pending(kind=kind, user_id=user_id, created=time.monotonic(), **kw)


def remember(chat_id: int, message_id: int, entry: Pending) -> None:
    now = time.monotonic()
    for key in [k for k, v in _pending.items() if now - v.created > _KEEP_S]:
        del _pending[key]
    _pending[(chat_id, message_id)] = entry


def lookup(chat_id: int, message_id: int) -> Pending | None:
    return _pending.get((chat_id, message_id))


def forget(chat_id: int, message_id: int) -> None:
    _pending.pop((chat_id, message_id), None)


def is_expired(entry: Pending) -> bool:
    return time.monotonic() - entry.created > REPLY_TTL_S


# --- контекст для владельца ------------------------------------------------


def _node_status(device: vd.Device, node: str, *, tz: tzinfo | None) -> str:
    conns = [c for c in device.issued if c.node == node]
    if not conns:
        return "не выдано"
    stamps = [c.last_handshake_at for c in conns if c.last_handshake_at]
    if stamps:
        return f"на связи {vd.fmt_stamp(max(stamps), tz=tz)}"
    return "ещё не подключалось"


def _transports_text(device: vd.Device) -> str:
    seen: list[str] = []
    for conn in device.issued:
        name = vd.TRANSPORT_NAME.get(conn.transport, conn.transport)
        if name not in seen:
            seen.append(name)
    return " + ".join(seen)


_CHECK_ICON = {vpn_check.OK: "🟢", vpn_check.PARTIAL: "🟠", vpn_check.ALERTING: "🔴"}


def context_lines(
    servers: list[dict],
    down: list[dict],
    device_key: str = "",
    *,
    tz: tzinfo | None = None,
) -> list[str]:
    """Строки под «Причиной»: устройство(а), связь по странам, проверки, трафик.
    ``servers`` — свежий usage по ответившим нодам, ``down`` — не ответившие
    (про них так и пишем). Результат — HTML, названия экранируются здесь."""
    countries = vd.countries_of(servers)
    for server in down:
        node = server.get("node")
        if node and node not in countries:
            countries[node] = vd.Country(node, server.get("label") or node)
    down_nodes = {s.get("node") for s in down}

    def where(node: str) -> str:
        return html.escape(countries[node].short)

    devices = vd.build_devices(servers)
    chosen = vd.find_device(devices, device_key) if device_key else None
    shown = [chosen] if chosen is not None else devices

    def status_line(dev: vd.Device) -> str:
        parts = []
        for node in countries:
            if node in down_nodes:
                parts.append(f"{where(node)} нода не ответила")
            else:
                parts.append(f"{where(node)} {_node_status(dev, node, tz=tz)}")
        return " · ".join(parts)

    lines: list[str] = []
    if len(shown) == 1:
        dev = shown[0]
        transports = _transports_text(dev)
        head = html.escape(dev.label) + (f" · {transports}" if transports else "")
        lines += [f"Устройство: {head}", status_line(dev)]
    elif shown:
        lines.append("Устройства:")
        lines += [f"• {html.escape(dev.label)} — {status_line(dev)}" for dev in shown]
    elif down_nodes:
        lines.append(
            " · ".join(f"{where(n)} нода не ответила" for n in countries if n in down_nodes)
        )

    checks = []
    for server in servers:
        health = vd._server_health(server)
        if health is not None and server.get("node"):
            checks.append(f"{where(server['node'])} {_CHECK_ICON.get(health, '❔')}")
    if checks:
        lines.append("Проверки: " + " · ".join(checks))

    traffic = []
    for server in servers:
        if not server.get("node"):
            continue
        used = int(server.get("used_bytes") or 0) / 1_000_000_000
        limit = int(server.get("limit_bytes") or 0) / 1_000_000_000
        text = f"{used:.1f} / {limit:.0f} ГБ"
        traffic.append(f"{where(server['node'])} {text}" if len(servers) > 1 else text)
    if traffic:
        lines.append("Трафик: " + " · ".join(traffic))
    return lines
