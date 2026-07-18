"""Wake-on-LAN: нормализация MAC, magic packet, ожидание пробуждения,
определение своего Ethernet-сегмента (этап 19 п.6)."""

from __future__ import annotations

import socket
from collections import namedtuple

import pytest

from sa_home_bot import wol

MAC = "aa:bb:cc:dd:ee:ff"

_Snic = namedtuple("snic", ["family", "address", "netmask", "broadcast", "ptp"])


@pytest.mark.parametrize(
    "raw",
    [
        "aa:bb:cc:dd:ee:ff",
        "AA:BB:CC:DD:EE:FF",
        "aa-bb-cc-dd-ee-ff",
        "aabb.ccdd.eeff",
        "aabbccddeeff",
        "  AA-bb-CC-dd-EE-ff  ",
    ],
)
def test_normalize_mac_variants(raw: str):
    assert wol.normalize_mac(raw) == MAC


@pytest.mark.parametrize("raw", ["", "aa:bb:cc", "aa:bb:cc:dd:ee:fg", "слово", "aabbccddeeff00"])
def test_normalize_mac_rejects_garbage(raw: str):
    with pytest.raises(ValueError):
        wol.normalize_mac(raw)


def test_build_magic_packet_layout():
    packet = wol.build_magic_packet(MAC)
    mac_bytes = bytes.fromhex("aabbccddeeff")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == mac_bytes * 16


def test_send_magic_packet_over_udp():
    # Локальный приёмник вместо broadcast — проверяем реальную отправку.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as recv:
        recv.bind(("127.0.0.1", 0))
        recv.settimeout(2)
        _, port = recv.getsockname()
        wol.send_magic_packet(MAC, broadcast="127.0.0.1", port=port, repeats=2)
        data, _ = recv.recvfrom(1024)
    assert data == wol.build_magic_packet(MAC)


async def test_wait_host_up_returns_elapsed(monkeypatch: pytest.MonkeyPatch):
    responses = iter([False, False, True])

    async def fake_ping(ip: str) -> bool:
        return next(responses)

    monkeypatch.setattr(wol, "ping_host", fake_ping)
    elapsed = await wol.wait_host_up("192.0.2.1", timeout_s=10, interval_s=0.01)
    assert elapsed is not None


async def test_wait_host_up_timeout(monkeypatch: pytest.MonkeyPatch):
    async def fake_ping(ip: str) -> bool:
        return False

    monkeypatch.setattr(wol, "ping_host", fake_ping)
    assert await wol.wait_host_up("192.0.2.1", timeout_s=0.05, interval_s=0.01) is None


def test_send_magic_packet_binds_to_interface():
    # bind_ip привязывает исходящий сокет — приёмник видит именно её как src.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as recv:
        recv.bind(("127.0.0.1", 0))
        recv.settimeout(2)
        _, port = recv.getsockname()
        wol.send_magic_packet(MAC, broadcast="127.0.0.1", port=port, repeats=1, bind_ip="127.0.0.1")
        data, addr = recv.recvfrom(1024)
    assert addr[0] == "127.0.0.1"
    assert data == wol.build_magic_packet(MAC)


# --- detect_local_wake_info: своя Ethernet-нода определяет себя (этап 19 п.6) ---


def _mock_lan(monkeypatch: pytest.MonkeyPatch, iface: str, ip: str = "192.168.0.100") -> None:
    monkeypatch.setattr(wol, "_default_route_ip", lambda: ip)
    monkeypatch.setattr(
        wol.psutil,
        "net_if_addrs",
        lambda: {
            iface: [
                _Snic(socket.AF_INET, ip, "255.255.255.0", "192.168.0.255", None),
                _Snic(wol.psutil.AF_LINK, "7c:83:34:b4:59:ac", None, None, None),
            ]
        },
    )


def test_detect_local_wake_info_on_ethernet(monkeypatch: pytest.MonkeyPatch):
    _mock_lan(monkeypatch, "enp1s0")
    info = wol.detect_local_wake_info()
    assert info == wol.LocalWakeInfo(
        mac="7c:83:34:b4:59:ac", ip="192.168.0.100", broadcast="192.168.0.255"
    )


def test_detect_local_wake_info_none_on_wifi(monkeypatch: pytest.MonkeyPatch):
    _mock_lan(monkeypatch, "wlp2s0")
    monkeypatch.setattr(wol, "_is_wireless", lambda name: True)
    assert wol.detect_local_wake_info() is None


def test_detect_local_wake_info_none_on_virtual_iface(monkeypatch: pytest.MonkeyPatch):
    _mock_lan(monkeypatch, "tailscale0")
    assert wol.detect_local_wake_info() is None


def test_detect_local_wake_info_none_without_default_route(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wol, "_default_route_ip", lambda: None)
    assert wol.detect_local_wake_info() is None
