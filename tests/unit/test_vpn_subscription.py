"""Подписка Hiddify и https-страница (57.10): токен, сборка, форматы, заголовки,
страница, отказ при отозванном, ходьба по нодам, кнопка в карточке устройства."""

from __future__ import annotations

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


def _cfg(host, location, **over) -> VpnConfig:
    return VpnConfig(
        subnet="10.9.0.0/29",
        base_quota_gb=1,
        endpoint_host=host,
        transports=[TRANSPORT_REALITY],
        location=location,
        reality=RealityTransportConfig(
            endpoint_host=host, server_public_key=f"PUB_{host}", short_id="abcd1234"
        ),
        sub_port=18444,
        **over,
    )


async def _make(tmp_path, name, host, location):
    db = Database(tmp_path / f"{name}.sqlite")
    await db.open()
    await apply_migrations(db)
    settings = Settings(vpn=_cfg(host, location), swarm=SwarmConfig(token=SWARM))
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


def test_page_has_button_copy_hint_qr_and_links():
    page = subs.render_page(
        _sub(),
        sub_url="https://h:8444/sub/tok",
        qr_data_uri="data:image/svg+xml;base64,AAA",
        ios_url="https://apps.apple.com/x",
        android_url="https://play.google.com/x",
        site_url="https://hiddify.com",
    )
    assert "Открыть в Hiddify" in page and "Скопировать ссылку" in page
    assert 'href="hiddify://import/https://h:8444/sub/tok"' in page
    assert "Открыть в браузере" in page and "data:image/svg+xml" in page
    assert "🇳🇱 Нидерланды" in page and "apps.apple.com" in page and "play.google.com" in page
    assert "VPN · 📱 iPhone" in page


def test_page_escapes_label():
    sub = subs.Subscription("<script>x</script>", _sub().entries)
    page = subs.render_page(
        sub, sub_url="https://h/sub/t", qr_data_uri="d", ios_url="i", android_url="a", site_url="s"
    )
    assert "<script>x</script>" not in page


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
    assert "Открыть в Hiddify" in text and f"/sub/{token}" in text


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
    assert first.text == "🔌 Подключить в Hiddify" and first.url == "https://h:8444/s/tok"
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
