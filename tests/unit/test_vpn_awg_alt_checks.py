from __future__ import annotations

import pytest_asyncio

from sa_home_bot.config import AwgAltConfig, Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.vpn import subscription as subs
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
        self.fail_server_pubkey = False

    async def server_public_key(self) -> str:
        if self.fail_server_pubkey:
            raise ProtoError(ERR_BAD_REQUEST, "server_public_key failed")
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


class TestAwgSubnets:
    def test_without_awg_alt(self):
        cfg = VpnConfig(subnet="10.9.0.0/24", endpoint_host="1.2.3.4")
        assert cfg.awg_subnets() == ("10.9.0.0/24",)

    def test_with_awg_alt(self):
        cfg = VpnConfig(
            subnet="10.9.0.0/24",
            endpoint_host="1.2.3.4",
            awg_alt=AwgAltConfig(subnet="10.9.1.0/24"),
        )
        assert cfg.awg_subnets() == ("10.9.0.0/24", "10.9.1.0/24")


class TestMarkBroken:
    async def test_alt_peer_not_broken_with_correct_key(self, env_dual):
        svc, backend, primary, second, events = env_dual
        result = await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        address = result["address"]
        assert address.startswith("10.9.1.")
        list(second.peers.keys())[0]

        devices = [{"device_label": "телефон", "transport": TRANSPORT_AWG}]
        marked = await svc._mark_broken(CHAT, devices, withheld=False)
        assert marked[0]["broken"] is False

    async def test_alt_peer_broken_with_wrong_key(self, env_dual):
        svc, backend, primary, second, events = env_dual
        result = await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        address = result["address"]
        assert address.startswith("10.9.1.")
        list(second.peers.keys())[0]

        await svc._db.conn.execute(
            "UPDATE vpn_peers SET server_pubkey = 'wrong-key' WHERE chat_id = ?",
            (CHAT,),
        )
        await svc._db.conn.commit()

        devices = [{"device_label": "телефон", "transport": TRANSPORT_AWG}]
        marked = await svc._mark_broken(CHAT, devices, withheld=False)
        assert marked[0]["broken"] is True

    async def test_primary_peer_unchanged_without_awg_alt(self, env_single):
        svc, backend, events = env_single
        result = await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        address = result["address"]
        assert address.startswith("10.9.0.")
        list(backend.peers.keys())[0]

        devices = [{"device_label": "телефон", "transport": TRANSPORT_AWG}]
        marked = await svc._mark_broken(CHAT, devices, withheld=False)
        assert marked[0]["broken"] is False

    async def test_server_pubkey_failure_returns_devices_unchanged(self, env_dual):
        svc, backend, primary, second, events = env_dual
        result = await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        address = result["address"]
        assert address.startswith("10.9.1.")
        list(second.peers.keys())[0]

        second.fail_server_pubkey = True

        devices = [{"device_label": "телефон", "transport": TRANSPORT_AWG}]
        marked = await svc._mark_broken(CHAT, devices, withheld=False)
        assert marked[0]["broken"] is False


class TestCheckRestore:
    async def test_first_start_with_awg_alt_does_not_mark_alt_peers_affected(self, env_dual):
        svc, backend, primary, second, events = env_dual
        await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        await svc.run_command(
            "issue", {"chat_id": PROBE_CHAT_ID, "device_label": "probe"}
        )

        affected = await svc.check_restore()
        alt_affected = [a for a in affected if a["device_label"] == "телефон"]
        assert len(alt_affected) == 0

    async def test_alt_key_change_affects_only_alt_peers(self, env_dual):
        svc, backend, primary, second, events = env_dual
        await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        await svc.run_command(
            "issue", {"chat_id": PROBE_CHAT_ID, "device_label": "probe"}
        )

        await svc.check_restore()
        events.clear()

        second.server_pub = "new-second-pub"
        affected = await svc.check_restore()

        alt_affected = [a for a in affected if a["device_label"] == "телефон"]
        primary_affected = [a for a in affected if a["device_label"] == "probe"]
        assert len(alt_affected) == 1
        assert len(primary_affected) == 0

    async def test_primary_key_change_affects_only_primary_peers(self, env_dual):
        svc, backend, primary, second, events = env_dual
        await svc.run_command(
            "issue", {"chat_id": CHAT, "device_label": "телефон"}
        )
        await svc.run_command(
            "issue", {"chat_id": PROBE_CHAT_ID, "device_label": "probe"}
        )

        await svc.check_restore()
        events.clear()

        primary.server_pub = "new-primary-pub"
        affected = await svc.check_restore()

        alt_affected = [a for a in affected if a["device_label"] == "телефон"]
        primary_affected = [a for a in affected if a["device_label"] == "probe"]
        assert len(alt_affected) == 0
        assert len(primary_affected) == 0


class TestDetectStatus:
    def test_alt_subnet_with_tuple_returns_awg(self):
        nodes = (
            subs.NodeInfo("local", "Local", "198.51.100.1", local=True),
            subs.NodeInfo("remote", "Remote", "198.51.100.2"),
        )
        status = subs.detect_status("10.9.1.5", nodes, ("10.9.0.0/24", "10.9.1.0/24"))
        assert status == subs.VpnStatus(True, "Local", "AmneziaVPN")

    def test_primary_subnet_with_tuple_returns_awg(self):
        nodes = (
            subs.NodeInfo("local", "Local", "198.51.100.1", local=True),
            subs.NodeInfo("remote", "Remote", "198.51.100.2"),
        )
        status = subs.detect_status("10.9.0.5", nodes, ("10.9.0.0/24", "10.9.1.0/24"))
        assert status == subs.VpnStatus(True, "Local", "AmneziaVPN")

    def test_alt_subnet_with_string_returns_awg(self):
        nodes = (
            subs.NodeInfo("local", "Local", "198.51.100.1", local=True),
            subs.NodeInfo("remote", "Remote", "198.51.100.2"),
        )
        status = subs.detect_status("10.9.1.5", nodes, "10.9.1.0/24")
        assert status == subs.VpnStatus(True, "Local", "AmneziaVPN")

    def test_non_vpn_address_returns_off(self):
        nodes = (
            subs.NodeInfo("local", "Local", "198.51.100.1", local=True),
            subs.NodeInfo("remote", "Remote", "198.51.100.2"),
        )
        status = subs.detect_status("203.0.113.9", nodes, ("10.9.0.0/24", "10.9.1.0/24"))
        assert status == subs.VpnStatus(False)

    def test_vless_address_returns_vless(self):
        nodes = (
            subs.NodeInfo("local", "Local", "198.51.100.1", local=True),
            subs.NodeInfo("remote", "Remote", "198.51.100.2"),
        )
        status = subs.detect_status("198.51.100.1", nodes, ("10.9.0.0/24", "10.9.1.0/24"))
        assert status == subs.VpnStatus(True, "Local", "VLESS · Hiddify")

    def test_peer_vless_address_returns_vless_no_method(self):
        nodes = (
            subs.NodeInfo("local", "Local", "198.51.100.1", local=True),
            subs.NodeInfo("remote", "Remote", "198.51.100.2"),
        )
        status = subs.detect_status("198.51.100.2", nodes, ("10.9.0.0/24", "10.9.1.0/24"))
        assert status == subs.VpnStatus(True, "Remote", "")
