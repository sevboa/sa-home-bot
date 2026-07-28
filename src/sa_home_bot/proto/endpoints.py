"""Endpoint'ы транспорта протокола v0: unix-сокет или TCP.

В конфиге и CLI endpoint задаётся строкой:

- ``./data/node.sock`` или ``unix:./data/node.sock`` — unix-сокет (Linux);
- ``tcp://127.0.0.1:8710`` — TCP (Windows и межнодовый канал), сервер на TCP
  требует токен (см. PROTOCOL.md, «Транспорт»).

Нода живёт не на одном адресе, а на нескольких (этап 24): unix-сокет для
локальных фронтендов плюс один или несколько TCP — LAN и tailscale. Отсюда
здесь же классификация адресов: по ней сосед решает, каким путём ходить
первым, а нода — какие из своих адресов вообще объявлять рою.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TCP_PREFIX = "tcp://"
UNIX_PREFIX = "unix:"

# Адреса, на которые нельзя стучаться к СОСЕДУ: у него это он сам, у нас по
# тому же адресу окажемся мы (и рискуем принять себя за него).
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})

# Tailscale раздаёт адреса из CGNAT-диапазона (RFC 6598). Формально Python
# считает его приватным, но по смыслу это оверлей поверх интернета: рабочий,
# зато и медленнее LAN, и мёртвый без сети. Отсюда отдельный ранг.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# Ранги для порядка проб (меньше — пробуем раньше).
RANK_LOCAL = 0  # unix-сокет: тот же хост, дешевле некуда
RANK_LAN = 1  # приватная сеть 192.168/10./172.16 — прямой путь без оверлея
RANK_OVERLAY = 2  # tailscale (CGNAT) — работает почти всегда, но не мгновенно
RANK_OTHER = 3  # публичный адрес или имя, которое ещё надо резолвить


def _unix_endpoint(path: Path) -> UnixEndpoint:
    if sys.platform == "win32":
        raise ValueError(
            f"unix-сокет {str(path)!r} недоступен на Windows — "
            "укажите в конфиге tcp://127.0.0.1:<порт> (см. PROTOCOL.md, «Транспорт»)"
        )
    return UnixEndpoint(path)


@dataclass(frozen=True)
class UnixEndpoint:
    path: Path

    def __str__(self) -> str:
        return str(self.path)


@dataclass(frozen=True)
class TcpEndpoint:
    host: str
    port: int

    def __str__(self) -> str:
        return f"tcp://{self.host}:{self.port}"


Endpoint = UnixEndpoint | TcpEndpoint


def parse_endpoint(value: str | Path | Endpoint) -> Endpoint:
    """Строка/путь → endpoint. ValueError на непонятный формат."""
    if isinstance(value, (UnixEndpoint, TcpEndpoint)):
        return value
    if isinstance(value, Path):
        return _unix_endpoint(value)

    raw = value.strip()
    if not raw:
        raise ValueError("пустой endpoint")

    if raw.startswith(TCP_PREFIX):
        rest = raw[len(TCP_PREFIX) :]
        host, sep, port_str = rest.rpartition(":")
        if not sep or not host or not port_str.isdigit():
            raise ValueError(f"невалидный tcp-endpoint: {raw!r} (ожидается tcp://host:port)")
        port = int(port_str)
        if not 1 <= port <= 65535:
            raise ValueError(f"невалидный порт в endpoint: {raw!r}")
        return TcpEndpoint(host=host.strip("[]"), port=port)

    if raw.startswith(UNIX_PREFIX):
        rest = raw[len(UNIX_PREFIX) :]
        if rest.startswith("//"):  # url-форма unix:///abs/path
            rest = rest[2:]
        if not rest:
            raise ValueError(f"невалидный unix-endpoint: {raw!r}")
        return _unix_endpoint(Path(rest))

    return _unix_endpoint(Path(raw))


def _host_ip(ep: Endpoint) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """IP из endpoint'а; None — unix-сокет или имя хоста (резолвить не пытаемся:
    классификация обязана быть мгновенной и без сети)."""
    if isinstance(ep, UnixEndpoint):
        return None
    try:
        return ipaddress.ip_address(ep.host)
    except ValueError:
        return None


def is_loopback(ep: Endpoint) -> bool:
    """Адрес указывает на «эту же машину»."""
    if isinstance(ep, UnixEndpoint):
        return True
    if ep.host.lower() in _LOOPBACK_HOSTNAMES:
        return True
    ip = _host_ip(ep)
    return ip is not None and ip.is_loopback


def is_unspecified(ep: Endpoint) -> bool:
    """``0.0.0.0``/``::`` — «слушать на всех интерфейсах». Слушать так можно,
    объявить рою — нет: это не адрес, по которому к нам придут."""
    ip = _host_ip(ep)
    return ip is not None and ip.is_unspecified


def endpoint_rank(ep: Endpoint) -> int:
    """Насколько путь предпочтителен (меньше — пробовать раньше).

    Домашние ноды и так ходят друг к другу по LAN напрямую (tailscale
    поднимает прямой туннель, `tailscale status` показывает
    ``direct 192.168.0.105:41641``) — но только когда tailscale поднялся.
    Собственный LAN-адрес доступен сразу после загрузки и не зависит от
    интернета: две машины в двух метрах друг от друга обязаны видеться,
    даже когда провайдер лежит (этап 24).
    """
    if isinstance(ep, UnixEndpoint):
        return RANK_LOCAL
    ip = _host_ip(ep)
    if ip is None:
        return RANK_OTHER
    if ip.version == 4 and ip in _CGNAT:
        return RANK_OVERLAY
    if ip.is_private and not ip.is_link_local:
        return RANK_LAN
    return RANK_OTHER


def local_ipv4_addresses() -> list[str]:
    """Свои IPv4, кроме loopback — чем раскрывается ``0.0.0.0`` при объявлении
    адресов рою (см. advertisable)."""
    # psutil уже в зависимостях (sensors, wol) — отдельной возиться не с чем.
    import psutil

    found: list[str] = []
    for addrs in psutil.net_if_addrs().values():
        for addr in addrs:
            if addr.family != socket.AF_INET or not addr.address:
                continue
            try:
                ip = ipaddress.ip_address(addr.address)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local:
                continue
            if addr.address not in found:
                found.append(addr.address)
    return found


def advertisable(endpoints: list[Endpoint] | tuple[Endpoint, ...]) -> list[str]:
    """Какие из своих адресов имеет смысл объявить соседям, в порядке проб.

    Отсеиваются unix-сокеты и loopback (сосед по ним попадёт не к нам, а к
    себе — и, что хуже, может принять себя за нас), ``0.0.0.0`` раскрывается
    в конкретные адреса интерфейсов.
    """
    out: list[str] = []
    for ep in endpoints:
        if isinstance(ep, UnixEndpoint) or is_loopback(ep):
            continue
        hosts = local_ipv4_addresses() if is_unspecified(ep) else [ep.host]
        for host in hosts:
            value = str(TcpEndpoint(host, ep.port))
            if value not in out:
                out.append(value)
    return sorted(out, key=lambda v: endpoint_rank(parse_endpoint(v)))


def resolve_endpoint(value: str | Path | Endpoint, base_dir: Path | None = None) -> Endpoint:
    """parse_endpoint + относительный unix-путь резолвится от ``base_dir``.

    Нужен фронтендам, читающим endpoint из чужого конфига (nodectl -c …):
    относительный путь в конфиге — относительно каталога конфига, а не CWD.
    """
    endpoint = parse_endpoint(value)
    if (
        isinstance(endpoint, UnixEndpoint)
        and base_dir is not None
        and not endpoint.path.is_absolute()
    ):
        return UnixEndpoint(base_dir / endpoint.path)
    return endpoint
