"""Подписка Hiddify и https-страница (57.10): токен, сборка, форматы, заголовки,
страница, отказ при отозванном, ходьба по нодам, кнопка в карточке устройства."""

from __future__ import annotations

import asyncio
import base64
import json
import ssl
import subprocess
from urllib.parse import quote

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_nodes
from sa_home_bot.bot.handlers import vpn as h
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import RealityTransportConfig, Settings, SwarmConfig, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.reality.client_config import RealityParams
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.protocol import TRANSPORT_REALITY
from sa_home_bot.vpn.service import VpnService
from sa_home_bot.vpn.subweb import SubscriptionWeb

from .test_vpn_expert import ADMIN, KEY, LABEL, _servers
from .test_vpn_service import CHAT, FakeAwg, FakeXray, allow

SWARM = "swarm-token-xyz"


def _cfg(host, location, transports=None, **over) -> VpnConfig:
    return VpnConfig(
        subnet="10.9.0.0/29",
        base_quota_gb=1,
        endpoint_host=host,
        transports=transports or [TRANSPORT_REALITY],
        location=location,
        reality=RealityTransportConfig(
            endpoint_host=host, server_public_key=f"PUB_{host}", short_id="abcd1234"
        ),
        sub_port=18444,
        **over,
    )


async def _make(tmp_path, name, host, location, transports=None):
    db = Database(tmp_path / f"{name}.sqlite")
    await db.open()
    await apply_migrations(db)
    settings = Settings(vpn=_cfg(host, location, transports), swarm=SwarmConfig(token=SWARM))
    svc = VpnService(settings, db, FakeAwg(), _noop, reality_backend=FakeXray())
    svc._node_id = name
    await allow(svc, CHAT)
    return svc, db


async def _noop(*_a) -> None:
    return None


class PeerLink:
    """node_link, ведущий к другим службам в памяти."""

    def __init__(self, peers: dict[str, VpnService]) -> None:
        self.peers = peers
        self.down: set[str] = set()

    async def command(self, action, args=None, dst=None, *, timeout=None):
        if dst.node in self.down:
            raise ServiceUnavailableError("лежит")
        return await self.peers[dst.node].run_command(action, args or {})


@pytest_asyncio.fixture
async def swarm(tmp_path, monkeypatch):
    a, dba = await _make(tmp_path, "jeeves", "198.51.100.1", "🇳🇱 Нидерланды")
    b, dbb = await _make(tmp_path, "wooster", "198.51.100.2", "🇺🇸 США")
    link = PeerLink({"jeeves": a, "wooster": b})
    a._node_link = b._node_link = link  # type: ignore[assignment]

    async def live(_link):
        return ["jeeves", "wooster"]

    monkeypatch.setattr(vpn_nodes, "live_vpn_nodes", live)
    for svc in (a, b):
        await svc.run_command(
            vpn_protocol.ACTION_ISSUE,
            {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": TRANSPORT_REALITY},
        )
    yield a, b, link
    await dba.close()
    await dbb.close()


def _token(svc, label="📱 iPhone"):
    return subs.make_token(svc._sub_secret, CHAT, label)


# --- токен ---


def test_token_is_stable_hides_chat_and_depends_on_secret():
    secret = subs.derive_secret("", SWARM)
    token = subs.make_token(secret, 188548043, "iPhone")
    assert token == subs.make_token(secret, 188548043, "iPhone")
    assert len(token) == subs.TOKEN_LEN and subs.valid_token_shape(token)
    assert "188548043" not in token
    assert token != subs.make_token(secret, 188548043, "Android")
    assert token != subs.make_token(secret, 1, "iPhone")
    assert token != subs.make_token(subs.derive_secret("", "other"), 188548043, "iPhone")
    assert subs.derive_secret("own", SWARM) != subs.derive_secret("", SWARM)
    assert subs.token_matches(secret, token, 188548043, "iPhone")
    assert not subs.token_matches(secret, token, 188548043, "iphone")


def test_token_shape_validation():
    assert not subs.valid_token_shape("")
    assert not subs.valid_token_shape("a" * 31)
    assert not subs.valid_token_shape("a" * 32 + "/")
    assert not subs.valid_token_shape("../" + "a" * 29)


# --- сборка и форматы ---


def _sub():
    entries = [
        subs.SubEntry(
            "wooster",
            "🇺🇸 США",
            "uuid-us",
            RealityParams("198.51.100.2", 8443, "PUB2", "ab", "www.google.com"),
        ),
        subs.SubEntry(
            "jeeves",
            "🇳🇱 Нидерланды",
            "uuid-nl",
            RealityParams("198.51.100.1", 8443, "PUB1", "ab", "www.google.com"),
        ),
    ]
    return subs.Subscription(
        "📱 iPhone", tuple(subs.sort_entries(entries)), 5_000, 200_000, 1_800_000_000
    )


def test_render_vless_list_default_and_plain():
    sub = _sub()
    body, ctype = subs.render_body(sub, subs.FORMAT_VLESS)
    assert ctype.startswith("text/plain")
    lines = base64.b64decode(body).decode().splitlines()
    assert len(lines) == 2 and all(line.startswith("vless://") for line in lines)
    assert lines[0].endswith(quote("🇳🇱 Нидерланды", safe=""))
    assert "uuid-nl@198.51.100.1:8443" in lines[0]
    plain, _ = subs.render_body(sub, subs.FORMAT_PLAIN)
    assert plain.splitlines() == lines


def test_render_singbox_has_all_countries_and_selector():
    body, ctype = subs.render_body(_sub(), subs.FORMAT_SINGBOX)
    assert ctype.startswith("application/json")
    cfg = json.loads(body)
    tags = {o["tag"]: o for o in cfg["outbounds"]}
    assert tags["🇳🇱 Нидерланды"]["uuid"] == "uuid-nl"
    assert tags["🇺🇸 США"]["tls"]["reality"]["public_key"] == "PUB2"
    assert tags["auto"]["type"] == "urltest" and tags["proxy"]["type"] == "selector"
    assert set(tags["auto"]["outbounds"]) == {"🇳🇱 Нидерланды", "🇺🇸 США"}
    assert cfg["route"]["final"] == "proxy"


def test_headers():
    headers = subs.render_headers(
        _sub(), page_url="https://h/s/t", update_interval_h=12, support_url="https://t.me/x"
    )
    assert headers["profile-update-interval"] == "12"
    assert (
        headers["subscription-userinfo"]
        == "upload=0; download=5000; total=200000; expire=1800000000"
    )
    title = headers["profile-title"]
    assert title.startswith("base64:")
    assert base64.b64decode(title[7:]).decode() == "VPN · 📱 iPhone"
    assert headers["profile-web-page-url"] == "https://h/s/t"
    assert headers["support-url"] == "https://t.me/x"
    for value in headers.values():  # HTTP-заголовки — latin-1
        value.encode("latin-1")


LINKS = subs.PageLinks(
    hiddify_ios="https://apps.apple.com/x",
    hiddify_android="https://play.google.com/x",
    hiddify_site="https://hiddify.com",
    amnezia_ios="https://apps.apple.com/amnezia",
    amnezia_android="https://play.google.com/amnezia",
    amnezia_site="https://amnezia.org",
)


def _page(sub=None, **kw):
    return subs.render_page(
        sub or _sub(),
        sub_url="https://h:8444/sub/tok",
        qr_data_uri="data:image/svg+xml;base64,AAA",
        links=LINKS,
        path="/s/tok",
        **kw,
    )


def test_page_is_hub_with_hiddify_steps_copy_qr_and_stores():
    page = _page()
    assert "📶 VPN · 📱 iPhone" in page and "VLESS · Hiddify" in page
    assert 'href="hiddify://import/https://h:8444/sub/tok"' in page
    assert "➕ Добавить в Hiddify" in page and "📋 Скопировать ссылку" in page
    assert (
        "data:image/svg+xml" in page and "apps.apple.com/x" in page and "play.google.com/x" in page
    )
    assert "включите круглую кнопку" in page.lower()
    assert "🇳🇱 Нидерланды" in page and "🇺🇸 США" in page
    # автопереход один раз и без внешних ресурсов
    assert "localStorage" in page and "setTimeout" in page
    assert "http://" not in page and 'src="http' not in page


def test_page_status_blocks_and_auto_open_off_when_on():
    off = _page(status=subs.VpnStatus(False))
    assert "❌ VPN сейчас выключен" in off and "🔄 Проверить ещё раз" in off
    assert "AUTO = true" in off or "AUTO = true" in off.replace("  ", " ")
    on = _page(status=subs.VpnStatus(True, "🇳🇱 Нидерланды", subs.METHOD_AWG))
    assert "✅ VPN включён — 🇳🇱 Нидерланды" in on and "Способ: AmneziaVPN" in on
    assert "AUTO = false" in on.replace("  ", " ") or "AUTO = false" in on


def test_page_countries_health_and_remaining():
    sub = subs.Subscription(
        "d",
        _sub().entries,
        used_bytes=13 * 10**9,
        total_bytes=100 * 10**9,
        expire_ts=1_793_491_200,  # 1 ноября 2026
        nodes=(
            subs.NodeInfo("jeeves", "🇳🇱 Нидерланды", "198.51.100.1", health="ok"),
            subs.NodeInfo("wooster", "🇺🇸 США", "198.51.100.2", health="bad"),
        ),
    )
    page = _page(sub)
    assert "🇳🇱 Нидерланды — работает" in page and "🇺🇸 США — может не работать" in page
    assert "Осталось 87 ГБ из 100 до 1 ноября" in page


def test_page_escapes_label():
    sub = subs.Subscription("<script>x</script>", _sub().entries)
    assert "<script>x</script>" not in _page(sub)


def test_page_awg_block_collapsed_with_forms_per_country():
    nodes = (
        subs.NodeInfo("jeeves", "🇳🇱 Нидерланды", "198.51.100.1", awg=True),
        subs.NodeInfo("wooster", "🇺🇸 США", "198.51.100.2", awg=True),
    )
    sub = subs.Subscription("d", _sub().entries, nodes=nodes)
    page = _page(sub, awg_forms={"jeeves": "N1", "wooster": "N2"})
    assert "<details><summary>AmneziaVPN</summary>" in page
    assert "Получить настройки 🇳🇱" in page and 'action="/s/tok/awg"' in page
    assert 'value="N1"' in page and "не на всех" in page
    assert "AmneziaVPN</summary>" not in _page(sub)  # без форм блока нет


def test_detect_status_by_address():
    nodes = (
        subs.NodeInfo("jeeves", "🇳🇱 Нидерланды", "198.51.100.1", local=True),
        subs.NodeInfo("wooster", "🇺🇸 США", "198.51.100.2"),
    )
    net = "10.9.0.0/29"
    own_awg = subs.detect_status("10.9.0.3", nodes, net)
    assert own_awg == subs.VpnStatus(True, "🇳🇱 Нидерланды", "AmneziaVPN")
    own_vless = subs.detect_status("198.51.100.1", nodes, net)
    assert own_vless == subs.VpnStatus(True, "🇳🇱 Нидерланды", "VLESS · Hiddify")
    peer = subs.detect_status("::ffff:198.51.100.2", nodes, net)
    assert peer == subs.VpnStatus(True, "🇺🇸 США", "")  # страна без способа
    assert subs.detect_status("203.0.113.9", nodes, net) == subs.VpnStatus(False)
    assert subs.detect_status("garbage", nodes, net) == subs.VpnStatus(False)
    assert subs.detect_status("", nodes, net) == subs.VpnStatus(False)


def test_token_carries_generation_and_rejects_forgery():
    secret = subs.derive_secret("", SWARM)
    t0, t1 = subs.make_token(secret, 5, "d"), subs.make_token(secret, 5, "d", 1_700_000_000)
    assert t0 != t1 and subs.token_gen(t1) == 1_700_000_000 and subs.token_gen(t0) == 0
    assert subs.token_matches(secret, t1, 5, "d")
    # подделка поколения (подмена первых 4 байт) не проходит подпись
    forged = subs.make_token(secret, 5, "d", 0)
    raw = base64.urlsafe_b64decode(forged + "==")
    raw = (1).to_bytes(4, "big") + raw[4:]
    assert not subs.token_matches(
        secret, base64.urlsafe_b64encode(raw).decode().rstrip("="), 5, "d"
    )


# --- служба: токен находится на любой ноде, подписка собирается из роя ---


async def test_get_subscription_requires_running_web(swarm):
    a, _b, _link = swarm
    with pytest.raises(ProtoError):
        await a.run_command(
            vpn_protocol.ACTION_GET_SUBSCRIPTION, {"chat_id": CHAT, "device_label": "📱 iPhone"}
        )


async def test_get_subscription_returns_urls(swarm):
    a, _b, _link = swarm
    a.sub_web = SubscriptionWeb(a._cfg, a.resolve_subscription)
    a.sub_web._runner = object()  # type: ignore[assignment]  # «слушает»
    res = await a.run_command(
        vpn_protocol.ACTION_GET_SUBSCRIPTION, {"chat_id": CHAT, "device_label": "📱 iPhone"}
    )
    token = _token(a)
    assert res["page_url"] == f"http://198.51.100.1:18444/s/{token}"
    assert res["sub_url"] == f"http://198.51.100.1:18444/sub/{token}"
    assert res["singbox_url"].endswith("?format=singbox")
    with pytest.raises(ProtoError):
        await a.run_command(
            vpn_protocol.ACTION_GET_SUBSCRIPTION, {"chat_id": CHAT, "device_label": "нет"}
        )


async def test_token_resolves_on_any_node_with_all_countries(swarm):
    a, b, _link = swarm
    token = _token(a)
    assert token == _token(b)  # общий секрет: тот же токен на обеих нодах
    for svc in (a, b):
        sub = await svc.resolve_subscription(token)
        assert sub is not None
        assert [e.name for e in sub.entries] == ["🇳🇱 Нидерланды", "🇺🇸 США"]
        assert sub.device_label == "📱 iPhone"
        assert sub.total_bytes == 2 * 10**9  # лимиты двух нод


async def test_peer_down_keeps_last_known_country(swarm):
    a, _b, link = swarm
    token = _token(a)
    assert len((await a.resolve_subscription(token)).entries) == 2
    a._sub_cache.clear()
    link.down.add("wooster")
    sub = await a.resolve_subscription(token)
    assert [e.name for e in sub.entries] == ["🇳🇱 Нидерланды", "🇺🇸 США"]


async def test_revoked_device_is_gone_everywhere(swarm):
    a, b, _link = swarm
    token = _token(a)
    assert await a.resolve_subscription(token) is not None
    for svc in (a, b):
        await svc.run_command(
            vpn_protocol.ACTION_REVOKE,
            {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": TRANSPORT_REALITY},
        )
        svc._sub_cache.clear()
    assert await a.resolve_subscription(token) is None
    assert await b.resolve_subscription(token) is None


async def test_revoked_on_one_node_leaves_the_other(swarm):
    a, b, _link = swarm
    token = _token(a)
    await b.run_command(
        vpn_protocol.ACTION_REVOKE,
        {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": TRANSPORT_REALITY},
    )
    sub = await a.resolve_subscription(token)
    assert [e.name for e in sub.entries] == ["🇳🇱 Нидерланды"]


async def test_unknown_and_malformed_tokens(swarm):
    a, _b, _link = swarm
    assert await a.resolve_subscription("x" * 32) is None
    assert await a.resolve_subscription("short") is None
    assert (await a.run_command(vpn_protocol.ACTION_SUB_LINKS, {"token": "bad"}))["found"] is False


async def test_sentinel_probe_never_gets_subscription(swarm):
    a, _b, _link = swarm
    await a.run_command(
        vpn_protocol.ACTION_ISSUE, {"chat_id": 0, "device_label": "probe", "transport": "reality"}
    )
    assert await a.resolve_subscription(subs.make_token(a._sub_secret, 0, "probe")) is None


# --- веб ---


@pytest_asyncio.fixture
async def client(swarm):
    a, _b, _link = swarm
    web = SubscriptionWeb(a._cfg, a.resolve_subscription)
    cl = TestClient(TestServer(web._app()))
    await cl.start_server()
    yield cl, a, web
    await cl.close()


async def test_web_subscription_and_page(client):
    cl, a, web = client
    token = _token(a)
    resp = await cl.get(f"/sub/{token}")
    assert resp.status == 200
    lines = base64.b64decode(await resp.text()).decode().splitlines()
    assert len(lines) == 2
    assert resp.headers["profile-update-interval"] == "12"
    assert resp.headers["subscription-userinfo"].startswith("upload=0; download=0; total=")
    assert resp.headers["cache-control"] == "no-store"
    resp = await cl.get(f"/sub/{token}?format=singbox")
    assert resp.status == 200 and json.loads(await resp.text())["outbounds"]
    resp = await cl.get(f"/sub/{token}?format=plain")
    assert (await resp.text()).startswith("vless://")
    page = await cl.get(f"/s/{token}")
    assert page.status == 200 and page.content_type == "text/html"
    text = await page.text()
    assert "Добавить в Hiddify" in text and f"/sub/{token}" in text


async def test_web_404_for_unknown_revoked_and_garbage(client):
    cl, a, _web = client
    assert (await cl.get("/s/" + "A" * 32)).status == 404
    assert (await cl.get("/sub/" + "A" * 32)).status == 404
    assert (await cl.get("/sub/short")).status == 404
    assert (await cl.get("/")).status == 404
    token = _token(a)
    for svc in (a, a._node_link.peers["wooster"]):
        await svc.run_command(
            vpn_protocol.ACTION_REVOKE,
            {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": TRANSPORT_REALITY},
        )
        svc._sub_cache.clear()
    assert (await cl.get(f"/sub/{token}")).status == 404
    assert (await cl.get(f"/s/{token}")).status == 404


# --- токен-поколение: перевыпуск гасит старую ссылку на всех нодах ---


async def _reissue(svc, label="📱 iPhone", transport=TRANSPORT_REALITY):
    await asyncio.sleep(1.1)  # поколение — секунды
    await svc.run_command(
        vpn_protocol.ACTION_REISSUE,
        {"chat_id": CHAT, "device_label": label, "transport": transport},
    )
    for node in (svc, *svc._node_link.peers.values()):
        node._sub_cache.clear()


async def _fresh_token(svc, label="📱 iPhone"):
    res = await svc.run_command(
        vpn_protocol.ACTION_GET_SUBSCRIPTION, {"chat_id": CHAT, "device_label": label}
    )
    return res["page_url"].rsplit("/", 1)[1]


@pytest_asyncio.fixture
async def webswarm(swarm):
    a, b, link = swarm
    for svc in (a, b):
        svc.sub_web = SubscriptionWeb(svc._cfg, svc.resolve_subscription, svc.web_issue_awg)
        svc.sub_web._runner = object()  # type: ignore[assignment]
    return a, b, link


async def test_reissue_on_any_node_kills_old_token_everywhere(webswarm):
    a, b, _link = webswarm
    old = await _fresh_token(a)
    assert old == await _fresh_token(b) == _token(a)  # поколения нет — токен прежний
    for svc in (a, b):
        assert await svc.resolve_subscription(old) is not None
    await _reissue(b)  # перевыпуск только в США
    new = await _fresh_token(a)  # другая нода узнаёт поколение у соседа
    assert new != old and new == await _fresh_token(b)
    for svc in (a, b):
        assert await svc.resolve_subscription(old) is None
        sub = await svc.resolve_subscription(new)
        assert sub is not None and len(sub.entries) == 2
    await _reissue(a)
    newest = await _fresh_token(b)
    assert newest not in (old, new)
    assert await b.resolve_subscription(new) is None
    assert await b.resolve_subscription(newest) is not None


async def test_reissue_token_survives_node_restore_from_peers_table(webswarm):
    """Поколение живёт в vpn_peers (revoked_at снятых строк) — другого хранилища нет."""
    a, b, _link = webswarm
    await _reissue(a)
    token = await _fresh_token(a)
    # «переустановка» ноды: новая служба на той же БД, кэшей нет
    fresh = VpnService(
        Settings(vpn=_cfg("198.51.100.1", "🇳🇱 Нидерланды"), swarm=SwarmConfig(token=SWARM)),
        a._db, FakeAwg(), _noop, reality_backend=FakeXray(),
    )  # fmt: skip
    fresh._node_id = "jeeves"
    fresh._node_link = a._node_link
    assert await fresh.resolve_subscription(token) is not None
    assert await fresh.resolve_subscription(_token(a)) is None  # токен нулевого поколения мёртв


async def test_new_country_does_not_change_token(tmp_path, monkeypatch):
    a, dba = await _make(tmp_path, "jeeves", "198.51.100.1", "🇳🇱 Нидерланды")
    b, dbb = await _make(tmp_path, "wooster", "198.51.100.2", "🇺🇸 США")
    a._node_link = b._node_link = PeerLink({"jeeves": a, "wooster": b})  # type: ignore[assignment]

    async def live(_link):
        return ["jeeves", "wooster"]

    monkeypatch.setattr(vpn_nodes, "live_vpn_nodes", live)
    for svc in (a, b):
        svc.sub_web = SubscriptionWeb(svc._cfg, svc.resolve_subscription)
        svc.sub_web._runner = object()  # type: ignore[assignment]
    args = {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": TRANSPORT_REALITY}
    await a.run_command(vpn_protocol.ACTION_ISSUE, args)
    before = await _fresh_token(a)
    await b.run_command(vpn_protocol.ACTION_ISSUE, args)  # страну открыли позже
    assert await _fresh_token(a) == before == await _fresh_token(b)
    assert len((await a.resolve_subscription(before)).entries) == 2
    await dba.close()
    await dbb.close()


async def test_deleted_device_is_404_even_with_fresh_token(webswarm):
    a, b, _link = webswarm
    token = await _fresh_token(a)
    for svc in (a, b):
        await svc.run_command(
            vpn_protocol.ACTION_REVOKE,
            {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": TRANSPORT_REALITY},
        )
    assert await a.resolve_subscription(token) is None


# --- веб: заголовки, 404, статус «VPN включён», выдача AmneziaWG ---


@pytest_asyncio.fixture
async def awg_swarm(tmp_path, monkeypatch):
    both = [TRANSPORT_REALITY, vpn_protocol.TRANSPORT_AWG]
    a, dba = await _make(tmp_path, "jeeves", "127.0.0.1", "🇳🇱 Нидерланды", both)
    b, dbb = await _make(tmp_path, "wooster", "198.51.100.2", "🇺🇸 США", both)
    a._node_link = b._node_link = PeerLink({"jeeves": a, "wooster": b})  # type: ignore[assignment]

    async def live(_link):
        return ["jeeves", "wooster"]

    monkeypatch.setattr(vpn_nodes, "live_vpn_nodes", live)
    for svc in (a, b):
        await svc.run_command(
            vpn_protocol.ACTION_ISSUE,
            {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": TRANSPORT_REALITY},
        )
    web = SubscriptionWeb(a._cfg, a.resolve_subscription, a.web_issue_awg)
    cl = TestClient(TestServer(web._app()))
    await cl.start_server()
    yield cl, a, b, web
    await cl.close()
    await dba.close()
    await dbb.close()


def _origin(cl):
    return {"Origin": f"http://{cl.server.host}:{cl.server.port}"}


async def _form_nonce(cl, token, node):
    text = await (await cl.get(f"/s/{token}")).text()
    marker = f'name="node" value="{node}"'
    chunk = text[: text.index(marker)]
    return chunk.rsplit('name="nonce" value="', 1)[1].split('"', 1)[0]


async def test_web_headers_no_server_and_bare_404s(awg_swarm):
    cl, a, _b, _web = awg_swarm
    token = _token(a)
    page = await cl.get(f"/s/{token}")
    assert page.status == 200
    for resp in (page, await cl.get("/s/" + "A" * 32), await cl.get("/nope")):
        assert "Server" not in resp.headers
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["Referrer-Policy"] == "same-origin"
        assert "noindex" in resp.headers["X-Robots-Tag"]
    assert "default-src 'none'" in page.headers["Content-Security-Policy"]
    for path in ("/", "/nope", "/s/", "/s/short", f"/s/{token}/x", "/favicon.ico"):
        resp = await cl.get(path)
        assert resp.status == 404 and (await resp.text()) == "Not found\n"
    assert (await cl.post(f"/s/{token}")).status == 404  # чужой метод — тот же 404
    assert (await cl.put(f"/sub/{token}")).status == 404
    assert (await cl.post("/s/" + "A" * 32 + "/awg", headers=_origin(cl))).status == 404


async def test_web_status_by_request_address(awg_swarm):
    cl, a, _b, _web = awg_swarm
    token = _token(a)
    # тестовый клиент приходит с 127.0.0.1 = публичный адрес «своей» ноды jeeves
    on = await (await cl.get(f"/s/{token}")).text()
    assert "✅ VPN включён — 🇳🇱 Нидерланды" in on and "VLESS · Hiddify" in on
    a._cfg.reality.endpoint_host = "198.51.100.9"  # теперь адрес запроса — чужой
    a._cfg.endpoint_host = "198.51.100.9"
    a._sub_cache.clear()
    off = await (await cl.get(f"/s/{token}")).text()
    assert "❌ VPN сейчас выключен" in off and "✅ VPN включён" not in off


async def test_awg_post_requires_origin_and_nonce(awg_swarm):
    cl, a, _b, _web = awg_swarm
    token = _token(a)
    nonce = await _form_nonce(cl, token, "jeeves")
    data = {"nonce": nonce, "node": "jeeves"}
    assert (await cl.post(f"/s/{token}/awg", data=data)).status == 404  # без Origin/Referer
    evil = {"Origin": "http://evil.example"}
    assert (await cl.post(f"/s/{token}/awg", data=data, headers=evil)).status == 404
    bad = await cl.post(
        f"/s/{token}/awg", data={"nonce": "x", "node": "jeeves"}, headers=_origin(cl)
    )
    assert bad.status == 400 and "устарела" in await bad.text()
    # Referer вместо Origin тоже годится
    ref = {"Referer": f"http://{cl.server.host}:{cl.server.port}/s/{token}"}
    ok = await cl.post(f"/s/{token}/awg", data=data, headers=ref)
    assert ok.status == 200
    # nonce одноразовый
    again = await cl.post(f"/s/{token}/awg", data=data, headers=_origin(cl))
    assert again.status == 400


async def test_awg_issue_local_and_remote_show_config_once(awg_swarm):
    cl, a, b, _web = awg_swarm
    token = _token(a)
    for node, svc, name in (("jeeves", a, "awg_nl_"), ("wooster", b, "awg_us_")):
        nonce = await _form_nonce(cl, token, node)
        resp = await cl.post(
            f"/s/{token}/awg", data={"nonce": nonce, "node": node}, headers=_origin(cl)
        )
        text = await resp.text()
        assert resp.status == 200, text
        assert "Настройки показаны один раз" in text and f'download="{name}' in text
        assert "AmneziaVPN" in text and "Проверьте имя подключения" in text
        assert "Скопировать ключ" in text and 'id="addkey"' in text and "vpn://" in text
        assert "Другие способы" in text and "Магазин недоступен? Запросите файл" in text
        assert "data:image/svg+xml" in text
        row = await svc._active_row(CHAT, "📱 iPhone", vpn_protocol.TRANSPORT_AWG)
        assert row is not None
    # на странице после выдачи ключ есть →
    a._sub_cache.clear()
    nonce = await _form_nonce(cl, token, "jeeves")
    assert nonce


async def test_awg_existing_key_needs_confirmation_then_replaces(awg_swarm):
    cl, a, _b, web = awg_swarm
    token = _token(a)
    await a.run_command(
        vpn_protocol.ACTION_ISSUE,
        {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": vpn_protocol.TRANSPORT_AWG},
    )
    old = await a._active_row(CHAT, "📱 iPhone", vpn_protocol.TRANSPORT_AWG)
    nonce = await _form_nonce(cl, token, "jeeves")
    ask = await cl.post(
        f"/s/{token}/awg", data={"nonce": nonce, "node": "jeeves"}, headers=_origin(cl)
    )
    text = await ask.text()
    assert "Новый заменит старый" in text and 'name="confirm" value="1"' in text
    assert (await a._active_row(CHAT, "📱 iPhone", vpn_protocol.TRANSPORT_AWG))["public_key"] == (
        old["public_key"]
    )  # без подтверждения ничего не заменено
    nonce2 = text.split('name="nonce" value="', 1)[1].split('"', 1)[0]
    done = await cl.post(
        f"/s/{token}/awg",
        data={"nonce": nonce2, "node": "jeeves", "confirm": "1"},
        headers=_origin(cl),
    )
    assert done.status == 200 and "Настройки показаны один раз" in await done.text()
    new = await a._active_row(CHAT, "📱 iPhone", vpn_protocol.TRANSPORT_AWG)
    assert new["public_key"] != old["public_key"]
    assert web._issue_awg is not None


async def test_awg_rate_limit_per_country_and_token(awg_swarm):
    cl, a, _b, web = awg_swarm
    token = _token(a)
    n1 = await _form_nonce(cl, token, "jeeves")
    n2 = await _form_nonce(cl, token, "jeeves")
    first = await cl.post(
        f"/s/{token}/awg", data={"nonce": n1, "node": "jeeves"}, headers=_origin(cl)
    )
    assert first.status == 200
    a._sub_cache.clear()
    # ключ уже есть → подтверждение; частота проверяется при самой выдаче
    ask = await cl.post(
        f"/s/{token}/awg", data={"nonce": n2, "node": "jeeves"}, headers=_origin(cl)
    )
    text = await ask.text()
    n3 = text.split('name="nonce" value="', 1)[1].split('"', 1)[0]
    limited = await cl.post(
        f"/s/{token}/awg",
        data={"nonce": n3, "node": "jeeves", "confirm": "1"},
        headers=_origin(cl),
    )
    assert limited.status == 429 and "Подождите минуту" in await limited.text()
    # окно прошло — можно; общий предел на токен — шесть в час
    web._awg_last.clear()
    assert web._rate_ok(token, "jeeves")
    for _i in range(10):
        web._awg_last.clear()
        web._rate_ok(token, "wooster")
    web._awg_last.clear()
    assert not web._rate_ok(token, "wooster")


async def test_awg_not_offered_without_issuer(swarm):
    a, _b, _link = swarm
    web = SubscriptionWeb(a._cfg, a.resolve_subscription)
    cl = TestClient(TestServer(web._app()))
    await cl.start_server()
    try:
        text = await (await cl.get(f"/s/{_token(a)}")).text()
        assert "<summary>AmneziaVPN" not in text
        resp = await cl.post(
            f"/s/{_token(a)}/awg", data={"nonce": "x", "node": "jeeves"}, headers=_origin(cl)
        )
        assert resp.status == 404
    finally:
        await cl.close()


# --- TLS: http как запасной путь, подхват сертификата ---


def _selfsigned(tmp_path, name):
    cert, key = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(cert), "-days", "2", "-subj", "/CN=test"],
        check=True, capture_output=True,
    )  # fmt: skip
    return cert, key


async def test_http_fallback_then_switch_to_https_and_reload(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cfg = VpnConfig(
        sub_port=18455,
        sub_bind="127.0.0.1",
        sub_tls_cert=str(cert),
        sub_tls_key=str(key),
        endpoint_host="198.51.100.1",
    )

    async def resolve(_t):
        return None

    web = SubscriptionWeb(cfg, resolve)
    try:
        assert await web.start() and not web.tls
        assert web.base_url == "http://198.51.100.1:18455"
        c1, k1 = _selfsigned(tmp_path, "one")
        cert.write_bytes(c1.read_bytes())
        key.write_bytes(k1.read_bytes())
        await web.check_tls()
        assert web.tls and web.base_url.startswith("https://")
        first = web._mtimes
        c2, k2 = _selfsigned(tmp_path, "two")
        cert.write_bytes(c2.read_bytes())
        key.write_bytes(k2.read_bytes())
        import os

        os.utime(cert, (first[0] + 5, first[0] + 5))
        await web.check_tls()
        assert web._mtimes != first
        assert isinstance(web._ctx, ssl.SSLContext)
    finally:
        await web.stop()


async def test_http_forbidden_without_cert(tmp_path):
    cfg = VpnConfig(
        sub_port=18456,
        sub_bind="127.0.0.1",
        sub_allow_http=False,
        sub_tls_cert=str(tmp_path / "no.pem"),
        sub_tls_key=str(tmp_path / "no.key"),
    )
    web = SubscriptionWeb(cfg, lambda _t: None)  # type: ignore[arg-type,return-value]
    assert not await web.start() and web.error


async def test_disabled_when_port_zero():
    web = SubscriptionWeb(VpnConfig(sub_port=0), lambda _t: None)  # type: ignore[arg-type,return-value]
    assert not await web.start()


# --- бот: кнопка в карточке устройства ---


def test_card_keyboard_has_hiddify_url_button_first():
    servers = _servers()
    device = vd.build_devices(servers)[0]
    markup = h._card_keyboard_for_device(
        device, servers, subscription=ADMIN, page_url="https://h:8444/s/tok"
    )
    first = markup.inline_keyboard[0][0]
    assert first.text == "📶 Настройки на странице" and first.url == "https://h:8444/s/tok"
    plain = h._card_keyboard_for_device(device, servers, subscription=ADMIN)
    assert all(b.url is None for row in plain.inline_keyboard for b in row)


async def test_subscription_page_url_prefers_https_and_skips_without_vless(monkeypatch):
    servers = _servers()
    device = next(d for d in vd.build_devices(servers) if d.label == LABEL)
    answers = [{"page_url": "http://a/s/t"}, {"page_url": "https://b/s/t"}, {}]

    async def fake_fanout(link, action, args):
        assert action == vpn_protocol.ACTION_GET_SUBSCRIPTION
        assert args == {"chat_id": 5, "device_label": LABEL}
        return answers

    monkeypatch.setattr(vpn_nodes, "fanout", fake_fanout)
    assert await h._subscription_page_url(object(), 5, device) == "https://b/s/t"  # type: ignore[arg-type]
    answers.clear()
    assert await h._subscription_page_url(object(), 5, device) is None  # type: ignore[arg-type]
    assert KEY  # ключ устройства стабилен


# --- фикс ufw ---


def test_sub_ufw_fixup_checks_both_ports(monkeypatch):
    from sa_home_bot.node import fixups

    settings = Settings(vpn=_cfg("198.51.100.1", "x"))
    assert fixups._sub_ports(settings) == ["18444/tcp", "80/tcp"]
    status = "Status: active\n\nTo Action From\n-- ------ ----\n18444/tcp ALLOW Anywhere\n"
    monkeypatch.setattr(fixups, "_ufw_status_text", lambda: status)
    assert not fixups._sub_ufw_check(settings)
    monkeypatch.setattr(fixups, "_ufw_status_text", lambda: status + "80/tcp ALLOW Anywhere\n")
    assert fixups._sub_ufw_check(settings)
    off = Settings(vpn=VpnConfig(sub_port=0))
    assert fixups._sub_ports(off) == []


# --- 57.12: способ подключения от каждой ноды, ключ vpn:// ---


def _node_pair(**kw):
    return (
        subs.NodeInfo("jeeves", "🇳🇱 Нидерланды", "198.51.100.1", base="https://198.51.100.1:8444"),
        subs.NodeInfo("wooster", "🇺🇸 США", "198.51.100.2", base="https://198.51.100.2:8444", **kw),
    )


def test_page_asks_every_node_and_csp_allows_only_them():
    sub = subs.Subscription("d", _sub().entries, nodes=_node_pair())
    page = _page(sub)
    assert '"u": "https://198.51.100.1:8444"' in page and '"u": "https://198.51.100.2:8444"' in page
    assert 'WHERE = "/s/tok/where"' in page and "fetch(n.u + WHERE" in page
    assert "credentials: 'omit'" in page
    csp = subs.page_csp(sub.nodes)
    assert "connect-src 'self' https://198.51.100.1:8444 https://198.51.100.2:8444;" in csp
    assert "default-src 'none'" in csp and "form-action 'self'" in csp
    # узел без адреса страницы не опрашивается
    bare = subs.Subscription("d", _sub().entries, nodes=(subs.NodeInfo("j", "x", "1.1.1.1"),))
    assert subs.check_nodes(bare.nodes) == [] and "connect-src 'self';" in subs.page_csp(bare.nodes)


def test_page_nodes_json_cannot_break_out_of_script():
    nodes = (subs.NodeInfo("j", "</script><b>", "1.1.1.1", base="https://h:1"),)
    page = _page(subs.Subscription("d", _sub().entries, nodes=nodes))
    assert "</script><b>" not in page


def test_detect_via_only_for_own_node():
    nodes = (
        subs.NodeInfo("jeeves", "🇳🇱 Нидерланды", "198.51.100.1", local=True),
        subs.NodeInfo("wooster", "🇺🇸 США", "198.51.100.2"),
    )
    net = "10.9.0.0/29"
    assert subs.detect_via("10.9.0.3", nodes, net) == "awg"
    assert subs.detect_via("198.51.100.1", nodes, net) == "vless"
    assert subs.detect_via("198.51.100.2", nodes, net) == ""  # соседняя нода — не «через меня»
    assert subs.detect_via("203.0.113.9", nodes, net) == ""


async def test_where_endpoint_cors_token_and_answers(awg_swarm):
    cl, a, _b, _web = awg_swarm
    token = _token(a)
    base_b = "https://198.51.100.2:18444"
    # наш origin (страница соседней ноды): ответ + CORS; запрос с 127.0.0.1 = адрес выхода jeeves
    resp = await cl.get(f"/s/{token}/where", headers={"Origin": base_b})
    assert resp.status == 200 and resp.content_type == "application/json"
    assert await resp.json() == {"via": "vless"}
    assert resp.headers["Access-Control-Allow-Origin"] == base_b
    assert resp.headers["Vary"] == "Origin"
    assert "Server" not in resp.headers and resp.headers["Cache-Control"] == "no-store"
    # чужой origin и без origin — ответ тот же, но CORS-разрешения нет
    for hdrs in ({"Origin": "https://evil.example"}, {}):
        r = await cl.get(f"/s/{token}/where", headers=hdrs)
        assert r.status == 200 and "Access-Control-Allow-Origin" not in r.headers
    # из awg-подсети этой ноды — AmneziaWG
    a._cfg.subnet = "127.0.0.0/8"
    assert (await (await cl.get(f"/s/{token}/where")).json()) == {"via": "awg"}
    # запрос приходит не через эту ноду
    a._cfg.subnet = "10.9.0.0/29"
    a._cfg.reality.endpoint_host = a._cfg.endpoint_host = "198.51.100.9"
    a._sub_cache.clear()
    assert (await (await cl.get(f"/s/{token}/where")).json()) == {"via": ""}
    # ничего лишнего в теле
    assert (await (await cl.get(f"/s/{token}/where")).text()) == '{"via": ""}'


async def test_where_unknown_token_and_methods_are_bare_404(awg_swarm):
    cl, a, _b, _web = awg_swarm
    token = _token(a)
    for path in ("/s/" + "A" * 32 + "/where", "/s/short/where"):
        r = await cl.get(path, headers={"Origin": "https://198.51.100.2:18444"})
        assert r.status == 404 and (await r.text()) == "Not found\n"
        assert "Access-Control-Allow-Origin" not in r.headers
    assert (await cl.post(f"/s/{token}/where")).status == 404
    assert (await cl.get(f"/s/{token}/where/x")).status == 404


async def test_web_page_has_node_csp_and_bases(awg_swarm):
    cl, a, _b, _web = awg_swarm
    page = await cl.get(f"/s/{_token(a)}")
    csp = page.headers["Content-Security-Policy"]
    assert "https://127.0.0.1:18444" in csp and "https://198.51.100.2:18444" in csp
    assert "WHERE" in await page.text()


CONF = (
    "[Interface]\nPrivateKey = cHJpdmF0ZUtleUZvclRlc3RzMTIzNDU2Nzg5MDEyMzQ=\n"
    "Address = 10.9.0.3/32\nDNS = 1.1.1.1\nMTU = 1280\nJc = 4\nJmin = 40\nJmax = 70\n"
    "S1 = 15\nS2 = 23\nH1 = 1001\nH2 = 1002\nH3 = 1003\nH4 = 1004\n\n"
    "[Peer]\nPublicKey = c2VydmVyUHVibGljS2V5Rm9yVGVzdHMxMjM0NTY3ODkwMTI=\n"
    "Endpoint = 203.0.113.7:51820\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n"
)


def test_amnezia_key_roundtrip_matches_conf():
    import json
    import zlib

    from sa_home_bot.vpn import amnezia_key as ak

    key = ak.build_key(CONF, "🇳🇱 📱 iPhone")
    assert key.startswith("vpn://") and not set("=+/") & set(key[6:])
    # как в клиенте: base64url -> qUncompress (4 байта длины + zlib) -> JSON
    raw = base64.urlsafe_b64decode(key[6:] + "=" * (-len(key[6:]) % 4))
    body = zlib.decompress(raw[4:])
    assert int.from_bytes(raw[:4], "big") == len(body)
    cfg = json.loads(body)
    assert cfg == ak.decode_key(key)
    assert cfg["description"] == "🇳🇱 📱 iPhone" and cfg["hostName"] == "203.0.113.7"
    assert cfg["defaultContainer"] == "amnezia-awg" and cfg["dns1"] == "1.1.1.1"
    (cont,) = cfg["containers"]
    awg = cont["awg"]
    assert cont["container"] == "amnezia-awg" and awg["isThirdPartyConfig"] is True
    assert awg["port"] == "51820" and awg["transport_proto"] == "udp"
    last = json.loads(awg["last_config"])
    assert last["config"] == CONF  # исходный .conf целиком
    assert last["hostName"] == "203.0.113.7" and last["port"] == 51820
    assert last["client_priv_key"] == "cHJpdmF0ZUtleUZvclRlc3RzMTIzNDU2Nzg5MDEyMzQ="
    assert last["client_ip"] == "10.9.0.3/32"
    assert last["server_pub_key"] == "c2VydmVyUHVibGljS2V5Rm9yVGVzdHMxMjM0NTY3ODkwMTI="
    assert last["allowed_ips"] == ["0.0.0.0/0"] and last["persistent_keep_alive"] == "25"
    assert {k: last[k] for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4")} == {
        "Jc": "4", "Jmin": "40", "Jmax": "70", "S1": "15", "S2": "23",
        "H1": "1001", "H2": "1002", "H3": "1003", "H4": "1004",
    }  # fmt: skip
    assert last["mtu"] == "1280"


def test_amnezia_key_from_real_client_conf_and_errors():
    import pytest as _pytest

    from sa_home_bot.vpn import amnezia_key as ak
    from sa_home_bot.vpn.service import _render_client_conf

    cfg = _cfg("203.0.113.7", "x")
    conf = _render_client_conf(cfg, "PRIV", "10.9.0.4", "SRVPUB")
    parsed = ak.decode_key(ak.build_key(conf, "n"))
    import json

    last = json.loads(parsed["containers"][0]["awg"]["last_config"])
    assert last["config"] == conf and last["client_ip"] == "10.9.0.4/32"
    assert last["Jc"] == str(cfg.jc) and last["H4"] == str(cfg.h4)
    with _pytest.raises(ak.ConfigError):
        ak.build_key("[Interface]\nAddress = 1.2.3.4/32\n", "n")


def test_awg_result_page_key_first_file_under_other_ways():
    sub = subs.Subscription("📱 iPhone", _sub().entries)
    node = subs.NodeInfo("jeeves", "🇳🇱 Нидерланды", "198.51.100.1")
    page = subs.render_awg_result(
        sub, node, filename="a.conf", conf_text="x", qr_data_uri="data:image/svg+xml;base64,AAA",
        links=LINKS, path="/s/tok", key="vpn://AbC_-",
    )  # fmt: skip
    assert "📋 Скопировать ключ" in page and "➕ Добавить в AmneziaVPN" in page
    assert 'value="vpn://AbC_-"' in page and 'href="vpn://AbC_-"' in page
    assert "/Android/" not in page and 'id="addkey" href="vpn://AbC_-">' in page
    assert "«+»" in page and "Вставьте ключ" in page
    assert "play.google.com/amnezia" in page and "apps.apple.com/amnezia" in page
    assert "«🇳🇱 📱 iPhone»" in page and "Проверьте имя подключения" in page
    assert "переименуйте" not in page
    assert page.index("Скопировать ключ") < page.index("Другие способы") < page.index("a.conf")
    no_key = subs.render_awg_result(
        sub, node, filename="a.conf", conf_text="x", qr_data_uri="data:image/svg+xml;base64,AAA",
        links=LINKS, path="/s/tok",
    )  # fmt: skip
    assert "Скопировать ключ" not in no_key and "<details open " in no_key
