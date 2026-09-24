"""/vpn — карточка своего доступа: расход, устройства, «+100 ГБ»,
перевыпуск, приложение. Админ дополнительно видит сводку по всем гостям и
решает заявки на трафик сверх потолка самообслуживания (кнопки approve/deny
приходят и из уведомления bot/node_events.py::EVENT_VPN_EXTRA_REQUESTED).

Кнопки идут по общей схеме ``act:vpn:<действие>[:<значение>]``
(commands.action_callback) — право на каждую уже проверила
CallbackAuthorizationMiddleware (``действие@vpn``), здесь только исполнение
и рендер. Диспетчеризация сюда — из bot/handlers/node.py::on_dynamic_action
(тот же приём, что apps/monitor: у "act:"-кнопок один обработчик на все
службы, разбор — по service).

Секрет (приватный ключ) уходит в личку дважды, но порядок способов зависит
от того, первое ли это устройство чата (решение пользователя 2026-08-04,
критерий — vpn/service.py::_issue → ``prior_device_count``):

- первое устройство чата — почти наверняка настраивается ПРЯМО С ЭТОГО
  телефона, поэтому сразу уходит файл ``.conf`` (тап на файл → «Открыть с
  помощью» → AmneziaWG импортирует тоннель без копирования — живая находка
  2026-08-03 про сам механизм импорта), а QR — по кнопке, если всё же нужно
  перенести на другое устройство;
- второе и последующие устройства чата — скорее всего заводятся ДЛЯ ДРУГОГО
  устройства (или человека), поэтому сразу уходит QR (сканируется камерой
  прямо из приложения), а файл — по кнопке.

Кнопка одна и исчезает после использования. Приватный ключ до этого момента
нигде не хранится: ни в БД (vpn/service.py и так хранит только публичный),
ни в callback_data (лимит Telegram — 64 байта, весь конфиг туда не влезает)
— он живёт в памяти процесса ровно до выдачи или до
``[vpn].config_message_ttl_s`` (bot/vpn_secrets.py::PendingVpnSecrets), когда
оба сообщения (первый способ и кнопка, а если второй способ всё же
попросили — то и он) бот удаляет сам."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from sa_home_bot.bot import commands, vpn_admin_view, vpn_nodes
from sa_home_bot.bot.invites import Gatekeeper
from sa_home_bot.bot.menu import refresh_chat_menu
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.pagination import clamp_offset
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.vpn_apk import deliver_apk
from sa_home_bot.bot.vpn_secrets import PendingVpnSecret, PendingVpnSecrets
from sa_home_bot.config import Settings
from sa_home_bot.domain import vpn_check
from sa_home_bot.proto.messages import ERR_UNKNOWN_ACTION, Address, ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

router = Router(name="vpn")

SERVICE = vpn_protocol.SERVICE_NAME

_VPN_UNAVAILABLE = "⚠️ Служба VPN недоступна — попробуйте позже."
# Локации гостю ещё не выдали: право `usage@vpn` у него есть (иначе он бы сюда
# не дошёл), а допуск хоть на один сервер — нет. Это не ошибка, поэтому и текст
# не про сбой.
_NO_VPN_ACCESS = "📶 <b>VPN</b>\n\nДоступ пока не выдан — попросите владельца открыть вам локацию."
# То же самое, но у гостя открыт прокси Telegram. Прокси от локаций не зависит
# вовсе (mtg живёт на том же VPS сам по себе), поэтому отказ «доступа нет» тут
# был бы неправдой: одно умение у человека есть, просто не VPN.
_PROXY_ONLY_CARD = (
    "📶 <b>VPN</b>\n\n"
    "✈️ Прокси Telegram вам открыт.\n"
    "VPN-доступ к локациям пока не выдан — попросите владельца, если он нужен."
)

# Имя действия «прислать приложение»: в отличие от остальных, живёт не в
# vpn/protocol.py (служба про него не знает — ссылки на магазины и .apk отдаёт
# сам бот, bot/vpn_apk.py), но право у него обычное — `apk@vpn`.
_ACTION_APK = "apk"

# Человеческие названия транспортов (vpn_peers.transport) для карточки и
# кнопок выбора «➕ Новое устройство».
_TRANSPORT_LABEL = {
    vpn_protocol.TRANSPORT_AWG: "AmneziaWG",
    vpn_protocol.TRANSPORT_REALITY: "VLESS (Reality)",
}
# Префикс значения кнопки выбора транспорта: act:vpn:issue:t_<transport>.
# Отличает её и от голого issue (value=None), и от «забрать выданное» (f_<токен>).
_TRANSPORT_PICK_PREFIX = "t_"

# Порядок транспортов везде, где их перечисляют рядом (индикатор, пикер):
# VLESS первым — он нужен гостям в РФ, с него и начинают выбирать.
_TRANSPORT_ORDER = (vpn_protocol.TRANSPORT_REALITY, vpn_protocol.TRANSPORT_AWG)

# Индикатор доступности по данным чекеров (39.0.7(f), vpn/service.py::
# _check_rollup). Трёхцветный по решению владельца 2026-09-18: 🟢 — сервер
# видят все наблюдатели, 🟠 — часть (для кого-то он уже заблокирован), 🔴 —
# никто. Виден ВСЕМ гостям, а не только админу за «🛰 Проверка сети»:
# «почему не подключается» — первый вопрос гостя, и ответ на него должен
# быть на самой карточке.
_CHECK_ICON = {
    vpn_check.OK: "🟢",
    vpn_check.PARTIAL: "🟠",
    vpn_check.ALERTING: "🔴",
}
_CHECK_LEGEND = (
    "🟠 — доступен не отовсюду (где-то уже блокируют), 🔴 — не отвечает ни "
    "одному наблюдателю."
)


def _is_private(chat_id: int) -> bool:
    return chat_id > 0


def _gb(bytes_: int) -> float:
    return bytes_ / 1_000_000_000


def _server_label(server: dict) -> str:
    """Как назвать сервер человеку: «🇳🇱 Нидерланды» из [vpn].location ноды,
    иначе — её голый id (нода со старым конфигом)."""
    return server.get("label") or server.get("node") or "сервер"


def _device_line(device: dict) -> str:
    handshake = device.get("last_handshake_at")
    seen = (
        f", было на связи {handshake[:16].replace('T', ' ')}"
        if handshake
        else ", ещё не подключалось"
    )
    transport = device.get("transport")
    tag = f" · {_TRANSPORT_LABEL.get(transport, transport)}" if transport else ""
    return f"• {html.escape(device['device_label'])}{tag}{seen}"


def _check_icons(server: dict) -> dict[str, str]:
    """``transport → 🟢/🟠/🔴`` для одного сервера.

    Пустой результат — проверок по нему нет (нода старая, пробники ещё не
    отчитались или все отчёты протухли, см. vpn/service.py::CHECK_STALE_FACTOR).
    Тогда индикатора нет вовсе: молчание честнее выдуманного зелёного.
    """
    return {
        row["transport"]: _CHECK_ICON[row["status"]]
        for row in (server.get("check") or [])
        if row.get("status") in _CHECK_ICON and row.get("transport")
    }


def _sorted_transports(transports: list[str]) -> list[str]:
    """Известные — в порядке _TRANSPORT_ORDER, незнакомые (нода новее бота) —
    следом, как пришли: показать их всё равно лучше, чем потерять."""
    known = [t for t in _TRANSPORT_ORDER if t in transports]
    return known + [t for t in transports if t not in _TRANSPORT_ORDER]


def _health_line(server: dict) -> str:
    """Строка индикаторов сервера: «🛰 VLESS (Reality) 🟢 · AmneziaWG 🔴».
    Пусто, если чекеры про этот сервер ещё ничего не сказали."""
    icons = _check_icons(server)
    if not icons:
        return ""
    transports = _sorted_transports(list(server.get("transports") or icons.keys()))
    parts = [
        f"{_TRANSPORT_LABEL.get(transport, transport)} {icons[transport]}"
        for transport in transports
        if transport in icons
    ]
    return f"🛰 {' · '.join(parts)}" if parts else ""


def _suffix(icon: str) -> str:
    """Индикатор хвостом к тексту кнопки — или ничего, когда его нет."""
    return f" {icon}" if icon else ""


def _server_icon(server: dict) -> str:
    """Один индикатор на всю локацию — для кнопки пикера, где на разбивку по
    транспортам места нет. Сводим те же значения тем же доменным правилом
    (все ok → 🟢, все alerting → 🔴, вразнобой → 🟠), что и наблюдателей."""
    statuses = [
        row["status"] for row in (server.get("check") or []) if row.get("status") in _CHECK_ICON
    ]
    if not statuses:
        return ""
    return _CHECK_ICON[vpn_check.rollup_status(statuses)]


def _is_allowed(server: dict) -> bool:
    """Открыта ли гостю эта локация.

    Отсутствие поля значит «служба допуск не ведёт» — нода ещё не обновлена
    (vpn/service.py::get_state::access_control). Дефолт True намеренный:
    инвертированный запер бы UI живым гостям на время раската.
    """
    return bool(server.get("allowed", True))


def _allowed_servers(servers: list[dict]) -> list[dict]:
    return [server for server in servers if _is_allowed(server)]


def _usage_text(servers: list[dict], *, show_access: bool = False) -> str:
    """Карточка расхода. Квота у каждого сервера своя (счёт за трафик у VPS
    раздельный) — лимиты НЕ суммируются, каждая локация идёт своим блоком.

    Гостю на вход идут только ДОПУЩЕННЫЕ локации: закрытая не показывается
    вовсе, а не строкой «доступа нет» (решение владельца 2026-09-19) — он
    видит то, что ему выдали, и не гадает, чего просить. ``show_access``
    включает пометку закрытых — это для админских экранов, где смотрят чужой
    расход и как раз надо понимать, где доступ открыт, а где нет.
    """
    multi = len(servers) > 1
    lines: list[str] = ["📶 <b>VPN</b>"] if multi else []
    for server in servers:
        used = _gb(server.get("used_bytes", 0))
        limit = _gb(server.get("limit_bytes", 0))
        remaining = _gb(server.get("remaining_bytes", 0))
        quota = f"{used:.1f} / {limit:.0f} ГБ (осталось {remaining:.1f} ГБ)"
        if multi:
            lines.append("")
            lines.append(f"<b>{html.escape(_server_label(server))}</b>: {quota}")
        else:
            lines.append(f"📶 <b>VPN</b>: {quota}")
        # Доступность по чекерам (39.0.7(f)) — сразу под квотой локации:
        # гость должен видеть «сервер жив, но из моей страны не виден» до
        # того, как начнёт грешить на своё устройство.
        health = _health_line(server)
        if health:
            lines.append(health)
        if show_access and not _is_allowed(server):
            lines.append("🔒 Доступ на эту локацию не открыт.")
        if server.get("blocked"):
            lines.append("⛔️ Доступ приостановлен — лимит месяца исчерпан.")
        devices = server.get("devices") or []
        if devices and not multi:
            lines.append("")
            lines.append("Устройства:")
        lines.extend(_device_line(device) for device in devices)
    # Легенду показываем, только когда есть что объяснять: при всех зелёных
    # она была бы шумом на каждой карточке.
    if any(icon != _CHECK_ICON[vpn_check.OK] for s in servers for icon in _check_icons(s).values()):
        lines.append("")
        lines.append(_CHECK_LEGEND)
    return "\n".join(lines)


def _summary_text(summary: dict) -> str:
    chats = summary.get("chats") or []
    lines = [f"📊 <b>VPN — расход за {summary.get('month', '?')}</b>"]
    node = summary.get("node") or {}
    if node:
        free = _gb(node.get("free_bytes", 0))
        reserved = _gb(node.get("reserved_bytes", 0))
        limit = _gb(node.get("limit_bytes", 0))
        lines.append(
            f"Резерв ноды: {reserved:.0f} / {limit:.0f} ГБ занято обещаниями "
            f"гостям, свободно {free:.0f} ГБ"
        )
    if not chats:
        lines.append("")
        lines.append("Гостей с активным VPN-доступом пока нет.")
        return "\n".join(lines)
    lines.append("")
    for row in sorted(chats, key=lambda r: r["used_bytes"], reverse=True):
        used = _gb(row["used_bytes"])
        limit = _gb(row["limit_bytes"])
        devices = row.get("device_count")
        devices_note = f", устройств: {devices}" if devices is not None else ""
        lines.append(f"• <code>{row['chat_id']}</code>: {used:.1f} / {limit:.0f} ГБ{devices_note}")
    return "\n".join(lines)


def _allows(subscription: Subscription | None, action_id: str) -> bool:
    """Есть ли у чата право на действие VPN. Нет подписки — нет и права
    (fail-closed): сюда без неё не дойти (SilenceGate отсекает раньше), но
    догадываться за неизвестного мы не станем."""
    return subscription is not None and subscription.allows_action(action_id, SERVICE)


def _card_keyboard(
    servers: list[dict],
    *,
    subscription: Subscription | None,
    self_serve_nodes: list[str],
) -> InlineKeyboardMarkup:
    """Клавиатура карточки — строго по правам чата.

    Раньше кнопки «Приложение», «Новое устройство», «Перевыпустить» и
    «Отозвать» рисовались безусловно, а право проверялось уже при нажатии
    (CallbackAuthorizationMiddleware). Для владельца разницы не было — у него
    весь комплект, — но с тонкой настройкой прав (bot/vpn_admin_view.py::
    VPN_TOGGLES) появился гость, которому открыли, скажем, только прокси: он
    видел четыре кнопки и на каждой получал «⛔️ Недоступно». Показываем то,
    что человек и правда может.
    """
    is_admin = _is_admin(subscription) if subscription is not None else False
    multi = len(servers) > 1
    devices = [device for server in servers for device in (server.get("devices") or [])]
    top_row: list[InlineKeyboardButton] = []
    if _allows(subscription, _ACTION_APK):
        top_row.append(
            InlineKeyboardButton(
                text="📱 Приложение",
                callback_data=commands.action_callback("apk", service=SERVICE),
            )
        )
    # Кнопка появляется, только когда самообслуживание реально доступно
    # (см. vpn/service.py::_grant_extra) — иначе гость с почти полной
    # квотой жал бы её впустую и получал отказ вместо понятной картины.
    # Квота у каждого сервера своя, поэтому кнопка — на каждый нуждающийся,
    # с явным node_id: иначе непонятно, где именно доливать.
    needy = self_serve_nodes if _allows(subscription, vpn_protocol.ACTION_GRANT_EXTRA) else []
    for server in servers:
        if server.get("node") not in needy:
            continue
        suffix = f" ({_server_label(server)})" if multi else ""
        top_row.insert(
            0,
            InlineKeyboardButton(
                text=f"➕ 100 ГБ{suffix}",
                callback_data=commands.action_callback(
                    "grant_extra", service=SERVICE, node_id=server.get("node")
                ),
            ),
        )
    rows: list[list[InlineKeyboardButton]] = [top_row] if top_row else []
    can_reissue = _allows(subscription, vpn_protocol.ACTION_REISSUE)
    can_revoke = _allows(subscription, vpn_protocol.ACTION_REVOKE)
    for device in devices:
        label = device["device_label"]
        # node_id подключения (vpn_peers.server) — перевыпуск и отзыв идут
        # ровно на ту ноду, где пир заведён (этап 39: серверов несколько).
        server = device.get("server")
        device_row: list[InlineKeyboardButton] = []
        if can_reissue:
            device_row.append(
                InlineKeyboardButton(
                    text=f"🔄 Перевыпустить «{label}»",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_REISSUE, label, service=SERVICE, node_id=server
                    ),
                )
            )
        if can_revoke:
            device_row.append(
                InlineKeyboardButton(
                    text=f"🗑 Отозвать «{label}»",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_REVOKE, label, service=SERVICE, node_id=server
                    ),
                )
            )
        if device_row:
            rows.append(device_row)
    # Имя устройства служба выбирает сама — случайный цветок (решение
    # пользователя 2026-08-04, см. vpn/service.py::_random_device_label) —
    # кнопке больше нечего предлагать заранее, а число устройств не
    # ограничено, поэтому она всего одна.
    if _allows(subscription, vpn_protocol.ACTION_ISSUE):
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ Новое устройство",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_ISSUE, service=SERVICE
                    ),
                )
            ]
        )
    if is_admin:
        admin_row = [
            InlineKeyboardButton(
                text="👥 Все гости",
                # Право кнопки (peers@vpn) теперь совпадает с признаком, по
                # которому она рисуется (_is_admin) — раньше рисовалась по
                # peers@vpn, а слала usage_all и отказывала админу с точечным
                # правом.
                callback_data=vpn_admin_view.guests_cb(0),
            )
        ]
        # Проверка сети — у любого живого vpn-инстанса (39.0.7(f)): состояние
        # проверок реплицировано на все живые (vpn_check/service.py::
        # _run_and_report фанаутит отчёт), а пробник умеет и reality, не только
        # awg-туннель в netns. Старый гейт «нужен сервер с awg» прятал кнопку
        # целиком, стоило упасть jeeves, хотя wooster жив и всё знает — тот же
        # класс окаменевшего гейта, что уже чинили для кнопки прокси.
        check_node = next((server.get("node") for server in servers if server.get("node")), None)
        if check_node is not None:
            admin_row.append(
                InlineKeyboardButton(
                    text="🛰 Проверка сети",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_CHECK_STATUS, service=SERVICE, node_id=check_node
                    ),
                )
            )
        rows.append(admin_row)
    # Прокси Telegram от VPN-транспорта не зависит — он живёт на том же
    # VPS сам по себе (на wooster поднят при reality-only раскладке).
    # Кнопка без node_id — обработчик фанаутит ACTION_PROXY_LINK по ВСЕМ
    # живым серверам с прокси и присылает ссылку каждого (решение
    # пользователя 2026-09-20: раньше показывался только один — первый
    # живой сервер списка, — второй сервер приходилось выцарапывать
    # отдельным запросом к ноде).
    #
    # Гейт — право `proxy_link@vpn`, а не админство (решение пользователя
    # 2026-09-24). Раньше кнопка жила внутри `if is_admin`, и прокси нельзя
    # было открыть гостю в принципе: право существовало, но ни выдать его
    # (в каталоге /guests его не было), ни нажать (кнопки гость не видел).
    if _allows(subscription, vpn_protocol.ACTION_PROXY_LINK) and any(
        server.get("proxy_available") for server in servers
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    text="✈️ Прокси Telegram",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_PROXY_LINK, service=SERVICE
                    ),
                ),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _target_label(url: str) -> str:
    """Короткое имя цели для плотной строки: «api.telegram.org» → «telegram»,
    «www.google.com» → «google», голый IP («1.1.1.1») — как есть. Выводится
    из самого URL, а не из хардкод-словаря — список целей (``check_targets``)
    растёт правкой конфига, без правки бота."""
    host = url.split("://", 1)[-1].split("/", 1)[0]
    host = host.removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 2 and not all(p.isdigit() for p in parts):
        return parts[-2]
    return host


def _short_error(error: object) -> str:
    """Однословный повод не грузить строку сырым выхлопом curl/системы —
    подробности всё равно ушли в БД (``last_error``), сюда — только чтобы
    отличить «не достучались» от «сервер ответил, но плохо»."""
    text = str(error or "").strip()
    if not text:
        return "ошибка"
    low = text.lower()
    if "exit 28" in low or "timeout" in low or "timed out" in low:
        return "таймаут"
    if low.startswith("http "):
        return text
    first = text.splitlines()[0]
    return first if len(first) <= 40 else first[:37] + "…"


def _check_status_text(
    states: list[dict], *, rollup: list[dict] | None = None, pending: bool = False
) -> str:
    """Админский экран проверок: сводный цвет пары (сервер, транспорт) и под
    ним — одна строка на наблюдателя со всеми целями сразу (не строка на
    каждую пару наблюдатель×цель — при матрице из нескольких целей это
    быстро тонет). Разбивка по наблюдателю нужна ровно затем, чтобы отличить
    блокировку в конкретной стране (кто-то видит, кто-то нет) от настоящей
    смерти сервера (не видит никто)."""
    lines = ["🛰 <b>VPN — проверка доступности</b>"]
    if not states:
        lines.append("")
        lines.append("Пока нет ни одной проверки — нажмите «Запустить проверку».")
    else:
        pair_status = {
            (row.get("server"), row.get("transport")): row.get("status")
            for row in (rollup or [])
        }
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in states:
            grouped.setdefault((row.get("server") or "?", row.get("transport") or "?"), []).append(
                row
            )
        for (server, transport), rows in sorted(grouped.items()):
            lines.append("")
            head_icon = _CHECK_ICON.get(pair_status.get((server, transport), ""), "")
            label = _TRANSPORT_LABEL.get(transport, transport)
            lines.append(
                f"<b>{html.escape(server)}</b> · {html.escape(label)}{_suffix(head_icon)}"
            )
            by_node: dict[str, list[dict]] = {}
            for row in rows:
                by_node.setdefault(row["node"], []).append(row)
            for node in sorted(by_node):
                node_rows = sorted(by_node[node], key=lambda r: r["target"])
                node_icon = (
                    "🔴"
                    if any(r["status"] == vpn_check.ALERTING for r in node_rows)
                    else "🟢"
                )
                parts = []
                for row in node_rows:
                    target_label = html.escape(_target_label(row["target"]))
                    if row["status"] == vpn_check.ALERTING:
                        error = html.escape(_short_error(row.get("last_error")))
                        parts.append(f"{target_label} — {error}")
                    else:
                        latency = row.get("last_latency_ms")
                        latency_note = f" {latency} мс" if latency is not None else ""
                        parts.append(f"{target_label}{latency_note}")
                lines.append(
                    f"  {node_icon} <code>{html.escape(node)}</code> — " + ", ".join(parts)
                )
    if pending:
        lines.append("")
        lines.append(
            "⏳ Проверка запущена — жмите «↻ Обновить», пока не увидите свежий результат."
        )
    return "\n".join(lines)


# Своя локальная кнопка «назад», без RPC к службе (см. _redraw_card) — не
# заводим отдельный ACTION_* в vpn/protocol.py, как и «usage_all» рядом:
# право проверяется middleware по тому же принципу («действие@vpn»).
_ACTION_VPN_CARD = "vpn_card"


def _check_status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Запустить проверку",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_CHECK_NOW, service=SERVICE
                    ),
                ),
                InlineKeyboardButton(
                    text="↻ Обновить",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_CHECK_STATUS, service=SERVICE
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=commands.action_callback(_ACTION_VPN_CARD, service=SERVICE),
                )
            ],
        ]
    )


def _proxy_text(result: dict, *, admin: bool) -> str:
    """Карточка прокси. Гостю — только то, чем он пользуется.

    SOCKS5-адрес (решение пользователя 2026-09-24) — служебный вход для ботов
    внутри tailnet, а не второй способ подключить Telegram: гостю он бесполезен
    (в tailnet его нет), а знать внутренний адрес ноды ему незачем.
    """
    label = result.get("label")
    heading = "✈️ <b>Прокси Telegram (mtg)</b>"
    if label:
        heading += f" · {html.escape(str(label))}"
    heading += " — общая ссылка, один секрет на всех.\n"
    text = (
        heading
        + f"Ссылка: {html.escape(result['tg_link'])}\n"
        f"t.me: {html.escape(result['t_me_link'])}\n\n"
        f"Сервер: <code>{html.escape(str(result['host']))}</code>\n"
        f"Порт: <code>{result['port']}</code>\n"
        f"Секрет: <code>{html.escape(result['secret'])}</code>"
    )
    if admin:
        text += (
            "\n\n🧦 SOCKS5 для ботов (доступен только внутри tailnet):\n"
            f"<code>{html.escape(str(result['socks_host']))}:{result['socks_port']}</code>"
        )
    return text


def _proxy_only_keyboard() -> InlineKeyboardMarkup:
    """Мини-карточка гостя, которому открыли прокси, но не VPN."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✈️ Прокси Telegram",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_PROXY_LINK, service=SERVICE
                    ),
                )
            ]
        ]
    )


def _proxy_keyboard(
    node_id: str | None = None, *, can_rotate: bool = True
) -> InlineKeyboardMarkup | None:
    """None — кнопок нет вовсе (гость без `proxy_rotate_secret@vpn`).

    Смена секрета рвёт ссылку у ВСЕХ, кто её получил, — такую кнопку нельзя
    показывать рядом с гостевой ссылкой даже в виде «⛔️ Недоступно» при
    нажатии: промахнуться по ней стоит слишком дорого.
    """
    if not can_rotate:
        return None
    # node_id привязывает «Сменить секрет» к тому же серверу, чей результат
    # выше — секрет хранится в vpn.sqlite каждой ноды по отдельности, без
    # node_id кнопка ушла бы на первую живую ноду (не обязательно эту).
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Сменить секрет (старая ссылка перестанет работать)",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_PROXY_ROTATE_SECRET, service=SERVICE, node_id=node_id
                    ),
                )
            ]
        ]
    )


def _store_row(emoji: str, store: str, vpn_url: str, wg_url: str) -> str:
    return (
        f'{emoji} {store}: <a href="{html.escape(vpn_url)}">AmneziaVPN</a> · '
        f'<a href="{html.escape(wg_url)}">AmneziaWG</a>'
    )


# Обе ссылки в строке текстом (не длинным URL): решение пользователя
# 2026-08-04, "на аппстор обе и на гугл плей обе" — по магазину, а не по
# приложению, чтобы у AmneziaWG остался явный официальный путь на iOS
# (сайдлоада там нет вовсе).
def _apk_links_text(config: Settings) -> str:
    cfg = config.vpn
    return (
        "📱 Настоятельно рекомендуем полную версию — <b>AmneziaVPN</b>. Есть и "
        "облегчённая — <b>AmneziaWG</b> (её и использует эта настройка).\n\n"
        + _store_row("🍎", "App Store", cfg.amneziavpn_ios_app_store_url, cfg.ios_app_store_url)
        + "\n"
        + _store_row("🤖", "Google Play", cfg.amneziavpn_google_play_url, cfg.google_play_url)
        + "\n"
        f"🌐 Официальный сайт (все платформы, обе версии): "
        f"{html.escape(cfg.official_download_url)}\n\n"
        "Если магазины недоступны — можно получить файл .apk облегчённой версии "
        "прямо здесь, кнопка ниже. Это аварийный способ на случай, если "
        "официальные способы не сработали, а не замена им."
    )


def _apk_links_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Дать APK",
                    callback_data=commands.action_callback("apk", "send", service=SERVICE),
                )
            ]
        ]
    )


def _hiddify_links_text(config: Settings) -> str:
    cfg = config.vpn
    return (
        "🌐 Для <b>VLESS</b> — приложение <b>Hiddify</b> (все платформы):\n"
        f'🍎 App Store: <a href="{html.escape(cfg.hiddify_ios_app_store_url)}">Hiddify</a>\n'
        f'🤖 Google Play: <a href="{html.escape(cfg.hiddify_google_play_url)}">Hiddify</a>\n'
        f"🖥 Windows / macOS / Linux / APK: {html.escape(cfg.hiddify_releases_url)}\n"
        f"🌐 Официальный сайт: {html.escape(cfg.hiddify_site_url)}\n\n"
        "📋 <b>Как подключиться:</b>\n"
        "1. Установите приложение по одной из ссылок выше.\n"
        "2. В /vpn нажмите «➕ Новое устройство».\n"
        "3. Придёт файл настроек (JSON) — импортируйте его: Hiddify → ⚙️ → "
        "«⋮» (три точки справа вверху) → «Импорт» → «Импортировать настройки "
        "из файла» → выберите скачанный файл. Это только маршруты/DNS, само "
        "подключение здесь ещё не появится.\n"
        "4. Следом придёт ссылка «vless://» — скопируйте её, вернитесь на "
        "главный экран Hiddify → «+» (справа вверху) → «Буфер обмена». "
        "Профиль подключения появится в списке.\n"
        "5. Нажмите на профиль — «Нажмите для подключения»."
    )


def _app_links_text(config: Settings, transports: list[str]) -> str:
    """Ссылки на клиенты под транспорты этой ноды: Hiddify для reality,
    AmneziaVPN/WG для awg (оба блока, если нода несёт оба)."""
    blocks: list[str] = []
    if vpn_protocol.TRANSPORT_REALITY in transports:
        blocks.append(_hiddify_links_text(config))
    if vpn_protocol.TRANSPORT_AWG in transports or not blocks:
        blocks.append(_apk_links_text(config))
    return "\n\n".join(blocks)


# Шаг 1 выдачи — только когда живых серверов несколько (этап 39.0.5).
_PICK_SERVER_TEXT = "Где завести новое устройство?"

# Выбор технологии перед выдачей нового устройства — только когда нода несёт
# оба транспорта. Порядок: VLESS первым (нужен гостям в РФ).
_PICK_TRANSPORT_TEXT = (
    "Какой технологией выдать новое устройство?\n\n"
    "• <b>VLESS (Reality)</b> — работает из России (для DPI неотличимо от "
    "обычного HTTPS).\n"
    "• <b>AmneziaWG</b> — быстрее там, где не блокируют (не Россия)."
)


def _server_picker_keyboard(servers: list[dict]) -> InlineKeyboardMarkup:
    """Шаг 1 выдачи: где завести устройство. Выбранный сервер уезжает в
    node_id callback'а — дальше его подхватывает _need_dst()."""
    rows = [
        [
            InlineKeyboardButton(
                # Индикатор прямо на кнопке (39.0.7(f)): выбор локации — это
                # ровно тот момент, когда «а этот сервер вообще доступен?»
                # решает дело; на кнопке места на разбивку по транспортам
                # нет, поэтому один сводный цвет (_server_icon).
                text=f"{_server_label(server)}{_suffix(_server_icon(server))}",
                callback_data=commands.action_callback(
                    vpn_protocol.ACTION_ISSUE, service=SERVICE, node_id=server["node"]
                ),
            )
        ]
        for server in servers
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=commands.action_callback(_ACTION_VPN_CARD, service=SERVICE),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _transport_picker_keyboard(
    transports: list[str], node_id: str | None = None, icons: dict[str, str] | None = None
) -> InlineKeyboardMarkup:
    """``icons`` — индикатор на транспорт (39.0.7(f)); без него кнопки те же,
    просто без цвета: нода могла и не прислать `check` (старая версия)."""
    icons = icons or {}
    rows: list[list[InlineKeyboardButton]] = []
    for transport in _TRANSPORT_ORDER:
        if transport not in transports:
            continue
        hint = " — для РФ" if transport == vpn_protocol.TRANSPORT_REALITY else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{_TRANSPORT_LABEL[transport]}{hint}{_suffix(icons.get(transport, ''))}",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_ISSUE,
                        f"{_TRANSPORT_PICK_PREFIX}{transport}",
                        service=SERVICE,
                        node_id=node_id,
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=commands.action_callback(_ACTION_VPN_CARD, service=SERVICE),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _is_admin(subscription: Subscription) -> bool:
    return subscription.allows_action(vpn_protocol.ACTION_PEERS, SERVICE)


async def usage_text(node_link: ServiceLink, chat_id: int) -> str:
    """Расход VPN произвольного чата — текстом, без клавиатуры.

    Переиспользуется карточкой гостя в /guests (кнопка «Статистика VPN»):
    та же служба и то же действие usage@vpn, что и у команды /vpn, только для
    чужого chat_id — админ имеет право знать расход того, кем управляет.
    """
    servers = await vpn_nodes.fanout(node_link, vpn_protocol.ACTION_USAGE, {"chat_id": chat_id})
    if not servers:
        return _VPN_UNAVAILABLE
    return _usage_text(servers, show_access=True)


async def _card(
    node_link: ServiceLink, chat_id: int
) -> tuple[str, list[dict]] | tuple[None, list[dict]]:
    """Расход гостя по его локациям. Мёртвая нода просто выпадает из списка
    (vpn_nodes.fanout) — карточка с одной локацией полезнее отказа; локация,
    куда гость не допущен, выпадает тоже, но по другой причине (этап D).

    Пустой список после фильтра — не ошибка связи, а «доступ ещё не выдан»:
    отличает их вызывающий по тому, пришло ли что-то от роя вообще.
    """
    servers = await vpn_nodes.fanout(node_link, vpn_protocol.ACTION_USAGE, {"chat_id": chat_id})
    if not servers:
        return _VPN_UNAVAILABLE, []
    allowed = _allowed_servers(servers)
    if not allowed:
        return _NO_VPN_ACCESS, []
    return None, allowed


def _self_serve_nodes(servers: list[dict], config: Settings) -> list[str]:
    """Ноды, где гость уже у порога своей квоты — там и предлагаем «+100 ГБ».
    Квоты раздельные, поэтому проверяем каждый сервер сам по себе."""
    threshold = config.vpn.warn_remaining_gb * 1_000_000_000
    return [
        server["node"]
        for server in servers
        if server.get("node")
        and _is_allowed(server)
        and server.get("remaining_bytes", 0) <= threshold
    ]


async def _live_servers_for(node_link: ServiceLink, chat_id: int) -> list[dict]:
    """Локации для пикера «➕ Новое устройство» — только открытые этому гостю.

    Транспорты приходят из ``live_vpn_servers`` (get_state ноды), допуск — из
    ``usage`` с chat_id: про конкретного гостя get_state ничего не знает.
    Сшиваем по ``node``.

    Пустой результат вызывающий трактует как «сказать нечего» и идёт прежним
    путём: решение о допуске принимает служба, здесь только выбор из того, что
    гостю и так открыто.
    """
    live = await vpn_nodes.live_vpn_servers(node_link)
    servers = await vpn_nodes.fanout(node_link, vpn_protocol.ACTION_USAGE, {"chat_id": chat_id})
    allowed = {server.get("node") for server in servers if _is_allowed(server)}
    return [item for item in live if item.get("node") in allowed]


@router.message(Command(commands.VPN.name))
async def cmd_vpn(
    message: Message,
    node_link: ServiceLink,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    error, servers = await _card(node_link, message.chat.id)
    if error is not None:
        # «Локаций нет» — ещё не повод закрыть дверь: прокси Telegram живёт
        # отдельно от VPN и допуска к локации не требует. Гостю, которому
        # открыли только его, показываем мини-карточку с одной кнопкой вместо
        # отказа (решение пользователя 2026-09-24).
        if error is _NO_VPN_ACCESS and _allows(subscription, vpn_protocol.ACTION_PROXY_LINK):
            await message.answer(_PROXY_ONLY_CARD, reply_markup=_proxy_only_keyboard())
            return
        await message.answer(error)
        return
    keyboard = _card_keyboard(
        servers,
        subscription=subscription,
        self_serve_nodes=_self_serve_nodes(servers, config),
    )
    await message.answer(_usage_text(servers), reply_markup=keyboard)


async def _redraw_card(
    callback: CallbackQuery, node_link: ServiceLink, subscription: Subscription, config: Settings
) -> None:
    error, servers = await _card(node_link, callback.message.chat.id)
    if error is not None:
        if error is _NO_VPN_ACCESS and _allows(subscription, vpn_protocol.ACTION_PROXY_LINK):
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_text(
                    _PROXY_ONLY_CARD, reply_markup=_proxy_only_keyboard()
                )
        return
    keyboard = _card_keyboard(
        servers,
        subscription=subscription,
        self_serve_nodes=_self_serve_nodes(servers, config),
    )
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(_usage_text(servers), reply_markup=keyboard)


def _conf_filename(device_label: str, location: str = "") -> str:
    return vpn_protocol.secret_filename(vpn_protocol.TRANSPORT_AWG, device_label, location)


def _file_caption(label_escaped: str) -> str:
    return (
        f"🔐 Конфиг устройства «{label_escaped}».\n"
        "Нажмите на файл → «Открыть с помощью» → AmneziaWG — тоннель "
        "добавится сразу, без копирования."
    )


def _qr_caption(label_escaped: str) -> str:
    return (
        f"📶 QR — устройство «{label_escaped}». Откройте AmneziaWG → "
        "«+» → «Сканировать QR-код» (удобно для настройки с ДРУГОГО "
        "устройства — сфотографировать собственный экран телефон не может)."
    )


def _reality_filename(device_label: str, location: str = "") -> str:
    return vpn_protocol.secret_filename(vpn_protocol.TRANSPORT_REALITY, device_label, location)


def _reality_file_caption(label_escaped: str) -> str:
    return (
        f"⚙️ Настройки маршрутизации «{label_escaped}» (VLESS) — файл.\n"
        "Hiddify → «+» → «Из файла» → выберите этот файл, «Импортировать». "
        "Это НЕ подключение, а РФ-маршруты/DNS — импортируйте один раз при "
        "первой установке приложения на устройстве. Само подключение "
        "добавьте отдельно — ссылкой или QR ниже, без этого шага профиля "
        "не будет."
    )


def _reality_qr_caption(label_escaped: str) -> str:
    return (
        f"📶 QR — «{label_escaped}» (VLESS), подключение. Hiddify → «+» → "
        "«Сканировать QR» (удобно для настройки с ДРУГОГО устройства). "
        "Если на этом устройстве Hiddify ставится впервые — один раз "
        "импортируйте ещё и файл настроек маршрутизации (см. предыдущие "
        "сообщения/ссылку ниже)."
    )


def _secret_filename(transport: str, device_label: str, location: str = "") -> str:
    return vpn_protocol.secret_filename(transport, device_label, location)


def _secret_file_caption(transport: str, label_escaped: str) -> str:
    if transport == vpn_protocol.TRANSPORT_REALITY:
        return _reality_file_caption(label_escaped)
    return _file_caption(label_escaped)


def _secret_qr_caption(transport: str, label_escaped: str) -> str:
    if transport == vpn_protocol.TRANSPORT_REALITY:
        return _reality_qr_caption(label_escaped)
    return _qr_caption(label_escaped)


def _reality_links_note(result_or_secret: dict | PendingVpnSecret) -> str:
    """Строка сообщения с deep-link и vless://-ссылкой (только reality) — это
    и есть само подключение (tap-to-copy); файл/QR из _reality_file_caption —
    только РФ-маршруты/DNS, нужны один раз при первой установке приложения на
    устройстве, подключение они не создают."""
    if isinstance(result_or_secret, PendingVpnSecret):
        deep_link, share_url = result_or_secret.deep_link, result_or_secret.share_url
    else:
        deep_link = result_or_secret.get("deep_link")
        share_url = result_or_secret.get("share_url")
    parts: list[str] = []
    if deep_link:
        parts.append(f"🔗 Импорт одним нажатием: <code>{html.escape(str(deep_link))}</code>")
    if share_url:
        parts.append(f"Ссылка: <code>{html.escape(str(share_url))}</code>")
    if not parts:
        return ""
    return "\n\n📲 Это и есть подключение (нужно для каждого устройства):\n" + "\n".join(parts)


async def _send_secret(
    notifier: Notifier,
    pending: PendingVpnSecrets,
    chat_id: int,
    action_id: str,
    result: dict,
    ttl_s: float,
    message_thread_id: int | None = None,
) -> None:
    device_label = str(result.get("device_label") or "")
    label_escaped = html.escape(device_label)
    config_text = str(result.get("config_text") or "")
    qr_b64 = result.get("qr_png_b64")
    transport = str(result.get("transport") or vpn_protocol.TRANSPORT_AWG)
    location = str(result.get("location") or "")
    # reality: deep-link и vless://-ссылка (tap-to-copy) — всегда в тексте
    # сообщения, независимо от того, каким способом ушёл основной артефакт.
    links_note = (
        _reality_links_note(result) if transport == vpn_protocol.TRANSPORT_REALITY else ""
    )

    # Первое устройство чата — почти наверняка настраивается прямо с этого
    # телефона (файл удобнее), второе и далее — обычно для другого устройства
    # или человека (QR удобнее). См. докстринг файла и
    # vpn/service.py::_issue::prior_device_count.
    file_first = int(result.get("prior_device_count") or 0) == 0

    primary_id: int | None
    if file_first:
        sent = await notifier.send_document(
            chat_id,
            config_text.encode("utf-8"),
            filename=_secret_filename(transport, device_label, location),
            caption=_secret_file_caption(transport, label_escaped),
            message_thread_id=message_thread_id,
        )
        primary_id = sent[0] if sent is not None else None
        reveal = "qr"
        prompt = (
            "Настраиваете ДРУГОЕ устройство? Удобнее QR-кодом — нажмите ниже. "
            "Кнопка одноразовая и скоро исчезнет."
        )
        button_text = "📶 Дать QR-код"
    else:
        primary_id = None
        if qr_b64:
            primary_id = await notifier.send_photo(
                chat_id,
                base64.b64decode(qr_b64),
                filename="vpn-qr.png",
                caption=_secret_qr_caption(transport, label_escaped),
                message_thread_id=message_thread_id,
            )
        reveal = "file"
        prompt = (
            "Настраиваете С ЭТОГО устройства? Удобнее файлом — нажмите ниже. "
            "Кнопка одноразовая и скоро исчезнет."
        )
        button_text = "📄 Дать файл конфига"

    token = pending.put(
        config_text,
        device_label,
        qr_b64,
        reveal,
        ttl_s,
        transport=transport,
        share_url=result.get("share_url"),
        deep_link=result.get("deep_link"),
        location=location,
    )
    button_id = await notifier.send_direct(
        chat_id,
        prompt + links_note,
        reply_markup=_get_config_keyboard(action_id, token, button_text),
        message_thread_id=message_thread_id,
    )

    async def _cleanup() -> None:
        await asyncio.sleep(ttl_s)
        pending.discard(token)
        if primary_id is not None:
            await notifier.delete_message(chat_id, primary_id)
        if button_id is not None:
            await notifier.delete_message(chat_id, button_id)

    asyncio.create_task(_cleanup(), name="vpn-secret-cleanup")


def _get_config_keyboard(action_id: str, token: str, button_text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button_text,
                    # action_id — тот же, что выдал секрет (issue/reissue):
                    # право на кнопку то же самое (issue@vpn/reissue@vpn),
                    # заводить отдельное право под "забрать уже выданное"
                    # незачем (тот же приём, что apk/apk:send).
                    callback_data=commands.action_callback(
                        action_id, f"f_{token}", service=SERVICE
                    ),
                )
            ]
        ]
    )


async def handle_action(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    pending_vpn_secrets: PendingVpnSecrets,
    book: SubscriptionBook | None = None,
    gate: Gatekeeper | None = None,
    bot: Bot | None = None,
) -> None:
    """Вызывается из bot/handlers/node.py::on_dynamic_action для service="vpn"."""
    parsed = commands.parse_action_callback(callback.data)
    if parsed is None or callback.message is None:
        await callback.answer()
        return
    _service, action_id, value, node_id = parsed
    chat_id = callback.message.chat.id

    async def _need_dst() -> Address | None:
        """Адрес ноды с ``vpn`` (держатель подключения из callback, иначе
        первая живая). None + алерт пользователю — VPN в рое нет. Резолвим
        по требованию, а не в начале: выдача уже готового секрета (кнопка
        ``f_…``) и ссылки на приложение не должны падать из-за отвала VPN."""
        dst = await vpn_nodes.resolve_vpn_dst(node_link, server=node_id)
        if dst is None:
            await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
        return dst

    if action_id == "apk":
        if not _is_private(chat_id):
            await callback.answer("Напишите мне в личку — там и пришлю.", show_alert=True)
            return
        if value == "send":
            # Второе нажатие — «📦 Дать APK» под сообщением со ссылками: сам
            # файл шлём только теперь, а не сразу на первое нажатие (решение
            # пользователя 2026-08-04 — на iOS сайдлоада нет вовсе, апстор
            # надёжнее любого .apk). Кнопка прячется — второй раз файл не
            # переспросить с того же сообщения по ошибке.
            dst = await _need_dst()
            if dst is None:
                return
            await callback.answer("Отправляю…")
            text = await deliver_apk(
                node_link,
                notifier,
                chat_id,
                dst,
                message_thread_id=callback.message.message_thread_id,
            )
            await callback.message.answer(text)
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_reply_markup(reply_markup=None)
            return
        # Ссылки на клиенты под транспорты ноды-держателя (Hiddify для VLESS,
        # AmneziaVPN/WG для awg). Определяем best-effort — на VPN-дауне
        # показываем awg-блок (исходное поведение), не падаем.
        transports: list[str] = []
        links_dst = await vpn_nodes.resolve_vpn_dst(node_link, server=node_id)
        if links_dst is not None:
            with contextlib.suppress(ServiceUnavailableError, ProtoError):
                state = await node_link.get_state(dst=links_dst)
                transports = state.get("transports") or []
        await callback.answer()
        await callback.message.answer(
            _app_links_text(config, transports),
            reply_markup=(
                _apk_links_keyboard()
                if vpn_protocol.TRANSPORT_AWG in transports or not transports
                else None
            ),
            disable_web_page_preview=True,
        )
        return

    if action_id == vpn_protocol.ACTION_GRANT_EXTRA:
        dst = await _need_dst()
        if dst is None:
            return
        try:
            await node_link.command(vpn_protocol.ACTION_GRANT_EXTRA, {"chat_id": chat_id}, dst=dst)
        except ProtoError as exc:
            if exc.code == vpn_protocol.ERR_QUOTA_CEILING:
                result = await node_link.command(
                    vpn_protocol.ACTION_REQUEST_EXTRA, {"chat_id": chat_id}, dst=dst
                )
                await callback.answer()
                await callback.message.answer(
                    f"✋ Потолок самообслуживания достигнут — заявка №{result['request_id']} "
                    "отправлена админу."
                )
                return
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        except ServiceUnavailableError:
            await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
            return
        await callback.answer("Добавлено")
        await _redraw_card(callback, node_link, subscription, config)
        return

    if action_id in (vpn_protocol.ACTION_ISSUE, vpn_protocol.ACTION_REISSUE):
        if not _is_private(chat_id):
            await callback.answer("Секрет доступа выдаётся только в личке.", show_alert=True)
            return
        if value and value.startswith("f_"):
            # Кнопка под уже отправленным первым способом (файл или QR) —
            # секрет ждёт в памяти (bot/vpn_secrets.py), а не в БД и не в
            # самом callback_data (см. докстринг файла). ``reveal`` говорит,
            # какой из двух способов отдать теперь.
            secret = pending_vpn_secrets.pop(value[2:])
            if secret is None:
                await callback.answer(
                    "Ссылка устарела — запросите доступ ещё раз.", show_alert=True
                )
                return
            await callback.answer("Отправляю…")
            label_escaped = html.escape(secret.device_label)
            if secret.reveal == "qr" and secret.qr_png_b64:
                await notifier.send_photo(
                    chat_id,
                    base64.b64decode(secret.qr_png_b64),
                    filename="vpn-qr.png",
                    caption=_secret_qr_caption(secret.transport, label_escaped),
                    message_thread_id=callback.message.message_thread_id,
                )
            else:
                await notifier.send_document(
                    chat_id,
                    secret.config_text.encode("utf-8"),
                    filename=_secret_filename(
                        secret.transport, secret.device_label, secret.location
                    ),
                    caption=_secret_file_caption(secret.transport, label_escaped),
                    message_thread_id=callback.message.message_thread_id,
                )
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_reply_markup(reply_markup=None)
            return

        # Выбор транспорта из пикера: act:vpn:issue:t_<transport> — дальше как
        # обычный issue, транспорт уходит в payload.
        chosen_transport: str | None = None
        if value and value.startswith(_TRANSPORT_PICK_PREFIX):
            chosen_transport = value[len(_TRANSPORT_PICK_PREFIX) :]
            value = None

        if action_id == vpn_protocol.ACTION_REISSUE and not value:
            await callback.answer()
            return

        # Шаг 1 голой выдачи: если живых серверов несколько — сперва спросить
        # локацию (reissue идёт на ноду своего устройства, ему пикер не нужен).
        if (
            action_id == vpn_protocol.ACTION_ISSUE
            and chosen_transport is None
            and not value
            and not node_id
        ):
            live = await _live_servers_for(node_link, chat_id)
            if len(live) > 1:
                await callback.answer()
                with contextlib.suppress(TelegramBadRequest):
                    await callback.message.edit_text(
                        _PICK_SERVER_TEXT,
                        reply_markup=_server_picker_keyboard(live),
                    )
                return
            if len(live) == 1:
                # Открытая локация одна — пикер не нужен, но адресовать надо
                # именно её: «первая живая» могла бы оказаться закрытой.
                node_id = live[0].get("node") or node_id
            # Пусто — сюда бот не лезет: отказ выдаёт служба (_require_access),
            # и её текст точнее. Своей проверкой здесь мы заперли бы гостя ещё
            # и на случайном сбое фанаута, при том что настоящий барьер всё
            # равно на той стороне.

        dst = await _need_dst()
        if dst is None:
            return

        # Шаг 2: у выбранной ноды два транспорта — показать выбор технологии.
        if action_id == vpn_protocol.ACTION_ISSUE and chosen_transport is None and not value:
            node_transports: list[str] = []
            transport_icons: dict[str, str] = {}
            with contextlib.suppress(ServiceUnavailableError, ProtoError):
                state = await node_link.get_state(dst=dst)
                node_transports = state.get("transports") or []
                transport_icons = _check_icons(state)
            if len(node_transports) > 1:
                await callback.answer()
                with contextlib.suppress(TelegramBadRequest):
                    await callback.message.edit_text(
                        _PICK_TRANSPORT_TEXT,
                        reply_markup=_transport_picker_keyboard(
                            node_transports, dst.node, transport_icons
                        ),
                    )
                return

        await callback.answer("Выпускаю…")
        payload: dict[str, object] = {"chat_id": chat_id}
        if value:
            payload["device_label"] = value
        if chosen_transport:
            payload["transport"] = chosen_transport
        try:
            result = await node_link.command(action_id, payload, dst=dst)
        except ProtoError as exc:
            await callback.message.answer(f"⚠️ {exc.message}")
            return
        except ServiceUnavailableError:
            await callback.message.answer("⚠️ Служба VPN недоступна.")
            return
        await _send_secret(
            notifier,
            pending_vpn_secrets,
            chat_id,
            action_id,
            result,
            config.vpn.config_message_ttl_s,
            message_thread_id=callback.message.message_thread_id,
        )
        await _redraw_card(callback, node_link, subscription, config)
        return

    if action_id == vpn_protocol.ACTION_REVOKE:
        if not value:
            await callback.answer()
            return
        dst = await _need_dst()
        if dst is None:
            return
        try:
            await node_link.command(
                vpn_protocol.ACTION_REVOKE, {"chat_id": chat_id, "device_label": value}, dst=dst
            )
        except ProtoError as exc:
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        except ServiceUnavailableError:
            await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
            return
        await callback.answer(f"Отозвано: «{value}»")
        await _redraw_card(callback, node_link, subscription, config)
        return

    if action_id == "usage_all":
        dst = await _need_dst()
        if dst is None:
            return
        try:
            summary = await node_link.command(vpn_protocol.ACTION_USAGE, {}, dst=dst)
        except ProtoError as exc:
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        except ServiceUnavailableError:
            await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(_summary_text(summary))
        return

    # Админский раздел «👥 Все гости»: список → гость → локация. Экраны идут
    # под правом peers@vpn (это чтение чужого доступа), сама правка — под
    # set_access@vpn; оба проверены middleware до входа сюда.
    if action_id == vpn_protocol.ACTION_PEERS:
        await _handle_admin_screen(callback, node_link, book, value, node_id)
        return

    if action_id == vpn_protocol.ACTION_SET_ACCESS:
        await _handle_set_access(callback, node_link, notifier, book, value, node_id)
        return

    if action_id == vpn_admin_view.ACTION_SET_RIGHTS:
        await _handle_set_rights(callback, book, gate, bot, value)
        return

    if action_id == _ACTION_VPN_CARD:
        await callback.answer()
        await _redraw_card(callback, node_link, subscription, config)
        return

    if action_id == vpn_protocol.ACTION_CHECK_STATUS:
        dst = await _need_dst()
        if dst is None:
            return
        try:
            result = await node_link.command(vpn_protocol.ACTION_CHECK_STATUS, {}, dst=dst)
        except ProtoError as exc:
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        except ServiceUnavailableError:
            await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
            return
        await callback.answer()
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(
                _check_status_text(
                    result.get("states") or [], rollup=result.get("rollup") or []
                ),
                reply_markup=_check_status_keyboard(),
            )
        return

    if action_id == vpn_protocol.ACTION_CHECK_NOW:
        dst = await _need_dst()
        if dst is None:
            return
        try:
            result = await node_link.command(vpn_protocol.ACTION_CHECK_NOW, {}, dst=dst)
        except ProtoError as exc:
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        except ServiceUnavailableError:
            await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
            return
        dispatched = result.get("dispatched_to") or []
        await callback.answer(
            f"Запущено на: {', '.join(dispatched)}" if dispatched else "Разослать не удалось",
            show_alert=not dispatched,
        )
        # Тот же экран, что и «↻ Обновить», редактируется на месте — вместо
        # отдельного сообщения «откройте проверку ещё раз» (решение
        # пользователя 2026-08-17): check_now не возвращает states сам
        # (только dispatched_to/unreachable/skipped), поэтому берём их
        # отдельным вызовом и помечаем текст как «в процессе» — результаты
        # ещё старые, до следующего нажатия «↻ Обновить».
        states: list[dict] = []
        rollup: list[dict] = []
        with contextlib.suppress(ProtoError, ServiceUnavailableError):
            status = await node_link.command(vpn_protocol.ACTION_CHECK_STATUS, {}, dst=dst)
            states = status.get("states") or []
            rollup = status.get("rollup") or []
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(
                _check_status_text(states, rollup=rollup, pending=True),
                reply_markup=_check_status_keyboard(),
            )
        return

    if action_id == vpn_protocol.ACTION_PROXY_LINK:
        # node_id из callback'а — только у старой кнопки/повторного действия
        # после смены секрета (тогда бьём в конкретный сервер). Новая кнопка
        # «✈️ Прокси Telegram» шлёт callback без node_id — фанаутим ACTION_
        # PROXY_LINK по всем живым серверам с прокси и присылаем ссылку
        # КАЖДОГО (решение пользователя 2026-09-20: раньше отвечал только
        # первый живой сервер списка).
        if node_id:
            dst = await _need_dst()
            if dst is None:
                return
            try:
                result = await node_link.command(vpn_protocol.ACTION_PROXY_LINK, {}, dst=dst)
            except ProtoError as exc:
                await callback.answer(f"⚠️ {exc.message}", show_alert=True)
                return
            except ServiceUnavailableError:
                await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
                return
            results = [result]
        else:
            results = await vpn_nodes.fanout(node_link, vpn_protocol.ACTION_PROXY_LINK, {})
            if not results:
                await callback.answer(
                    "⚠️ Прокси Telegram сейчас не настроен ни на одном сервере.",
                    show_alert=True,
                )
                return
        await callback.answer()
        can_rotate = _allows(subscription, vpn_protocol.ACTION_PROXY_ROTATE_SECRET)
        for result in results:
            qr_b64 = result.get("qr_png_b64")
            if qr_b64:
                label = result.get("label")
                caption = "✈️ QR прокси Telegram"
                if label:
                    caption += f" · {label}"
                caption += " — отсканируйте в приложении."
                await notifier.send_photo(
                    chat_id,
                    base64.b64decode(qr_b64),
                    filename="proxy-qr.png",
                    caption=caption,
                    message_thread_id=callback.message.message_thread_id,
                )
            await callback.message.answer(
                _proxy_text(result, admin=can_rotate),
                reply_markup=_proxy_keyboard(result.get("node"), can_rotate=can_rotate),
            )
        return

    if action_id == vpn_protocol.ACTION_PROXY_ROTATE_SECRET:
        dst = await _need_dst()
        if dst is None:
            return
        try:
            result = await node_link.command(vpn_protocol.ACTION_PROXY_ROTATE_SECRET, {}, dst=dst)
        except ProtoError as exc:
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        except ServiceUnavailableError:
            await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
            return
        await callback.answer("Секрет сменён")
        # Сюда доходит только тот, у кого есть proxy_rotate_secret@vpn
        # (право проверено CallbackAuthorizationMiddleware), — значит админ.
        await callback.message.answer(
            "🔁 Старая ссылка перестала работать.\n\n" + _proxy_text(result, admin=True),
            reply_markup=_proxy_keyboard(result.get("node")),
        )
        return

    if action_id == vpn_protocol.ACTION_RESOLVE_REQUEST:
        if not value or "_" not in value:
            await callback.answer()
            return
        raw_id, _, decision = value.partition("_")
        approve = decision == "approve"
        dst = await _need_dst()
        if dst is None:
            return
        try:
            result = await node_link.command(
                vpn_protocol.ACTION_RESOLVE_REQUEST,
                {"request_id": int(raw_id), "approve": approve},
                dst=dst,
            )
        except (ProtoError, ServiceUnavailableError, ValueError) as exc:
            await callback.answer(f"⚠️ {exc}", show_alert=True)
            return
        await callback.answer("Одобрено" if approve else "Отклонено")
        await callback.message.answer(
            f"Заявка №{result['request_id']} — {'✅ одобрена' if approve else '🚫 отклонена'}."
        )
        return

    await callback.answer()


# --- админский раздел «👥 Все гости» ---------------------------------------


async def _guest_servers(node_link: ServiceLink, chat_id: int) -> list[dict]:
    """Все локации глазами конкретного гостя — включая закрытые: админ как раз
    и решает, какие открыть."""
    return await vpn_nodes.fanout(node_link, vpn_protocol.ACTION_USAGE, {"chat_id": chat_id})


async def _redraw_screen(callback: CallbackQuery, text: str, keyboard) -> None:
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=keyboard)


async def _handle_admin_screen(
    callback: CallbackQuery,
    node_link: ServiceLink,
    book: SubscriptionBook | None,
    value: str | None,
    node_id: str | None,
) -> None:
    if book is None:
        await callback.answer("⚠️ Список гостей сейчас недоступен.", show_alert=True)
        return
    guests = book.guests()

    rights_chat_id = vpn_admin_view.parse_rights_value(value)
    if rights_chat_id is not None:  # экран умений гостя
        guest = next((g for g in guests if g.chat_id == rights_chat_id), None)
        if guest is None:
            await callback.answer("Гость больше не в списке.", show_alert=True)
            return
        text, keyboard = vpn_admin_view.build_rights_view(guest)
        await callback.answer()
        await _redraw_screen(callback, text, keyboard)
        return

    chat_id = vpn_admin_view.parse_guest_value(value)
    if chat_id is None:  # список гостей, value — номер страницы
        offset = _parse_offset(value)
        access = {guest.chat_id: await _guest_servers(node_link, guest.chat_id) for guest in guests}
        offset = clamp_offset(offset, vpn_admin_view.GUEST_PAGE_SIZE, len(guests))
        text, keyboard = vpn_admin_view.build_guests_view(guests, access, offset)
        await callback.answer()
        await _redraw_screen(callback, text, keyboard)
        return

    guest = next((g for g in guests if g.chat_id == chat_id), None)
    if guest is None:
        await callback.answer("Гость больше не в списке.", show_alert=True)
        return
    servers = await _guest_servers(node_link, chat_id)
    if node_id is None:
        text, keyboard = vpn_admin_view.build_guest_view(guest, servers)
    else:
        server = next((s for s in servers if s.get("node") == node_id), None)
        if server is None:
            await callback.answer("Эта нода сейчас не на связи.", show_alert=True)
            return
        text, keyboard = vpn_admin_view.build_location_view(guest, server)
    await callback.answer()
    await _redraw_screen(callback, text, keyboard)


async def _handle_set_rights(
    callback: CallbackQuery,
    book: SubscriptionBook | None,
    gate: Gatekeeper | None,
    bot: Bot | None,
    value: str | None,
) -> None:
    """Тумблер умения гостя (bot/vpn_admin_view.py::VPN_TOGGLES).

    Меняется гостевая подписка, а не состояние службы — сети тут нет вовсе.
    """
    parsed = vpn_admin_view.parse_set_access_value(value)  # тот же «<chat_id>_<арг>»
    if parsed is None or book is None or gate is None:
        await callback.answer("⚠️ Список гостей сейчас недоступен.", show_alert=True)
        return
    chat_id, key = parsed
    toggle = vpn_admin_view.toggle_by_key(key)
    guest = next((g for g in book.guests() if g.chat_id == chat_id), None)
    if toggle is None or guest is None:
        await callback.answer("Гость больше не в списке.", show_alert=True)
        return
    updated = gate.set_guest_rights(chat_id, vpn_admin_view.toggle_rights(guest, toggle))
    if updated is None:
        # Гостя отозвали, пока экран был открыт (между отрисовкой и нажатием).
        await callback.answer("Гость больше не в списке.", show_alert=True)
        return
    was_on = vpn_admin_view.toggle_state(guest, toggle)
    # Меню Telegram у гостя перестраивается сразу: сняли «Видеть карточку» —
    # /vpn должна пропасть из его списка команд, а не молча отказывать.
    if bot is not None:
        await refresh_chat_menu(bot, chat_id, updated)
    await callback.answer(f"{toggle.label}: {'снято' if was_on else 'выдано'}")
    text, keyboard = vpn_admin_view.build_rights_view(updated)
    await _redraw_screen(callback, text, keyboard)


async def _handle_set_access(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    book: SubscriptionBook | None,
    value: str | None,
    node_id: str | None,
) -> None:
    parsed = vpn_admin_view.parse_set_access_value(value)
    if parsed is None or node_id is None or book is None:
        await callback.answer()
        return
    chat_id, arg = parsed
    guest = next((g for g in book.guests() if g.chat_id == chat_id), None)
    if guest is None:
        await callback.answer("Гость больше не в списке.", show_alert=True)
        return

    # «on»/«off» — только тумблер (гигабайты не трогаем: вернут доступ, и цифру
    # не придётся вспоминать); число — выдать столько ГБ и заодно открыть.
    payload: dict[str, object] = {"chat_id": chat_id}
    if arg in ("on", "off"):
        payload["allowed"] = arg == "on"
    else:
        try:
            payload["base_gb"] = int(arg)
        except ValueError:
            await callback.answer()
            return
        payload["allowed"] = True

    dst = Address(node=node_id, service=SERVICE)
    try:
        server = await node_link.command(vpn_protocol.ACTION_SET_ACCESS, payload, dst=dst)
    except ProtoError as exc:
        # Старая нода про допуск не знает — это рассинхрон версий, а не отказ.
        if exc.code == ERR_UNKNOWN_ACTION:
            await callback.answer(
                f"Нода «{node_id}» ещё не умеет выдавать доступ — обновите её.",
                show_alert=True,
            )
        else:
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
        return
    except ServiceUnavailableError:
        await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
        return

    await callback.answer(_access_toast(server))
    await _notify_guest_access(notifier, chat_id, server, opened=bool(server.get("allowed")))
    text, keyboard = vpn_admin_view.build_location_view(guest, server)
    await _redraw_screen(callback, text, keyboard)


def _access_toast(server: dict) -> str:
    # Тот же короткий вид, что и на экранах выдачи (флаг без названия страны):
    # тост всплывает прямо над ними.
    where = vpn_admin_view.server_name(server)
    if not server.get("allowed"):
        return f"{where}: доступ закрыт"
    base_gb = server.get("base_limit_bytes", 0) / 1_000_000_000
    return f"{where}: открыт, {base_gb:.0f} ГБ"


async def _notify_guest_access(
    notifier: Notifier, chat_id: int, server: dict, *, opened: bool
) -> None:
    """Сказать гостю, что у него изменилось. Отдельного события протокола не
    заводим: кнопку нажал человек в боте — бот и сообщает (события службы
    ходят по другому поводу, см. bot/node_events.py)."""
    if not _is_private(chat_id):
        return
    where = html.escape(str(server.get("label") or server.get("node") or "VPN"))
    if opened:
        base_gb = server.get("base_limit_bytes", 0) / 1_000_000_000
        text = f"📶 VPN: вам открыт доступ — {where}, {base_gb:.0f} ГБ в месяц. Карточка: /vpn"
    else:
        text = f"📶 VPN: доступ к локации {where} закрыт."
    with contextlib.suppress(Exception):
        await notifier.send_direct(chat_id, text)


def _parse_offset(value: str | None) -> int:
    try:
        return max(0, int(value)) if value else 0
    except ValueError:
        return 0


def resolve_request_callback(request_id: int) -> InlineKeyboardMarkup:
    """Кнопки approve/deny под уведомлением админу о заявке на трафик
    (bot/node_events.py::EVENT_VPN_EXTRA_REQUESTED)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_RESOLVE_REQUEST,
                        f"{request_id}_approve",
                        service=SERVICE,
                    ),
                ),
                InlineKeyboardButton(
                    text="🚫 Отклонить",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_RESOLVE_REQUEST, f"{request_id}_deny", service=SERVICE
                    ),
                ),
            ]
        ]
    )
