from __future__ import annotations

import html

from sa_home_bot.bot import vpn_callbacks, vpn_devices, vpn_keyboards
from sa_home_bot.bot.ui.keyboards import back_row, button, markup
from sa_home_bot.domain import vpn_check
from sa_home_bot.vpn import protocol as vpn_protocol

STATUS_ICON = {
    vpn_check.OK: "🟢",
    vpn_check.PARTIAL: "🟠",
    vpn_check.ALERTING: "🔴",
}
LEGEND = "🟠 — доступен не отовсюду (где-то уже блокируют), 🔴 — не отвечает ни одному наблюдателю."


def transport_icons(server: dict) -> dict[str, str]:
    return {
        row["transport"]: STATUS_ICON[row["status"]]
        for row in (server.get("check") or [])
        if row.get("status") in STATUS_ICON and row.get("transport")
    }


def location_icon(server: dict) -> str:
    statuses = [
        row["status"] for row in (server.get("check") or []) if row.get("status") in STATUS_ICON
    ]
    if not statuses:
        return ""
    return STATUS_ICON[vpn_check.rollup_status(statuses)]


def health_line(server: dict) -> str:
    icons = transport_icons(server)
    if not icons:
        return ""
    transports = vpn_devices.sorted_transports(list(server.get("transports") or icons.keys()))
    parts = [
        f"{vpn_devices.TECHNICAL_TRANSPORT_NAME.get(transport, transport)} {icons[transport]}"
        for transport in transports
        if transport in icons
    ]
    return f"🛰 {' · '.join(parts)}" if parts else ""


def needs_legend(servers: list[dict]) -> bool:
    return any(
        icon != STATUS_ICON[vpn_check.OK] for s in servers for icon in transport_icons(s).values()
    )


def target_label(url: str) -> str:
    host = url.split("://", 1)[-1].split("/", 1)[0]
    host = host.removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 2 and not all(p.isdigit() for p in parts):
        return parts[-2]
    return host


def short_error(error: object) -> str:
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


def _observer_line(node: str, rows: list[dict]) -> str:
    rows = sorted(rows, key=lambda r: r["target"])
    icon = "🔴" if any(r["status"] == vpn_check.ALERTING for r in rows) else "🟢"
    parts = []
    for row in rows:
        label = html.escape(target_label(row["target"]))
        if row["status"] == vpn_check.ALERTING:
            parts.append(f"{label} — {html.escape(short_error(row.get('last_error')))}")
        else:
            latency = row.get("last_latency_ms")
            parts.append(f"{label}{f' {latency} мс' if latency is not None else ''}")
    return f"  {icon} <code>{html.escape(node)}</code> — " + ", ".join(parts)


def status_text(
    states: list[dict], *, rollup: list[dict] | None = None, pending: bool = False
) -> str:
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
            key = (row.get("server") or "?", row.get("transport") or "?")
            grouped.setdefault(key, []).append(row)
        for (server, transport), rows in sorted(grouped.items()):
            lines.append("")
            head_icon = STATUS_ICON.get(pair_status.get((server, transport), ""), "")
            label = vpn_devices.TECHNICAL_TRANSPORT_NAME.get(transport, transport)
            lines.append(
                f"<b>{html.escape(server)}</b> · {html.escape(label)}"
                f"{vpn_keyboards.suffix_after_space(head_icon)}"
            )
            by_node: dict[str, list[dict]] = {}
            for row in rows:
                by_node.setdefault(row["node"], []).append(row)
            lines.extend(_observer_line(node, by_node[node]) for node in sorted(by_node))
    if pending:
        lines.append("")
        lines.append("⏳ Проверка запущена — жмите «↻ Обновить», пока не увидите свежий результат.")
    return "\n".join(lines)


def status_keyboard():
    return markup(
        [
            button(
                "🔄 Запустить проверку", vpn_callbacks.vpn_callback(vpn_protocol.ACTION_CHECK_NOW)
            ),
            button("↻ Обновить", vpn_callbacks.vpn_callback(vpn_protocol.ACTION_CHECK_STATUS)),
        ],
        back_row(vpn_callbacks.home_cb()),
    )
