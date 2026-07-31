"""Маячок роя (этап 31): кадр и подпись, кадеданс, транспорт, переезд адреса."""

from __future__ import annotations

import asyncio
import json

import pytest

from sa_home_bot.config import DiscoveryConfig
from sa_home_bot.node.discovery import (
    DISCOVERY_V,
    MAX_DATAGRAM_BYTES,
    SwarmDiscovery,
    encode,
    make_query,
    make_reply,
    next_interval,
    parse,
    usable_endpoints,
)
from sa_home_bot.node.peers import NodeRouter

TOKEN = "s3cret"


class FakeLink:
    """Пир с управляемой живостью: запоминает, чему его научили."""

    def __init__(self, name: str, *, alive: bool, endpoints: list[str] | None = None):
        self.name = name
        self.endpoints = endpoints or [f"tcp://{name}:8710"]
        self.endpoint = self.endpoints[0]
        self.alive = alive
        self.learned: list[list[str]] = []
        self.reconnects: list[str] = []

    def learn_endpoints(self, advertised) -> None:
        self.learned.append(list(advertised))
        for value in advertised:
            if value not in self.endpoints:
                self.endpoints.append(value)

    def reconnect_now(self, reason: str) -> None:
        self.reconnects.append(reason)


def _router(node_id: str, *links: FakeLink) -> NodeRouter:
    return NodeRouter(node_id, peers={link.name: link for link in links})


def _discovery(node_id: str, router: NodeRouter, **kw) -> tuple[SwarmDiscovery, list[str]]:
    """Маячок с фейковым join: адреса, к которым он присоединялся."""
    joined: list[str] = []

    async def join(endpoint: str) -> dict:
        joined.append(endpoint)
        return {"joined_via": endpoint, "peers_added": []}

    kw.setdefault("advertise", lambda: [f"tcp://{node_id}:8710"])
    kw.setdefault("port", 0)  # порт у ОС — тесты не занимают боевой 32167
    # По умолчанию целей нет: тест не должен рассылать broadcast в настоящую
    # сеть. Кому нужен обмен — передаёт targets явно.
    kw.setdefault("targets", list)
    return SwarmDiscovery(node_id, router, token=TOKEN, join=join, **kw), joined


# --- кадр и подпись ---------------------------------------------------------


def test_valid_frame_survives_round_trip():
    parsed = parse(
        encode(TOKEN, make_reply("winpc", "abc123", ["tcp://192.168.0.105:8710"])), TOKEN
    )
    assert parsed is not None
    assert parsed["node"] == "winpc"
    assert parsed["nonce"] == "abc123"
    assert parsed["endpoints"] == ["tcp://192.168.0.105:8710"]
    # Токен в эфир не выходит ни в каком виде — только его HMAC.
    assert TOKEN not in encode(TOKEN, make_query("alfred", "abc123")).decode()


def test_wrong_token_is_dropped():
    data = encode("другой-токен", make_query("alfred", "abc123"))
    assert parse(data, TOKEN) is None


def test_tampered_payload_is_dropped():
    """Подпись держит именно содержимое: подменённый адрес не проходит."""
    body = make_reply("winpc", "abc123", ["tcp://192.168.0.105:8710"])
    signed = json.loads(encode(TOKEN, body))
    signed["endpoints"] = ["tcp://10.0.0.66:8710"]
    assert parse(json.dumps(signed).encode(), TOKEN) is None


def test_foreign_version_is_dropped():
    body = {**make_query("alfred", "abc123"), "v": DISCOVERY_V + 1}
    assert parse(encode(TOKEN, body), TOKEN) is None


def test_garbage_and_oversized_datagrams_are_dropped():
    assert parse(b"\xff\xfe not json", TOKEN) is None
    assert parse(b"[]", TOKEN) is None
    assert parse(b"x" * (MAX_DATAGRAM_BYTES + 1), TOKEN) is None


def test_empty_token_never_accepts_anything():
    """Нода без токена подписывать не умеет — и верить не должна никому."""
    assert parse(encode("", make_query("alfred", "abc")), "") is None


def test_usable_endpoints_filters_loopback_and_junk():
    assert usable_endpoints(
        [
            "tcp://127.0.0.1:8710",  # сосед по нему — это мы сами
            "tcp://192.168.0.105:8710",
            "не адрес",
            42,
            "tcp://192.168.0.105:8710",  # дубль
        ]
    ) == ["tcp://192.168.0.105:8710"]
    assert usable_endpoints("tcp://192.168.0.105:8710") == []


# --- кадеданс ---------------------------------------------------------------


def test_lonely_node_asks_often():
    cfg = DiscoveryConfig()
    assert next_interval([], cfg) == cfg.active_interval_s


def test_unreachable_peer_keeps_us_active():
    cfg = DiscoveryConfig()
    links = [FakeLink("winpc", alive=False), FakeLink("jeeves", alive=True)]
    assert next_interval(links, cfg) == cfg.active_interval_s


def test_healthy_swarm_falls_quiet():
    cfg = DiscoveryConfig()
    assert next_interval([FakeLink("winpc", alive=True)], cfg) == cfg.idle_interval_s


# --- транспорт --------------------------------------------------------------


async def test_broadcast_goes_to_the_swarm_port_not_our_own():
    """Найдено живым прогоном: цель рассылки — порт роя из конфига, а не
    порт, который ОС выдала нашему сокету. В проде они совпадают (оба 32167),
    и ошибка была бы не видна до первой ноды с другим портом."""
    # targets по умолчанию пустые (в эфир не стреляем), но саму цель
    # рассылки спрашиваем у боевого метода.
    seeker, _ = _discovery("alfred", _router("alfred"), cfg=DiscoveryConfig(port=32167))
    await seeker.start()
    try:
        assert seeker.port != 32167  # порт выдала ОС
        assert all(port == 32167 for _, port in seeker._broadcast_targets())
    finally:
        await seeker.stop()


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_query_finds_unknown_node_and_joins_it():
    """Сквозной путь: запрос в эфир → ответ соседа → обычный join."""
    listener, _ = _discovery(
        "winpc", _router("winpc"), advertise=lambda: ["tcp://192.168.0.105:8710"]
    )
    await listener.start()
    seeker, joined = _discovery(
        "alfred", _router("alfred"), targets=lambda: [("127.0.0.1", listener.port)]
    )
    try:
        await seeker.start()
        await _wait_for(lambda: joined)
    finally:
        await seeker.stop()
        await listener.stop()

    assert joined == ["tcp://192.168.0.105:8710"]
    assert seeker.state()["peers_found"] == ["winpc"]


async def test_reply_to_someone_elses_nonce_is_ignored():
    """Защита от replay: ответ, которого мы не спрашивали, не действует."""
    router = _router("alfred")
    seeker, joined = _discovery("alfred", router)
    await seeker.start()
    try:
        body = make_reply("winpc", "нашего-такого-не-было", ["tcp://192.168.0.105:8710"])
        seeker.handle_datagram(encode(TOKEN, body), ("192.168.0.105", 32167))
        await asyncio.sleep(0.05)
    finally:
        await seeker.stop()
    assert joined == []


async def test_own_broadcast_coming_back_is_ignored():
    seeker, joined = _discovery("alfred", _router("alfred"))
    await seeker.start()
    try:
        seeker.query_once()
        nonce = seeker._nonces[-1]
        echo = make_reply("alfred", nonce, ["tcp://192.168.0.101:8710"])
        seeker.handle_datagram(encode(TOKEN, echo), ("192.168.0.101", 32167))
        await asyncio.sleep(0.05)
    finally:
        await seeker.stop()
    assert joined == []


async def test_moved_peer_gets_new_address_without_join():
    """Уехал DHCP-адрес: линк переучивается и будится, join не нужен —
    пир уже известен."""
    link = FakeLink("winpc", alive=False, endpoints=["tcp://192.168.0.105:8710"])
    seeker, joined = _discovery("alfred", _router("alfred", link))
    await seeker.start()
    try:
        seeker.query_once()
        nonce = seeker._nonces[-1]
        body = make_reply("winpc", nonce, ["tcp://192.168.0.177:8710"])
        seeker.handle_datagram(encode(TOKEN, body), ("192.168.0.177", 32167))
        await _wait_for(lambda: link.reconnects)
    finally:
        await seeker.stop()

    assert link.learned == [["tcp://192.168.0.177:8710"]]
    assert "192.168.0.177" in link.reconnects[0]
    assert joined == []


async def test_live_peer_is_left_alone():
    link = FakeLink("winpc", alive=True, endpoints=["tcp://192.168.0.105:8710"])
    seeker, joined = _discovery("alfred", _router("alfred", link))
    await seeker.start()
    try:
        seeker.query_once()
        body = make_reply("winpc", seeker._nonces[-1], ["tcp://192.168.0.177:8710"])
        seeker.handle_datagram(encode(TOKEN, body), ("192.168.0.177", 32167))
        await asyncio.sleep(0.05)
    finally:
        await seeker.stop()
    assert link.learned == [] and link.reconnects == [] and joined == []


async def test_query_from_stranger_gets_no_reply(monkeypatch):
    """Порт неотличим от закрытого: на пакет с чужой подписью — тишина."""
    listener, _ = _discovery("winpc", _router("winpc"))
    await listener.start()
    sent: list[tuple] = []
    monkeypatch.setattr(listener._transport, "sendto", lambda *a: sent.append(a))
    try:
        listener.handle_datagram(
            encode("чужой-токен", make_query("intruder", "abc")), ("192.168.0.9", 32167)
        )
        await asyncio.sleep(0.05)
    finally:
        await listener.stop()
    assert sent == []


async def test_busy_port_does_not_break_the_node():
    """Маячок — удобство, а не транспорт: занятый порт ноду не роняет."""
    first, _ = _discovery("winpc", _router("winpc"))
    await first.start()
    try:
        second, _ = _discovery("alfred", _router("alfred"), port=first.port)
        await second.start()  # исключения быть не должно
        assert not second.running
        await second.stop()
    finally:
        await first.stop()


async def test_disabled_and_tokenless_discovery_stays_silent():
    off, _ = _discovery("alfred", _router("alfred"), cfg=DiscoveryConfig(enabled=False))
    await off.start()
    assert not off.running
    await off.stop()

    router = _router("alfred")

    async def join(endpoint: str) -> dict:  # pragma: no cover - не должен зваться
        raise AssertionError("join без токена невозможен")

    silent = SwarmDiscovery("alfred", router, token="", join=join, advertise=list, port=0)
    await silent.start()
    assert not silent.running
    await silent.stop()


@pytest.mark.parametrize("body", [make_query("alfred", "n1"), make_reply("winpc", "n1", [])])
def test_signature_covers_every_field(body):
    signed = json.loads(encode(TOKEN, body))
    for field in ("node", "t", "nonce"):
        tampered = {**signed, field: "подмена"}
        assert parse(json.dumps(tampered).encode(), TOKEN) is None
