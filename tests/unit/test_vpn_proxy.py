from __future__ import annotations

import pytest
import pytest_asyncio

from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.utils.process import ProcResult
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn import proxy_backend as proxy_backend_module
from sa_home_bot.vpn.proxy_backend import RealProxyBackend
from sa_home_bot.vpn.service import GB, VpnService


class DummyAwg:
    async def server_public_key(self) -> str:
        return "server-pubkey"

    async def add_peer(self, public_key: str, address: str) -> None:
        pass

    async def remove_peer(self, public_key: str) -> None:
        pass

    async def transfer(self) -> dict[str, tuple[int, int]]:
        return {}

    async def latest_handshakes(self) -> dict[str, int]:
        return {}

    async def generate_keypair(self) -> tuple[str, str]:
        return "priv", "pub"


class FakeProxyBackend:
    def __init__(self) -> None:
        self.mtg_bytes = 0
        self.socks_bytes = 0
        self.rotated_to: str | None = None
        self.generated_domain: str | None = None
        self._next_secret = "new-secret"

    async def counters(self) -> dict[str, int]:
        return {"mtg": self.mtg_bytes, "socks": self.socks_bytes}

    async def generate_secret(self, domain: str) -> str:
        self.generated_domain = domain
        return self._next_secret

    async def rotate_secret(self, new_secret: str) -> None:
        self.rotated_to = new_secret


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)
    proxy = FakeProxyBackend()
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = VpnConfig(
        mtg_public_host="203.0.113.9",
        mtg_port=443,
        socks_host="100.111.4.42",
        socks_port=1080,
        node_limit_gb=1,
        warn_remaining_gb=0,
    )
    svc = VpnService(Settings(vpn=cfg), db, DummyAwg(), emit, proxy_backend=proxy)
    yield svc, proxy, events
    await db.close()


async def test_proxy_link_requires_public_host(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)

    async def emit(event_type: str, data: dict) -> None:
        pass

    svc = VpnService(
        Settings(vpn=VpnConfig()), db, DummyAwg(), emit, proxy_backend=FakeProxyBackend()
    )
    try:
        await svc.run_command(vpn_protocol.ACTION_PROXY_LINK, {})
        raise AssertionError("должен был отказать без mtg_public_host")
    except ProtoError:
        pass
    await db.close()


async def test_proxy_declared_on_reality_only_node(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)

    async def emit(event_type: str, data: dict) -> None:
        pass

    cfg = VpnConfig(transports=["reality"], mtg_public_host="203.0.113.9")
    svc = VpnService(Settings(vpn=cfg), db, DummyAwg(), emit, proxy_backend=FakeProxyBackend())
    caps = svc.describe().capabilities
    assert vpn_protocol.ACTION_PROXY_LINK in caps
    assert vpn_protocol.ACTION_CHECK_STATUS in caps
    assert vpn_protocol.ACTION_APK_INFO not in caps
    await db.close()


async def test_proxy_not_declared_without_mtg_host(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)

    async def emit(event_type: str, data: dict) -> None:
        pass

    svc = VpnService(
        Settings(vpn=VpnConfig()), db, DummyAwg(), emit, proxy_backend=FakeProxyBackend()
    )
    assert vpn_protocol.ACTION_PROXY_LINK not in svc.describe().capabilities
    await db.close()


async def test_proxy_link_seeds_and_reuses_secret(env):
    svc, _proxy, _events = env
    first = await svc.run_command(vpn_protocol.ACTION_PROXY_LINK, {})
    assert first["secret"] == vpn_protocol.PROXY_SECRET_SEED
    assert first["host"] == "203.0.113.9"
    assert "tg://proxy?" in first["tg_link"]
    assert first["socks_host"] == "100.111.4.42"
    second = await svc.run_command(vpn_protocol.ACTION_PROXY_LINK, {})
    assert second["secret"] == first["secret"]


async def test_proxy_link_exposes_configured_priority(env):
    svc, _proxy, _events = env
    result = await svc.run_command(vpn_protocol.ACTION_PROXY_LINK, {})
    assert result["priority"] == 0
    svc._cfg = VpnConfig(mtg_public_host="203.0.113.9", mtg_priority=5)
    assert (await svc.run_command(vpn_protocol.ACTION_PROXY_LINK, {}))["priority"] == 5


async def test_proxy_rotate_secret_updates_backend_and_db(env):
    svc, proxy, _events = env
    result = await svc.run_command(vpn_protocol.ACTION_PROXY_ROTATE_SECRET, {})
    assert proxy.generated_domain == "www.microsoft.com"
    assert proxy.rotated_to == "new-secret"
    assert result["secret"] == "new-secret"
    again = await svc.run_command(vpn_protocol.ACTION_PROXY_LINK, {})
    assert again["secret"] == "new-secret"


async def test_proxy_usage_accumulates_via_sampler(env):
    svc, proxy, _events = env
    proxy.mtg_bytes = 1000
    proxy.socks_bytes = 500
    await svc.sample_once()
    usage = await svc.run_command(vpn_protocol.ACTION_PROXY_USAGE, {})
    assert usage["mtg_bytes"] == 1000
    assert usage["socks_bytes"] == 500
    assert usage["node_used_bytes"] == 1500

    proxy.mtg_bytes = 1500
    proxy.socks_bytes = 500
    await svc.sample_once()
    usage2 = await svc.run_command(vpn_protocol.ACTION_PROXY_USAGE, {})
    assert usage2["mtg_bytes"] == 1500
    assert usage2["node_used_bytes"] == 2000


async def test_proxy_bytes_count_toward_node_limit_warning(env):
    svc, proxy, events = env
    proxy.mtg_bytes = 2 * GB
    await svc.sample_once()
    kinds = [e for e, _ in events]
    assert vpn_protocol.EVENT_VPN_NODE_QUOTA_WARNING in kinds


async def test_sample_proxy_tolerates_missing_sudoers(env):
    svc, proxy, _events = env

    async def failing_counters():
        raise ProtoError("needs_privilege", "нет прав")

    proxy.counters = failing_counters
    await svc.sample_once()


@pytest.mark.parametrize("daemon_code", [0, 1])
async def test_real_backend_rotate_reloads_unit_before_restart(tmp_path, monkeypatch, daemon_code):
    unit = tmp_path / "mtg.service"
    unit.write_text("ExecStart=/usr/local/bin/simple-run mtg OLDSECRET\n")
    calls: list[list[str]] = []

    async def fake_run(argv: list[str]):
        calls.append(list(argv))
        return ProcResult(daemon_code, b"", b"boom" if daemon_code else b"")

    monkeypatch.setattr(proxy_backend_module, "MTG_UNIT_PATH", unit)
    monkeypatch.setattr(proxy_backend_module, "run", fake_run)
    backend = RealProxyBackend()
    if daemon_code == 0:
        await backend.rotate_secret("NEWSECRET")
    else:
        with pytest.raises(ProtoError, match="daemon-reload"):
            await backend.rotate_secret("NEWSECRET")
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        *([] if daemon_code else [["systemctl", "--user", "restart", "mtg.service"]]),
    ]
    assert "NEWSECRET" in unit.read_text()
