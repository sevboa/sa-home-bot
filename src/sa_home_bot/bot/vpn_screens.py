from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import (
    vpn_admin_view,
    vpn_callbacks,
    vpn_devices,
    vpn_keyboards,
    vpn_proxy_screen,
    vpn_report,
    vpn_rights,
    vpn_settings,
    vpn_wizard,
)
from sa_home_bot.bot import vpn_check_view as check_view
from sa_home_bot.bot.ui.keyboards import button, markup
from sa_home_bot.bot.vpn_nodes import server_label
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

_cb = vpn_callbacks

SERVER_PICKER_TEXT = "Где завести новое устройство?"
TRANSPORT_PICKER_TEXT = (
    "Какой технологией выдать новое устройство?\n\n"
    "• <b>VLESS · Hiddify</b> — для DPI неотличимо от обычного HTTPS.\n"
    "• <b>AmneziaVPN</b> — быстрее, но в части стран может не работать."
)
TRANSPORT_REALITY_HINT = " — для РФ"


def admin_row(
    servers: list[dict], subscription: Subscription | None
) -> list[InlineKeyboardButton] | None:
    if not vpn_rights.is_admin(subscription):
        return None
    row = [button("👥 Все гости", vpn_admin_view.guests_cb(0))]
    check_node = next((server.get("node") for server in servers if server.get("node")), None)
    if check_node is not None:
        row.append(
            button(
                "🛰 Проверка сети",
                _cb.vpn_callback(vpn_protocol.ACTION_CHECK_STATUS, node=check_node),
            )
        )
    return row


def proxy_row(
    servers: list[dict], subscription: Subscription | None
) -> list[InlineKeyboardButton] | None:
    if vpn_rights.allows(subscription, vpn_protocol.ACTION_PROXY_LINK) and any(
        server.get("proxy_available") for server in servers
    ):
        return [open_proxy_button()]
    return None


def open_proxy_button() -> InlineKeyboardButton:
    return button(
        vpn_proxy_screen.OPEN_BUTTON_TEXT, _cb.vpn_callback(vpn_protocol.ACTION_PROXY_LINK)
    )


def _append(rows: list, row: list[InlineKeyboardButton] | None) -> None:
    if row is not None:
        rows.append(row)


def home_keyboard(
    servers: list[dict],
    *,
    subscription: Subscription | None,
    self_serve_nodes: list[str],
    has_devices: bool = True,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if not has_devices and vpn_rights.allows(subscription, vpn_protocol.ACTION_ISSUE):
        rows.append([vpn_wizard.wizard_button()])
    rows.append([button("⚙️ Управление устройствами", _cb.card_cb(_cb.SCREEN_LIST))])
    if vpn_rights.allows(subscription, vpn_protocol.ACTION_VPN_APK):
        rows.append([vpn_keyboards.help_button()])
    _append(rows, proxy_row(servers, subscription))
    if vpn_report.can_report(subscription):
        rows.append([vpn_report.report_button()])
    _append(rows, admin_row(servers, subscription))
    if vpn_rights.allows(subscription, vpn_protocol.ACTION_GRANT_EXTRA):
        multi = len(servers) > 1
        grants = [
            vpn_keyboards.grant_extra_button(
                server["node"],
                vpn_keyboards.suffix_after_space(
                    vpn_devices.Country(server["node"], server_label(server)).short if multi else ""
                ),
            )
            for server in servers
            if server.get("node") in self_serve_nodes
        ]
        if grants:
            rows.append(grants)
    return markup(*rows)


def list_keyboard(
    devices: list[vpn_devices.Device], *, subscription: Subscription | None
) -> InlineKeyboardMarkup:
    buttons = [button(dev.label[:28], _cb.card_cb(_cb.SCREEN_DEVICE, dev.key)) for dev in devices]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if vpn_rights.allows(subscription, vpn_protocol.ACTION_ISSUE):
        rows.append(
            [
                vpn_wizard.wizard_button(),
                button(vpn_keyboards.NEW_DEVICE_TEXT, _cb.card_cb(_cb.SCREEN_NEW)),
            ]
        )
    rows.append(vpn_keyboards.back_row(_cb.home_cb()))
    return markup(*rows)


def device_card_keyboard(
    device: vpn_devices.Device,
    servers: list[dict],
    *,
    subscription: Subscription | None,
    page_url: str | None = None,
) -> InlineKeyboardMarkup:
    countries = vpn_devices.countries_of(servers)
    rows: list[list[InlineKeyboardButton]] = []
    if page_url:
        rows.append([InlineKeyboardButton(text=vpn_settings.PAGE_CARD_BUTTON, url=page_url)])
    if vpn_rights.allows(subscription, vpn_protocol.ACTION_ISSUE):
        rows.append(
            [
                button(
                    "📥 Получить настройки",
                    _cb.card_cb(_cb.SCREEN_LINK if page_url else _cb.SCREEN_PICK, device.key),
                )
            ]
        )
        if page_url:
            rows.append(
                [button(vpn_settings.OTHER_WAYS_BUTTON, _cb.card_cb(_cb.SCREEN_PICK, device.key))]
            )
    if vpn_rights.allows(subscription, vpn_protocol.ACTION_REISSUE):
        rows.append([button("🔄 Перевыпустить ключи", _cb.card_cb(_cb.SCREEN_REISSUE, device.key))])
        for node in device.broken_nodes():
            country = countries.get(node)
            rows.append(
                [
                    button(
                        f"🔧 Починить {country.short if country else node}",
                        _cb.expert_cb(vpn_protocol.ACTION_REISSUE, _cb.KIND_FIX, device.key, node),
                    )
                ]
            )
    if vpn_rights.allows(subscription, vpn_protocol.ACTION_REVOKE):
        rows.append([button("🗑 Удалить устройство", _cb.card_cb(_cb.SCREEN_DELETE, device.key))])
    if vpn_report.can_report(subscription):
        rows.append([vpn_report.report_button(device.key)])
    rows.append(vpn_keyboards.back_row(_cb.card_cb(_cb.SCREEN_LIST), "⬅️ К устройствам"))
    return markup(*rows)


def delete_keyboard(device: vpn_devices.Device) -> InlineKeyboardMarkup:
    return markup(
        [
            button(
                "Да, удалить",
                _cb.expert_cb(vpn_protocol.ACTION_REVOKE, _cb.KIND_DELETE, device.key),
            ),
            button(vpn_settings.CANCEL_TEXT, _cb.card_cb(_cb.SCREEN_DEVICE, device.key)),
        ]
    )


def server_picker_keyboard(servers: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            button(
                f"{server_label(server)}"
                f"{vpn_keyboards.suffix_after_space(check_view.location_icon(server))}",
                _cb.vpn_callback(vpn_protocol.ACTION_ISSUE, node=server["node"]),
            )
        ]
        for server in servers
    ]
    rows.append(vpn_keyboards.back_row(_cb.home_cb()))
    return markup(*rows)


def transport_picker_keyboard(
    transports: list[str], node_id: str | None = None, icons: dict[str, str] | None = None
) -> InlineKeyboardMarkup:
    icons = icons or {}
    rows: list[list[InlineKeyboardButton]] = []
    for transport in vpn_devices.TRANSPORT_ORDER:
        if transport not in transports:
            continue
        hint = TRANSPORT_REALITY_HINT if transport == vpn_protocol.TRANSPORT_REALITY else ""
        name = vpn_devices.TECHNICAL_TRANSPORT_NAME[transport]
        rows.append(
            [
                button(
                    f"{name}{hint}{vpn_keyboards.suffix_after_space(icons.get(transport, ''))}",
                    _cb.vpn_callback(
                        vpn_protocol.ACTION_ISSUE,
                        f"{_cb.TRANSPORT_PICK_PREFIX}{transport}",
                        node_id,
                    ),
                )
            ]
        )
    rows.append(vpn_keyboards.back_row(_cb.home_cb()))
    return markup(*rows)
