from __future__ import annotations

import html

from sa_home_bot.bot import vpn_callbacks, vpn_devices, vpn_keyboards
from sa_home_bot.bot.notifier import MAX_LEN
from sa_home_bot.bot.ui.keyboards import back_row, button, markup
from sa_home_bot.domain import vpn_check
from sa_home_bot.vpn import protocol as vpn_protocol

STATUS_ICON = {
    vpn_check.OK: "🟢",
    vpn_check.PARTIAL: "🟠",
    vpn_check.ALERTING: "🔴",
}
SERIES_VERDICT_TEXT = {
    "tcp_blocked": "TCP до сервера не проходит",
    "proto_blocked": "рукопожатие VPN режется",
    "freeze": "заморозка",
    "cut": "обрыв",
    "error": "ошибка замера",
}
LEGEND = "🟠 — доступен не отовсюду (где-то уже блокируют), 🔴 — не отвечает ни одному наблюдателю."
NOTHING_OPENED = "не открылось ничего"
TRUNCATED_NOTE = "… не влезло в сообщение"


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


def series_problem(verdict: str, at_bytes: object = None) -> str:
    text = SERIES_VERDICT_TEXT.get(verdict, verdict)
    if verdict in ("freeze", "cut") and isinstance(at_bytes, int | float):
        text += f" после {max(1, round(at_bytes / 1024))} КБ"
    return text


def _series_lines(series: list[dict]) -> list[str]:
    lines = ["", "🇷🇺 <b>Из РФ</b> (серийная проверка)"]
    for row in series:
        label = vpn_devices.TECHNICAL_TRANSPORT_NAME.get(row["transport"], row["transport"])
        head = (
            f"<code>{html.escape(row['node'])}</code> → <b>{html.escape(row['server'])}</b>"
            f" · {html.escape(label)}"
        )
        if row["status"] == vpn_check.ALERTING:
            problem = html.escape(series_problem(row.get("last_verdict") or ""))
            lines.append(f"  🔴 {head} — не работает: {problem}")
        elif row.get("last_verdict") not in (None, "ok"):
            problem = html.escape(series_problem(row["last_verdict"]))
            lines.append(f"  🟠 {head} — последний замер: {problem}")
        else:
            lines.append(f"  🟢 {head}")
    return lines


def _latency_span(rows: list[dict]) -> str:
    latencies = [row["last_latency_ms"] for row in rows if row.get("last_latency_ms") is not None]
    if not latencies:
        return ""
    low, high = min(latencies), max(latencies)
    return f"{low} мс" if low == high else f"{low}–{high} мс"


def _observer_line(node: str, rows: list[dict]) -> str:
    rows = sorted(rows, key=lambda r: r["target"])
    failed = [row for row in rows if row["status"] == vpn_check.ALERTING]
    head = f"<code>{html.escape(node)}</code> — "
    if not failed:
        span = _latency_span(rows)
        return f"  🟢 {head}{len(rows)}/{len(rows)}{f', {span}' if span else ''}"
    if len(failed) == len(rows):
        errors = sorted({short_error(row.get("last_error")) for row in failed})
        return f"  🔴 {head}{NOTHING_OPENED} ({html.escape(', '.join(errors))})"
    parts = []
    for row in rows:
        label = html.escape(target_label(row["target"]))
        if row["status"] == vpn_check.ALERTING:
            parts.append(f"{label} — {html.escape(short_error(row.get('last_error')))}")
        else:
            latency = row.get("last_latency_ms")
            parts.append(f"{label}{f' {latency} мс' if latency is not None else ''}")
    return f"  🔴 {head}" + ", ".join(parts)


def _fit(lines: list[str], tail: list[str]) -> str:
    text = "\n".join([*lines, *tail])
    if len(text) <= MAX_LEN:
        return text
    tail = ["", TRUNCATED_NOTE, *tail]
    budget = MAX_LEN - len("\n".join(tail)) - 1
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join([*kept, *tail])


def status_text(
    states: list[dict],
    *,
    rollup: list[dict] | None = None,
    series: list[dict] | None = None,
    pending: bool = False,
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
    if series:
        lines.extend(_series_lines(series))
    tail: list[str] = []
    if pending:
        tail = ["", "⏳ Проверка запущена — жмите «↻ Обновить», пока не увидите свежий результат."]
    return _fit(lines, tail)


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
