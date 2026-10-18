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

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})

_CGNAT = ipaddress.ip_network("100.64.0.0/10")

_VIRTUAL_IFACE_MARKERS = ("vethernet", "docker", "virbr", "vmnet", "br-", "veth")

RANK_LOCAL = 0
RANK_LAN = 1
RANK_OVERLAY = 2
RANK_OTHER = 3


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
        if rest.startswith("//"):
            rest = rest[2:]
        if not rest:
            raise ValueError(f"невалидный unix-endpoint: {raw!r}")
        return _unix_endpoint(Path(rest))

    return _unix_endpoint(Path(raw))


def _host_ip(ep: Endpoint) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if isinstance(ep, UnixEndpoint):
        return None
    try:
        return ipaddress.ip_address(ep.host)
    except ValueError:
        return None


def is_loopback(ep: Endpoint) -> bool:
    if isinstance(ep, UnixEndpoint):
        return True
    if ep.host.lower() in _LOOPBACK_HOSTNAMES:
        return True
    ip = _host_ip(ep)
    return ip is not None and ip.is_loopback


def is_unspecified(ep: Endpoint) -> bool:
    ip = _host_ip(ep)
    return ip is not None and ip.is_unspecified


def endpoint_rank(ep: Endpoint) -> int:
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


def is_virtual_iface(name: str) -> bool:
    low = name.lower()
    return any(marker in low for marker in _VIRTUAL_IFACE_MARKERS)


def local_ipv4_addresses() -> list[str]:
    import psutil

    found: list[str] = []
    for iface, addrs in psutil.net_if_addrs().items():
        if is_virtual_iface(iface):
            log.debug("адреса интерфейса %s не объявляем: виртуальная сеть", iface)
            continue
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


def local_lan_ipv4_address() -> str | None:
    for addr in local_ipv4_addresses():
        ip = ipaddress.ip_address(addr)
        if ip.is_private and ip not in _CGNAT:
            return addr
    return None


def local_broadcast_addresses() -> list[str]:
    import psutil

    found: list[str] = []
    for iface, addrs in psutil.net_if_addrs().items():
        if is_virtual_iface(iface):
            continue
        for addr in addrs:
            if addr.family != socket.AF_INET or not addr.broadcast:
                continue
            if addr.broadcast not in found:
                found.append(addr.broadcast)
    return found


def advertisable(endpoints: list[Endpoint] | tuple[Endpoint, ...]) -> list[str]:
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
    endpoint = parse_endpoint(value)
    if (
        isinstance(endpoint, UnixEndpoint)
        and base_dir is not None
        and not endpoint.path.is_absolute()
    ):
        return UnixEndpoint(base_dir / endpoint.path)
    return endpoint


def merge_endpoints(first: str, raw: object) -> list[str]:
    values = [first] if first else []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item and item not in values:
                values.append(item)
    return values
