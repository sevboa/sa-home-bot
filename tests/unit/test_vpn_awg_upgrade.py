from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio

from sa_home_bot.config import AwgAltConfig, Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.vpn.awg import DualAwgBackend
from sa_home_bot.vpn.protocol import PROBE_CHAT_ID, TRANSPORT_AWG
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


class TestReissueWithKeepOld:
    async def _create_legacy_peer(self, svc, primary, chat_id, label):
        priv, pub = await primary.generate_keypair()
        address = "10.9.0.5"
        now = datetime.now(UTC).isoformat()
        await svc._db.conn.execute(
            "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
            "status, created_at, server, server_pubkey) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (chat_id, label, TRANSPORT_AWG, pub, address, now, "test-node", "primary-pub"),
        )
        await svc._db.conn.commit()
        await primary.add_peer(pub, address)
        return pub, address

    async def test_reissue_legacy_awg_with_keep_old_creates_retiring(
        self, env_dual
    ):
        svc, backend, primary, second, events = env_dual
        old_pubkey, old_address = await self._create_legacy_peer(
            svc, primary, CHAT, "телефон"
        )
        assert old_address == "10.9.0.5"
        assert old_pubkey in primary.peers

        result2 = await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )
        assert result2["address"].startswith("10.9.1.")
        new_address = result2["address"]
        new_pubkey = list(second.peers.keys())[0]

        assert old_address != new_address
        assert old_pubkey != new_pubkey

        cur = await svc._db.conn.execute(
            "SELECT status, revoked_at FROM vpn_peers WHERE public_key = ?",
            (old_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "retiring"
        assert row["revoked_at"] is None

        cur = await svc._db.conn.execute(
            "SELECT status FROM vpn_peers WHERE public_key = ?",
            (new_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "active"

        assert old_pubkey in primary.peers
        assert new_pubkey in second.peers

        devices = await svc._peers_for_chat(CHAT)
        assert [(d["device_label"], d["awg_legacy"]) for d in devices] == [("телефон", False)]
        snapshot = await svc.run_command("peers", {})
        statuses = sorted(p["status"] for p in snapshot["peers"] if p["chat_id"] == CHAT)
        assert statuses == ["active", "retiring"]

    async def test_reissue_legacy_awg_without_keep_old_expires_old(
        self, env_dual
    ):
        svc, backend, primary, second, events = env_dual
        old_pubkey, old_address = await self._create_legacy_peer(
            svc, primary, CHAT, "телефон"
        )

        result2 = await svc.run_command(
            "reissue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        result2["address"]
        new_pubkey = list(second.peers.keys())[0]

        cur = await svc._db.conn.execute(
            "SELECT status FROM vpn_peers WHERE public_key = ?",
            (old_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "expired"

        assert old_pubkey not in primary.peers
        assert new_pubkey in second.peers

    async def test_reissue_already_in_alt_subnet_expires_old(
        self, env_dual
    ):
        svc, backend, primary, second, events = env_dual
        result = await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        result["address"]
        old_pubkey = list(second.peers.keys())[0]

        result2 = await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )
        result2["address"]
        new_pubkey = list(second.peers.keys())[0]

        result3 = await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )
        result3["address"]
        newest_pubkey = list(second.peers.keys())[0]

        cur = await svc._db.conn.execute(
            "SELECT status FROM vpn_peers WHERE public_key = ?",
            (old_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "expired"

        cur = await svc._db.conn.execute(
            "SELECT status FROM vpn_peers WHERE public_key = ?",
            (new_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "expired"

        assert new_pubkey not in second.peers
        assert newest_pubkey in second.peers

    async def test_reissue_without_awg_alt_unchanged(self, env_single):
        svc, backend, events = env_single
        result = await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        result["address"]
        old_pubkey = list(backend.peers.keys())[0]

        result2 = await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )
        result2["address"]
        new_pubkey = list(backend.peers.keys())[0]

        cur = await svc._db.conn.execute(
            "SELECT status FROM vpn_peers WHERE public_key = ?",
            (old_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "expired"

        assert old_pubkey not in backend.peers
        assert new_pubkey in backend.peers


class TestPeersForChat:
    async def _create_legacy_peer(self, svc, primary, chat_id, label):
        priv, pub = await primary.generate_keypair()
        address = "10.9.0.5"
        now = datetime.now(UTC).isoformat()
        await svc._db.conn.execute(
            "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
            "status, created_at, server, server_pubkey) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (chat_id, label, TRANSPORT_AWG, pub, address, now, "test-node", "primary-pub"),
        )
        await svc._db.conn.commit()
        await primary.add_peer(pub, address)
        return pub, address

    async def test_peers_for_chat_shows_awg_legacy_false_for_new(
        self, env_dual
    ):
        svc, backend, primary, second, events = env_dual
        old_pubkey, old_address = await self._create_legacy_peer(
            svc, primary, CHAT, "телефон"
        )

        await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )

        peers = await svc._peers_for_chat(CHAT)
        assert len(peers) == 1
        assert peers[0]["device_label"] == "телефон"
        assert peers[0]["awg_legacy"] is False

    async def test_peers_for_chat_without_awg_alt_shows_false(
        self, env_single
    ):
        svc, backend, events = env_single
        await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )

        peers = await svc._peers_for_chat(CHAT)
        assert len(peers) == 1
        assert peers[0]["awg_legacy"] is False

    async def test_probe_never_gets_awg_legacy_true(self, env_dual):
        svc, backend, primary, second, events = env_dual
        await svc.run_command(
            "issue", {"chat_id": PROBE_CHAT_ID, "device_label": "probe"}
        )

        peers = await svc._peers_for_chat(PROBE_CHAT_ID)
        assert len(peers) == 1
        assert peers[0]["awg_legacy"] is False


class TestReconcile:
    async def _create_legacy_peer(self, svc, primary, chat_id, label):
        priv, pub = await primary.generate_keypair()
        address = "10.9.0.5"
        now = datetime.now(UTC).isoformat()
        await svc._db.conn.execute(
            "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
            "status, created_at, server, server_pubkey) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (chat_id, label, TRANSPORT_AWG, pub, address, now, "test-node", "primary-pub"),
        )
        await svc._db.conn.commit()
        await primary.add_peer(pub, address)
        return pub, address

    async def test_reconcile_keeps_retiring_on_backend(self, env_dual):
        svc, backend, primary, second, events = env_dual
        old_pubkey, old_address = await self._create_legacy_peer(
            svc, primary, CHAT, "телефон"
        )

        await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )
        new_pubkey = list(second.peers.keys())[0]
        new_address = second.peers[new_pubkey]

        second.peers.clear()
        primary.peers.clear()
        await svc.reconcile()

        assert old_pubkey in primary.peers
        assert primary.peers[old_pubkey] == old_address
        assert new_pubkey in second.peers
        assert second.peers[new_pubkey] == new_address
        assert old_address != new_address


class TestRetireUpgraded:
    async def _create_legacy_peer(self, svc, primary, chat_id, label):
        priv, pub = await primary.generate_keypair()
        address = "10.9.0.5"
        now = datetime.now(UTC).isoformat()
        await svc._db.conn.execute(
            "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
            "status, created_at, server, server_pubkey) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (chat_id, label, TRANSPORT_AWG, pub, address, now, "test-node", "primary-pub"),
        )
        await svc._db.conn.commit()
        await primary.add_peer(pub, address)
        return pub, address

    async def _set_handshake(self, svc, pubkey, ts):
        await svc._db.conn.execute(
            "UPDATE vpn_peers SET last_handshake_at = ? WHERE public_key = ?",
            (ts.isoformat(), pubkey),
        )
        await svc._db.conn.commit()

    async def test_retire_upgraded_no_handshake_does_nothing(self, env_dual):
        svc, backend, primary, second, events = env_dual
        old_pubkey, old_address = await self._create_legacy_peer(
            svc, primary, CHAT, "телефон"
        )

        await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )
        list(second.peers.keys())[0]

        now = datetime.now(UTC)
        await svc._retire_upgraded(now)

        cur = await svc._db.conn.execute(
            "SELECT status, revoked_at FROM vpn_peers WHERE public_key = ?",
            (old_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "retiring"
        assert row["revoked_at"] is None

        assert old_pubkey in primary.peers

    async def test_retire_upgraded_with_handshake_sets_revoked_at(
        self, env_dual
    ):
        svc, backend, primary, second, events = env_dual
        old_pubkey, old_address = await self._create_legacy_peer(
            svc, primary, CHAT, "телефон"
        )

        await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )
        new_pubkey = list(second.peers.keys())[0]

        now = datetime.now(UTC)
        await self._set_handshake(svc, new_pubkey, now)
        await svc._retire_upgraded(now)

        cur = await svc._db.conn.execute(
            "SELECT status, revoked_at FROM vpn_peers WHERE public_key = ?",
            (old_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "retiring"
        assert row["revoked_at"] is not None

        assert old_pubkey in primary.peers

    async def test_retire_upgraded_before_deadline_keeps_peer(self, env_dual):
        svc, backend, primary, second, events = env_dual
        old_pubkey, old_address = await self._create_legacy_peer(
            svc, primary, CHAT, "телефон"
        )

        await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )
        new_pubkey = list(second.peers.keys())[0]

        now = datetime.now(UTC)
        await self._set_handshake(svc, new_pubkey, now)
        await svc._retire_upgraded(now)

        before_deadline = now + timedelta(days=2)
        await svc._retire_upgraded(before_deadline)

        cur = await svc._db.conn.execute(
            "SELECT status FROM vpn_peers WHERE public_key = ?",
            (old_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "retiring"

        assert old_pubkey in primary.peers

    async def test_retire_upgraded_after_deadline_expires_and_removes(
        self, env_dual
    ):
        svc, backend, primary, second, events = env_dual
        old_pubkey, old_address = await self._create_legacy_peer(
            svc, primary, CHAT, "телефон"
        )

        await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )
        new_pubkey = list(second.peers.keys())[0]

        now = datetime.now(UTC)
        await self._set_handshake(svc, new_pubkey, now)
        await svc._retire_upgraded(now)

        after_deadline = now + timedelta(days=4)
        await svc._retire_upgraded(after_deadline)

        cur = await svc._db.conn.execute(
            "SELECT status, revoked_at FROM vpn_peers WHERE public_key = ?",
            (old_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "expired"
        assert row["revoked_at"] is not None

        assert old_pubkey not in primary.peers


class TestRevoke:
    async def _create_legacy_peer(self, svc, primary, chat_id, label):
        priv, pub = await primary.generate_keypair()
        address = "10.9.0.5"
        now = datetime.now(UTC).isoformat()
        await svc._db.conn.execute(
            "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
            "status, created_at, server, server_pubkey) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (chat_id, label, TRANSPORT_AWG, pub, address, now, "test-node", "primary-pub"),
        )
        await svc._db.conn.commit()
        await primary.add_peer(pub, address)
        return pub, address

    async def test_revoke_new_expires_retiring(self, env_dual):
        svc, backend, primary, second, events = env_dual
        old_pubkey, old_address = await self._create_legacy_peer(
            svc, primary, CHAT, "телефон"
        )

        await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )
        new_pubkey = list(second.peers.keys())[0]

        await svc.run_command(
            "revoke", {"chat_id": CHAT, "device_label": "телефон"}
        )

        cur = await svc._db.conn.execute(
            "SELECT status FROM vpn_peers WHERE public_key = ?",
            (old_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "expired"

        cur = await svc._db.conn.execute(
            "SELECT status FROM vpn_peers WHERE public_key = ?",
            (new_pubkey,),
        )
        row = await cur.fetchone()
        assert row["status"] == "revoked"

        assert old_pubkey not in primary.peers
        assert new_pubkey not in second.peers


class TestActiveAddresses:
    async def _create_legacy_peer(self, svc, primary, chat_id, label):
        priv, pub = await primary.generate_keypair()
        address = "10.9.0.5"
        now = datetime.now(UTC).isoformat()
        await svc._db.conn.execute(
            "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
            "status, created_at, server, server_pubkey) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (chat_id, label, TRANSPORT_AWG, pub, address, now, "test-node", "primary-pub"),
        )
        await svc._db.conn.commit()
        await primary.add_peer(pub, address)
        return pub, address

    async def test_active_addresses_includes_retiring(self, env_dual):
        svc, backend, primary, second, events = env_dual
        old_pubkey, old_address = await self._create_legacy_peer(
            svc, primary, CHAT, "телефон"
        )

        await svc.run_command(
            "reissue",
            {"chat_id": CHAT, "device_label": "телефон", "keep_old": True},
        )
        new_pubkey = list(second.peers.keys())[0]
        new_address = second.peers[new_pubkey]

        addresses = await svc._active_addresses()
        assert old_address in addresses
        assert new_address in addresses
