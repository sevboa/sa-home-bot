from __future__ import annotations

import asyncio

import pytest

from sa_home_bot.bot import vpn_nodes
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import Settings, VpnCheckConfig
from sa_home_bot.node import vpn_probe_state
from sa_home_bot.node.fixups import probe_net, probe_tunnel
from sa_home_bot.node.vpn_probe_state import ProbeSlot
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn_check.service import VpnCheckService
from sa_home_bot.vpn_series.raw_probe import RawTarget, RawVerdict
from sa_home_bot.vpn_series.runner import build_curl_argv
from tests.unit.fixups_world import FakeRoot, make_reality_slot, make_settings


@pytest.fixture
def root(monkeypatch):
    return FakeRoot(monkeypatch)


class FakeLink:
    def __init__(self, own: dict, peer_states: dict[str, dict] | None = None) -> None:
        self._own = own
        self._peers = peer_states or {}

    async def get_state(self, dst=None):
        if dst is None:
            return self._own
        if dst.node in self._peers:
            return self._peers[dst.node]
        raise ServiceUnavailableError(f"нет связи с {dst.node}")


def _svc(name: str, status: str = "running") -> dict:
    return {"name": name, "service": name, "status": status}


def _node_state(node: str, services: list[dict], peers: list[dict] | None = None) -> dict:
    return {"node": node, "kind": "vps", "services": services, "peers": peers or []}


def _vpn_state(
    label: str, transports: list[str], probe_transports: list[str] | None = None
) -> dict:
    state = {"label": label, "transports": transports}
    if probe_transports is not None:
        state["probe_transports"] = probe_transports
    return state


def _async_return(value):
    async def fake(settings, slot):
        return value

    return fake


def _reality_conf(socks_port: int) -> str:
    from sa_home_bot.reality import probe_client_config
    from sa_home_bot.reality.client_config import RealityParams

    REALITY_PARAMS = RealityParams(
        endpoint_host="172.245.159.221",
        port=8443,
        server_public_key="pta_VF3o7pvmIw_X3VDePlqIsgOuyWVGrFBvPxjA-wk",
        short_id="51ccffcdddd273ab",
        sni="www.google.com",
    )
    return probe_client_config.render_probe_client_config(
        REALITY_PARAMS, "uuid-1", socks_port=socks_port
    )


def _alt_reality_conf(socks_port: int) -> str:
    from sa_home_bot.reality import probe_client_config
    from sa_home_bot.reality.client_config import RealityParams

    REALITY_PARAMS = RealityParams(
        endpoint_host="172.245.159.221",
        port=2083,
        server_public_key="pta_VF3o7pvmIw_X3VDePlqIsgOuyWVGrFBvPxjA-wk",
        short_id="51ccffcdddd273ab",
        sni="www.cloudflare.com",
    )
    return probe_client_config.render_probe_client_config(
        REALITY_PARAMS, "uuid-alt", socks_port=socks_port
    )


def test_probe_targets_without_probe_transports():
    class ServiceStateLink(FakeLink):
        def __init__(self, own, peer_states, vpn_states, *, dead=()) -> None:
            super().__init__(own, peer_states)
            self._vpn_states = vpn_states
            self._dead = set(dead)

        async def get_state(self, dst=None):
            if dst is not None and dst.service == "vpn":
                if dst.node in self._dead:
                    raise ServiceUnavailableError(f"нет связи с {dst.node}")
                return self._vpn_states[dst.node]
            return await super().get_state(dst)

    link = ServiceStateLink(
        _node_state(
            "alfred", [_svc("monitor")], peers=[{"id": "jeeves", "alive": True, "kind": "vps"}]
        ),
        {"jeeves": _node_state("jeeves", [_svc("vpn")])},
        {"jeeves": _vpn_state("🇳🇱 Нидерланды", ["awg", "reality"])},
    )
    import asyncio

    targets = asyncio.run(vpn_nodes.probe_targets(link, exclude="alfred"))
    assert targets == [
        vpn_nodes.ProbeTarget(server="jeeves", transport="awg"),
        vpn_nodes.ProbeTarget(server="jeeves", transport="reality"),
    ]


def test_probe_targets_with_probe_transports():
    class ServiceStateLink(FakeLink):
        def __init__(self, own, peer_states, vpn_states, *, dead=()) -> None:
            super().__init__(own, peer_states)
            self._vpn_states = vpn_states
            self._dead = set(dead)

        async def get_state(self, dst=None):
            if dst is not None and dst.service == "vpn":
                if dst.node in self._dead:
                    raise ServiceUnavailableError(f"нет связи с {dst.node}")
                return self._vpn_states[dst.node]
            return await super().get_state(dst)

    link = ServiceStateLink(
        _node_state(
            "alfred", [_svc("monitor")], peers=[{"id": "jeeves", "alive": True, "kind": "vps"}]
        ),
        {"jeeves": _node_state("jeeves", [_svc("vpn")])},
        {
            "jeeves": _vpn_state(
                "🇳🇱 Нидерланды", ["awg", "reality"], ["awg", "reality", "reality_alt"]
            )
        },
    )
    import asyncio

    targets = asyncio.run(vpn_nodes.probe_targets(link, exclude="alfred"))
    assert targets == [
        vpn_nodes.ProbeTarget(server="jeeves", transport="awg"),
        vpn_nodes.ProbeTarget(server="jeeves", transport="reality"),
        vpn_nodes.ProbeTarget(server="jeeves", transport="reality_alt"),
    ]


def test_probe_slots_from_targets_without_alt():
    targets = [
        vpn_nodes.ProbeTarget(server="jeeves", transport="awg"),
        vpn_nodes.ProbeTarget(server="jeeves", transport="reality"),
        vpn_nodes.ProbeTarget(server="wooster", transport="reality"),
    ]
    slots = probe_net.probe_slots_from_targets(targets)
    assert len(slots) == 3
    assert slots[0].transport == "awg"
    assert slots[1].transport == "reality"
    assert slots[2].transport == "reality"
    assert slots[0].netns == "vpn-probe-jeeves-awg"
    assert slots[1].netns == "vpn-probe-jeeves-reality"
    assert slots[2].netns == "vpn-probe-wooster-reality"
    assert slots[0].iface == "awg-probe0"
    assert slots[1].iface is None
    assert slots[2].iface is None
    assert slots[0].socks_port is None
    assert slots[1].socks_port == 11081
    assert slots[2].socks_port == 11082
    for field in ("veth_host", "veth_ns", "subnet", "netns"):
        assert len({getattr(s, field) for s in slots}) == 3


def test_probe_slots_from_targets_with_alt():
    targets = [
        vpn_nodes.ProbeTarget(server="jeeves", transport="awg"),
        vpn_nodes.ProbeTarget(server="jeeves", transport="reality"),
        vpn_nodes.ProbeTarget(server="wooster", transport="reality"),
        vpn_nodes.ProbeTarget(server="jeeves", transport="reality_alt"),
        vpn_nodes.ProbeTarget(server="wooster", transport="reality_alt"),
    ]
    slots = probe_net.probe_slots_from_targets(targets)
    normal = [s for s in slots if s.transport != "reality_alt"]
    alt = [s for s in slots if s.transport == "reality_alt"]
    assert len(normal) == 3
    assert len(alt) == 2
    assert normal[0].netns == "vpn-probe-jeeves-awg"
    assert normal[1].netns == "vpn-probe-jeeves-reality"
    assert normal[2].netns == "vpn-probe-wooster-reality"
    assert alt[0].netns == "vpn-probe-jeeves-reality_alt"
    assert alt[1].netns == "vpn-probe-wooster-reality_alt"
    assert alt[0].socks_port == 11080 + probe_net.ALT_SLOT_INDEX_BASE + 0
    assert alt[1].socks_port == 11080 + probe_net.ALT_SLOT_INDEX_BASE + 1
    assert alt[0].iface is None
    assert alt[1].iface is None
    for field in ("veth_host", "veth_ns", "subnet", "netns"):
        assert len({getattr(s, field) for s in slots}) == 5
    subnets = [s.subnet for s in slots]
    assert len(set(subnets)) == 5


def test_probe_slots_from_targets_alt_index_stable():
    targets_wooster_only = [
        vpn_nodes.ProbeTarget(server="jeeves", transport="reality"),
        vpn_nodes.ProbeTarget(server="wooster", transport="reality"),
        vpn_nodes.ProbeTarget(server="wooster", transport="reality_alt"),
    ]
    slots_wooster_only = probe_net.probe_slots_from_targets(targets_wooster_only)
    alt_wooster = [s for s in slots_wooster_only if s.transport == "reality_alt"][0]

    targets_both = [
        vpn_nodes.ProbeTarget(server="jeeves", transport="reality"),
        vpn_nodes.ProbeTarget(server="jeeves", transport="reality_alt"),
        vpn_nodes.ProbeTarget(server="wooster", transport="reality"),
        vpn_nodes.ProbeTarget(server="wooster", transport="reality_alt"),
    ]
    slots_both = probe_net.probe_slots_from_targets(targets_both)
    alt_wooster_both = [
        s for s in slots_both if s.transport == "reality_alt" and s.server == "wooster"
    ][0]

    assert alt_wooster.netns == alt_wooster_both.netns
    assert alt_wooster.socks_port == alt_wooster_both.socks_port
    assert alt_wooster.subnet == alt_wooster_both.subnet


def test_fetch_probe_config_reality_alt_uses_alt_share_url(root, monkeypatch):
    slot = make_reality_slot()
    slot.transport = "reality_alt"
    slot.socks_port = 11112

    class FakeClient:
        async def command(self, action, args, dst, timeout):
            return {"alt_share_url": "vless://uuid-alt@1.2.3.4:2083?security=reality&sni=www.cloudflare.com&fp=chrome&pbk=key&sid=sid#probe"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    async def fake_resolve_vpn_dst(client, server):
        return type("Dst", (), {"node": server})()

    monkeypatch.setattr(probe_tunnel, "ensure_xray_binary", lambda: None)
    monkeypatch.setattr(probe_tunnel, "_node_client", lambda settings: FakeClient())
    monkeypatch.setattr(probe_tunnel.vpn_nodes, "resolve_vpn_dst", fake_resolve_vpn_dst)
    settings = make_settings(["vpn_check"])
    probe_tunnel._probe_conf_apply(settings, slot)
    install = root.sudo_commands("install")[0]
    assert install[-1] == str(vpn_probe_state.reality_conf_path(slot))
    assert install[1:3] == ["-D", "-m"] and "0600" in install


def test_fetch_probe_config_reality_alt_without_alt_share_url_raises(root, monkeypatch):
    slot = make_reality_slot()
    slot.transport = "reality_alt"
    slot.socks_port = 11112

    class FakeClient:
        async def command(self, action, args, dst, timeout):
            return {"share_url": "vless://..."}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    async def fake_resolve_vpn_dst(client, server):
        return type("Dst", (), {"node": server})()

    monkeypatch.setattr(probe_tunnel, "ensure_xray_binary", lambda: None)
    monkeypatch.setattr(probe_tunnel, "_node_client", lambda settings: FakeClient())
    monkeypatch.setattr(probe_tunnel.vpn_nodes, "resolve_vpn_dst", fake_resolve_vpn_dst)
    settings = make_settings(["vpn_check"])
    with pytest.raises(probe_tunnel.FixupError, match="не вернул alt_share_url"):
        probe_tunnel._probe_conf_apply(settings, slot)


def test_fetch_probe_config_reality_uses_share_url(root, monkeypatch):
    slot = make_reality_slot()
    slot.transport = "reality"
    slot.socks_port = 11081

    class FakeClient:
        async def command(self, action, args, dst, timeout):
            return {"share_url": "vless://uuid-1@1.2.3.4:8443?security=reality&sni=www.google.com&fp=chrome&pbk=key&sid=sid#probe"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    async def fake_resolve_vpn_dst(client, server):
        return type("Dst", (), {"node": server})()

    monkeypatch.setattr(probe_tunnel, "ensure_xray_binary", lambda: None)
    monkeypatch.setattr(probe_tunnel, "_node_client", lambda settings: FakeClient())
    monkeypatch.setattr(probe_tunnel.vpn_nodes, "resolve_vpn_dst", fake_resolve_vpn_dst)
    settings = make_settings(["vpn_check"])
    probe_tunnel._probe_conf_apply(settings, slot)
    install = root.sudo_commands("install")[0]
    assert install[-1] == str(vpn_probe_state.reality_conf_path(slot))
    assert install[1:3] == ["-D", "-m"] and "0600" in install


def test_build_curl_argv_reality_alt():
    slot = ProbeSlot(
        server="jeeves",
        transport="reality_alt",
        netns="vpn-probe-jeeves-reality_alt",
        veth_host="vprobe32h0",
        veth_ns="vprobe32n0",
        veth_host_addr="10.200.200.129/30",
        veth_ns_addr="10.200.200.130/30",
        subnet="10.200.200.128/30",
        iface=None,
        socks_port=11112,
    )
    argv = build_curl_argv(slot, "https://example.com", 10.0)
    assert "--socks5-hostname" in argv
    assert "127.0.0.1:11112" in argv


def test_reality_alt_in_supported_probe_transports():
    assert "reality_alt" in probe_tunnel.SUPPORTED_PROBE_TRANSPORTS
    assert "reality_alt" not in vpn_protocol.TRANSPORTS


def test_vpn_service_state_probe_transports_only_with_alt():
    import asyncio
    import tempfile

    from sa_home_bot.config import RealityAltConfig, RealityTransportConfig, Settings, VpnConfig
    from sa_home_bot.db.connection import Database
    from sa_home_bot.db.migrations import apply_migrations
    from sa_home_bot.vpn.service import VpnService

    class FakeAwg:
        def __init__(self):
            self.peers = {}
            self.server_pub = "server-pubkey"

        async def server_public_key(self):
            return self.server_pub

        async def add_peer(self, public_key, address):
            self.peers[public_key] = address

        async def remove_peer(self, public_key):
            self.peers.pop(public_key, None)

        async def transfer(self):
            return {}

        async def latest_handshakes(self):
            return {}

        async def generate_keypair(self):
            return "private", "public"

    class FakeXray:
        async def add_client(self, client_uuid, email, flow):
            pass

        async def remove_client(self, email):
            pass

        async def list_clients(self):
            return set()

        async def stats(self):
            return {}

    async def test_state():
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp + "/vpn.sqlite")
            await db.open()
            await apply_migrations(db)
            backend = FakeAwg()
            xray = FakeXray()
            events = []

            async def emit(event_type, data):
                events.append((event_type, data))

            cfg_no_alt = VpnConfig(
                subnet="10.9.0.0/29",
                base_quota_gb=1,
                extra_step_gb=1,
                self_ceiling_gb=3,
                warn_remaining_gb=0,
                endpoint_host="203.0.113.9",
                transports=["awg", "reality"],
                reality=RealityTransportConfig(
                    endpoint_host="1.2.3.4",
                    port=8443,
                    server_public_key="key",
                    short_id="sid",
                    sni="www.google.com",
                ),
            )
            svc_no_alt = VpnService(
            Settings(vpn=cfg_no_alt), db, backend, emit, reality_backend=xray
        )
            state_no_alt = await svc_no_alt.get_state()
            assert "probe_transports" not in state_no_alt
            assert state_no_alt["transports"] == ["awg", "reality"]

            cfg_with_alt = VpnConfig(
                subnet="10.9.0.0/29",
                base_quota_gb=1,
                extra_step_gb=1,
                self_ceiling_gb=3,
                warn_remaining_gb=0,
                endpoint_host="203.0.113.9",
                transports=["awg", "reality"],
                reality=RealityTransportConfig(
                    endpoint_host="1.2.3.4",
                    port=8443,
                    server_public_key="key",
                    short_id="sid",
                    sni="www.google.com",
                    alt=RealityAltConfig(port=2083, sni="www.cloudflare.com"),
                ),
            )
            svc_with_alt = VpnService(
            Settings(vpn=cfg_with_alt), db, backend, emit, reality_backend=xray
        )

            async def rollup(*, server=None):
                return [
                    {"transport": "reality", "status": "ok"},
                    {"transport": "reality_alt", "status": "alerting"},
                ]

            svc_with_alt._check_rollup = rollup
            state_with_alt = await svc_with_alt.get_state()
            assert [r["transport"] for r in state_with_alt["check"]] == ["reality"]
            assert "probe_transports" in state_with_alt
            assert state_with_alt["probe_transports"] == ["awg", "reality", "reality_alt"]
            assert state_with_alt["transports"] == ["awg", "reality"]
            await db.close()

    asyncio.run(test_state())


def test_raw_probe_reality_alt_tcp_check(monkeypatch):
    import asyncio

    from sa_home_bot.vpn_series import raw_probe

    slot = ProbeSlot(
        server="jeeves",
        transport="reality_alt",
        netns="vpn-probe-jeeves-reality_alt",
        veth_host="vprobe32h0",
        veth_ns="vprobe32n0",
        veth_host_addr="10.200.200.129/30",
        veth_ns_addr="10.200.200.130/30",
        subnet="10.200.200.128/30",
        iface=None,
        socks_port=11112,
    )

    async def fake_tcp(host, port, timeout=0):
        return None

    monkeypatch.setattr(raw_probe, "tcp_connect", fake_tcp)

    async def handshake_ok():
        return None

    result = asyncio.run(
        raw_probe.raw_probe(slot, RawTarget(host="1.2.3.4", port=443), handshake_ok)
    )
    assert result.verdict == RawVerdict.OK
    assert result.tcp_ok is True


def test_raw_probe_reality_alt_tcp_blocked(monkeypatch):
    import asyncio

    from sa_home_bot.vpn_series import raw_probe

    slot = ProbeSlot(
        server="jeeves",
        transport="reality_alt",
        netns="vpn-probe-jeeves-reality_alt",
        veth_host="vprobe32h0",
        veth_ns="vprobe32n0",
        veth_host_addr="10.200.200.129/30",
        veth_ns_addr="10.200.200.130/30",
        subnet="10.200.200.128/30",
        iface=None,
        socks_port=11112,
    )

    async def fake_tcp(host, port, timeout=0):
        return "refused"

    monkeypatch.setattr(raw_probe, "tcp_connect", fake_tcp)

    async def handshake_ok():
        return None

    result = asyncio.run(
        raw_probe.raw_probe(slot, RawTarget(host="1.2.3.4", port=443), handshake_ok)
    )
    assert result.verdict == RawVerdict.TCP_BLOCKED
    assert result.tcp_ok is False


def test_vpn_check_service_tunnel_up_down_reality_alt(monkeypatch):
    from sa_home_bot.config import NodeConfig
    from sa_home_bot.node.vpn_probe_state import ProbeSlot

    class FakeNodeLink:
        state = {
            "node": "wooster",
            "kind": "vps",
            "peers": [],
            "services": [{"name": "vpn", "service": "vpn", "status": "running"}],
        }

        async def get_state(self, dst=None):
            return self.state

        async def command(self, action, args=None, dst=None, *, timeout=None):
            return {"accepted": True}

    slot = ProbeSlot(
        server="jeeves",
        transport="reality_alt",
        netns="vpn-probe-jeeves-reality_alt",
        veth_host="vprobe32h0",
        veth_ns="vprobe32n0",
        veth_host_addr="10.200.200.129/30",
        veth_ns_addr="10.200.200.130/30",
        subnet="10.200.200.128/30",
        iface=None,
        socks_port=11112,
    )

    calls = []

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        calls.append(cmd)
        if "xray" in cmd:
            return _FakeProc(alive=True)
        if "route" in cmd and "get" in cmd:
            return _FakeProc(b"1.1.1.1 dev vprobe32h0 src 10.9.0.14\n".encode(), b"", 0)
        return _FakeProc(b"200", b"", 0)

    async def instant_sleep(_delay):
        return None

    monkeypatch.setattr("sa_home_bot.vpn_check.service.shutil.which", lambda _name: None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    settings = Settings(node=NodeConfig(assignments=[]), vpn_check=VpnCheckConfig())
    service = VpnCheckService(settings, FakeNodeLink(), slots=[slot])

    async def run():
        err = await service._tunnel_up(slot)
        assert err is None
        await service._tunnel_down(slot)

    asyncio.run(run())

    xray_calls = [c for c in calls if "xray" in c]
    assert len(xray_calls) == 1
    assert "run" in xray_calls[0]
    assert "-c" in xray_calls[0]


class _FakeStderr:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _FakeProc:
    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int | None = 0,
        *,
        alive=False,
    ):
        self._stdout = stdout
        self._stderr_bytes = stderr
        self.returncode = None if alive else returncode
        self.stderr = _FakeStderr(stderr)
        self.terminated = False
        self.killed = False

    async def communicate(self, input=None):
        return self._stdout, self._stderr_bytes

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def test_vpn_check_service_curl_argv_reality_alt():
    from sa_home_bot.config import NodeConfig
    from sa_home_bot.node.vpn_probe_state import ProbeSlot

    slot = ProbeSlot(
        server="jeeves",
        transport="reality_alt",
        netns="vpn-probe-jeeves-reality_alt",
        veth_host="vprobe32h0",
        veth_ns="vprobe32n0",
        veth_host_addr="10.200.200.129/30",
        veth_ns_addr="10.200.200.130/30",
        subnet="10.200.200.128/30",
        iface=None,
        socks_port=11112,
    )

    settings = Settings(node=NodeConfig(assignments=[]), vpn_check=VpnCheckConfig())
    service = VpnCheckService(settings, None, slots=[slot])
    argv = service._curl_argv(slot, ["-s", "https://example.com"])
    assert "--socks5-hostname" in argv
    assert "127.0.0.1:11112" in argv


def _capture_fetch(monkeypatch, reply):
    sent = {}

    class FakeClient:
        async def command(self, action, args, dst, timeout):
            sent.update(args)
            return reply

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    async def fake_resolve_vpn_dst(client, server):
        return type("Dst", (), {"node": server})()

    monkeypatch.setattr(probe_tunnel, "_node_client", lambda settings: FakeClient())
    monkeypatch.setattr(probe_tunnel.vpn_nodes, "resolve_vpn_dst", fake_resolve_vpn_dst)
    return sent


_OLD_URL = "vless://uuid-a@1.2.3.4:8443?security=reality&sni=www.google.com&fp=chrome&pbk=key&sid=ab#p"
_ALT_URL = "vless://uuid-a@1.2.3.4:2083?security=reality&sni=feedwonk.com&fp=chrome&pbk=key&sid=ab#p"


def test_fetch_probe_config_alt_issues_reality_and_targets_alt_entry(monkeypatch):
    slot = make_reality_slot()
    slot.transport = "reality_alt"
    sent = _capture_fetch(monkeypatch, {"share_url": _OLD_URL, "alt_share_url": _ALT_URL})
    text = asyncio.run(probe_tunnel._fetch_probe_config(make_settings(["vpn_check"]), slot))
    assert sent["transport"] == "reality"
    assert sent["chat_id"] == vpn_protocol.PROBE_CHAT_ID
    assert "2083" in text and "feedwonk.com" in text
    assert "8443" not in text


def test_fetch_probe_config_reality_keeps_old_entry(monkeypatch):
    slot = make_reality_slot()
    sent = _capture_fetch(monkeypatch, {"share_url": _OLD_URL, "alt_share_url": _ALT_URL})
    text = asyncio.run(probe_tunnel._fetch_probe_config(make_settings(["vpn_check"]), slot))
    assert sent["transport"] == "reality"
    assert "8443" in text and "www.google.com" in text
    assert "feedwonk.com" not in text
