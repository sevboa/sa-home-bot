"""Wake-on-LAN: нормализация MAC, magic packet, ожидание пробуждения."""

from __future__ import annotations

import socket

import pytest

from sa_home_bot import wol

MAC = "aa:bb:cc:dd:ee:ff"


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
