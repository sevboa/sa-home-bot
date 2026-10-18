"""Разбор endpoint'ов транспорта: unix-путь, unix:-префикс, tcp://host:port."""

import socket
import sys
import types
from pathlib import Path

import pytest

from sa_home_bot.config import NodeConfig
from sa_home_bot.proto import endpoints
from sa_home_bot.proto.endpoints import (
    RANK_LAN,
    RANK_OTHER,
    RANK_OVERLAY,
    TcpEndpoint,
    UnixEndpoint,
    advertisable,
    endpoint_rank,
    is_loopback,
    is_unspecified,
    is_virtual_iface,
    local_ipv4_addresses,
    local_lan_ipv4_address,
    parse_endpoint,
    resolve_endpoint,
)


def test_plain_path_is_unix():
    ep = parse_endpoint("./data/node.sock")
    assert ep == UnixEndpoint(Path("./data/node.sock"))
    assert str(ep) == "data/node.sock"


def test_path_object_is_unix():
    assert parse_endpoint(Path("/tmp/x.sock")) == UnixEndpoint(Path("/tmp/x.sock"))


def test_unix_prefix():
    assert parse_endpoint("unix:./data/x.sock") == UnixEndpoint(Path("data/x.sock"))


def test_unix_url_form():
    assert parse_endpoint("unix:///tmp/x.sock") == UnixEndpoint(Path("/tmp/x.sock"))


def test_tcp():
    ep = parse_endpoint("tcp://127.0.0.1:8710")
    assert ep == TcpEndpoint("127.0.0.1", 8710)
    assert str(ep) == "tcp://127.0.0.1:8710"


def test_tcp_hostname_and_brackets():
    assert parse_endpoint("tcp://alfred.tailnet-example.ts.net:8710").host.endswith("ts.net")
    assert parse_endpoint("tcp://[::1]:8710") == TcpEndpoint("::1", 8710)


def test_endpoint_passthrough():
    ep = TcpEndpoint("h", 1)
    assert parse_endpoint(ep) is ep


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "tcp://127.0.0.1",
        "tcp://:99",
        "tcp://h:0",
        "tcp://h:65536",
        "tcp://h:port",
        "unix:",
    ],
)
def test_invalid_raises(bad):
    with pytest.raises(ValueError):
        parse_endpoint(bad)


def test_resolve_relative_unix_from_base():
    ep = resolve_endpoint("./data/node.sock", base_dir=Path("/opt/bot"))
    assert ep == UnixEndpoint(Path("/opt/bot/data/node.sock"))


def test_resolve_absolute_and_tcp_unchanged():
    assert resolve_endpoint("/tmp/x.sock", Path("/opt")) == UnixEndpoint(Path("/tmp/x.sock"))
    assert resolve_endpoint("tcp://h:1", Path("/opt")) == TcpEndpoint("h", 1)
    assert resolve_endpoint("./x.sock", None) == UnixEndpoint(Path("./x.sock"))


def test_unix_rejected_on_windows(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    for raw in ("./data/node.sock", "unix:/tmp/x.sock", Path("/tmp/x.sock")):
        with pytest.raises(ValueError, match="tcp://127.0.0.1"):
            parse_endpoint(raw)
    # tcp продолжает работать
    assert parse_endpoint("tcp://127.0.0.1:8710") == TcpEndpoint("127.0.0.1", 8710)


# --- local_lan_ipv4_address: нужен именно домашний адрес, tailscale (CGNAT)
# не в счёт (apps/service.py::_resolve_urls подставляет его в "{lan_ip}") ---


def test_local_lan_ipv4_address_skips_tailscale_cgnat(monkeypatch):
    # local_ipv4_addresses не сортирован по рангу — если tailscale оказался
    # первым в списке, наивный "первый элемент" подставил бы оверлей вместо
    # домашнего IP.
    monkeypatch.setattr(
        endpoints, "local_ipv4_addresses", lambda: ["100.73.52.92", "192.168.0.102"]
    )
    assert local_lan_ipv4_address() == "192.168.0.102"


def test_local_lan_ipv4_address_none_without_lan(monkeypatch):
    monkeypatch.setattr(endpoints, "local_ipv4_addresses", lambda: ["100.73.52.92"])
    assert local_lan_ipv4_address() is None


def test_локальная_сеть_пробуется_раньше_оверлея():
    """Ровно то, ради чего затевался этап: LAN — основной путь, tailscale —
    аварийный. 100.64.0.0/10 Python считает приватным, поэтому ранг у него
    свой, а не «как у 192.168»."""
    assert endpoint_rank(TcpEndpoint("192.168.0.100", 8710)) == RANK_LAN
    assert endpoint_rank(TcpEndpoint("100.73.52.92", 8710)) == RANK_OVERLAY
    assert endpoint_rank(TcpEndpoint("8.8.8.8", 8710)) == RANK_OTHER
    assert RANK_LAN < RANK_OVERLAY < RANK_OTHER


def test_loopback_и_0000_опознаются():
    assert is_loopback(TcpEndpoint("127.0.0.1", 8710))
    assert is_loopback(TcpEndpoint("localhost", 8710))
    assert is_loopback(UnixEndpoint(Path("/tmp/x.sock")))
    assert not is_loopback(TcpEndpoint("192.168.0.5", 8710))
    assert is_unspecified(TcpEndpoint("0.0.0.0", 8710))
    assert not is_unspecified(TcpEndpoint("192.168.0.5", 8710))


def test_объявляем_только_адреса_по_которым_к_нам_придут():
    """Unix-сокет и loopback соседу бесполезны: по ним он попадёт к себе."""
    advertised = advertisable(
        [
            UnixEndpoint(Path("/tmp/node.sock")),
            TcpEndpoint("127.0.0.1", 8710),
            TcpEndpoint("100.73.52.92", 8710),
            TcpEndpoint("192.168.0.100", 8710),
        ]
    )
    assert advertised == ["tcp://192.168.0.100:8710", "tcp://100.73.52.92:8710"]


def test_0000_раскрывается_в_адреса_интерфейсов(monkeypatch):
    """`listen = "tcp://0.0.0.0:8710"` — законная настройка, но объявить
    рою «ноль» нельзя: это не адрес, по которому к нам придут."""
    monkeypatch.setattr(
        "sa_home_bot.proto.endpoints.local_ipv4_addresses",
        lambda: ["192.168.0.100", "100.73.52.92"],
    )
    assert advertisable([TcpEndpoint("0.0.0.0", 8710)]) == [
        "tcp://192.168.0.100:8710",
        "tcp://100.73.52.92:8710",
    ]


def test_адреса_виртуальных_сетей_не_объявляются(monkeypatch):
    """Найдено при деплое этапа 24 на winpc: WSL и Hyper-V дают хосту адреса
    172.x, снаружи недостижимые НИКОГДА. Объявленные рою, они становятся
    мёртвыми кандидатами впереди настоящего LAN-адреса — сосед тратит на
    каждый полный CONNECT_TIMEOUT_S, и «LAN как основной путь» не работает."""
    class FakeAddr:
        def __init__(self, address: str) -> None:
            self.family = socket.AF_INET
            self.address = address

    fake_ifaces = {
        "vEthernet (WSL)": [FakeAddr("172.18.64.1")],
        "vEthernet (Default Switch)": [FakeAddr("172.26.48.1")],
        "docker0": [FakeAddr("172.17.0.1")],
        "Ethernet": [FakeAddr("192.168.0.105")],
        "Tailscale": [FakeAddr("100.78.225.83")],
    }
    # local_ipv4_addresses импортирует psutil внутри функции — подменяем модуль.
    monkeypatch.setitem(
        sys.modules, "psutil", types.SimpleNamespace(net_if_addrs=lambda: fake_ifaces)
    )

    assert local_ipv4_addresses() == ["192.168.0.105", "100.78.225.83"]


def test_настоящий_мост_и_оверлей_под_фильтр_не_попадают():
    """`br0` — обычно РЕАЛЬНЫЙ мост LAN, `br-<hash>` — docker; tailscale0 —
    рабочий путь, просто медленнее LAN."""
    assert not is_virtual_iface("br0")
    assert not is_virtual_iface("tailscale0")
    assert not is_virtual_iface("enp1s0")
    assert is_virtual_iface("br-1a2b3c4d")
    assert is_virtual_iface("vEthernet (WSL)")


def test_listen_принимает_и_строку_и_список():
    """Конфиги, написанные до этапа 24, продолжают работать как есть."""
    assert NodeConfig(listen="tcp://100.1.2.3:8710").listen == ["tcp://100.1.2.3:8710"]
    assert NodeConfig(listen="").listen == []
    assert NodeConfig(listen=["tcp://a:1", "tcp://b:2"]).listen == ["tcp://a:1", "tcp://b:2"]
