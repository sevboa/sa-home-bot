"""Прокси на jeeves (mtg/microsocks) поверх VpnService: ссылка, ротация
секрета, агрегатный расход — поверх подменяемого ProxyBackend (настоящие
`nft`/`systemctl` в тестах не зовём)."""

from __future__ import annotations

import pytest_asyncio

from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol
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
        node_limit_gb=1,  # маленький лимит — быстрые пороги
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


async def test_proxy_link_seeds_and_reuses_secret(env):
    svc, _proxy, _events = env
    first = await svc.run_command(vpn_protocol.ACTION_PROXY_LINK, {})
    assert first["secret"] == vpn_protocol.PROXY_SECRET_SEED
    assert first["host"] == "203.0.113.9"
    assert "tg://proxy?" in first["tg_link"]
    assert first["socks_host"] == "100.111.4.42"
    second = await svc.run_command(vpn_protocol.ACTION_PROXY_LINK, {})
    assert second["secret"] == first["secret"]  # тот же секрет, не пересидировался


async def test_proxy_rotate_secret_updates_backend_and_db(env):
    svc, proxy, _events = env
    result = await svc.run_command(vpn_protocol.ACTION_PROXY_ROTATE_SECRET, {})
    assert proxy.generated_domain == "www.microsoft.com"
    assert proxy.rotated_to == "new-secret"
    assert result["secret"] == "new-secret"
    # Ссылка после ротации отдаёт уже новый секрет, не старый.
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

    # Второй тик — только дельта, не повторный полный счётчик.
    proxy.mtg_bytes = 1500
    proxy.socks_bytes = 500
    await svc.sample_once()
    usage2 = await svc.run_command(vpn_protocol.ACTION_PROXY_USAGE, {})
    assert usage2["mtg_bytes"] == 1500
    assert usage2["node_used_bytes"] == 2000


async def test_proxy_bytes_count_toward_node_limit_warning(env):
    svc, proxy, events = env
    proxy.mtg_bytes = 2 * GB  # больше node_limit_gb=1 из фикстуры
    await svc.sample_once()
    kinds = [e for e, _ in events]
    assert vpn_protocol.EVENT_VPN_NODE_QUOTA_WARNING in kinds


async def test_sample_proxy_tolerates_missing_sudoers(env):
    """Если nodectl fix ещё не прогнан (ProxyBackend.counters кидает
    ProtoError) — тик сэмплера не должен падать целиком."""
    svc, proxy, _events = env

    async def failing_counters():
        raise ProtoError("needs_privilege", "нет прав")

    proxy.counters = failing_counters
    await svc.sample_once()  # не должно бросить исключение
