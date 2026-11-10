from __future__ import annotations

from pathlib import Path

import pytest

from sa_home_bot.backup import apply as bapply
from sa_home_bot.backup import restore as rs
from sa_home_bot.backup import sealed
from sa_home_bot.backup.identity import (
    IdentityError,
    IdentityPublisher,
    collect_identity,
)
from sa_home_bot.config import (
    AwgAltConfig,
    BackupConfig,
    Settings,
    VpnConfig,
)
from sa_home_bot.node.instances import InstanceStore
from tests.unit.test_backup_identity import AWG_KEY, PUB, _settings


def _settings_awg_alt(tmp_path, *, jc=5, awg_alt=None):
    vpn = VpnConfig(
        endpoint_host="1.2.3.4",
        jc=jc,
        transports=["awg"],
        awg_alt=awg_alt,
    )
    settings = Settings(
        vpn=vpn,
        backup=BackupConfig(
            recipient_public_key=sealed.dump_key(PUB), partner="wooster"
        ),
        node={"id": "jeeves", "state_path": str(tmp_path / "jeeves" / "data" / "node-state.json")},
    )
    return settings


def test_collect_without_awg_alt_unchanged(tmp_path):
    settings = _settings(tmp_path, reality=False)
    doc = collect_identity(settings, awg_private_key=AWG_KEY, xray_config=None)
    doc2 = collect_identity(
        settings, awg_private_key=AWG_KEY, xray_config=None, awg_alt_private_key="ALT"
    )
    assert doc == doc2
    assert "awg_alt" not in doc


def test_collect_awg_alt_without_key_raises(tmp_path):
    awg_alt = AwgAltConfig(interface="awg1", port=51821)
    settings = _settings_awg_alt(tmp_path, awg_alt=awg_alt)
    with pytest.raises(IdentityError, match="второго awg-интерфейса"):
        collect_identity(settings, awg_private_key=AWG_KEY, xray_config=None)


def test_collect_awg_alt_with_key_includes_doc(tmp_path):
    awg_alt = AwgAltConfig(interface="awg1", port=51821)
    settings = _settings_awg_alt(tmp_path, awg_alt=awg_alt)
    doc = collect_identity(
        settings, awg_private_key=AWG_KEY, xray_config=None, awg_alt_private_key="ALTKEY"
    )
    assert doc["awg_alt"] == {"interface": "awg1", "private_key": "ALTKEY"}


async def test_publisher_calls_alt_reader_only_when_awg_alt(tmp_path):
    settings = _settings(tmp_path, reality=False)
    store = InstanceStore(tmp_path / "instances", "jeeves")

    awg_calls = 0
    alt_calls = 0

    async def read_awg():
        nonlocal awg_calls
        awg_calls += 1
        return AWG_KEY + "\n"

    async def read_alt():
        nonlocal alt_calls
        alt_calls += 1
        return "ALTKEY\n"

    pub = IdentityPublisher(
        settings,
        store,
        "jeeves",
        read_awg_private_key=read_awg,
        read_awg_alt_private_key=read_alt,
        read_xray=lambda: None,
    )
    await pub.publish_if_changed()
    assert awg_calls == 1
    assert alt_calls == 0

    settings2 = _settings_awg_alt(tmp_path, awg_alt=AwgAltConfig(interface="awg1"))
    pub2 = IdentityPublisher(
        settings2,
        store,
        "jeeves",
        read_awg_private_key=read_awg,
        read_awg_alt_private_key=read_alt,
        read_xray=lambda: None,
    )
    await pub2.publish_if_changed()
    assert awg_calls == 2
    assert alt_calls == 1


async def test_apply_existing_awg_alt_replaces_only_private_key(tmp_path):
    AWG_PRIV, _ = sealed.generate_keypair()
    AWG_KEY = sealed.dump_key(AWG_PRIV)
    ALT_PRIV, _ = sealed.generate_keypair()
    ALT_KEY = sealed.dump_key(ALT_PRIV)

    settings = _settings_awg_alt(tmp_path, awg_alt=AwgAltConfig(interface="awg1"))
    ident = collect_identity(
        settings, awg_private_key=AWG_KEY, xray_config=None, awg_alt_private_key=ALT_KEY
    )
    bundle = {
        "format": "sa-home-bot/vpn-restore/1",
        "node": "jeeves",
        "identity_label": "latest",
        "identity": ident,
        "snapshot": None,
    }

    awg1_conf = Path("/etc/amnezia/amneziawg/awg1.conf")
    old_content = (
        "[Interface]\n"
        "Address = 10.9.1.1/24\n"
        "ListenPort = 51821\n"
        "PrivateKey = OLDKEY\n"
        "MTU = 1420\n"
        "Jc = 5\n"
        "Jmin = 40\n"
        "Jmax = 70\n"
        "S1 = 50\n"
        "S2 = 80\n"
        "S3 = 0\n"
        "S4 = 0\n"
        "H1 = 5\n"
        "H2 = 6\n"
        "H3 = 7\n"
        "H4 = 8\n"
        "PostUp = iptables ...\n"
        "PostDown = iptables ...\n"
    )

    class FakeIO:
        def __init__(self, files=None, active=()):
            self.files = dict(files or {})
            self.active = set(active)
            self.restarted = []

        def read_root(self, path):
            return self.files.get(Path(path))

        def write_root(self, path, text):
            self.files[Path(path)] = text

        def unit_active(self, unit, *, user=False):
            return unit in self.active

        def restart(self, unit, *, user=False):
            self.restarted.append(unit)

    io = FakeIO({awg1_conf: old_content}, active={"awg-quick@awg1"})
    cfg = tmp_path / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[node]\nid = "jeeves"\n\n[vpn]\ninterface = "awg0"\n')

    await bapply.apply_bundle(bundle, settings, config_path=cfg, io=io, log=lambda m: None)

    new_content = io.files[awg1_conf]
    assert f"PrivateKey = {ALT_KEY}" in new_content
    assert "OLDKEY" not in new_content
    assert "Address = 10.9.1.1/24" in new_content
    assert "ListenPort = 51821" in new_content
    assert "MTU = 1420" in new_content
    assert "Jc = 5" in new_content
    assert "PostUp = iptables ..." in new_content
    assert new_content.count("PrivateKey") == 1
    assert "awg-quick@awg1" in io.restarted


async def test_apply_creates_awg_alt_conf_when_absent(tmp_path, monkeypatch):
    AWG_PRIV, _ = sealed.generate_keypair()
    AWG_KEY = sealed.dump_key(AWG_PRIV)
    ALT_PRIV, _ = sealed.generate_keypair()
    ALT_KEY = sealed.dump_key(ALT_PRIV)

    settings = _settings_awg_alt(tmp_path, awg_alt=AwgAltConfig(interface="awg1", port=51821))
    ident = collect_identity(
        settings, awg_private_key=AWG_KEY, xray_config=None, awg_alt_private_key=ALT_KEY
    )
    bundle = {
        "format": "sa-home-bot/vpn-restore/1",
        "node": "jeeves",
        "identity_label": "latest",
        "identity": ident,
        "snapshot": None,
    }

    class FakeIO:
        def __init__(self):
            self.files = {}
            self.active = set()
            self.restarted = []

        def read_root(self, path):
            return self.files.get(Path(path))

        def write_root(self, path, text):
            self.files[Path(path)] = text

        def unit_active(self, unit, *, user=False):
            return unit in self.active

        def restart(self, unit, *, user=False):
            self.restarted.append(unit)

    io = FakeIO()
    cfg = tmp_path / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        '[node]\nid = "jeeves"\n\n[vpn]\ninterface = "awg0"\nmtu = 1420\n'
    )

    monkeypatch.setattr(bapply, "default_route_iface", lambda: "eth0")
    await bapply.apply_bundle(bundle, settings, config_path=cfg, io=io, log=lambda m: None)

    awg1_conf = Path("/etc/amnezia/amneziawg/awg1.conf")
    assert awg1_conf in io.files
    content = io.files[awg1_conf]
    assert "ListenPort = 51821" in content
    assert f"PrivateKey = {ALT_KEY}" in content
    assert "S3 = 0" in content
    assert "PostUp = iptables" in content
    assert io.restarted == []


async def test_apply_awg_alt_missing_default_route_raises(tmp_path, monkeypatch):
    AWG_PRIV, _ = sealed.generate_keypair()
    AWG_KEY = sealed.dump_key(AWG_PRIV)
    ALT_PRIV, _ = sealed.generate_keypair()
    ALT_KEY = sealed.dump_key(ALT_PRIV)

    settings = _settings_awg_alt(tmp_path, awg_alt=AwgAltConfig(interface="awg1"))
    ident = collect_identity(
        settings, awg_private_key=AWG_KEY, xray_config=None, awg_alt_private_key=ALT_KEY
    )
    bundle = {
        "format": "sa-home-bot/vpn-restore/1",
        "node": "jeeves",
        "identity_label": "latest",
        "identity": ident,
        "snapshot": None,
    }

    class FakeIO:
        def __init__(self):
            self.files = {}
            self.active = set()
            self.restarted = []

        def read_root(self, path):
            return self.files.get(Path(path))

        def write_root(self, path, text):
            self.files[Path(path)] = text

        def unit_active(self, unit, *, user=False):
            return unit in self.active

        def restart(self, unit, *, user=False):
            self.restarted.append(unit)

    io = FakeIO()
    cfg = tmp_path / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[node]\nid = "jeeves"\n\n[vpn]\ninterface = "awg0"\n')

    monkeypatch.setattr(bapply, "default_route_iface", lambda: None)
    with pytest.raises(bapply.ApplyError, match="внешний интерфейс для второго awg"):
        await bapply.apply_bundle(bundle, settings, config_path=cfg, io=io, log=lambda m: None)


def test_summarize_bundle_awg_alt_no_private_key(tmp_path):
    AWG_PRIV, _ = sealed.generate_keypair()
    AWG_KEY = sealed.dump_key(AWG_PRIV)
    ALT_PRIV, _ = sealed.generate_keypair()
    ALT_KEY = sealed.dump_key(ALT_PRIV)
    ALT_PUB = sealed.dump_key(sealed.public_from_private(ALT_PRIV))

    settings = _settings_awg_alt(tmp_path, awg_alt=AwgAltConfig(interface="awg1"))
    ident = collect_identity(
        settings, awg_private_key=AWG_KEY, xray_config=None, awg_alt_private_key=ALT_KEY
    )
    bundle = {
        "format": "sa-home-bot/vpn-restore/1",
        "node": "jeeves",
        "identity_label": "latest",
        "identity": ident,
        "snapshot": None,
    }

    summary = rs.summarize_bundle(bundle)
    assert "awg_alt: интерфейс awg1, публичный ключ сервера" in summary
    assert ALT_PUB in summary
    assert ALT_KEY not in summary
