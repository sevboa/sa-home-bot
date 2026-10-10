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
from aiogram.filters import Command, Filter
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from sa_home_bot.bot import (
    commands,
    vpn_admin_view,
    vpn_devices,
    vpn_faq,
    vpn_help,
    vpn_nodes,
    vpn_proxy_screen,
    vpn_report,
    vpn_settings,
    vpn_wizard,
)
from sa_home_bot.bot.invites import Gatekeeper
from sa_home_bot.bot.menu import refresh_chat_menu
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.pagination import clamp_offset
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.vpn_apk import deliver_apk
from sa_home_bot.bot.vpn_secrets import PendingVpnSecret, PendingVpnSecrets
from sa_home_bot.config import Settings
from sa_home_bot.domain import vpn_check
from sa_home_bot.people.book import PeopleBook
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
    "🟠 — доступен не отовсюду (где-то уже блокируют), 🔴 — не отвечает ни одному наблюдателю."
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
    # Пира нет на живом сервере (vpn/service.py::_mark_broken, 39.0.8(e)):
    # конфиг гостя уже не сработает, поможет только перевыпуск. Поле нет —
    # нода старая, и «сломано» мы не утверждаем.
    if device.get("broken"):
        return (
            f"• 🔧 «{html.escape(device['device_label'])}» не отвечает — "
            "сервер его не помнит, перевыпустите"
        )
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


def _usage_text(
    servers: list[dict], *, show_access: bool = False, unavailable: list[dict] | None = None
) -> str:
    """Карточка расхода. Квота у каждого сервера своя (счёт за трафик у VPS
    раздельный) — лимиты НЕ суммируются, каждая локация идёт своим блоком.

    Гостю на вход идут только ДОПУЩЕННЫЕ локации: закрытая не показывается
    вовсе, а не строкой «доступа нет» (решение владельца 2026-09-19) — он
    видит то, что ему выдали, и не гадает, чего просить. ``show_access``
    включает пометку закрытых — это для админских экранов, где смотрят чужой
    расход и как раз надо понимать, где доступ открыт, а где нет.
    """
    unavailable = unavailable or []
    multi = len(servers) + len(unavailable) > 1 or bool(unavailable)
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
    for server in unavailable:
        lines.append("")
        lines.append(f"<b>{html.escape(_server_label(server))}</b>: 🔌 Сервер недоступен")
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


def _admin_row(
    servers: list[dict], subscription: Subscription | None
) -> list[InlineKeyboardButton] | None:
    """Админская строка «👥 Все гости» / «🛰 Проверка сети» (None — не админ)."""
    if subscription is None or not _is_admin(subscription):
        return None
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
    return admin_row


def _proxy_row(
    servers: list[dict], subscription: Subscription | None
) -> list[InlineKeyboardButton] | None:
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
        return [
            InlineKeyboardButton(
                text="✈️ Прокси Telegram",
                callback_data=commands.action_callback(
                    vpn_protocol.ACTION_PROXY_LINK, service=SERVICE
                ),
            )
        ]
    return None


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
    multi = len(servers) > 1
    devices = [device for server in servers for device in (server.get("devices") or [])]
    top_row: list[InlineKeyboardButton] = []
    if _allows(subscription, _ACTION_APK):
        top_row.append(
            InlineKeyboardButton(
                text="❓ Помощь",
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
    admin_row = _admin_row(servers, subscription)
    if admin_row is not None:
        rows.append(admin_row)
    proxy_row = _proxy_row(servers, subscription)
    if proxy_row is not None:
        rows.append(proxy_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- Экспертный режим (этап 57.3): главная, «⚙️ Управление», устройства ------
#
# Кнопки идут по схеме act:vpn:<действие>[:<значение>[:<нода>]]. Права — те, что
# уже есть: экраны чтения живут под ``vpn_card@vpn`` (значение — экран), удаление
# — ``revoke@vpn``, перевыпуск/починка — ``reissue@vpn``; новых прав нет.
# Устройство в callback — 8-символьный хэш имени (vpn_devices.device_key), по
# свежему usage он разрешается обратно в имя; префикс «~» в значении отличает
# новые кнопки от старых («значение = имя устройства»).
#
# Значения ``vpn_card``:  m — список устройств; d<ключ> — карточка;
#   r<ключ>[-<подпись>-<маска>] — галочки перевыпуска (57.5, bot/vpn_settings.py);
#   x<ключ> — подтверждение удаления.
# Значения ``reissue``:  ~m<ключ>-<подпись>-<маска> — перевыпустить выбранные (57.5);
#   ~r<ключ> / ~a<ключ> + нода — перевыпустить VLESS / AmneziaWG
#   этой страны; ~f<ключ> + нода — починить страну (все сломанные подключения).
# Значения ``revoke``:  ~d<ключ> — удалить устройство целиком.
_SCREEN_LIST = "m"
_SCREEN_DEVICE = "d"
_SCREEN_REISSUE = "r"
_SCREEN_DELETE = "x"
_EXPERT_PREFIX = "~"
_KIND_REALITY = "r"
_KIND_AWG = "a"
_KIND_FIX = "f"
_KIND_DELETE = "d"
_KIND_BY_TRANSPORT = {
    vpn_protocol.TRANSPORT_REALITY: _KIND_REALITY,
    vpn_protocol.TRANSPORT_AWG: _KIND_AWG,
}
_TRANSPORT_BY_KIND = {kind: transport for transport, kind in _KIND_BY_TRANSPORT.items()}


def _screen_cb(screen: str, key: str = "") -> str:
    return commands.action_callback(_ACTION_VPN_CARD, f"{screen}{key}", service=SERVICE)


def _home_cb() -> str:
    return commands.action_callback(_ACTION_VPN_CARD, service=SERVICE)


def _back_button(text: str, callback_data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=text, callback_data=callback_data)]


def _new_device_button() -> InlineKeyboardButton:
    # Выбор платформы → устройство с VLESS во всех странах → «Получить настройки» (57.4).
    return InlineKeyboardButton(
        text="➕ Новое устройство",
        callback_data=vpn_settings.card_cb(vpn_settings.SCREEN_NEW),
    )


def _home_keyboard(
    servers: list[dict], *, subscription: Subscription | None, self_serve_nodes: list[str]
) -> InlineKeyboardMarkup:
    """Новая главная (у человека есть устройства). Кнопки — строго по правам."""
    rows: list[list[InlineKeyboardButton]] = []
    if _allows(subscription, vpn_protocol.ACTION_ISSUE):
        rows.append([vpn_wizard.wizard_button(), _new_device_button()])
    second: list[InlineKeyboardButton] = []
    if _allows(subscription, _ACTION_APK):
        second.append(
            InlineKeyboardButton(
                text="❓ Помощь",
                callback_data=commands.action_callback("apk", service=SERVICE),
            )
        )
    second.append(InlineKeyboardButton(text="⚙️ Управление", callback_data=_screen_cb(_SCREEN_LIST)))
    rows.append(second)
    proxy_row = _proxy_row(servers, subscription)
    if proxy_row is not None:
        rows.append(proxy_row)
    if vpn_report.can_report(subscription):
        rows.append([vpn_report.report_button()])
    admin_row = _admin_row(servers, subscription)
    if admin_row is not None:
        rows.append(admin_row)
    if _allows(subscription, vpn_protocol.ACTION_GRANT_EXTRA):
        multi = len(servers) > 1
        grants = [
            InlineKeyboardButton(
                text=f"➕ 100 ГБ{' ' + vpn_devices.Country(server['node'], _server_label(server)).short if multi else ''}",
                callback_data=commands.action_callback(
                    "grant_extra", service=SERVICE, node_id=server["node"]
                ),
            )
            for server in servers
            if server.get("node") in self_serve_nodes
        ]
        if grants:
            rows.append(grants)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _list_keyboard(
    devices: list[vpn_devices.Device], *, subscription: Subscription | None
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for dev in devices:
        row.append(
            InlineKeyboardButton(
                text=dev.label[:28], callback_data=_screen_cb(_SCREEN_DEVICE, dev.key)
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if _allows(subscription, vpn_protocol.ACTION_ISSUE):
        rows.append([vpn_wizard.wizard_button(), _new_device_button()])
    rows.append(_back_button("⬅️ Назад", _home_cb()))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _subscription_page_url(
    node_link: ServiceLink, chat_id: int, device: vpn_devices.Device
) -> str | None:
    """Адрес страницы подписки Hiddify (57.10) для карточки устройства — с любой
    живой ноды, у которой есть страница. ``None`` — у устройства нет VLESS или
    страницы нет нигде (кнопка тогда не рисуется)."""
    if not any(c.transport == vpn_protocol.TRANSPORT_REALITY for c in device.issued):
        return None
    answers = await vpn_nodes.fanout(
        node_link,
        vpn_protocol.ACTION_GET_SUBSCRIPTION,
        {"chat_id": chat_id, "device_label": device.label},
    )
    urls = [str(a["page_url"]) for a in answers if a.get("page_url")]
    # https надёжнее http: Telegram и браузеры охотнее открывают его.
    urls.sort(key=lambda u: not u.startswith("https://"))
    return urls[0] if urls else None


def _card_keyboard_for_device(
    device: vpn_devices.Device,
    servers: list[dict],
    *,
    subscription: Subscription | None,
    page_url: str | None = None,
) -> InlineKeyboardMarkup:
    countries = vpn_devices.countries_of(servers)
    rows: list[list[InlineKeyboardButton]] = []
    if page_url:
        rows.append([InlineKeyboardButton(text="🔌 Подключить в Hiddify", url=page_url)])
    if _allows(subscription, vpn_protocol.ACTION_ISSUE):
        rows.append(
            [
                InlineKeyboardButton(
                    text="📥 Получить настройки",
                    callback_data=vpn_settings.card_cb(vpn_settings.SCREEN_PICK, device.key),
                )
            ]
        )
    if _allows(subscription, vpn_protocol.ACTION_REISSUE):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Перевыпустить ключи",
                    callback_data=_screen_cb(_SCREEN_REISSUE, device.key),
                )
            ]
        )
        for node in device.broken_nodes():
            country = countries.get(node)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🔧 Починить {country.short if country else node}",
                        callback_data=commands.action_callback(
                            vpn_protocol.ACTION_REISSUE,
                            f"{_EXPERT_PREFIX}{_KIND_FIX}{device.key}",
                            node_id=node,
                            service=SERVICE,
                        ),
                    )
                ]
            )
    if _allows(subscription, vpn_protocol.ACTION_REVOKE):
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑 Удалить устройство",
                    callback_data=_screen_cb(_SCREEN_DELETE, device.key),
                )
            ]
        )
    if vpn_report.can_report(subscription):
        rows.append([vpn_report.report_button(device.key)])
    rows.append(_back_button("⬅️ К устройствам", _screen_cb(_SCREEN_LIST)))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _delete_keyboard(device: vpn_devices.Device) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_REVOKE,
                        f"{_EXPERT_PREFIX}{_KIND_DELETE}{device.key}",
                        service=SERVICE,
                    ),
                ),
                InlineKeyboardButton(
                    text="Отмена", callback_data=_screen_cb(_SCREEN_DEVICE, device.key)
                ),
            ]
        ]
    )


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
            (row.get("server"), row.get("transport")): row.get("status") for row in (rollup or [])
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
            lines.append(f"<b>{html.escape(server)}</b> · {html.escape(label)}{_suffix(head_icon)}")
            by_node: dict[str, list[dict]] = {}
            for row in rows:
                by_node.setdefault(row["node"], []).append(row)
            for node in sorted(by_node):
                node_rows = sorted(by_node[node], key=lambda r: r["target"])
                node_icon = (
                    "🔴" if any(r["status"] == vpn_check.ALERTING for r in node_rows) else "🟢"
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
        lines.append("⏳ Проверка запущена — жмите «↻ Обновить», пока не увидите свежий результат.")
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
        heading + f"Ссылка: {html.escape(result['tg_link'])}\n"
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


# Шаг 1 выдачи — только когда живых серверов несколько (этап 39.0.5).
_PICK_SERVER_TEXT = "Где завести новое устройство?"

# Выбор технологии перед выдачей нового устройства — только когда нода несёт
# оба транспорта. Порядок: VLESS первым (нужен гостям в РФ).
_PICK_TRANSPORT_TEXT = (
    "Какой технологией выдать новое устройство?\n\n"
    "• <b>VLESS · Hiddify</b> — для DPI неотличимо от обычного HTTPS.\n"
    "• <b>AmneziaWG</b> — быстрее, но в части стран может не работать."
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
    node_link: ServiceLink, chat_id: int, subscription: Subscription | None = None
) -> tuple[str | None, list[dict], list[dict]]:
    """Расход гостя по его локациям + недоступные серверы (``error, servers,
    unavailable``). Мёртвая нода выпадает из ответа (vpn_nodes.fanout) — карточка
    с одной локацией полезнее отказа; локация, куда гость не допущен, выпадает
    тоже, но по другой причине (этап D).

    Недоступные серверы (известные, но не ответившие) показываются только тем,
    у кого есть админское право на VPN: допуск гостя к локации хранит сама
    нода, и у лежащей про него не спросить — гостю «недоступен» было бы догадкой.

    Пустой список после фильтра — не ошибка связи, а «доступ ещё не выдан»:
    отличает их вызывающий по тому, пришло ли что-то от роя вообще.
    """
    answered = await vpn_nodes.fanout(node_link, vpn_protocol.ACTION_USAGE, {"chat_id": chat_id})
    vpn_nodes.remember_servers(answered)
    down = (
        vpn_nodes.unavailable_servers(answered)
        if subscription is not None and _is_admin(subscription)
        else []
    )
    if not answered and not down:
        return _VPN_UNAVAILABLE, [], []
    allowed = _allowed_servers(answered)
    if not allowed and not down:
        return _NO_VPN_ACCESS, [], []
    return None, allowed, down


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
    error, servers, down = await _card(node_link, message.chat.id, subscription)
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
    text, keyboard = _render_main(
        servers, down, subscription, config, wizard=_is_private(message.chat.id)
    )
    await message.answer(text, reply_markup=keyboard)


def _wizard_available(servers: list[dict], subscription: Subscription | None) -> bool:
    """Мастер возможен, если можно выдать устройство и есть открытый сервер
    с VLESS (мастер всегда VLESS · Hiddify)."""
    return _allows(subscription, vpn_protocol.ACTION_ISSUE) and any(
        _is_allowed(server) and vpn_protocol.TRANSPORT_REALITY in (server.get("transports") or [])
        for server in servers
    )


def _render_main(
    servers: list[dict],
    down: list[dict],
    subscription: Subscription | None,
    config: Settings,
    *,
    wizard: bool = False,
    expert: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Главная /vpn. Есть устройства — новая (этап 57.3): остаток квоты и
    предупреждение словами, без цветов и трафика по странам. Нет устройств —
    первый экран мастера (57.3a, ``wizard`` — личка и без «Я разберусь сам»);
    после «Я разберусь сам» (``expert``) — та же новая главная с пустым списком
    (57.4); иначе прежняя карточка (группа, нет VLESS-сервера)."""
    self_serve = _self_serve_nodes(servers, config)
    if vpn_devices.build_devices(servers) or (expert and _wizard_available(servers, subscription)):
        return (
            vpn_devices.home_text(servers, unavailable=down),
            _home_keyboard(servers, subscription=subscription, self_serve_nodes=self_serve),
        )
    if wizard and _wizard_available(servers, subscription):
        return vpn_wizard.INTRO_TEXT, vpn_wizard.intro_keyboard()
    return (
        _usage_text(servers, unavailable=down),
        _card_keyboard(servers, subscription=subscription, self_serve_nodes=self_serve),
    )


async def _redraw_card(
    callback: CallbackQuery,
    node_link: ServiceLink,
    subscription: Subscription,
    config: Settings,
    *,
    expert: bool = False,
) -> None:
    error, servers, down = await _card(node_link, callback.message.chat.id, subscription)
    if error is not None:
        if error is _NO_VPN_ACCESS and _allows(subscription, vpn_protocol.ACTION_PROXY_LINK):
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_text(
                    _PROXY_ONLY_CARD, reply_markup=_proxy_only_keyboard()
                )
        return
    text, keyboard = _render_main(
        servers,
        down,
        subscription,
        config,
        wizard=not expert and _is_private(callback.message.chat.id),
        expert=expert,
    )
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=keyboard)


async def _show_screen(
    callback: CallbackQuery,
    node_link: ServiceLink,
    subscription: Subscription,
    config: Settings,
    screen: str,
    key: str = "",
    *,
    answered: bool = True,
) -> None:
    """Экраны экспертного режима — одним сообщением, редактируется на месте.
    Устройство разрешается по свежему usage; пропало — назад к списку."""
    error, servers, _down = await _card(node_link, callback.message.chat.id, subscription)
    if error is not None:
        if not answered:
            await callback.answer()
        await _redraw_card(callback, node_link, subscription, config)
        return
    devices = vpn_devices.build_devices(servers)
    device = vpn_devices.find_device(devices, key) if key else None
    if screen not in (_SCREEN_LIST, vpn_settings.SCREEN_NEW) and device is None:
        if not answered:
            await callback.answer("Устройство не найдено — обновил список.", show_alert=True)
            answered = True
        screen = _SCREEN_LIST
    if not answered:
        await callback.answer()
    if screen == _SCREEN_DEVICE:
        text = vpn_devices.card_text(device, servers)
        page_url = await _subscription_page_url(node_link, callback.message.chat.id, device)
        keyboard = _card_keyboard_for_device(
            device, servers, subscription=subscription, page_url=page_url
        )
    elif screen == _SCREEN_DELETE:
        text, keyboard = vpn_devices.delete_text(device), _delete_keyboard(device)
    elif screen == vpn_settings.SCREEN_PICK:
        text = vpn_settings.pick_text(device.label)
        keyboard = vpn_settings.pick_keyboard(device.key)
    elif screen == vpn_settings.SCREEN_AWG:
        awg_servers = _awg_servers(servers)
        text = vpn_settings.awg_pick_text(device.label) if awg_servers else vpn_settings.NO_AWG_TEXT
        keyboard = vpn_settings.awg_pick_keyboard(device.key, awg_servers)
    elif screen == vpn_settings.SCREEN_NEW:
        text, keyboard = vpn_wizard.PICK_TEXT, vpn_settings.new_device_keyboard()
    else:
        text = vpn_devices.list_text(devices)
        keyboard = _list_keyboard(devices, subscription=subscription)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=keyboard)


async def _expert_reissue(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    pending: PendingVpnSecrets,
    value: str,
    node_id: str | None,
) -> None:
    """``reissue:~<вид><ключ>:<нода>`` — перевыпуск подключений устройства в одной
    стране: ``r``/``a`` — один VLESS / AmneziaWG, ``f`` — «🔧 Починить»: только
    сломанные. Новые настройки приходят тем же способом, что и при обычном
    перевыпуске (_send_secret)."""
    chat_id = callback.message.chat.id
    if not _is_private(chat_id):
        await callback.answer("Секрет доступа выдаётся только в личке.", show_alert=True)
        return
    kind, key = value[1:2], value[2:]
    error, servers, _down = await _card(node_link, chat_id, subscription)
    device = (
        vpn_devices.find_device(vpn_devices.build_devices(servers), key) if error is None else None
    )
    if device is None or not node_id:
        await callback.answer("Устройство не найдено — обновите список.", show_alert=True)
        await _show_screen(callback, node_link, subscription, config, _SCREEN_LIST)
        return
    if kind == _KIND_FIX:
        transports = [c.transport for c in device.broken_in(node_id)]
    else:
        transport = _TRANSPORT_BY_KIND.get(kind)
        found = device.connection(node_id, transport) if transport else None
        transports = [found.transport] if found is not None else []
    if not transports:
        await callback.answer("Тут нечего перевыпускать.", show_alert=True)
        await _show_screen(callback, node_link, subscription, config, _SCREEN_DEVICE, key)
        return
    await callback.answer("Перевыпускаю…")
    dst = Address(node=node_id, service=SERVICE)
    for transport in transports:
        try:
            result = await node_link.command(
                vpn_protocol.ACTION_REISSUE,
                {"chat_id": chat_id, "device_label": device.label, "transport": transport},
                dst=dst,
            )
        except ProtoError as exc:
            await callback.message.answer(f"⚠️ {exc.message}")
            continue
        except ServiceUnavailableError:
            await callback.message.answer("⚠️ Служба VPN недоступна.")
            break
        await _send_secret(
            notifier,
            pending,
            chat_id,
            vpn_protocol.ACTION_REISSUE,
            result,
            config.vpn.config_message_ttl_s,
            message_thread_id=callback.message.message_thread_id,
        )
    await _show_screen(callback, node_link, subscription, config, _SCREEN_DEVICE, key)


async def _show_reissue_select(
    callback: CallbackQuery,
    node_link: ServiceLink,
    subscription: Subscription,
    config: Settings,
    rest: str,
    *,
    answered: bool,
    note: str = "",
) -> None:
    """``vpn_card:r<ключ>[-<подпись>-<маска>]`` — галочки по выданным подключениям (57.5).
    Подпись списка не сошлась с текущей (подключение пропало или появилось) —
    выбор сбрасывается: номера битов уже указывают на другие подключения."""
    key, sig, mask = vpn_settings.parse_selection(rest)
    found = await _fresh_device(callback, node_link, subscription, config, key)
    if found is None:
        return
    device, servers = found
    conns = vpn_settings.reissue_connections(device)
    if not answered:
        await callback.answer()
    if sig is not None and (
        sig != vpn_settings.list_signature(conns) or mask & ~vpn_settings.all_mask(conns)
    ):
        mask, note = 0, vpn_settings.LIST_CHANGED_TEXT
    elif sig is None:
        mask = 0
    await _wizard_edit(
        callback,
        vpn_settings.reissue_select_text(device.label, note),
        vpn_settings.reissue_select_keyboard(
            device,
            conns,
            vpn_devices.countries_of(servers),
            mask,
            can_run=_allows(subscription, vpn_protocol.ACTION_REISSUE),
        ),
    )


async def _expert_reissue_multi(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    pending: PendingVpnSecrets,
    rest: str,
) -> None:
    """``reissue:~m<ключ>-<подпись>-<маска>`` — «🔄 Перевыпустить (N)»: reissue по
    каждой выбранной паре (нода, транспорт). Новые настройки — как при выдаче
    (57.4): AmneziaWG файлом + шаги, VLESS одним сообщением «VLESS · Hiddify».
    Часть нод отказала — итог говорит, что удалось, а что нет."""
    chat_id = callback.message.chat.id
    if not _is_private(chat_id):
        await callback.answer("Секрет доступа выдаётся только в личке.", show_alert=True)
        return
    key, sig, mask = vpn_settings.parse_selection(rest)
    error, servers, _down = await _card(node_link, chat_id, subscription)
    device = (
        vpn_devices.find_device(vpn_devices.build_devices(servers), key) if error is None else None
    )
    if device is None:
        await callback.answer("Устройство не найдено — обновите список.", show_alert=True)
        await _show_screen(callback, node_link, subscription, config, _SCREEN_LIST)
        return
    conns = vpn_settings.reissue_connections(device)
    if sig != vpn_settings.list_signature(conns) or mask & ~vpn_settings.all_mask(conns):
        await callback.answer(vpn_settings.LIST_CHANGED_TEXT, show_alert=True)
        await _show_reissue_select(
            callback,
            node_link,
            subscription,
            config,
            key,
            answered=True,
            note=vpn_settings.LIST_CHANGED_TEXT,
        )
        return
    chosen = vpn_settings.selected(conns, mask)
    if not chosen:
        await callback.answer(vpn_settings.NOTHING_PICKED_TEXT, show_alert=True)
        return
    await callback.answer("Перевыпускаю…")
    countries = vpn_devices.countries_of(servers)
    done: list[str] = []
    failed: list[tuple[str, str]] = []
    results: list[tuple[vpn_devices.Connection, dict]] = []
    for conn in chosen:
        name = vpn_settings.reissue_conn_text(conn, countries)
        try:
            result = await node_link.command(
                vpn_protocol.ACTION_REISSUE,
                {"chat_id": chat_id, "device_label": device.label, "transport": conn.transport},
                dst=Address(node=conn.node, service=SERVICE),
            )
        except ProtoError as exc:
            failed.append((name, exc.message))
            continue
        except ServiceUnavailableError:
            failed.append((name, "сервер не ответил"))
            continue
        done.append(name)
        results.append((conn, result))
    for conn, result in results:
        if conn.transport == vpn_protocol.TRANSPORT_AWG:
            await _awg_send(
                callback,
                notifier,
                config,
                pending,
                device,
                countries.get(conn.node) or vpn_devices.Country(conn.node, conn.node),
                result,
            )
    if any(conn.transport == vpn_protocol.TRANSPORT_REALITY for conn, _ in results):
        _err, fresh_servers, _d = await _card(node_link, chat_id, subscription)
        fresh = vpn_devices.find_device(vpn_devices.build_devices(fresh_servers), key)
        sent = fresh is not None and await _vless_send(
            callback,
            node_link,
            notifier,
            config,
            pending,
            fresh,
            fresh_servers,
            issue_missing=False,
            reissued=True,
        )
        if not sent:
            failed.append(
                (
                    "VLESS · Hiddify — настройки",
                    "ключи перевыпущены, но настройки не получены: нажмите «📥 Получить настройки»",
                )
            )
    await _wizard_edit(
        callback,
        vpn_settings.reissue_result_text(device.label, done, failed),
        vpn_settings.reissue_result_keyboard(key),
    )


async def _expert_delete(
    callback: CallbackQuery,
    node_link: ServiceLink,
    subscription: Subscription,
    config: Settings,
    value: str,
) -> None:
    """``revoke:~d<ключ>`` — снять ключи устройства на всех нодах и транспортах."""
    chat_id = callback.message.chat.id
    key = value[2:]
    error, servers, _down = await _card(node_link, chat_id, subscription)
    device = (
        vpn_devices.find_device(vpn_devices.build_devices(servers), key) if error is None else None
    )
    if device is None:
        await callback.answer("Устройство не найдено — обновите список.", show_alert=True)
        await _show_screen(callback, node_link, subscription, config, _SCREEN_LIST)
        return
    failed = 0
    for conn in device.issued:
        try:
            await node_link.command(
                vpn_protocol.ACTION_REVOKE,
                {"chat_id": chat_id, "device_label": device.label, "transport": conn.transport},
                dst=Address(node=conn.node, service=SERVICE),
            )
        except (ProtoError, ServiceUnavailableError):
            failed += 1
    if failed:
        await callback.answer(
            f"Удалено не всё: {failed} подключ. не отвечают. Повторите позже.", show_alert=True
        )
        await _show_screen(callback, node_link, subscription, config, _SCREEN_DEVICE, key)
        return
    await callback.answer(f"Удалено: {device.label}")
    await _show_screen(callback, node_link, subscription, config, _SCREEN_LIST)


# --- выдача настроек в экспертном режиме (этап 57.4) --------------------------


def _awg_servers(servers: list[dict]) -> list[dict]:
    """Открытые человеку страны, где выдаётся AmneziaWG."""
    return [
        server
        for server in servers
        if server.get("node")
        and _is_allowed(server)
        and vpn_protocol.TRANSPORT_AWG in (server.get("transports") or [])
    ]


def _schedule_cleanup(
    notifier: Notifier,
    pending: PendingVpnSecrets,
    chat_id: int,
    message_ids: list[int | None],
    tokens: list[str],
    ttl_s: float,
) -> None:
    """Сообщения с настройками и ожидающие кнопки-секреты исчезают по TTL."""

    async def _cleanup() -> None:
        await asyncio.sleep(ttl_s)
        for token in tokens:
            pending.discard(token)
        for message_id in message_ids:
            if message_id is not None:
                await notifier.delete_message(chat_id, message_id)

    asyncio.create_task(_cleanup(), name="vpn-settings-cleanup")


async def _fresh_device(
    callback: CallbackQuery,
    node_link: ServiceLink,
    subscription: Subscription,
    config: Settings,
    key: str,
) -> tuple[vpn_devices.Device, list[dict]] | None:
    """Устройство по ключу из свежего usage. Нет — алерт и возврат к списку."""
    error, servers, _down = await _card(node_link, callback.message.chat.id, subscription)
    device = (
        vpn_devices.find_device(vpn_devices.build_devices(servers), key) if error is None else None
    )
    if device is None:
        await callback.answer("Устройство не найдено — обновите список.", show_alert=True)
        await _show_screen(callback, node_link, subscription, config, _SCREEN_LIST)
        return None
    return device, servers


async def _command_or_none(
    node_link: ServiceLink, action: str, payload: dict, node: str
) -> dict | None:
    try:
        return await node_link.command(action, payload, dst=Address(node=node, service=SERVICE))
    except (ProtoError, ServiceUnavailableError) as exc:
        log.warning("vpn: %s на %s не удалось: %s", action, node, exc)
        return None


async def _deliver_vless(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    pending: PendingVpnSecrets,
    key: str,
) -> None:
    """``issue:~v<ключ>`` — VLESS · Hiddify одним сообщением: «🔌 Подключить»,
    ссылки всех стран, QR по кнопке; файл настроек — если устройство ещё не
    выходило на связь. Недостающие страны сначала довыпускаются."""
    chat_id = callback.message.chat.id
    if not _is_private(chat_id):
        await callback.answer("Секрет доступа выдаётся только в личке.", show_alert=True)
        return
    found = await _fresh_device(callback, node_link, subscription, config, key)
    if found is None:
        return
    device, servers = found
    await callback.answer("Готовлю настройки…")
    if not await _vless_send(
        callback, node_link, notifier, config, pending, device, servers, issue_missing=True
    ):
        await _wizard_edit(
            callback, vpn_settings.UNAVAILABLE_TEXT, vpn_settings.back_keyboard(device.key)
        )


async def _vless_send(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    pending: PendingVpnSecrets,
    device: vpn_devices.Device,
    servers: list[dict],
    *,
    issue_missing: bool,
    reissued: bool = False,
) -> bool:
    """Сообщение «VLESS · Hiddify» (57.4): файл — если без handshake, «🔌 Подключить»,
    ссылки стран, QR. ``issue_missing`` — довыпустить недостающие страны; после
    перевыпуска (``reissued``) — без довыпуска и с пометкой про обновление
    подписки. False — ни одна нода не ответила."""
    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id
    ttl_s = config.vpn.config_message_ttl_s
    reality = vpn_protocol.TRANSPORT_REALITY

    added: list[str] = []
    nodes: list[str] = []
    for server in servers:
        node = server.get("node")
        if not node:
            continue
        if device.connection(node, reality) is not None:
            nodes.append(node)
            continue
        if issue_missing and _is_allowed(server) and reality in (server.get("transports") or []):
            issued = await _command_or_none(
                node_link,
                vpn_protocol.ACTION_ISSUE,
                {"chat_id": chat_id, "device_label": device.label, "transport": reality},
                node,
            )
            if issued is not None:
                nodes.append(node)
                added.append(_server_label(server))
    countries = vpn_devices.countries_of(servers)
    results = await asyncio.gather(
        *(
            _command_or_none(
                node_link,
                vpn_protocol.ACTION_GET_VLESS,
                {"chat_id": chat_id, "device_label": device.label},
                node,
            )
            for node in nodes
        )
    )
    got = [(node, res) for node, res in zip(nodes, results, strict=True) if res]
    if not got:
        return False

    wants_file = config.vpn.wizard_settings_file
    file_needed = wants_file and not vpn_settings.had_vless_handshake(device)
    message_ids: list[int | None] = []
    first = got[0][1]
    config_text = str(first.get("config_text") or "")
    file_sent = False
    if file_needed and config_text:
        sent = await notifier.send_document(
            chat_id,
            config_text.encode("utf-8"),
            filename=_reality_filename(device.label, str(first.get("location") or "")),
            caption=vpn_settings.VLESS_FILE_CAPTION,
            message_thread_id=thread_id,
        )
        message_ids.append(sent[0] if sent is not None else None)
        file_sent = sent is not None

    page_url = await _subscription_page_url(node_link, chat_id, device)
    multi = len(got) > 1
    tokens: list[str] = []
    qr_buttons: list[tuple[str, str]] = []
    links: list[tuple[str, str]] = []
    for node, res in got:
        country = countries.get(node) or vpn_devices.Country(node, node)
        if res.get("share_url"):
            links.append((country.label, str(res["share_url"])))
        if res.get("qr_png_b64"):
            token = pending.put(
                str(res.get("config_text") or ""),
                device.label,
                res.get("qr_png_b64"),
                "qr",
                ttl_s,
                transport=reality,
                share_url=res.get("share_url"),
                deep_link=res.get("deep_link"),
                location=str(res.get("location") or country.label),
            )
            tokens.append(token)
            qr_buttons.append(
                (
                    vpn_settings.qr_button_text(country, multi),
                    commands.action_callback(
                        vpn_protocol.ACTION_ISSUE, f"f_{token}", service=SERVICE
                    ),
                )
            )
    text = vpn_settings.vless_text(
        device.label,
        links,
        has_page=bool(page_url),
        file_sent=file_sent,
        added=added,
        reissued=reissued,
    )
    button_id = await notifier.send_direct(
        chat_id,
        text,
        reply_markup=vpn_settings.vless_keyboard(
            page_url=page_url,
            file_again_key=device.key if wants_file and not file_sent else None,
            qr_buttons=qr_buttons,
        ),
        message_thread_id=thread_id,
    )
    message_ids.append(button_id)
    _schedule_cleanup(notifier, pending, chat_id, message_ids, tokens, ttl_s)
    return True


async def _vless_file_again(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    key: str,
) -> None:
    """``issue:~s<ключ>`` — файл настроек Hiddify ещё раз (по TTL удаляется)."""
    chat_id = callback.message.chat.id
    if not _is_private(chat_id):
        await callback.answer("Секрет доступа выдаётся только в личке.", show_alert=True)
        return
    found = await _fresh_device(callback, node_link, subscription, config, key)
    if found is None:
        return
    device, _servers = found
    conn = next((c for c in device.issued if c.transport == vpn_protocol.TRANSPORT_REALITY), None)
    result = (
        await _command_or_none(
            node_link,
            vpn_protocol.ACTION_GET_VLESS,
            {"chat_id": chat_id, "device_label": device.label},
            conn.node,
        )
        if conn is not None
        else None
    )
    config_text = str((result or {}).get("config_text") or "")
    if not config_text:
        await callback.answer("Сервер не ответил — повторите чуть позже.", show_alert=True)
        return
    await callback.answer("Отправляю…")
    sent = await notifier.send_document(
        chat_id,
        config_text.encode("utf-8"),
        filename=_reality_filename(device.label, str(result.get("location") or "")),
        caption=vpn_settings.VLESS_FILE_CAPTION,
        message_thread_id=callback.message.message_thread_id,
    )
    _delete_later(
        notifier, chat_id, sent[0] if sent is not None else None, config.vpn.config_message_ttl_s
    )


async def _awg_country(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    pending: PendingVpnSecrets,
    key: str,
    node: str | None,
) -> None:
    """``issue:~g<ключ>:<нода>`` — страна выбрана. Ключ AmneziaWG уже есть —
    сначала предупреждение, иначе выпуск."""
    found = await _fresh_device(callback, node_link, subscription, config, key)
    if found is None:
        return
    device, servers = found
    server = next((s for s in _awg_servers(servers) if s.get("node") == node), None)
    if server is None or not node:
        await callback.answer("В этой стране AmneziaWG сейчас не выдаётся.", show_alert=True)
        await _show_screen(callback, node_link, subscription, config, vpn_settings.SCREEN_AWG, key)
        return
    if device.connection(node, vpn_protocol.TRANSPORT_AWG) is not None:
        await callback.answer()
        country = vpn_devices.Country(node, _server_label(server))
        await _wizard_edit(
            callback,
            vpn_settings.awg_replace_text(device.label, country),
            vpn_settings.awg_replace_keyboard(key, node),
        )
        return
    await _awg_deliver(
        callback,
        node_link,
        notifier,
        config,
        subscription,
        pending,
        device,
        server,
        vpn_protocol.ACTION_ISSUE,
    )


async def _awg_deliver(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    pending: PendingVpnSecrets,
    device: vpn_devices.Device,
    server: dict,
    action: str,
) -> None:
    """Выпустить (issue) или заменить (reissue) ключ AmneziaWG в стране и отдать
    файл .conf + шаги установки. Всё удаляется по TTL."""
    chat_id = callback.message.chat.id
    if not _is_private(chat_id):
        await callback.answer("Секрет доступа выдаётся только в личке.", show_alert=True)
        return
    await callback.answer("Выпускаю…")
    node = server["node"]
    try:
        result = await node_link.command(
            action,
            {
                "chat_id": chat_id,
                "device_label": device.label,
                "transport": vpn_protocol.TRANSPORT_AWG,
            },
            dst=Address(node=node, service=SERVICE),
        )
    except ProtoError as exc:
        await callback.message.answer(f"⚠️ {exc.message}")
        return
    except ServiceUnavailableError:
        await callback.message.answer("⚠️ Служба VPN недоступна.")
        return
    await _awg_send(
        callback,
        notifier,
        config,
        pending,
        device,
        vpn_devices.Country(node, _server_label(server)),
        result,
    )
    if action == vpn_protocol.ACTION_REISSUE:
        # Предупреждение с «Выпустить новый» не должно остаться под рукой.
        await _show_screen(
            callback,
            node_link,
            subscription,
            config,
            vpn_settings.SCREEN_AWG,
            device.key,
        )


async def _awg_send(
    callback: CallbackQuery,
    notifier: Notifier,
    config: Settings,
    pending: PendingVpnSecrets,
    device: vpn_devices.Device,
    country: vpn_devices.Country,
    result: dict,
) -> None:
    """Файл .conf + шаги установки AmneziaVPN (57.4); всё удаляется по TTL."""
    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id
    ttl_s = config.vpn.config_message_ttl_s
    config_text = str(result.get("config_text") or "")
    qr_b64 = result.get("qr_png_b64")
    sent = await notifier.send_document(
        chat_id,
        config_text.encode("utf-8"),
        filename=_conf_filename(device.label, str(result.get("location") or "")),
        caption=vpn_settings.awg_file_caption(device.label, country),
        message_thread_id=thread_id,
    )
    tokens: list[str] = []
    qr_callback = None
    if qr_b64:
        token = pending.put(
            config_text,
            device.label,
            qr_b64,
            "qr",
            ttl_s,
            transport=vpn_protocol.TRANSPORT_AWG,
            location=str(result.get("location") or ""),
        )
        tokens.append(token)
        qr_callback = commands.action_callback(
            vpn_protocol.ACTION_ISSUE, f"f_{token}", service=SERVICE
        )
    button_id = await notifier.send_direct(
        chat_id,
        vpn_settings.awg_steps_text(device.label, country),
        reply_markup=vpn_settings.awg_steps_keyboard(config.vpn, qr_callback),
        message_thread_id=thread_id,
    )
    _schedule_cleanup(
        notifier,
        pending,
        chat_id,
        [sent[0] if sent is not None else None, button_id],
        tokens,
        ttl_s,
    )


async def _expert_issue(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    pending: PendingVpnSecrets,
    value: str,
    node_id: str | None,
) -> None:
    """``issue:~<вид><ключ>`` экспертного режима (57.4): v — VLESS · Hiddify,
    s — файл настроек ещё раз, g — AmneziaWG в стране, n — новое устройство."""
    kind, rest = value[1:2], value[2:]
    if kind == vpn_settings.KIND_VLESS:
        await _deliver_vless(callback, node_link, notifier, config, subscription, pending, rest)
    elif kind == vpn_settings.KIND_FILE:
        await _vless_file_again(callback, node_link, notifier, config, subscription, rest)
    elif kind == vpn_settings.KIND_AWG:
        await _awg_country(
            callback, node_link, notifier, config, subscription, pending, rest, node_id
        )
    elif kind == vpn_settings.KIND_NEW:
        await _wizard_create(callback, node_link, config, subscription, rest[:1], expert=True)
    else:
        await callback.answer()


async def _awg_replace(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    pending: PendingVpnSecrets,
    value: str,
    node_id: str | None,
) -> None:
    """``reissue:~g<ключ>:<нода>`` — «Выпустить новый» после предупреждения."""
    found = await _fresh_device(callback, node_link, subscription, config, value[2:])
    if found is None:
        return
    device, servers = found
    server = next((s for s in _awg_servers(servers) if s.get("node") == node_id), None)
    if server is None:
        await callback.answer("В этой стране AmneziaWG сейчас не выдаётся.", show_alert=True)
        return
    await _awg_deliver(
        callback,
        node_link,
        notifier,
        config,
        subscription,
        pending,
        device,
        server,
        vpn_protocol.ACTION_REISSUE,
    )


# --- пошаговая настройка (этап 57.3a) ---------------------------------------


def _delete_later(notifier: Notifier, chat_id: int, message_id: int | None, ttl_s: float) -> None:
    """Файл с настройками удаляется по тому же TTL, что и прочие секреты."""
    if message_id is None:
        return

    async def _cleanup() -> None:
        await asyncio.sleep(ttl_s)
        await notifier.delete_message(chat_id, message_id)

    asyncio.create_task(_cleanup(), name="vpn-wizard-file-cleanup")


async def _wizard_edit(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup) -> None:
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=keyboard)


async def _wizard_issue_one(
    node_link: ServiceLink, chat_id: int, node: str, label: str
) -> dict | None:
    try:
        return await node_link.command(
            vpn_protocol.ACTION_ISSUE,
            {
                "chat_id": chat_id,
                "device_label": label,
                "transport": vpn_protocol.TRANSPORT_REALITY,
            },
            dst=Address(node=node, service=SERVICE),
        )
    except (ProtoError, ServiceUnavailableError) as exc:
        log.warning("vpn: мастер не смог выпустить VLESS на %s: %s", node, exc)
        return None


async def _wizard_create(
    callback: CallbackQuery,
    node_link: ServiceLink,
    config: Settings,
    subscription: Subscription,
    platform_code: str,
    *,
    expert: bool = False,
) -> None:
    """``issue:~w<платформа>`` — создать устройство: VLESS во всех странах,
    открытых человеку. Часть нод не ответила — идём с тем, что есть.
    ``expert`` (``issue:~n<платформа>``, 57.4) — после создания открывается экран
    «Получить настройки» этого устройства, а не шаги мастера."""
    chat_id = callback.message.chat.id
    platform = vpn_wizard.PLATFORMS.get(platform_code)
    if platform is None:
        await callback.answer()
        return
    if not _is_private(chat_id):
        await callback.answer("Настройка идёт только в личке — напишите мне туда.", show_alert=True)
        return
    await callback.answer("Готовлю подключение…")
    error, servers, _down = await _card(node_link, chat_id, subscription)
    targets = [
        server
        for server in (servers if error is None else [])
        if _is_allowed(server)
        and vpn_protocol.TRANSPORT_REALITY in (server.get("transports") or [])
        and server.get("node")
    ]
    devices = vpn_devices.build_devices(servers) if error is None else []
    label = vpn_wizard.next_label(platform, {dev.label for dev in devices})
    results = await asyncio.gather(
        *(_wizard_issue_one(node_link, chat_id, server["node"], label) for server in targets)
    )
    if not any(result is not None for result in results):
        await _wizard_edit(
            callback,
            vpn_wizard.CREATE_FAILED_TEXT,
            vpn_settings.create_failed_keyboard(platform.code)
            if expert
            else vpn_wizard.create_failed_keyboard(platform.code),
        )
        return
    key = vpn_devices.device_key(label)
    if expert:
        await _wizard_edit(callback, vpn_settings.pick_text(label), vpn_settings.pick_keyboard(key))
        return
    needs_file = vpn_wizard.needs_settings_file(config.vpn, devices, key)
    await _wizard_edit(
        callback,
        _wizard_step_text(vpn_wizard.INSTALL, needs_file),
        vpn_wizard.step_keyboard(
            vpn_wizard.INSTALL, platform, key, config.vpn, needs_file=needs_file
        ),
    )


def _wizard_step_text(code: str, needs_file: bool) -> str:
    title = vpn_wizard.step_title(code, needs_file)
    body = {
        vpn_wizard.INSTALL: "Установите приложение Hiddify.",
        vpn_wizard.FILE: "Нажмите на файл ниже и выберите «Hiddify».",
        vpn_wizard.CONNECT: "Нажмите кнопку — откроется Hiddify, нажмите там «Добавить».",
    }[code]
    return f"<b>{title}</b>\n{body}"


async def _wizard_show(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    book: SubscriptionBook | None,
    value: str,
    store=None,
) -> None:
    """``vpn_card:w…`` — экраны мастера, одно сообщение, правится на месте."""
    chat_id = callback.message.chat.id
    code, step, rest = vpn_wizard.parse(value)
    if code == vpn_wizard.PICK:
        await callback.answer()
        await _wizard_edit(callback, vpn_wizard.PICK_TEXT, vpn_wizard.pick_keyboard())
        return
    if code == vpn_wizard.EXPERT:
        await callback.answer()
        await _redraw_card(callback, node_link, subscription, config, expert=True)
        return
    if code == vpn_wizard.DONE:
        await callback.answer()
        await _wizard_edit(callback, vpn_wizard.DONE_TEXT, vpn_wizard.done_keyboard())
        return

    # Остальные экраны: ``<шаг (для f/o/h)><платформа><ключ>``.
    platform = vpn_wizard.PLATFORMS.get(rest[:1])
    key = rest[1:]
    if platform is None:
        await callback.answer()
        await _redraw_card(callback, node_link, subscription, config)
        return
    p = platform.code

    if code == vpn_wizard.FAIL:
        await callback.answer()
        await _wizard_edit(callback, vpn_wizard.FAIL_TEXT, vpn_wizard.fail_keyboard(step, p, key))
        return
    if code == vpn_wizard.OTHER:
        await callback.answer()
        await _wizard_edit(
            callback,
            vpn_wizard.OTHER_TEXT,
            vpn_wizard.back_to_step_keyboard(step, p, key, help_button=True),
        )
        return

    error, servers, _down = await _card(node_link, chat_id, subscription)
    devices = vpn_devices.build_devices(servers) if error is None else []
    device = vpn_devices.find_device(devices, key) if key else None
    needs_file = vpn_wizard.needs_settings_file(config.vpn, devices, key)

    if code == vpn_wizard.HELP:
        await _wizard_help(
            callback, node_link, notifier, subscription, book, store,
            platform, step, key, needs_file,
        )
        return

    if code not in vpn_wizard.STEP_ORDER + (vpn_wizard.CHECK,):
        await callback.answer()
        return
    if device is None:
        await callback.answer("Устройство не найдено — начнём заново.", show_alert=True)
        await _wizard_edit(callback, vpn_wizard.PICK_TEXT, vpn_wizard.pick_keyboard())
        return
    await callback.answer()
    if code == vpn_wizard.FILE and not needs_file:
        code = vpn_wizard.CONNECT  # флаг выключили или Hiddify уже настроен
    page_url: str | None = None
    if code == vpn_wizard.FILE:
        if not _is_private(chat_id):
            return
        if not await _wizard_send_settings(
            callback, node_link, notifier, config, chat_id, device, platform, needs_file
        ):
            await _wizard_edit(
                callback, vpn_wizard.UNAVAILABLE_TEXT, vpn_wizard.unavailable_keyboard(code, p, key)
            )
        return
    if code == vpn_wizard.CONNECT:
        page_url = await _subscription_page_url(node_link, chat_id, device)
        if page_url is None:
            await _wizard_edit(
                callback, vpn_wizard.UNAVAILABLE_TEXT, vpn_wizard.unavailable_keyboard(code, p, key)
            )
            return
    text = (
        vpn_wizard.CHECK_TEXT if code == vpn_wizard.CHECK else _wizard_step_text(code, needs_file)
    )
    await _wizard_edit(
        callback,
        text,
        vpn_wizard.step_keyboard(
            code, platform, key, config.vpn, needs_file=needs_file, page_url=page_url
        ),
    )


async def _wizard_send_settings(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    chat_id: int,
    device: vpn_devices.Device,
    platform: vpn_wizard.Platform,
    needs_file: bool,
) -> bool:
    """Шаг 2: текст шага правится на месте, файл настроек (sing-box из
    ``get_vless``) уходит отдельным сообщением и удаляется по TTL секретов.
    False — получить файл не удалось (экран ошибки рисует вызывающий)."""
    conn = next((c for c in device.issued if c.transport == vpn_devices.REALITY), None)
    if conn is None:
        return False
    try:
        result = await node_link.command(
            vpn_protocol.ACTION_GET_VLESS,
            {"chat_id": chat_id, "device_label": device.label},
            dst=Address(node=conn.node, service=SERVICE),
        )
    except (ProtoError, ServiceUnavailableError):
        return False
    config_text = str(result.get("config_text") or "")
    if not config_text:
        return False
    key = device.key
    await _wizard_edit(
        callback,
        _wizard_step_text(vpn_wizard.FILE, needs_file),
        vpn_wizard.step_keyboard(vpn_wizard.FILE, platform, key, config.vpn, needs_file=needs_file),
    )
    sent = await notifier.send_document(
        chat_id,
        config_text.encode("utf-8"),
        filename=_reality_filename(device.label, str(result.get("location") or "")),
        caption=vpn_wizard.FILE_CAPTION,
        message_thread_id=callback.message.message_thread_id,
    )
    _delete_later(
        notifier, chat_id, sent[0] if sent is not None else None, config.vpn.config_message_ttl_s
    )
    return True


async def _wizard_help(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    subscription: Subscription,
    book: SubscriptionBook | None,
    store,
    platform: vpn_wizard.Platform,
    step: str,
    key: str,
    needs_file: bool,
) -> None:
    """«🙋 Позвать на помощь» → владельцу тем же механизмом, что «⚠️ Сообщить о
    проблеме» (``_submit_report``), причина — шаг мастера."""
    stuck = vpn_wizard.stuck_text(step, platform, needs_file)
    reason = html.escape(stuck[:1].upper() + stuck[1:])
    outcome = await _submit_report(
        node_link,
        notifier,
        book,
        store,
        chat_id=callback.message.chat.id,
        user=getattr(callback, "from_user", None),
        subscription=subscription,
        reason=reason,
        device_key=key,
    )
    if outcome == "throttled":
        await callback.answer(vpn_wizard.HELP_THROTTLED_TEXT, show_alert=True)
        return
    await callback.answer()
    text = vpn_wizard.HELP_SENT_TEXT if outcome == "sent" else vpn_wizard.HELP_NOBODY_TEXT
    await _wizard_edit(
        callback,
        text,
        vpn_wizard.back_to_step_keyboard(step, platform.code, key, help_button=False),
    )


# --- «⚠️ Сообщить о проблеме» и «💬 Ответить» (57.6b) ------------------------


async def _person(
    chat_id: int, user, subscription: Subscription | None, book, store
) -> tuple[str, str | None]:
    """Имя и ник человека для уведомления владельцу: карточка человека
    (people/book.py::PeopleBook), без БД — профиль Telegram из самого апдейта."""
    name = username = None
    pid = getattr(user, "id", None) or chat_id
    if store is not None:
        try:
            people = await PeopleBook.load(store, book)
            name, username = people.name(pid), people.username(pid) or None
        except Exception:  # noqa: BLE001 — имя не стоит срыва заявки
            log.warning("Карточка человека %s не прочиталась", pid, exc_info=True)
    name = name or getattr(user, "full_name", None) or (subscription.name if subscription else "")
    return name or str(chat_id), username or getattr(user, "username", None)


async def _report_context(node_link: ServiceLink, chat_id: int, device_key: str) -> list[str]:
    """Строки контекста по свежему usage всех нод; сбой — без контекста."""
    try:
        answered = await vpn_nodes.fanout(
            node_link, vpn_protocol.ACTION_USAGE, {"chat_id": chat_id}
        )
        vpn_nodes.remember_servers(answered)
        down = vpn_nodes.unavailable_servers(answered)
        return vpn_report.context_lines(_allowed_servers(answered), down, device_key)
    except Exception:  # noqa: BLE001
        log.warning("Контекст заявки по VPN не собрался (chat=%s)", chat_id, exc_info=True)
        return []


async def _submit_report(
    node_link: ServiceLink,
    notifier: Notifier,
    book,
    store,
    *,
    chat_id: int,
    user,
    subscription: Subscription | None,
    reason: str,
    device_key: str = "",
) -> vpn_help.Outcome:
    """Одна заявка владельцу (мастер и «Сообщить о проблеме»); ``reason`` — HTML."""
    if chat_id == 0 or not vpn_help.has_admins(book):  # chat_id=0 — пробник, не человек
        return "nobody"
    if vpn_help.is_throttled(chat_id):
        return "throttled"
    name, username = await _person(chat_id, user, subscription, book, store)
    context = await _report_context(node_link, chat_id, device_key)
    return await vpn_help.send_help_request(
        book,
        notifier,
        chat_id=chat_id,
        who=vpn_help.who_text(name, username),
        reason=reason,
        context=context,
    )


async def _handle_report(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    subscription: Subscription,
    book,
    store,
    value: str | None,
) -> None:
    chat_id = callback.message.chat.id
    code, key = vpn_report.parse_report(value)
    if chat_id == 0 or not vpn_report.can_report(subscription):
        await callback.answer("⛔️ Недоступно", show_alert=True)
        return
    if code in (vpn_report.MENU, vpn_report.MENU_FAQ):
        await callback.answer()
        if key:
            back = _screen_cb(_SCREEN_DEVICE, key)
        elif code == vpn_report.MENU_FAQ:
            back = vpn_faq.list_cb()
        else:
            back = _home_cb()
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(
                vpn_report.MENU_TEXT, reply_markup=vpn_report.menu_keyboard(key, back_cb=back)
            )
        return
    if vpn_help.is_throttled(chat_id):
        await callback.answer(vpn_report.THROTTLED_TEXT, show_alert=True)
        return
    user = getattr(callback, "from_user", None)
    if code == vpn_report.OTHER:
        await callback.answer()
        sent = await callback.message.answer(
            vpn_report.OTHER_PROMPT,
            reply_markup=ForceReply(input_field_placeholder="Что случилось?"),
        )
        message_id = getattr(sent, "message_id", None)
        if message_id is not None:
            vpn_report.remember(
                chat_id,
                message_id,
                vpn_report.new_pending(
                    vpn_report.KIND_REPORT, getattr(user, "id", None) or chat_id, device_key=key
                ),
            )
        return
    reason = vpn_report.REASONS[code][1]
    outcome = await _submit_report(
        node_link,
        notifier,
        book,
        store,
        chat_id=chat_id,
        user=user,
        subscription=subscription,
        reason=reason,
        device_key=key,
    )
    if outcome == "throttled":
        await callback.answer(vpn_report.THROTTLED_TEXT, show_alert=True)
        return
    await callback.answer()
    text = vpn_report.SENT_TEXT if outcome == "sent" else vpn_report.NOBODY_TEXT
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=vpn_report.done_keyboard(_home_cb()))


async def _handle_owner_reply_button(
    callback: CallbackQuery, subscription: Subscription, book, store, value: str | None
) -> None:
    """«💬 Ответить» под заявкой — только админу: ForceReply, ответ уйдёт человеку."""
    try:
        target = int(value or "")
    except ValueError:
        target = 0
    if not vpn_report.can_reply(subscription) or target == 0:
        await callback.answer("⛔️ Недоступно", show_alert=True)
        return
    await callback.answer()
    user = getattr(callback, "from_user", None)
    target_sub = book.for_chat(target) if book is not None else None
    name, username = await _person(target, None, target_sub, book, store)
    shown = f"{name} (@{username})" if username else name
    sent = await callback.message.answer(
        html.escape(vpn_report.owner_prompt(shown)),
        reply_markup=ForceReply(input_field_placeholder="Ответ человеку"),
    )
    message_id = getattr(sent, "message_id", None)
    if message_id is not None:
        vpn_report.remember(
            callback.message.chat.id,
            message_id,
            vpn_report.new_pending(
                vpn_report.KIND_ANSWER,
                getattr(user, "id", None) or callback.message.chat.id,
                target_chat=target,
            ),
        )


class VpnReplyFilter(Filter):
    """Текстовый reply на сообщение бота, которое просило описание проблемы или
    ответ владельца, от того же человека, что нажимал кнопку. Чужой reply и
    reply на что-то другое — пропуск дальше по роутерам (в диалог с Альфредом)."""

    async def __call__(self, message: Message) -> bool | dict:
        reply = message.reply_to_message
        if (
            reply is None
            or message.chat is None
            or message.from_user is None
            or not message.text
            or message.text.startswith("/")
        ):
            return False
        entry = vpn_report.lookup(message.chat.id, reply.message_id)
        if entry is None or entry.user_id != message.from_user.id:
            return False
        return {"vpn_reply": entry}


@router.message(VpnReplyFilter())
async def on_vpn_reply(
    message: Message,
    vpn_reply: vpn_report.Pending,
    node_link: ServiceLink,
    notifier: Notifier,
    book: SubscriptionBook | None = None,
    store=None,
    subscription: Subscription | None = None,
) -> None:
    chat_id = message.chat.id
    reply_id = message.reply_to_message.message_id
    if vpn_report.is_expired(vpn_reply):
        vpn_report.forget(chat_id, reply_id)
        await message.answer(vpn_report.EXPIRED_TEXT)
        return
    text = (message.text or "").strip()[: vpn_report.MAX_DESCRIPTION]
    if subscription is None and book is not None:
        subscription = book.for_chat(chat_id)
    if vpn_reply.kind == vpn_report.KIND_ANSWER:
        if not vpn_report.can_reply(subscription):
            return
        vpn_report.forget(chat_id, reply_id)
        delivered = await notifier.send_direct(
            vpn_reply.target_chat, vpn_report.ANSWER_PREFIX + html.escape(text)
        )
        await message.answer(
            vpn_report.OWNER_SENT_TEXT if delivered is not None else vpn_report.OWNER_FAILED_TEXT
        )
        return
    if not vpn_report.can_report(subscription) or chat_id == 0:
        return
    if vpn_help.is_throttled(chat_id):
        await message.answer(vpn_report.THROTTLED_TEXT)
        return
    vpn_report.forget(chat_id, reply_id)
    outcome = await _submit_report(
        node_link,
        notifier,
        book,
        store,
        chat_id=chat_id,
        user=message.from_user,
        subscription=subscription,
        reason=html.escape(text),
        device_key=vpn_reply.device_key,
    )
    if outcome == "throttled":
        await message.answer(vpn_report.THROTTLED_TEXT)
    else:
        await message.answer(vpn_report.SENT_TEXT if outcome == "sent" else vpn_report.NOBODY_TEXT)


def _conf_filename(device_label: str, location: str = "") -> str:
    return vpn_protocol.secret_filename(vpn_protocol.TRANSPORT_AWG, device_label, location)


def _file_caption(label_escaped: str) -> str:
    return (
        f"🔐 Конфиг устройства «{label_escaped}».\n"
        "Нажмите на файл → «Открыть с помощью» → AmneziaWG — тоннель "
        "добавится сразу, без копирования."
    )


def _qr_caption(label_escaped: str, flag: str = "") -> str:
    return (
        f"📶 QR — «{label_escaped}» · AmneziaWG{' ' + flag if flag else ''}. Откройте AmneziaVPN → "
        "«+» → «QR-код» (удобно для настройки с ДРУГОГО "
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


def _reality_qr_caption(label_escaped: str, flag: str = "") -> str:
    return (
        f"📶 QR — «{label_escaped}» · VLESS · Hiddify{' ' + flag if flag else ''}. "
        "Hiddify → «+» → «Сканировать QR» (удобно для настройки с ДРУГОГО устройства)."
    )


def _secret_filename(transport: str, device_label: str, location: str = "") -> str:
    return vpn_protocol.secret_filename(transport, device_label, location)


def _secret_file_caption(transport: str, label_escaped: str) -> str:
    if transport == vpn_protocol.TRANSPORT_REALITY:
        return _reality_file_caption(label_escaped)
    return _file_caption(label_escaped)


def _secret_qr_caption(transport: str, label_escaped: str, location: str = "") -> str:
    where = html.escape(vpn_protocol.country_flag(location))
    if transport == vpn_protocol.TRANSPORT_REALITY:
        return _reality_qr_caption(label_escaped, where)
    return _qr_caption(label_escaped, where)


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
    links_note = _reality_links_note(result) if transport == vpn_protocol.TRANSPORT_REALITY else ""

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
                caption=_secret_qr_caption(transport, label_escaped, location),
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


def _without_button(message: object, callback_data: str | None) -> InlineKeyboardMarkup | None:
    """Клавиатура сообщения без нажатой кнопки; пусто — ``None`` (у сообщения с
    несколькими кнопками-секретами, напр. QR по странам, остальные остаются)."""
    markup = getattr(message, "reply_markup", None)
    rows = [
        [b for b in row if b.callback_data != callback_data]
        for row in (getattr(markup, "inline_keyboard", None) or [])
    ]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


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
    store=None,
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

    if action_id == vpn_report.REPORT_ACTION:
        await _handle_report(callback, node_link, notifier, subscription, book, store, value)
        return

    if action_id == vpn_report.REPLY_ACTION:
        await _handle_owner_reply_button(callback, subscription, book, store, value)
        return

    if action_id == "apk":
        if not _is_private(chat_id):
            await callback.answer("Напишите мне в личку — там и пришлю.", show_alert=True)
            return
        if value == vpn_settings.APK_NO_STORE:
            await callback.answer()
            await callback.message.answer(
                vpn_settings.NO_STORE_TEXT, reply_markup=vpn_settings.no_store_keyboard()
            )
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
        # Справка: пустое значение — вопросы, q<код> — ответ (bot/vpn_faq.py).
        await callback.answer()
        code = vpn_faq.parse_question(value)
        if code is None:
            text = vpn_faq.LIST_TEXT
            keyboard = vpn_faq.list_keyboard(
                home_cb=_home_cb(), report=vpn_report.can_report(subscription)
            )
        else:
            text = vpn_faq.answer_text(code)
            keyboard = vpn_faq.answer_keyboard(
                code, config.vpn, can_wizard=_allows(subscription, vpn_protocol.ACTION_ISSUE)
            )
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(
                text, reply_markup=keyboard, disable_web_page_preview=True
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

    if (
        action_id == vpn_protocol.ACTION_REISSUE
        and value
        and value.startswith(f"{_EXPERT_PREFIX}{vpn_settings.KIND_MULTI}")
    ):
        await _expert_reissue_multi(
            callback, node_link, notifier, config, subscription, pending_vpn_secrets, value[2:]
        )
        return

    if (
        action_id == vpn_protocol.ACTION_REISSUE
        and value
        and value.startswith(f"{_EXPERT_PREFIX}{vpn_settings.KIND_AWG}")
    ):
        await _awg_replace(
            callback, node_link, notifier, config, subscription, pending_vpn_secrets, value, node_id
        )
        return

    if action_id == vpn_protocol.ACTION_REISSUE and value and value.startswith(_EXPERT_PREFIX):
        await _expert_reissue(
            callback, node_link, notifier, config, subscription, pending_vpn_secrets, value, node_id
        )
        return

    if (
        action_id == vpn_protocol.ACTION_ISSUE
        and value
        and value.startswith(f"~{vpn_wizard.PREFIX}")
    ):
        await _wizard_create(callback, node_link, config, subscription, value[2:3])
        return

    if action_id == vpn_protocol.ACTION_ISSUE and value and value.startswith(_EXPERT_PREFIX):
        await _expert_issue(
            callback, node_link, notifier, config, subscription, pending_vpn_secrets, value, node_id
        )
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
                sent_id = await notifier.send_photo(
                    chat_id,
                    base64.b64decode(secret.qr_png_b64),
                    filename="vpn-qr.png",
                    caption=_secret_qr_caption(secret.transport, label_escaped, secret.location),
                    message_thread_id=callback.message.message_thread_id,
                )
            else:
                sent = await notifier.send_document(
                    chat_id,
                    secret.config_text.encode("utf-8"),
                    filename=_secret_filename(
                        secret.transport, secret.device_label, secret.location
                    ),
                    caption=_secret_file_caption(secret.transport, label_escaped),
                    message_thread_id=callback.message.message_thread_id,
                )
                sent_id = sent[0] if sent is not None else None
            # Отданный секрет исчезает, как и само сообщение (по TTL).
            _delete_later(notifier, chat_id, sent_id, config.vpn.config_message_ttl_s)
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_reply_markup(
                    reply_markup=_without_button(callback.message, callback.data)
                )
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

    if action_id == vpn_protocol.ACTION_REVOKE and value and value.startswith(_EXPERT_PREFIX):
        await _expert_delete(callback, node_link, subscription, config, value)
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
        if value and value.startswith(vpn_wizard.PREFIX):
            await _wizard_show(
                callback, node_link, notifier, config, subscription, book, value, store
            )
        elif value and value[0] == vpn_settings.SCREEN_REISSUE:
            await _show_reissue_select(
                callback, node_link, subscription, config, value[1:], answered=False
            )
        elif value:
            await _show_screen(
                callback, node_link, subscription, config, value[0], value[1:], answered=False
            )
        else:
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
                _check_status_text(result.get("states") or [], rollup=result.get("rollup") or []),
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
        # Экран (bot/vpn_proxy_screen.py): кнопки-ссылки по странам из ответов
        # всех живых нод; «qr» — QR одной страны; «d» — секрет и порт, только
        # тем, у кого есть proxy_rotate_secret@vpn (смена секрета рвёт ссылку
        # у всех — подробности гостю не нужны).
        can_rotate = _allows(subscription, vpn_protocol.ACTION_PROXY_ROTATE_SECRET)
        if value == vpn_proxy_screen.VALUE_QR:
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
            qr_b64 = result.get("qr_png_b64")
            if not qr_b64:
                await callback.answer("⚠️ QR сейчас недоступен.", show_alert=True)
                return
            await callback.answer()
            country = vpn_proxy_screen.country_of(result)
            await notifier.send_photo(
                chat_id,
                base64.b64decode(qr_b64),
                filename="proxy-qr.png",
                caption=f"✈️ QR прокси Telegram {country.short}\nОтсканируйте в Telegram.",
                message_thread_id=callback.message.message_thread_id,
            )
            return
        results = await vpn_nodes.fanout(node_link, vpn_protocol.ACTION_PROXY_LINK, {})
        if not results:
            await callback.answer(
                "⚠️ Прокси Telegram сейчас не настроен ни на одном сервере.",
                show_alert=True,
            )
            return
        if value == vpn_proxy_screen.VALUE_SECRET:
            if not can_rotate:
                await callback.answer("⛔️ Недоступно.", show_alert=True)
                return
            await callback.answer()
            for result in results:
                await callback.message.answer(
                    _proxy_text(result, admin=True),
                    reply_markup=_proxy_keyboard(result.get("node"), can_rotate=True),
                )
            return
        await callback.answer()
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(
                vpn_proxy_screen.TEXT,
                reply_markup=vpn_proxy_screen.keyboard(
                    results, can_secret=can_rotate, home_cb=_home_cb()
                ),
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
