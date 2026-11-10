from __future__ import annotations

import asyncio
import json

from sa_home_bot.config import RealityTransportConfig
from sa_home_bot.reality.client_config import RealityParams
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.protocol import PROBE_CHAT_ID


def _params(port: int = 8443, sni: str = "www.google.com") -> RealityParams:
    return RealityParams("198.51.100.1", port, "PUB1", "ab", sni)


def _entry(
    name: str = "🇳🇱 Нидерланды",
    is_alt: bool = False,
    port: int = 8443,
    sni: str = "www.google.com",
) -> subs.SubEntry:
    return subs.SubEntry(
        node="jeeves",
        name=name,
        uuid="uuid-nl",
        params=_params(port=port, sni=sni),
        is_alt=is_alt,
    )


def _sub(entries: list[subs.SubEntry]) -> subs.Subscription:
    return subs.Subscription(
        "📱 iPhone",
        tuple(subs.sort_entries(entries)),
        5_000,
        200_000,
        1_800_000_000,
    )


def test_subentry_to_wire_without_alt_has_no_is_alt():
    e = _entry()
    wire = e.to_wire()
    assert "is_alt" not in wire


def test_subentry_to_wire_with_alt_has_is_alt_true():
    e = _entry(is_alt=True)
    wire = e.to_wire()
    assert wire["is_alt"] is True


def test_subentry_from_wire_roundtrip_preserves_is_alt():
    e = _entry(is_alt=True)
    wire = e.to_wire()
    parsed = subs.SubEntry.from_wire(wire)
    assert parsed.is_alt is True
    assert parsed.name == e.name
    assert parsed.params.port == e.params.port


def test_sort_entries_alt_after_regular():
    entries = [
        _entry("🇺🇸 США", is_alt=True),
        _entry("🇳🇱 Нидерланды", is_alt=False),
        _entry("🇺🇸 США", is_alt=False),
        _entry("🇳🇱 Нидерланды", is_alt=True),
    ]
    sorted_entries = subs.sort_entries(entries)
    assert [e.name for e in sorted_entries] == [
        "🇳🇱 Нидерланды",
        "🇺🇸 США",
        "🇳🇱 Нидерланды",
        "🇺🇸 США",
    ]
    assert [e.is_alt for e in sorted_entries] == [False, False, True, True]


def test_expand_spares_with_alt_creates_spares_only_for_alt():
    entries = [
        _entry("🇳🇱 Нидерланды", is_alt=False, port=8443),
        _entry("🇳🇱 Нидерланды · 2", is_alt=True, port=2083),
    ]
    result = subs.expand_spares(entries, ["firefox"])
    assert len(result) == 3
    assert result[0].name == "🇳🇱 Нидерланды"
    assert result[0].params.fingerprint == "chrome"
    assert result[1].name == "🇳🇱 Нидерланды · 2"
    assert result[1].params.fingerprint == "chrome"
    assert result[2].name == "🇳🇱 Нидерланды · 2 · firefox"
    assert result[2].params.fingerprint == "firefox"
    assert result[2].params.port == 2083


def test_expand_spares_without_alt_creates_spares_for_all():
    entries = [
        _entry("🇳🇱 Нидерланды", is_alt=False, port=8443),
        _entry("🇺🇸 США", is_alt=False, port=8443),
    ]
    result = subs.expand_spares(entries, ["firefox"])
    assert len(result) == 4
    assert result[0].name == "🇳🇱 Нидерланды"
    assert result[1].name == "🇺🇸 США"
    assert result[2].name == "🇳🇱 Нидерланды · firefox"
    assert result[3].name == "🇺🇸 США · firefox"


def test_render_plain_with_alt_contains_both_ports_and_sni():
    entries = [
        _entry("🇳🇱 Нидерланды", is_alt=False, port=8443),
        _entry("🇳🇱 Нидерланды", is_alt=True, port=2083, sni="www.cloudflare.com"),
    ]
    sub = _sub(entries)
    body, _ = subs.render_body(sub, subs.FORMAT_PLAIN)
    lines = body.splitlines()
    assert len(lines) == 2
    assert "198.51.100.1:8443" in lines[0]
    assert "www.google.com" in lines[0]
    assert "198.51.100.1:2083" in lines[1]
    assert "www.cloudflare.com" in lines[1]


def test_render_vless_with_alt_contains_both_ports_and_sni():
    entries = [
        _entry("🇳🇱 Нидерланды", is_alt=False, port=8443),
        _entry("🇳🇱 Нидерланды", is_alt=True, port=2083, sni="www.cloudflare.com"),
    ]
    sub = _sub(entries)
    body, _ = subs.render_body(sub, subs.FORMAT_VLESS)
    import base64
    lines = base64.b64decode(body).decode().splitlines()
    assert len(lines) == 2
    assert "198.51.100.1:8443" in lines[0]
    assert "www.google.com" in lines[0]
    assert "198.51.100.1:2083" in lines[1]
    assert "www.cloudflare.com" in lines[1]


def test_render_singbox_with_alt_contains_both_ports_and_sni():
    entries = [
        _entry("🇳🇱 Нидерланды", is_alt=False, port=8443),
        _entry("🇳🇱 Нидерланды", is_alt=True, port=2083, sni="www.cloudflare.com"),
    ]
    sub = _sub(entries)
    body, _ = subs.render_body(sub, subs.FORMAT_SINGBOX)
    cfg = json.loads(body)
    tags = {o["tag"]: o for o in cfg["outbounds"]}
    assert "🇳🇱 Нидерланды" in tags
    assert "🇳🇱 Нидерланды" in tags
    ports = {o["server_port"] for o in cfg["outbounds"] if o["type"] == "vless"}
    assert 8443 in ports
    assert 2083 in ports
    snis = {o["tls"]["server_name"] for o in cfg["outbounds"] if o["type"] == "vless"}
    assert "www.google.com" in snis
    assert "www.cloudflare.com" in snis


def test_render_without_alt_equals_old_behavior():
    entries = [_entry("🇳🇱 Нидерланды", is_alt=False, port=8443)]
    sub = _sub(entries)
    body_plain, _ = subs.render_body(sub, subs.FORMAT_PLAIN)
    body_vless, _ = subs.render_body(sub, subs.FORMAT_VLESS)
    body_singbox, _ = subs.render_body(sub, subs.FORMAT_SINGBOX)

    entries_old = [
        subs.SubEntry(
            "jeeves",
            "🇳🇱 Нидерланды",
            "uuid-nl",
            RealityParams("198.51.100.1", 8443, "PUB1", "ab", "www.google.com"),
        )
    ]
    sub_old = subs.Subscription(
        "📱 iPhone", tuple(subs.sort_entries(entries_old)), 5_000, 200_000, 1_800_000_000
    )
    body_plain_old, _ = subs.render_body(sub_old, subs.FORMAT_PLAIN)
    body_vless_old, _ = subs.render_body(sub_old, subs.FORMAT_VLESS)
    body_singbox_old, _ = subs.render_body(sub_old, subs.FORMAT_SINGBOX)

    assert body_plain == body_plain_old
    assert body_vless == body_vless_old
    assert body_singbox == body_singbox_old


def test_sub_links_with_alt_returns_entries_and_entry():
    from sa_home_bot.config import RealityAltConfig
    from sa_home_bot.vpn.subscription_links import SubscriptionLinksMixin

    class FakeMixin(SubscriptionLinksMixin):
        _node_id = "jeeves"
        _cfg = type("obj", (), {"location": "🇳🇱 Нидерланды", "sub_port": 0})()
        _reality_cfg = RealityTransportConfig(
            endpoint_host="198.51.100.1",
            server_public_key="PUB1",
            short_id="abcd1234",
            sni="www.google.com",
            alt=RealityAltConfig(port=2083, sni="www.cloudflare.com", inbound_tag="reality-alt"),
        )
        _sub_secret = b"secret"

        def _reality_params(self):
            return RealityParams.from_config(self._reality_cfg)

        def _reality_alt_params(self):
            return RealityParams.alt_from_config(self._reality_cfg)

        def _sub_entry_name(self):
            return self._cfg.location

        def _node_ip(self):
            return "198.51.100.1"

        def _sub_base(self):
            return ""

        def _has(self, _):
            return False

        async def _sub_local(self, token):
            return (123, "📱 iPhone", "uuid-nl")

        async def _device_gen(self, chat_id, label):
            return 0

        async def _active_row(self, chat_id, label, transport):
            return None

        async def _sub_health(self):
            return "ok"

        async def _used_bytes(self, chat_id, month):
            return 0

        async def _limit_bytes(self, chat_id, month):
            return 0

    import asyncio

    mixin = FakeMixin()
    result = asyncio.run(mixin._sub_links({"token": subs.make_token(b"secret", 123, "📱 iPhone")}))
    assert result["found"] is True
    assert "entry" in result
    assert "entries" in result
    assert len(result["entries"]) == 2
    assert result["entry"]["name"] == "🇳🇱 Нидерланды"
    assert result["entries"][0]["name"] == "🇳🇱 Нидерланды"
    assert result["entries"][1]["name"] == "🇳🇱 Нидерланды · 2"
    assert result["entries"][1]["is_alt"] is True


def test_sub_links_without_alt_has_no_entries_key():
    from sa_home_bot.vpn.subscription_links import SubscriptionLinksMixin

    class FakeMixin(SubscriptionLinksMixin):
        _node_id = "jeeves"
        _cfg = type("obj", (), {"location": "🇳🇱 Нидерланды", "sub_port": 0})()
        _reality_cfg = RealityTransportConfig(
            endpoint_host="198.51.100.1",
            server_public_key="PUB1",
            short_id="abcd1234",
            sni="www.google.com",
            alt=None,
        )
        _sub_secret = b"secret"

        def _reality_params(self):
            return RealityParams.from_config(self._reality_cfg)

        def _reality_alt_params(self):
            return RealityParams.alt_from_config(self._reality_cfg)

        def _sub_entry_name(self):
            return self._cfg.location

        def _node_ip(self):
            return "198.51.100.1"

        def _sub_base(self):
            return ""

        def _has(self, _):
            return False

        async def _sub_local(self, token):
            return (123, "📱 iPhone", "uuid-nl")

        async def _device_gen(self, chat_id, label):
            return 0

        async def _active_row(self, chat_id, label, transport):
            return None

        async def _sub_health(self):
            return "ok"

        async def _used_bytes(self, chat_id, month):
            return 0

        async def _limit_bytes(self, chat_id, month):
            return 0

    import asyncio

    mixin = FakeMixin()
    result = asyncio.run(mixin._sub_links({"token": subs.make_token(b"secret", 123, "📱 iPhone")}))
    assert result["found"] is True
    assert "entry" in result
    assert "entries" not in result


def _resolve_with_reply(extra: dict):
    from sa_home_bot.vpn.subscription_links import SubscriptionLinksMixin

    class FakeMixin(SubscriptionLinksMixin):
        _node_id = "jeeves"
        _cfg = type("obj", (), {"location": "🇳🇱 Нидерланды", "sub_port": 0})()
        _reality_cfg = RealityTransportConfig(
            endpoint_host="198.51.100.1",
            server_public_key="PUB1",
            short_id="abcd1234",
            sni="www.google.com",
            alt=None,
        )
        _sub_secret = b"secret"
        _sub_cache = {}
        _sub_stale = {}
        _sub_stale_info = {}
        _node_link = None

        def _reality_params(self):
            return RealityParams.from_config(self._reality_cfg)

        def _reality_alt_params(self):
            return RealityParams.alt_from_config(self._reality_cfg)

        def _sub_entry_name(self):
            return self._cfg.location

        def _node_ip(self):
            return "198.51.100.1"

        def _sub_base(self):
            return ""

        def _has(self, _):
            return False

        async def _sub_local(self, token):
            return (1, "x", "uuid-us")

        async def _sub_peer_nodes(self):
            return []

        async def _device_gen(self, chat_id, label):
            return 0

        async def _active_row(self, chat_id, label, transport):
            return None

        async def _sub_health(self):
            return "ok"

        async def _used_bytes(self, chat_id, month):
            return 0

        async def _limit_bytes(self, chat_id, month):
            return 0

        async def _sub_links(self, args):
            entry = subs.SubEntry(
                node="wooster",
                name="🇺🇸 США",
                uuid="uuid-us",
                params=RealityParams("198.51.100.2", 8443, "PUB2", "ab", "www.google.com"),
            )
            return {
                "found": True,
                "device_label": "x",
                "chat_id": 1,
                "gen": 0,
                "entry": entry.to_wire(),
                "used_bytes": 0,
                "limit_bytes": 0,
                **extra,
            }

    mixin = FakeMixin()
    token = subs.make_token(b"secret", 1, "x")
    return asyncio.run(mixin.resolve_subscription(token))


def test_resolve_subscription_parses_old_reply_with_only_entry():
    sub = _resolve_with_reply({})
    assert sub is not None
    assert len(sub.entries) == 1
    assert sub.entries[0].name == "🇺🇸 США"
    assert sub.entries[0].is_alt is False


def test_resolve_subscription_parses_new_reply_with_entries():
    regular = subs.SubEntry(
        node="wooster",
        name="🇺🇸 США",
        uuid="uuid-us",
        params=RealityParams("198.51.100.2", 8443, "PUB2", "ab", "www.google.com"),
    )
    alt = subs.SubEntry(
        node="wooster",
        name="🇺🇸 США · 2",
        uuid="uuid-us",
        params=RealityParams("198.51.100.2", 2083, "PUB2", "ab", "feedwonk.com"),
        is_alt=True,
    )
    sub = _resolve_with_reply({"entries": [regular.to_wire(), alt.to_wire()]})
    assert sub is not None
    assert [e.is_alt for e in sub.entries] == [False, True]
    assert [e.params.port for e in sub.entries] == [8443, 2083]
    assert sub.entries[1].params.sni == "feedwonk.com"


def test_countries_card_skips_alt_entries():
    entries = [
        _entry("🇳🇱 Нидерланды", is_alt=False),
        _entry("🇳🇱 Нидерланды · 2", is_alt=True),
    ]
    sub = _sub(entries)
    card = subs._countries_card(sub)
    assert "Нидерланды" in card
    assert "· 2" not in card


def test_reality_artifacts_regular_chat_with_alt_uses_alt_for_share_url():
    from sa_home_bot.config import RealityAltConfig
    from sa_home_bot.vpn.peers import PeersMixin

    class FakeMixin(PeersMixin):
        _node = "jeeves"
        _cfg = type("obj", (), {"location": "🇳🇱 Нидерланды"})()
        _reality_cfg = RealityTransportConfig(
            endpoint_host="198.51.100.1",
            server_public_key="PUB1",
            short_id="abcd1234",
            sni="www.google.com",
            alt=RealityAltConfig(port=2083, sni="www.cloudflare.com", inbound_tag="reality-alt"),
        )

        def _reality_params(self):
            return RealityParams.from_config(self._reality_cfg)

        def _reality_alt_params(self):
            return RealityParams.alt_from_config(self._reality_cfg)

    mixin = FakeMixin()
    artifacts = mixin._reality_artifacts("uuid-nl", "📱 iPhone", chat_id=123)
    assert "198.51.100.1:2083" in artifacts["share_url"]
    assert "www.cloudflare.com" in artifacts["share_url"]
    assert "alt_share_url" in artifacts
    assert "198.51.100.1:2083" in artifacts["alt_share_url"]


def test_reality_artifacts_probe_chat_with_alt_uses_original_for_share_url():
    from sa_home_bot.config import RealityAltConfig
    from sa_home_bot.vpn.peers import PeersMixin

    class FakeMixin(PeersMixin):
        _node = "jeeves"
        _cfg = type("obj", (), {"location": "🇳🇱 Нидерланды"})()
        _reality_cfg = RealityTransportConfig(
            endpoint_host="198.51.100.1",
            server_public_key="PUB1",
            short_id="abcd1234",
            sni="www.google.com",
            alt=RealityAltConfig(port=2083, sni="www.cloudflare.com", inbound_tag="reality-alt"),
        )

        def _reality_params(self):
            return RealityParams.from_config(self._reality_cfg)

        def _reality_alt_params(self):
            return RealityParams.alt_from_config(self._reality_cfg)

    mixin = FakeMixin()
    artifacts = mixin._reality_artifacts("uuid-nl", "📱 iPhone", chat_id=PROBE_CHAT_ID)
    assert "198.51.100.1:8443" in artifacts["share_url"]
    assert "www.google.com" in artifacts["share_url"]
    assert "alt_share_url" in artifacts
    assert "198.51.100.1:2083" in artifacts["alt_share_url"]


def test_reality_artifacts_without_alt_uses_original_and_no_alt_share_url():
    from sa_home_bot.vpn.peers import PeersMixin

    class FakeMixin(PeersMixin):
        _node = "jeeves"
        _cfg = type("obj", (), {"location": "🇳🇱 Нидерланды"})()
        _reality_cfg = RealityTransportConfig(
            endpoint_host="198.51.100.1",
            server_public_key="PUB1",
            short_id="abcd1234",
            sni="www.google.com",
            alt=None,
        )

        def _reality_params(self):
            return RealityParams.from_config(self._reality_cfg)

        def _reality_alt_params(self):
            return RealityParams.alt_from_config(self._reality_cfg)

    mixin = FakeMixin()
    artifacts = mixin._reality_artifacts("uuid-nl", "📱 iPhone", chat_id=123)
    assert "198.51.100.1:8443" in artifacts["share_url"]
    assert "www.google.com" in artifacts["share_url"]
    assert "alt_share_url" not in artifacts
