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
from sa_home_bot.bot import vpn_nodes, vpn_screens
from sa_home_bot.bot.handlers.vpn import screens as vpn_screens_handlers
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import RealityTransportConfig, Settings, SwarmConfig, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.reality.client_config import RealityParams
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn import subweb
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
    for value in headers.values():
        value.encode("latin-1")
    sb = subs.render_headers(
        _sub(), page_url="https://h/s/t", update_interval_h=12, fmt=subs.FORMAT_SINGBOX
    )
    assert base64.b64decode(sb["profile-title"][7:]).decode() == "VPN · 📱 iPhone · маршруты"


LINKS = subs.PageLinks(
    hiddify_ios="https://apps.apple.com/x",
    hiddify_android="https://play.google.com/x",
    hiddify_site="https://hiddify.com",
    amnezia_ios="https://apps.apple.com/amnezia",
    amnezia_android="https://play.google.com/amnezia",
    amnezia_site="https://amnezia.org",
)


CHECK = "нажмите «🔄 Проверить ещё раз» — должно загореться зелёное «✅ VPN включён»"


def _page(sub=None, **kw):
    return subs.render_page(
        sub or _sub(),
        sub_url="https://h:8444/sub/tok",
        qr_data_uri="data:image/svg+xml;base64,AAA",
        links=LINKS,
        path="/s/tok",
        **kw,
    )


def test_page_hiddify_one_button_others_folded_no_autoopen():
    page = _page()
    assert "📶 VPN · 📱 iPhone" in page and "VLESS · Hiddify" in page
    assert 'id="add" href="hiddify://import/https://h:8444/sub/tok?format=singbox"' in page
    assert 'href="hiddify://import/https://h:8444/sub/tok"' not in page
    assert "➕ Добавить в Hiddify" in page and "🧪" not in page
    assert "В Hiddify нажмите большую кнопку «Нажмите для подключения»" in page
    assert "Убедитесь, что установлено приложение <b>Hiddify</b>" in page
    assert "apps.apple.com/x" in page and "play.google.com/x" not in page
    assert f"{CHECK} и «Способ: VLESS · Hiddify»" in page
    other = page.index("Другие способы")
    for marker in ("📋 Скопировать ссылку", "data:image/svg+xml", "⬇️ Файл настроек"):
        assert page.index(marker) > other
    assert 'value="https://h:8444/sub/tok?format=singbox"' in page
    assert "localStorage" not in page and "setTimeout(function () { location" not in page
    assert "AUTO" not in page and "autoOpen" not in page
    assert "http://" not in page and 'src="http' not in page


def test_page_status_blocks_green_when_on():
    off = _page(status=subs.VpnStatus(False))
    assert "❌ VPN сейчас выключен" in off and "🔄 Проверить ещё раз" in off
    assert 'class="card" id="stcard"' in off
    on = _page(status=subs.VpnStatus(True, "🇳🇱 Нидерланды", subs.METHOD_AWG))
    assert "✅ VPN включён — 🇳🇱 Нидерланды" in on and "Способ: AmneziaVPN" in on
    assert 'class="card ok" id="stcard"' in on and ".card.ok" in on
    assert "stcard.className = 'card' + (cls === 'on' ? ' ok' : '')" in on


def test_page_countries_health_and_remaining():
    sub = subs.Subscription(
        "d",
        _sub().entries,
        used_bytes=13 * 10**9,
        total_bytes=100 * 10**9,
        expire_ts=1_793_491_200,
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


def _awg_sub(have=()):
    nodes = (
        subs.NodeInfo(
            "jeeves", "🇳🇱 Нидерланды", "198.51.100.1", awg=True, awg_key="jeeves" in have
        ),
        subs.NodeInfo("wooster", "🇺🇸 США", "198.51.100.2", awg=True, awg_key="wooster" in have),
    )
    return subs.Subscription("d", _sub().entries, nodes=nodes)


FORMS = {"jeeves": "N1", "wooster": "N2"}


def test_page_awg_block_collapsed_cells_per_country_no_forms():
    page = _page(_awg_sub(), awg_forms=FORMS)
    assert '<details id="awg" open><summary>AmneziaVPN — основной способ, быстрее' in page
    assert 'data-node="jeeves"' in page and 'data-nonce="N1"' in page and 'data-have="0"' in page
    assert "<form" not in page and "/awg" in page
    assert "🔑 Сгенерировать ключ 🇳🇱" in page and "🔑 Сгенерировать ключ 🇺🇸" in page
    assert "AmneziaVPN — основной" not in _page(_awg_sub())


def test_page_awg_existing_key_shows_reissue_button():
    page = _page(_awg_sub(have=("jeeves",)), awg_forms=FORMS)
    assert "🔄 Перевыпустить 🇳🇱" in page and "показать его нельзя" in page
    assert 'data-node="jeeves" data-flag="🇳🇱" data-have="1"' in page
    assert 'data-node="wooster" data-flag="🇺🇸" data-have="0"' in page
    assert "Старый ключ перестанет работать — нажмите ещё раз" in page


def test_detect_platform_by_user_agent():
    android = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/120 Mobile"
    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605 Safari"
    ipad = "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605"
    desktop = "Mozilla/5.0 (X11; Linux x86_64) Firefox/120.0"
    assert subs.detect_platform(android) == "android"
    assert subs.detect_platform(iphone) == subs.detect_platform(ipad) == "ios"
    assert subs.detect_platform(desktop) == subs.detect_platform("") == "other"


def test_page_awg_layout_per_platform():
    sub = _awg_sub()
    android = _page(sub, awg_forms=FORMS, platform="android")
    ios = _page(sub, awg_forms=FORMS, platform="ios")
    other = _page(sub, awg_forms=FORMS, platform="other")
    for page in (android, ios, other):
        assert (
            "Убедитесь, что установлено именно приложение <b>AmneziaVPN</b> (не AmneziaWG)" in page
        )
        assert "без него ключ не заработает" in page
        assert "play.google.com/amnezia" in page and "apps.apple.com/amnezia" in page
        assert f"{CHECK} и «Способ: AmneziaVPN»" in page
        assert "без него выпуск ключа может не сработать" in page
        assert "«🔑 Сгенерировать ключ»" in page and "«🔄 Перевыпустить» (два раза)" in page
        assert "В AmneziaVPN нажмите «Подключиться»" in page
    assert "Только после этого появится кнопка «➕ Подключить»" in android
    assert "Только после этого появится кнопка «📋 Скопировать»" in ios
    assert 'PLATFORM = "android"' in android and "«➕ Подключить»" in android
    assert "Вставьте ключ" not in android
    assert 'PLATFORM = "ios"' in ios and "«📋 Скопировать»" in ios
    assert "«+», в поле «Вставьте ключ» — «Вставить», затем «Продолжить»" in ios
    assert "«➕ Подключить»" not in ios and "Добавить в AmneziaVPN" not in ios
    assert 'PLATFORM = "other"' in other and "«➕ Подключить»" in other
    assert "«📋 Скопировать»" in other and ".conf" in other


def test_page_scripts_are_valid_js():
    esprima = pytest.importorskip("esprima")
    page = _page(_awg_sub(have=("jeeves",)), awg_forms=FORMS, platform="ios")
    scripts = page.split("<script>\n", 1)[1].rsplit("\n</script>", 1)[0]
    esprima.parseScript(scripts)


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
    assert peer == subs.VpnStatus(True, "🇺🇸 США", "")
    assert subs.detect_status("203.0.113.9", nodes, net) == subs.VpnStatus(False)
    assert subs.detect_status("garbage", nodes, net) == subs.VpnStatus(False)
    assert subs.detect_status("", nodes, net) == subs.VpnStatus(False)


def test_token_carries_generation_and_rejects_forgery():
    secret = subs.derive_secret("", SWARM)
    t0, t1 = subs.make_token(secret, 5, "d"), subs.make_token(secret, 5, "d", 1_700_000_000)
    assert t0 != t1 and subs.token_gen(t1) == 1_700_000_000 and subs.token_gen(t0) == 0
    assert subs.token_matches(secret, t1, 5, "d")
    forged = subs.make_token(secret, 5, "d", 0)
    raw = base64.urlsafe_b64decode(forged + "==")
    raw = (1).to_bytes(4, "big") + raw[4:]
    assert not subs.token_matches(
        secret, base64.urlsafe_b64encode(raw).decode().rstrip("="), 5, "d"
    )


async def test_get_subscription_requires_running_web(swarm):
    a, _b, _link = swarm
    with pytest.raises(ProtoError):
        await a.run_command(
            vpn_protocol.ACTION_GET_SUBSCRIPTION, {"chat_id": CHAT, "device_label": "📱 iPhone"}
        )


async def test_get_subscription_returns_urls(swarm):
    a, _b, _link = swarm
    a.sub_web = SubscriptionWeb(a._cfg, a.resolve_subscription)
    a.sub_web._runner = object()  # type: ignore[assignment]
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
    assert token == _token(b)
    for svc in (a, b):
        sub = await svc.resolve_subscription(token)
        assert sub is not None
        assert [e.name for e in sub.entries] == ["🇳🇱 Нидерланды", "🇺🇸 США"]
        assert sub.device_label == "📱 iPhone"
        assert sub.total_bytes == 2 * 10**9


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


async def _reissue(svc, label="📱 iPhone", transport=TRANSPORT_REALITY):
    await asyncio.sleep(1.1)
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
    assert old == await _fresh_token(b) == _token(a)
    for svc in (a, b):
        assert await svc.resolve_subscription(old) is not None
    await _reissue(b)
    new = await _fresh_token(a)
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
    a, b, _link = webswarm
    await _reissue(a)
    token = await _fresh_token(a)
    fresh = VpnService(
        Settings(vpn=_cfg("198.51.100.1", "🇳🇱 Нидерланды"), swarm=SwarmConfig(token=SWARM)),
        a._db, FakeAwg(), _noop, reality_backend=FakeXray(),
    )  # fmt: skip
    fresh._node_id = "jeeves"
    fresh._node_link = a._node_link
    assert await fresh.resolve_subscription(token) is not None
    assert await fresh.resolve_subscription(_token(a)) is None


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
    await b.run_command(vpn_protocol.ACTION_ISSUE, args)
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
    marker = f'data-node="{node}"'
    chunk = text[text.index(marker) :]
    return chunk.split('data-nonce="', 1)[1].split('"', 1)[0]


async def _awg(cl, token, node, action="issue", nonce=None, headers=None):
    nonce = nonce if nonce is not None else await _form_nonce(cl, token, node)
    resp = await cl.post(
        f"/s/{token}/awg",
        data={"nonce": nonce, "node": node, "action": action},
        headers=headers or _origin(cl),
    )
    return resp


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
    assert (await cl.post(f"/s/{token}")).status == 404
    assert (await cl.put(f"/sub/{token}")).status == 404
    assert (await cl.post("/s/" + "A" * 32 + "/awg", headers=_origin(cl))).status == 404


async def test_web_status_by_request_address(awg_swarm):
    cl, a, _b, _web = awg_swarm
    token = _token(a)
    on = await (await cl.get(f"/s/{token}")).text()
    assert "✅ VPN включён — 🇳🇱 Нидерланды" in on and "VLESS · Hiddify" in on
    a._cfg.reality.endpoint_host = "198.51.100.9"
    a._cfg.endpoint_host = "198.51.100.9"
    a._sub_cache.clear()
    off = await (await cl.get(f"/s/{token}")).text()
    assert "❌ VPN сейчас выключен" in off and 'class="status on"' not in off


async def test_get_page_issues_nothing_and_never_autoopens(awg_swarm):
    cl, a, b, _web = awg_swarm
    token = _token(a)
    for _ in range(3):
        text = await (await cl.get(f"/s/{token}")).text()
        assert '="vpn://' not in text and "AUTO" not in text
    for svc in (a, b):
        assert await svc._active_row(CHAT, "📱 iPhone", vpn_protocol.TRANSPORT_AWG) is None


async def test_page_layout_by_user_agent_over_http(awg_swarm):
    cl, a, _b, _web = awg_swarm
    token = _token(a)
    ua_android = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120 Mobile"}
    ua_iphone = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}
    android = await (await cl.get(f"/s/{token}", headers=ua_android)).text()
    ios = await (await cl.get(f"/s/{token}", headers=ua_iphone)).text()
    other = await (await cl.get(f"/s/{token}")).text()
    assert 'PLATFORM = "android"' in android and 'PLATFORM = "ios"' in ios
    assert 'PLATFORM = "other"' in other


async def test_awg_post_requires_origin_and_nonce(awg_swarm):
    cl, a, _b, _web = awg_swarm
    token = _token(a)
    nonce = await _form_nonce(cl, token, "jeeves")
    data = {"nonce": nonce, "node": "jeeves", "action": "issue"}
    assert (await cl.post(f"/s/{token}/awg", data=data)).status == 404
    evil = {"Origin": "http://evil.example"}
    assert (await cl.post(f"/s/{token}/awg", data=data, headers=evil)).status == 404
    bad = await _awg(cl, token, "jeeves", nonce="x")
    assert bad.status == 400 and (await bad.json()) == {"ok": False, "error": "stale"}
    assert (await _awg(cl, token, "nowhere", nonce=nonce)).status == 404
    assert (await _awg(cl, token, "jeeves", action="drop", nonce=nonce)).status == 404
    ref = {"Referer": f"http://{cl.server.host}:{cl.server.port}/s/{token}"}
    ok = await cl.post(f"/s/{token}/awg", data=data, headers=ref)
    assert ok.status == 200
    again = await cl.post(f"/s/{token}/awg", data=data, headers=_origin(cl))
    assert again.status == 400


async def test_awg_post_issues_missing_keys_local_and_remote_json_no_store(awg_swarm):
    cl, a, b, _web = awg_swarm
    token = _token(a)
    for node, svc, name in (("jeeves", a, "awg_nl_"), ("wooster", b, "awg_us_")):
        resp = await _awg(cl, token, node)
        assert resp.status == 200
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["Content-Type"].startswith("application/json")
        j = await resp.json()
        assert j["ok"] and j["key"].startswith("vpn://") and j["filename"].startswith(name)
        assert "[Interface]" in j["conf"] and j["qr"].startswith("data:image/svg+xml")
        assert j["nonce"]
        row = await svc._active_row(CHAT, "📱 iPhone", vpn_protocol.TRANSPORT_AWG)
        assert row is not None
    a._sub_cache.clear()
    text = await (await cl.get(f"/s/{token}")).text()
    assert text.count('data-have="1"') == 2 and '="vpn://' not in text


async def test_awg_issue_never_replaces_existing_key_reissue_does(awg_swarm):
    cl, a, _b, web = awg_swarm
    token = _token(a)
    await a.run_command(
        vpn_protocol.ACTION_ISSUE,
        {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": vpn_protocol.TRANSPORT_AWG},
    )
    old = await a._active_row(CHAT, "📱 iPhone", vpn_protocol.TRANSPORT_AWG)
    nonce = await _form_nonce(cl, token, "jeeves")
    exists = await _awg(cl, token, "jeeves", "issue", nonce=nonce)
    j = await exists.json()
    assert exists.status == 409 and j["error"] == "exists" and j["nonce"]
    assert "key" not in j
    assert (await a._active_row(CHAT, "📱 iPhone", vpn_protocol.TRANSPORT_AWG))["public_key"] == (
        old["public_key"]
    )
    done = await _awg(cl, token, "jeeves", "reissue", nonce=j["nonce"])
    dj = await done.json()
    assert done.status == 200 and dj["ok"] and dj["key"].startswith("vpn://")
    new = await a._active_row(CHAT, "📱 iPhone", vpn_protocol.TRANSPORT_AWG)
    assert new["public_key"] != old["public_key"]
    assert web._issue_awg is not None


async def test_awg_rate_limits_allow_issue_two_countries_and_reissues(awg_swarm):
    cl, a, _b, web = awg_swarm
    token = _token(a)
    first = await _awg(cl, token, "jeeves")
    nonce = (await first.json())["nonce"]
    limited = await _awg(cl, token, "jeeves", "reissue", nonce=nonce)
    lj = await limited.json()
    assert limited.status == 429 and lj["error"] == "rate" and lj["nonce"]
    assert (await _awg(cl, token, "wooster")).status == 200
    web._awg_last.clear()
    a._sub_cache.clear()
    assert (await _awg(cl, token, "jeeves", "reissue", nonce=lj["nonce"])).status == 200
    web._awg_last.clear()
    web._awg_by_token.clear()
    for _i in range(subweb.AWG_PER_TOKEN[0]):
        web._awg_last.clear()
        assert web._rate_ok(token, "wooster")
    web._awg_last.clear()
    assert not web._rate_ok(token, "wooster")


async def test_awg_offered_where_device_has_no_vless(awg_swarm):
    cl, a, b, _web = awg_swarm
    token = _token(a)
    await b.run_command(
        vpn_protocol.ACTION_REVOKE,
        {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": TRANSPORT_REALITY},
    )
    await b.run_command(
        vpn_protocol.ACTION_ISSUE,
        {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": vpn_protocol.TRANSPORT_AWG},
    )
    a._sub_cache.clear()
    text = await (await cl.get(f"/s/{token}")).text()
    assert 'data-node="jeeves"' in text and 'data-node="wooster"' in text
    wooster = text[text.index('data-node="wooster"') :]
    assert 'data-have="1"' in wooster.split("</div>", 1)[0]
    exists = await _awg(cl, token, "wooster")
    assert exists.status == 409
    await b.run_command(
        vpn_protocol.ACTION_REVOKE,
        {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": vpn_protocol.TRANSPORT_AWG},
    )
    a._sub_cache.clear()
    resp = await _awg(cl, token, "wooster")
    assert resp.status == 200 and (await resp.json())["key"].startswith("vpn://")
    assert await b._active_row(CHAT, "📱 iPhone", vpn_protocol.TRANSPORT_AWG) is not None


async def test_sub_links_hint_must_match_token_signature(awg_swarm):
    _cl, a, b, _web = awg_swarm
    token = _token(a)
    await b.run_command(
        vpn_protocol.ACTION_REVOKE,
        {"chat_id": CHAT, "device_label": "📱 iPhone", "transport": TRANSPORT_REALITY},
    )
    for chat_id, label in ((CHAT + 1, "📱 iPhone"), (CHAT, "чужое"), (0, "📱 iPhone")):
        reply = await b._sub_links({"token": token, "chat_id": chat_id, "device_label": label})
        assert not reply["found"] and not reply["info"].get("device")
    ok = await b._sub_links({"token": token, "chat_id": CHAT, "device_label": "📱 iPhone"})
    assert not ok["found"] and ok["info"]["device"] and not ok["info"]["awg_key"]


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


def test_card_keyboard_has_hiddify_url_button_first():
    servers = _servers()
    device = vd.build_devices(servers)[0]
    markup = vpn_screens.device_card_keyboard(
        device, servers, subscription=ADMIN, page_url="https://h:8444/s/tok"
    )
    first = markup.inline_keyboard[0][0]
    assert first.text == "📶 Настройки на странице" and first.url == "https://h:8444/s/tok"
    plain = vpn_screens.device_card_keyboard(device, servers, subscription=ADMIN)
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
    assert await vpn_screens_handlers.subscription_page_url(object(), 5, device) == "https://b/s/t"  # type: ignore[arg-type]
    answers.clear()
    assert await vpn_screens_handlers.subscription_page_url(object(), 5, device) is None  # type: ignore[arg-type]
    assert KEY


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
    assert subs.detect_via("198.51.100.2", nodes, net) == ""
    assert subs.detect_via("203.0.113.9", nodes, net) == ""


async def test_where_endpoint_cors_token_and_answers(awg_swarm):
    cl, a, _b, _web = awg_swarm
    token = _token(a)
    base_b = "https://198.51.100.2:18444"
    resp = await cl.get(f"/s/{token}/where", headers={"Origin": base_b})
    assert resp.status == 200 and resp.content_type == "application/json"
    assert await resp.json() == {"via": "vless"}
    assert resp.headers["Access-Control-Allow-Origin"] == base_b
    assert resp.headers["Vary"] == "Origin"
    assert "Server" not in resp.headers and resp.headers["Cache-Control"] == "no-store"
    for hdrs in ({"Origin": "https://evil.example"}, {}):
        r = await cl.get(f"/s/{token}/where", headers=hdrs)
        assert r.status == 200 and "Access-Control-Allow-Origin" not in r.headers
    a._cfg.subnet = "127.0.0.0/8"
    assert (await (await cl.get(f"/s/{token}/where")).json()) == {"via": "awg"}
    a._cfg.subnet = "10.9.0.0/29"
    a._cfg.reality.endpoint_host = a._cfg.endpoint_host = "198.51.100.9"
    a._sub_cache.clear()
    assert (await (await cl.get(f"/s/{token}/where")).json()) == {"via": ""}
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


def _decode_amnezia_key(key: str) -> dict:
    import json
    import zlib

    body = key.removeprefix("vpn://")
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    return json.loads(zlib.decompress(raw[4:]))


def test_amnezia_key_roundtrip_matches_conf():
    import json
    import zlib

    from sa_home_bot.vpn import amnezia_key as ak

    key = ak.build_key(CONF, "🇳🇱 📱 iPhone")
    assert key.startswith("vpn://") and not set("=+/") & set(key[6:])
    raw = base64.urlsafe_b64decode(key[6:] + "=" * (-len(key[6:]) % 4))
    body = zlib.decompress(raw[4:])
    assert int.from_bytes(raw[:4], "big") == len(body)
    cfg = json.loads(body)
    assert cfg["description"] == "🇳🇱 📱 iPhone" and cfg["hostName"] == "203.0.113.7"
    assert cfg["defaultContainer"] == "amnezia-awg" and cfg["dns1"] == "1.1.1.1"
    (cont,) = cfg["containers"]
    awg = cont["awg"]
    assert cont["container"] == "amnezia-awg" and awg["isThirdPartyConfig"] is True
    assert awg["port"] == "51820" and awg["transport_proto"] == "udp"
    last = json.loads(awg["last_config"])
    assert last["config"] == CONF
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
    from sa_home_bot.vpn.awg_conf import render as render_client_conf

    cfg = _cfg("203.0.113.7", "x")
    conf = render_client_conf(cfg, "PRIV", "10.9.0.4", "SRVPUB")
    parsed = _decode_amnezia_key(ak.build_key(conf, "n"))
    import json

    last = json.loads(parsed["containers"][0]["awg"]["last_config"])
    assert last["config"] == conf and last["client_ip"] == "10.9.0.4/32"
    assert last["Jc"] == str(cfg.jc) and last["H4"] == str(cfg.h4)
    with _pytest.raises(ak.ConfigError):
        ak.build_key("[Interface]\nAddress = 1.2.3.4/32\n", "n")


def test_page_order_hiddify_first_amnezia_main_no_autoissue():
    page = _page(_awg_sub(), awg_forms=FORMS, status=subs.VpnStatus(False))
    assert (
        page.index('id="stcard"')
        < page.index("VLESS · Hiddify — резервный способ")
        < page.index("AmneziaVPN — основной способ, быстрее")
    )
    assert "Поставьте его первым" in page and "Рекомендуем пользоваться им" in page
    assert "Не выключая Hiddify, переходите к AmneziaVPN ниже" in page
    assert "переходите к AmneziaVPN" not in _page()
    assert "toggle" not in page
    plain = _page()
    assert "резервный способ" not in plain and "Поставьте его первым" not in plain


def test_page_other_ways_have_store_qrs_and_open_awg_alternatives():
    page = _page(_awg_sub(), awg_forms=FORMS)
    assert "Скачать Hiddify на телефон — наведите камеру на QR" in page
    assert "Скачать AmneziaVPN на телефон — наведите камеру на QR" in page
    assert 'alt="QR-код: Hiddify в Google Play"' in page
    assert 'alt="QR-код: AmneziaVPN в App Store"' in page
    assert "в Hiddify «+» &rarr; «Сканировать QR-код»" in page
    assert '<details id="awgmore"><summary' in page and 'id="awgmore" hidden' not in page
    assert page.index("Скачать AmneziaVPN") > page.index('id="awgmore"')
    assert "QR-код подключения появятся здесь" in page
    assert "Скачать AmneziaVPN" not in _page()


def _labeled(label):
    return subs.Subscription(label, _sub().entries, nodes=_awg_sub().nodes)


def test_page_links_and_qrs_only_for_the_device_being_connected():
    android = _page(_labeled("🤖 Peony"), awg_forms=FORMS, platform="other")
    assert "play.google.com/x" in android and "play.google.com/amnezia" in android
    assert "apps.apple.com" not in android and "hiddify.com" not in android
    assert 'alt="QR-код: AmneziaVPN в Google Play"' in android
    assert "Только после этого появится кнопка «➕ Подключить»" in android
    ios = _page(_labeled("📱 iPhone"), awg_forms=FORMS, platform="other")
    assert "apps.apple.com/amnezia" in ios and "play.google.com" not in ios
    assert "Только после этого появится кнопка «📋 Скопировать»" in ios
    desktop = _page(_labeled("💻 Rose"), awg_forms=FORMS, platform="android")
    assert "hiddify.com" in desktop and "amnezia.org" in desktop
    assert "play.google.com" not in desktop and "apps.apple.com" not in desktop
    assert "наведите камеру на QR" not in desktop and "Файл .conf" in desktop
    old = _page(_labeled("Rose"), awg_forms=FORMS, platform="ios")
    assert "play.google.com/x" in old and "apps.apple.com/x" in old and "hiddify.com" in old


def test_page_hints_when_app_does_not_open():
    page = _page(_awg_sub(), awg_forms=FORMS, platform="android")
    assert page.index("Если Hiddify не открылся — проверьте ещё раз") > page.index('id="add"')
    assert "Если AmneziaVPN не открылся — проверьте ещё раз, что приложение установлено" in page
