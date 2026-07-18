"""Wake-on-LAN: отправка magic packet + проверка пробуждения через ping."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

_MAC_HEX_RE = re.compile(r"^[0-9a-f]{12}$")

# Виртуальные/туннельные интерфейсы — даже с IPv4 (docker0, tailscale0) это
# не физический Ethernet-сегмент, ни целью, ни отправителем WoL не годятся.
_VIRTUAL_IFACE_RE = re.compile(
    r"(?i)^(lo|docker|veth|br-|virbr|tailscale|wg|tun|tap|vmware|virtualbox|vbox|hyper-?v)"
)
_WIRELESS_NAME_RE = re.compile(r"(?i)wi-?fi|wireless|wlan")


def normalize_mac(mac: str) -> str:
    """Привести MAC к канону ``aa:bb:cc:dd:ee:ff``.

    Принимает разделители ``:``, ``-``, ``.`` или их отсутствие.
    ValueError — если после очистки не осталось ровно 12 hex-символов.
    """
    cleaned = re.sub(r"[:\-.\s]", "", mac.strip().lower())
    if not _MAC_HEX_RE.match(cleaned):
        raise ValueError(f"Некорректный MAC-адрес: {mac!r}")
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


def build_magic_packet(mac: str) -> bytes:
    """Собрать magic packet: 6 байт 0xFF + MAC, повторённый 16 раз (102 байта)."""
    raw = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    return b"\xff" * 6 + raw * 16


def send_magic_packet(
    mac: str,
    broadcast: str = "255.255.255.255",
    port: int = 9,
    repeats: int = 3,
    bind_ip: str = "",
) -> None:
    """Отправить magic packet по UDP-broadcast.

    UDP не гарантирует доставку, поэтому шлём несколько копий подряд —
    для просыпающейся сетевой карты это безвредно. ``bind_ip`` привязывает
    сокет к конкретному локальному интерфейсу — нода роя может быть
    многодомной (LAN + tailscale), без привязки ОС не гарантирует, что
    broadcast уйдёт именно в LAN-сегмент цели (см. detect_local_wake_info).
    """
    packet = build_magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if bind_ip:
            sock.bind((bind_ip, 0))
        for _ in range(repeats):
            sock.sendto(packet, (broadcast, port))


async def ping_host(ip: str) -> bool:
    """Один ICMP-пинг с таймаутом 1 с (без sudo — iputils разрешает всем)."""
    proc = await asyncio.create_subprocess_exec(
        "ping",
        "-c",
        "1",
        "-W",
        "1",
        ip,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait() == 0


async def wait_host_up(ip: str, timeout_s: float, interval_s: float = 3.0) -> float | None:
    """Ждать, пока хост ответит на ping.

    Возвращает секунды до первого ответа либо None, если таймаут истёк.
    """
    start = time.monotonic()
    while (elapsed := time.monotonic() - start) < timeout_s:
        if await ping_host(ip):
            return elapsed
        await asyncio.sleep(interval_s)
    return None


@dataclass(frozen=True)
class LocalWakeInfo:
    """Реквизиты проводного сегмента этой машины — то, чем её саму можно
    будет разбудить по WoL, когда она уснёт (см. node/service.py:get_state)."""

    mac: str
    ip: str
    broadcast: str


def _is_wireless(name: str) -> bool:
    """Проводной ли интерфейс. Linux — точный признак (sysfs), Windows —
    эвристика по имени (без WMI/доп. зависимостей — нода уже опознаёт себя
    по default-route интерфейсу, ошибиться можно только на многодомном
    Wi-Fi-ноутбуке, WoL там всё равно ненадёжен)."""
    wireless_marker = Path(f"/sys/class/net/{name}/wireless")
    if wireless_marker.parent.exists():  # /sys/class/net есть только на Linux
        return wireless_marker.exists()
    return bool(_WIRELESS_NAME_RE.search(name))


def _default_route_ip() -> str | None:
    """IP интерфейса, которым машина по умолчанию уходит в сеть.

    Классический трюк: UDP connect() не отправляет ни одного пакета — просто
    просит ядро выбрать исходящий интерфейс для маршрута к адресу,
    getsockname() отдаёт его IP. Работает и без доступа в интернет (чистая
    локальная маршрутизация, не сетевой запрос).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return None


def detect_local_wake_info() -> LocalWakeInfo | None:
    """MAC/IP/broadcast интерфейса, которым эта нода смотрит в свой LAN.

    None — нет проводного интерфейса по умолчанию (Wi-Fi-ноутбук, dev-песочница
    без сети и т.п.): WoL на таких ненадёжен, умение сознательно не
    объявляется (IMPLEMENTATION_PLAN.md, этап 19 п.6 — только Ethernet).
    """
    ip = _default_route_ip()
    if ip is None:
        return None
    addrs = psutil.net_if_addrs()
    iface = next(
        (name for name, snics in addrs.items() if any(s.address == ip for s in snics)), None
    )
    if iface is None or _VIRTUAL_IFACE_RE.match(iface) or _is_wireless(iface):
        return None
    mac_raw = next(
        (s.address for s in addrs[iface] if s.family == psutil.AF_LINK and s.address), None
    )
    netmask = next(
        (s.netmask for s in addrs[iface] if s.family == socket.AF_INET and s.address == ip),
        None,
    )
    if not mac_raw or not netmask:
        return None
    try:
        mac = normalize_mac(mac_raw)
    except ValueError:
        return None
    broadcast = str(ipaddress.ip_network(f"{ip}/{netmask}", strict=False).broadcast_address)
    return LocalWakeInfo(mac=mac, ip=ip, broadcast=broadcast)
