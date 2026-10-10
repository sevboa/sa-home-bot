"""Слой устройств /vpn (этап 57.2): ответы ``usage`` всех нод → устройства.

Устройство — это ``device_label``, общий для всех нод и транспортов; под ним до
четырёх подключений {нода × транспорт}. Отдельной таблицы нет: каждая нода
отдаёт в ``usage`` с ``chat_id`` свою часть (``device_usage``), здесь они
склеиваются по имени. Модуль чистый — без aiogram и сети, поэтому годится и
боту, и ``tool_vpn``; тексты экранов эксперт-режима тоже здесь (клавиатуры —
в bot/vpn_expert_view.py).

Пробник ``chat_id=0`` сюда не попадает: ответы запрашиваются по chat_id
человека, а на всякий случай ``build_devices`` его всё равно не знает.
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo

from sa_home_bot.domain import vpn_check
from sa_home_bot.vpn import protocol as vpn_protocol

REALITY = vpn_protocol.TRANSPORT_REALITY
AWG = vpn_protocol.TRANSPORT_AWG
# VLESS первым — с него начинают; незнакомые транспорты (нода новее бота) — в хвост.
TRANSPORT_ORDER = (REALITY, AWG)
# Имена транспортов в текстах: «VLESS · Hiddify» всегда вместе, AmneziaWG — без
# оговорок про страны (решение владельца 2026-10-10).
TRANSPORT_NAME = {REALITY: "VLESS · Hiddify", AWG: "AmneziaWG"}

_MONTHS_NOM = (
    "январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь"
).split()
_MONTHS_GEN = (
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря"
).split()


@dataclass(frozen=True)
class Country:
    """Страна = сервер (нода). ``label`` — «🇳🇱 Нидерланды» из [vpn].location."""

    node: str
    label: str

    @property
    def flag(self) -> str:
        return vpn_protocol.country_flag(self.label)

    @property
    def name(self) -> str:
        """«🇳🇱 Нидерланды» → «Нидерланды»; без флага — как есть (или id ноды)."""
        flag = self.flag
        text = self.label.replace(flag, "", 1).strip() if flag else self.label.strip()
        return text or self.node

    @property
    def short(self) -> str:
        """Флаг, а без флага — название: для узких кнопок."""
        return self.flag or self.name


@dataclass(frozen=True)
class Connection:
    node: str
    transport: str
    status: str  # active | …; не выданное подключение — запись с ``issued=False``
    last_handshake_at: str | None
    created_at: str | None
    broken: bool
    used_bytes: int
    issued: bool = True

    @property
    def never_connected(self) -> bool:
        return self.issued and not self.last_handshake_at


@dataclass
class Device:
    label: str
    connections: list[Connection] = field(default_factory=list)
    # node → трафик устройства на этой ноде за месяц (включая перевыпущенные ключи)
    traffic_by_node: dict[str, int] = field(default_factory=dict)
    first_created_at: str = ""

    @property
    def key(self) -> str:
        return device_key(self.label)

    @property
    def used_bytes(self) -> int:
        return sum(self.traffic_by_node.values())

    @property
    def issued(self) -> list[Connection]:
        return [c for c in self.connections if c.issued]

    @property
    def last_handshake_at(self) -> str | None:
        stamps = [c.last_handshake_at for c in self.issued if c.last_handshake_at]
        return max(stamps) if stamps else None

    @property
    def never_connected(self) -> bool:
        """Ни одно подключение ещё ни разу не выходило на связь."""
        return not self.last_handshake_at

    def broken_nodes(self) -> list[str]:
        """Ноды (в порядке появления), где у устройства есть «сломанные» подключения."""
        seen: list[str] = []
        for conn in self.issued:
            if conn.broken and conn.node not in seen:
                seen.append(conn.node)
        return seen

    def broken_in(self, node: str) -> list[Connection]:
        return [c for c in self.issued if c.broken and c.node == node]

    def connection(self, node: str, transport: str) -> Connection | None:
        for conn in self.issued:
            if conn.node == node and conn.transport == transport:
                return conn
        return None


def device_key(label: str) -> str:
    """Короткий стабильный ключ устройства для callback_data (лимит 64 байта,
    имя может быть длинным/кириллицей). Обратно разрешается по свежему usage."""
    return hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]


def find_device(devices: list[Device], key: str) -> Device | None:
    return next((d for d in devices if d.key == key), None)


def countries_of(servers: list[dict]) -> dict[str, Country]:
    return {
        s["node"]: Country(node=s["node"], label=s.get("label") or s["node"])
        for s in servers
        if s.get("node")
    }


def _sorted_transports(transports: list[str]) -> list[str]:
    known = [t for t in TRANSPORT_ORDER if t in transports]
    return known + [t for t in transports if t not in TRANSPORT_ORDER]


def _entries(server: dict) -> list[dict]:
    """``device_usage`` ноды; у ноды старее 57.1 его нет — собираем из ``devices``
    (трафик тогда неизвестен и считается нулём)."""
    usage = server.get("device_usage")
    if usage is not None:
        return usage
    by_label: dict[str, dict] = {}
    for dev in server.get("devices") or []:
        entry = by_label.setdefault(
            dev["device_label"], {"device_label": dev["device_label"], "connections": []}
        )
        entry["connections"].append(
            {
                "transport": dev.get("transport") or AWG,
                "status": dev.get("status") or "active",
                "last_handshake_at": dev.get("last_handshake_at"),
                "created_at": dev.get("created_at"),
                "broken": bool(dev.get("broken")),
                "used_bytes": 0,
            }
        )
    return list(by_label.values())


def build_devices(servers: list[dict]) -> list[Device]:
    """Склеить ответы ``usage`` нод (по одному на ноду, уже отфильтрованные по
    допуску) в устройства. Порядок — по самому раннему подключению."""
    by_label: dict[str, Device] = {}
    for server in servers:
        node = server.get("node")
        if not node:
            continue
        for entry in _entries(server):
            label = entry["device_label"]
            dev = by_label.setdefault(label, Device(label=label))
            dev.traffic_by_node[node] = int(entry.get("used_bytes") or 0)
            for conn in entry.get("connections") or []:
                created = conn.get("created_at")
                dev.connections.append(
                    Connection(
                        node=node,
                        transport=conn.get("transport") or AWG,
                        status=conn.get("status") or "active",
                        last_handshake_at=conn.get("last_handshake_at"),
                        created_at=created,
                        broken=bool(conn.get("broken")),
                        used_bytes=int(conn.get("used_bytes") or 0),
                    )
                )
                if created and (not dev.first_created_at or created < dev.first_created_at):
                    dev.first_created_at = created
    order = {s["node"]: i for i, s in enumerate(servers) if s.get("node")}
    for dev in by_label.values():
        dev.connections.sort(
            key=lambda c: (
                order.get(c.node, 99),
                _sorted_transports([AWG, REALITY, c.transport]).index(c.transport),
            )
        )
    return sorted(by_label.values(), key=lambda d: (d.first_created_at or "~", d.label))


def card_rows(device: Device, servers: list[dict]) -> list[Connection]:
    """Строки раздела «Подключения»: выданные + «не выдано» по транспортам,
    которые нода умеет. Порядок: страны как в ``servers``, VLESS перед AmneziaWG."""
    rows: list[Connection] = []
    for server in servers:
        node = server.get("node")
        if not node:
            continue
        transports = _sorted_transports(list(server.get("transports") or []))
        have = {c.transport for c in device.issued if c.node == node}
        for transport in _sorted_transports(list({*transports, *have})):
            conn = device.connection(node, transport)
            if conn is None:
                conn = Connection(
                    node=node,
                    transport=transport,
                    status="none",
                    last_handshake_at=None,
                    created_at=None,
                    broken=False,
                    used_bytes=0,
                    issued=False,
                )
            rows.append(conn)
    return rows


# --- время и числа ---------------------------------------------------------


def fmt_stamp(stamp: str | None, *, tz: tzinfo | None = None, with_time: bool = True) -> str:
    """ISO-метка → «10.10 14:02» в локальном поясе бота (``tz`` — для тестов)."""
    if not stamp:
        return ""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp[:16].replace("T", " ")
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(tz)
    return when.strftime("%d.%m %H:%M" if with_time else "%d.%m")


def fmt_gb(bytes_: int) -> str:
    gb = bytes_ / 1_000_000_000
    if bytes_ > 0 and gb < 0.05:
        return "<0.1 ГБ"
    return f"{gb:.1f} ГБ"


def _gb_short(bytes_: int) -> str:
    gb = bytes_ / 1_000_000_000
    return f"{gb:.0f}" if gb >= 10 else f"{gb:.1f}".removesuffix(".0")


def next_reset_phrase(now: datetime) -> str:
    """«1 ноября» — когда месячная квота обнулится."""
    month = now.month % 12  # индекс следующего месяца в 0-базе
    return f"1 {_MONTHS_GEN[month]}"


def month_name(now: datetime) -> str:
    return _MONTHS_NOM[now.month - 1]


# --- сводка для главной ----------------------------------------------------


def _server_health(server: dict) -> str | None:
    """Свёрнутый статус проверок страны или None, если проверок нет. Если есть
    проверка VLESS — смотрим её (это основной способ подключения); иначе по
    всем транспортам."""
    rows = [r for r in (server.get("check") or []) if r.get("status")]
    if not rows:
        return None
    reality = [r["status"] for r in rows if r.get("transport") == REALITY]
    statuses = reality or [r["status"] for r in rows]
    return vpn_check.rollup_status(statuses)


_STATUS_OK = "✅ работает"
_STATUS_BAD = "⚠️ может не работать"
_FALLBACK_LINE = "Если одна страна не работает — выберите другую."


def transport_health(server: dict, transport: str) -> str | None:
    """Свёрнутый статус проверок одного транспорта в стране или None, если данных нет."""
    statuses = [
        r["status"]
        for r in (server.get("check") or [])
        if r.get("status") and r.get("transport") == transport
    ]
    return vpn_check.rollup_status(statuses) if statuses else None


def home_summary(servers: list[dict], *, now: datetime | None = None) -> list[str]:
    """Строки сводки главной: по стране — статус проверки (если есть данные) и остаток
    квоты до 1 числа; в конце подсказка выбрать другую страну, если одна не работает,
    а другая работает. Одна страна без данных проверки — одна строка без названия."""
    now = now or datetime.now(UTC)
    reset = next_reset_phrase(now)
    multi = len(servers) > 1
    lines: list[str] = []
    statuses: list[str | None] = []
    for server in servers:
        country = Country(server.get("node") or "?", server.get("label") or "")
        health = _server_health(server)
        statuses.append(health)
        titled = health is not None
        prefix = f"{html.escape(country.label)}: " if multi and country.label and not titled else ""
        if titled:
            mark = _STATUS_OK if health == vpn_check.OK else _STATUS_BAD
            lines.append(f"{html.escape(country.label or country.name)} — {mark}")
        if server.get("blocked"):
            lines.append(f"⛔️ {prefix}лимит исчерпан — связь приостановлена до {reset}.")
            continue
        remaining = int(server.get("remaining_bytes") or 0)
        limit = int(server.get("limit_bytes") or 0)
        verb = "осталось" if prefix or (titled and multi) else "Осталось"
        lines.append(
            f"{prefix}{verb} {_gb_short(remaining)} ГБ из {limit / 1_000_000_000:.0f} до {reset}."
        )
    known = [h for h in statuses if h is not None]
    if any(h != vpn_check.OK for h in known) and any(h == vpn_check.OK for h in known):
        lines += ["", _FALLBACK_LINE]
    return lines


# --- тексты экранов --------------------------------------------------------


def home_text(
    servers: list[dict], *, unavailable: list[dict] | None = None, now: datetime | None = None
) -> str:
    lines = ["📶 <b>VPN</b>", ""]
    lines.extend(home_summary(servers, now=now))
    for server in unavailable or []:
        name = html.escape(server.get("label") or server.get("node") or "?")
        lines.append(f"🔌 {name}: сервер недоступен")
    return "\n".join(lines)


def conn_status(conn: Connection, *, tz: tzinfo | None = None) -> str:
    """Статус подключения словами для карточки."""
    if not conn.issued:
        return "не выдано"
    if conn.broken:
        return "🔧 сервер его не помнит"
    if conn.never_connected:
        return "ещё не подключалось"
    return f"на связи {fmt_stamp(conn.last_handshake_at, tz=tz)}"


def list_text(devices: list[Device], *, tz: tzinfo | None = None) -> str:
    lines = ["⚙️ <b>Мои устройства</b>", ""]
    if not devices:
        lines.append("Устройств пока нет.")
    for dev in devices:
        name = html.escape(dev.label)
        if dev.never_connected:
            tail = "ещё не подключалось"
        else:
            tail = f"{fmt_gb(dev.used_bytes)}, на связи {fmt_stamp(dev.last_handshake_at, tz=tz)}"
        flag = " 🔧" if dev.broken_nodes() else ""
        lines.append(f"{name} — {tail}{flag}")
    return "\n".join(lines)


def card_text(
    device: Device,
    servers: list[dict],
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> str:
    now = now or datetime.now(UTC)
    countries = countries_of(servers)
    lines = [f"<b>{html.escape(device.label)}</b> · {month_name(now)}", "", "Трафик:"]
    for node, country in countries.items():
        if node in device.traffic_by_node or any(c.node == node for c in device.issued):
            lines.append(
                f"{html.escape(country.label)} — {fmt_gb(device.traffic_by_node.get(node, 0))}"
            )
    lines += ["", "Подключения:"]
    by_node = {s.get("node"): s for s in servers}
    for conn in card_rows(device, servers):
        country = countries.get(conn.node)
        where = country.short if country else conn.node
        name = TRANSPORT_NAME.get(conn.transport, conn.transport)
        tail = ""
        server = by_node.get(conn.node)
        if server is not None and transport_health(server, conn.transport) not in (
            None,
            vpn_check.OK,
        ):
            tail = " · ⚠️ сервер сейчас может не работать"
        lines.append(f"{where} {name} — {conn_status(conn, tz=tz)}{tail}")
    return "\n".join(lines)


def reissue_text(device: Device) -> str:
    return (
        f"🔄 <b>{html.escape(device.label)}</b> — какие ключи перевыпустить?\n"
        "Старые перестанут работать сразу, новые настройки придут следом."
    )


def delete_text(device: Device) -> str:
    return (
        f"🗑 Удалить <b>{html.escape(device.label)}</b>?\n"
        "Его ключи во всех странах перестанут работать."
    )
