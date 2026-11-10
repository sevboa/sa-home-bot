from __future__ import annotations

import pytest_asyncio

from sa_home_bot.config import AwgAltConfig, Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.vpn.awg import DualAwgBackend
from sa_home_bot.vpn.protocol import PROBE_CHAT_ID
from sa_home_bot.vpn.service import VpnService

CHAT = 111
OTHER_CHAT = 222


class FakeAwg:
    def __init__(self, server_pub: str = "server-pubkey") -> None:
        self.peers: dict[str, str] = {}
        self.transfer_bytes: dict[str, tuple[int, int]] = {}
        self.handshakes: dict[str, int] = {}
        self._keygen = 0
        self.server_pub = server_pub
        self.fail_transfer = False
        self.fail_handshakes = False
        self.fail_remove = False

    async def server_public_key(self) -> str:
        return self.server_pub

    async def server_private_key(self) -> str:
        return "server-privkey"

    async def add_peer(self, public_key: str, address: str) -> None:
        self.peers[public_key] = address
        self.transfer_bytes.setdefault(public_key, (0, 0))

    async def remove_peer(self, public_key: str) -> None:
        if self.fail_remove:
            raise ProtoError(ERR_BAD_REQUEST, "remove failed")
        self.peers.pop(public_key, None)

    async def transfer(self) -> dict[str, tuple[int, int]]:
        if self.fail_transfer:
            raise ProtoError(ERR_BAD_REQUEST, "transfer failed")
        return {pk: self.transfer_bytes.get(pk, (0, 0)) for pk in self.peers}

    async def latest_handshakes(self) -> dict[str, int]:
        if self.fail_handshakes:
            raise ProtoError(ERR_BAD_REQUEST, "handshakes failed")
        return dict(self.handshakes)

    async def generate_keypair(self) -> tuple[str, str]:
        self._keygen += 1
        return f"private-{self._keygen}", f"public-{self._keygen}"

    def set_traffic(self, public_key: str, rx: int, tx: int) -> None:
        self.transfer_bytes[public_key] = (rx, tx)


@pytest_asyncio.fixture
async def env_dual(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)

    primary = FakeAwg("primary-pub")
    second = FakeAwg("second-pub")
    backend = DualAwgBackend(primary, second, "10.9.1.0/24")

    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = VpnConfig(
        subnet="10.9.0.0/29",
        base_quota_gb=1,
        extra_step_gb=1,
        self_ceiling_gb=3,
        warn_remaining_gb=0,
        endpoint_host="203.0.113.9",
        awg_alt=AwgAltConfig(
            interface="awg1",
            port=51821,
            subnet="10.9.1.0/24",
        ),
    )
    svc = VpnService(Settings(vpn=cfg), db, backend, emit)
    await svc.run_command(
        "set_access", {"chat_id": CHAT, "allowed": True, "base_gb": 1}
    )
    await svc.run_command(
        "set_access", {"chat_id": OTHER_CHAT, "allowed": True, "base_gb": 1}
    )
    events.clear()
    yield svc, backend, primary, second, events
    await db.close()


@pytest_asyncio.fixture
async def env_single(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)

    backend = FakeAwg("single-pub")

    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = VpnConfig(
        subnet="10.9.0.0/29",
        base_quota_gb=1,
        extra_step_gb=1,
        self_ceiling_gb=3,
        warn_remaining_gb=0,
        endpoint_host="203.0.113.9",
    )
    svc = VpnService(Settings(vpn=cfg), db, backend, emit)
    await svc.run_command(
        "set_access", {"chat_id": CHAT, "allowed": True, "base_gb": 1}
    )
    await svc.run_command(
        "set_access", {"chat_id": OTHER_CHAT, "allowed": True, "base_gb": 1}
    )
    events.clear()
    yield svc, backend, events
    await db.close()


class TestDualAwgBackend:
    async def test_is_second(self):
        primary = FakeAwg()
        second = FakeAwg()
        backend = DualAwgBackend(primary, second, "10.9.1.0/24")
        assert backend.is_second("10.9.1.5") is True
        assert backend.is_second("10.9.1.254") is True
        assert backend.is_second("10.9.0.5") is False
        assert backend.is_second("10.9.2.5") is False

    async def test_add_peer_routes_to_second(self):
        primary = FakeAwg()
        second = FakeAwg()
        backend = DualAwgBackend(primary, second, "10.9.1.0/24")
        await backend.add_peer("pub1", "10.9.1.5")
        assert "pub1" in second.peers
        assert "pub1" not in primary.peers
        await backend.add_peer("pub2", "10.9.0.5")
        assert "pub2" in primary.peers
        assert "pub2" not in second.peers

    async def test_remove_peer_on_both(self):
        primary = FakeAwg()
        second = FakeAwg()
        backend = DualAwgBackend(primary, second, "10.9.1.0/24")
        await backend.add_peer("pub1", "10.9.1.5")
        await backend.add_peer("pub2", "10.9.0.5")
        await backend.remove_peer("pub1")
        assert "pub1" not in primary.peers
        assert "pub1" not in second.peers
        await backend.remove_peer("pub2")
        assert "pub2" not in primary.peers
        assert "pub2" not in second.peers

    async def test_remove_peer_second_failure_logged(self, caplog):
        primary = FakeAwg()
        second = FakeAwg()
        second.fail_remove = True
        backend = DualAwgBackend(primary, second, "10.9.1.0/24")
        await backend.add_peer("pub1", "10.9.1.5")
        await backend.remove_peer("pub1")
        assert "pub1" not in primary.peers
        assert "awg: second remove_peer pub1" in caplog.text

    async def test_transfer_merges_both(self):
        primary = FakeAwg()
        second = FakeAwg()
        backend = DualAwgBackend(primary, second, "10.9.1.0/24")
        await backend.add_peer("pub1", "10.9.1.5")
        await backend.add_peer("pub2", "10.9.0.5")
        primary.set_traffic("pub2", 100, 200)
        second.set_traffic("pub1", 300, 400)
        result = await backend.transfer()
        assert result["pub1"] == (300, 400)
        assert result["pub2"] == (100, 200)

    async def test_transfer_second_failure_returns_primary(self, caplog):
        primary = FakeAwg()
        second = FakeAwg()
        second.fail_transfer = True
        backend = DualAwgBackend(primary, second, "10.9.1.0/24")
        await backend.add_peer("pub1", "10.9.1.5")
        await backend.add_peer("pub2", "10.9.0.5")
        primary.set_traffic("pub2", 100, 200)
        result = await backend.transfer()
        assert result == {"pub2": (100, 200)}
        assert "awg: second transfer failed" in caplog.text

    async def test_latest_handshakes_merges_both(self):
        primary = FakeAwg()
        second = FakeAwg()
        backend = DualAwgBackend(primary, second, "10.9.1.0/24")
        await backend.add_peer("pub1", "10.9.1.5")
        await backend.add_peer("pub2", "10.9.0.5")
        primary.handshakes["pub2"] = 1000
        second.handshakes["pub1"] = 2000
        result = await backend.latest_handshakes()
        assert result["pub1"] == 2000
        assert result["pub2"] == 1000

    async def test_latest_handshakes_second_failure_returns_primary(self, caplog):
        primary = FakeAwg()
        second = FakeAwg()
        second.fail_handshakes = True
        backend = DualAwgBackend(primary, second, "10.9.1.0/24")
        await backend.add_peer("pub1", "10.9.1.5")
        await backend.add_peer("pub2", "10.9.0.5")
        primary.handshakes["pub2"] = 1000
        result = await backend.latest_handshakes()
        assert result == {"pub2": 1000}
        assert "awg: second latest_handshakes failed" in caplog.text

    async def test_server_public_key_from_primary(self):
        primary = FakeAwg("primary-pub")
        second = FakeAwg("second-pub")
        backend = DualAwgBackend(primary, second, "10.9.1.0/24")
        assert await backend.server_public_key() == "primary-pub"

    async def test_second_server_public_key_from_second(self):
        primary = FakeAwg("primary-pub")
        second = FakeAwg("second-pub")
        backend = DualAwgBackend(primary, second, "10.9.1.0/24")
        assert await backend.second_server_public_key() == "second-pub"

    async def test_generate_keypair_from_primary(self):
        primary = FakeAwg()
        second = FakeAwg()
        backend = DualAwgBackend(primary, second, "10.9.1.0/24")
        priv, pub = await backend.generate_keypair()
        assert priv == "private-1"
        assert pub == "public-1"


class TestIssueWithAwgAlt:
    async def test_guest_gets_alt_subnet_and_config(self, env_dual):
        svc, backend, primary, second, events = env_dual
        result = await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        assert result["address"].startswith("10.9.1.")
        assert "Endpoint = 203.0.113.9:51821" in result["config_text"]
        assert "Jc = 5" in result["config_text"]
        assert "S3 = 0" in result["config_text"]
        assert "S4 = 0" in result["config_text"]
        assert "second-pub" in result["config_text"]
        assert "primary-pub" not in result["config_text"]
        priv_in_config = result["config_text"].split("PrivateKey = ")[1].split("\n")[0]
        pub_in_config = "public-" + priv_in_config.split("-")[1]
        assert list(second.peers.keys())[0] == pub_in_config
        assert len(second.peers) == 1
        assert len(primary.peers) == 0

    async def test_probe_gets_primary_subnet_and_config(self, env_dual):
        svc, backend, primary, second, events = env_dual
        result = await svc.run_command(
            "issue", {"chat_id": PROBE_CHAT_ID, "device_label": "probe"}
        )
        assert result["address"].startswith("10.9.0.")
        assert "Endpoint = 203.0.113.9:51820" in result["config_text"]
        assert "primary-pub" in result["config_text"]
        assert "second-pub" not in result["config_text"]
        assert len(primary.peers) == 1
        assert len(second.peers) == 0

    async def test_without_awg_alt_unchanged(self, env_single):
        svc, backend, events = env_single
        result = await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        assert result["address"].startswith("10.9.0.")
        assert "Endpoint = 203.0.113.9:51820" in result["config_text"]
        assert "single-pub" in result["config_text"]
        assert len(backend.peers) == 1

    async def test_db_stores_correct_server_pubkey_for_alt(self, env_dual):
        svc, backend, primary, second, events = env_dual
        await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        cur = await svc._db.conn.execute(
            "SELECT server_pubkey, address FROM vpn_peers WHERE chat_id = ?",
            (CHAT,),
        )
        row = await cur.fetchone()
        assert row["server_pubkey"] == "second-pub"
        assert row["address"].startswith("10.9.1.")

    async def test_db_stores_correct_server_pubkey_for_primary(self, env_dual):
        svc, backend, primary, second, events = env_dual
        await svc.run_command(
            "issue", {"chat_id": PROBE_CHAT_ID, "device_label": "probe"}
        )
        cur = await svc._db.conn.execute(
            "SELECT server_pubkey, address FROM vpn_peers WHERE chat_id = ?",
            (PROBE_CHAT_ID,),
        )
        row = await cur.fetchone()
        assert row["server_pubkey"] == "primary-pub"
        assert row["address"].startswith("10.9.0.")


class TestReconcileWithDualAwgBackend:
    async def test_reconcile_adds_alt_peer_on_second(self, env_dual):
        svc, backend, primary, second, events = env_dual
        result = await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        address = result["address"]
        pubkey = list(second.peers.keys())[0]
        second.peers.clear()
        await svc.reconcile()
        assert pubkey in second.peers
        assert second.peers[pubkey] == address

    async def test_reconcile_adds_primary_peer_on_primary(self, env_dual):
        svc, backend, primary, second, events = env_dual
        result = await svc.run_command(
            "issue", {"chat_id": PROBE_CHAT_ID, "device_label": "probe"}
        )
        address = result["address"]
        pubkey = list(primary.peers.keys())[0]
        primary.peers.clear()
        await svc.reconcile()
        assert pubkey in primary.peers
        assert primary.peers[pubkey] == address

    async def test_reconcile_removes_stale_from_both(self, env_dual):
        svc, backend, primary, second, events = env_dual
        await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        await svc.run_command(
            "issue", {"chat_id": PROBE_CHAT_ID, "device_label": "probe"}
        )
        await svc.run_command(
            "revoke", {"chat_id": CHAT, "device_label": "телефон"}
        )
        await svc.reconcile()
        pubkeys = list(primary.peers.keys()) + list(second.peers.keys())
        cur = await svc._db.conn.execute(
            "SELECT public_key FROM vpn_peers WHERE status = 'active'"
        )
        active = {row["public_key"] for row in await cur.fetchall()}
        for pk in pubkeys:
            assert pk in active
