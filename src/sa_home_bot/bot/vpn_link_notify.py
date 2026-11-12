from __future__ import annotations

import html
from collections.abc import Callable

from sa_home_bot.bot import vpn_devices
from sa_home_bot.domain import vpn_link
from sa_home_bot.vpn import protocol as vpn_protocol

SCOPE_ICON = {vpn_link.SCOPE_GLOBAL: "🌍", vpn_link.SCOPE_RUSSIA: "🇷🇺"}
LEVEL_ICON = {vpn_link.LEVEL_OK: "🟢", vpn_link.LEVEL_PARTIAL: "🟡", vpn_link.LEVEL_DOWN: "🔴"}
UNKNOWN_LEVEL_ICON = "⚪"
LINK_VERB = {vpn_link.LEVEL_DOWN: "не работает", vpn_link.LEVEL_OK: "снова работает"}
LINK_HEAD_ICON = {vpn_link.LEVEL_DOWN: "🔴", vpn_link.LEVEL_OK: "🟢"}
MUTED_LEVEL_ICON = "⚪"
LAYER_DOWN_TEXT = {
    vpn_link.LAYER_NET: "сервер не отвечает",
    vpn_link.LAYER_NODE: "нода недоступна",
    vpn_link.LAYER_VPN: "VPN недоступен",
}
LAYER_UP_TEXT = {
    vpn_link.LAYER_NET: "сервер снова отвечает",
    vpn_link.LAYER_NODE: "нода снова на связи",
    vpn_link.LAYER_VPN: "VPN снова доступен",
}
PARTIAL_NOTE = " (частично)"
SERVER_SEPARATOR = "|"


def server_flag(label: str) -> str:
    return label.split(" ", 1)[0] if label else ""


def server_title(server: str, label: str) -> str:
    code = f"<code>{html.escape(server)}</code>"
    flag = server_flag(label)
    return f"{flag} {code}" if flag else code


def server_mark(server: object, label_of: Callable[[str], str]) -> str:
    name = html.escape(str(server))
    flag = server_flag(label_of(str(server)))
    return f"{flag} {name}" if flag else name


def _by_server(table: object) -> dict[str, dict[str, object]]:
    if not isinstance(table, dict):
        return {}
    rows: dict[str, dict[str, object]] = {}
    for scope in vpn_link.SCOPES:
        servers = table.get(scope)
        if not isinstance(servers, dict):
            continue
        for server, value in servers.items():
            rows.setdefault(str(server), {})[scope] = value
    return rows


def _server_rows(
    table: object, label_of: Callable[[str], str], icons_of: Callable[[object], str]
) -> list[str]:
    rows = _by_server(table)
    scopes = [scope for scope in vpn_link.SCOPES if any(scope in row for row in rows.values())]
    return [
        " ".join(
            [
                *(f"{SCOPE_ICON[scope]}{icons_of(rows[server].get(scope))}" for scope in scopes),
                SERVER_SEPARATOR,
                server_mark(server, label_of),
            ]
        )
        for server in rows
    ]


def scopes_line(scopes: object, label_of: Callable[[str], str]) -> str:
    return "\n".join(
        _server_rows(scopes, label_of, lambda level: LEVEL_ICON.get(level, UNKNOWN_LEVEL_ICON))
    )


def link_changed_text(data: dict, label_of: Callable[[str], str]) -> str | None:
    node = data.get("node")
    server = data.get("server")
    transport = data.get("transport")
    to = data.get("to")
    if not node or not server or not transport or to not in LINK_HEAD_ICON:
        return None
    transport_name = html.escape(vpn_devices.TECHNICAL_TRANSPORT_NAME.get(transport, transport))
    scope = SCOPE_ICON.get(data.get("scope"), "")
    head = (
        f"{LINK_HEAD_ICON[to]} {server_title(server, label_of(server))} · {transport_name}"
        f" — {LINK_VERB[to]}{f' {scope}' if scope else ''} (<code>{html.escape(str(node))}</code>)"
    )
    line = scopes_line(data.get("scopes"), label_of)
    return f"{head}\n{line}" if line else head


def layer_icons(by_layer: object) -> str:
    if not isinstance(by_layer, dict):
        return UNKNOWN_LEVEL_ICON * len(vpn_link.LAYERS)
    icons = []
    broken = False
    for layer in vpn_link.LAYERS:
        level = by_layer.get(layer)
        icons.append(MUTED_LEVEL_ICON if broken else LEVEL_ICON.get(level, UNKNOWN_LEVEL_ICON))
        broken = broken or level == vpn_link.LEVEL_DOWN
    return "".join(icons)


def layers_lines(layers: object, label_of: Callable[[str], str]) -> list[str]:
    return _server_rows(layers, label_of, layer_icons)


def outage_text(data: dict, label_of: Callable[[str], str]) -> str | None:
    server = data.get("server")
    scope = data.get("scope")
    layer = data.get("layer") or vpn_link.LAYER_VPN
    if not server or layer not in LAYER_DOWN_TEXT:
        return None
    if scope not in SCOPE_ICON and layer != vpn_link.LAYER_NODE:
        return None
    where = f" {SCOPE_ICON[scope]}" if scope in SCOPE_ICON else ""
    title = server_title(server, label_of(server))
    to = data.get("to")
    if to == vpn_link.LEVEL_DOWN:
        head = f"🚨 {title} — {LAYER_DOWN_TEXT[layer]}{where}"
    elif data.get("from") == vpn_link.LEVEL_DOWN and to == vpn_link.LEVEL_OK:
        head = f"✅ {title} — {LAYER_UP_TEXT[layer]}{where}"
    elif data.get("from") == vpn_link.LEVEL_DOWN and to == vpn_link.LEVEL_PARTIAL:
        head = f"✅ {title} — {LAYER_UP_TEXT[layer]}{where}{PARTIAL_NOTE}"
    else:
        return None
    line = scopes_line(data.get("scopes"), label_of)
    return f"{head}\n{line}" if line else head


def render(name: str, data: dict, label_of: Callable[[str], str]) -> str | None:
    if name == vpn_protocol.EVENT_VPN_LINK_CHANGED:
        return link_changed_text(data, label_of)
    if name == vpn_protocol.EVENT_VPN_OUTAGE:
        return outage_text(data, label_of)
    return None


def dedup_key(name: str, data: dict) -> str:
    if name == vpn_protocol.EVENT_VPN_LINK_CHANGED:
        fields = (data.get("node"), data.get("server"), data.get("transport"), data.get("to"))
    else:
        fields = (
            data.get("layer") or vpn_link.LAYER_VPN,
            data.get("scope") or "",
            data.get("server"),
            data.get("to"),
        )
    return "|".join([name, *(str(field) for field in fields)])
