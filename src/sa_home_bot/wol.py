"""Wake-on-LAN: отправка magic packet + проверка пробуждения через ping."""

from __future__ import annotations

import asyncio
import re
import socket
import time

_MAC_HEX_RE = re.compile(r"^[0-9a-f]{12}$")


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
) -> None:
    """Отправить magic packet по UDP-broadcast.

    UDP не гарантирует доставку, поэтому шлём несколько копий подряд —
    для просыпающейся сетевой карты это безвредно.
    """
    packet = build_magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
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
