"""Бэкап статической identity vpn-сервера (39.0.8(b)): сбор, ревизии, репликация напарнику."""

from __future__ import annotations

import json

import pytest

from sa_home_bot.backup import sealed
from sa_home_bot.backup.identity import (
    IDENTITY_SERVICE,
    IdentityError,
    IdentityPublisher,
    canonical_bytes,
    collect_identity,
    open_identity,
    parse_package,
    publishing_enabled,
    replicator_hooks,
)
from sa_home_bot.backup.store import BackupStore
from sa_home_bot.config import BackupConfig, RealityTransportConfig, Settings, VpnConfig
from sa_home_bot.node.instances import InstanceStore
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.replication import ConfigReplicator
from sa_home_bot.node.supervisor import Supervisor
from tests.unit.test_replication import FakePeer

PRIV, PUB = sealed.generate_keypair()
AWG_KEY = "AWGPRIVATEKEYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
XRAY = {
    "inbounds": [
        {"tag": "api-in", "protocol": "dokodemo-door"},
        {
            "tag": "reality-in",
            "streamSettings": {"realitySettings": {"privateKey": "REALPRIV", "shortIds": ["ab12"]}},
        },
    ]
}


def _settings(tmp_path, *, node="jeeves", partner="wooster", pub=True, reality=True, jc=5):
    vpn = VpnConfig(
        endpoint_host="1.2.3.4",
        jc=jc,
        transports=["awg", "reality"] if reality else ["awg"],
        reality=RealityTransportConfig(server_public_key="PUBK", short_id="ab12")
        if reality else None,
    )
    return Settings(
        vpn=vpn,
        backup=BackupConfig(
            recipient_public_key=sealed.dump_key(PUB) if pub else "", partner=partner
        ),
        node={"id": node, "state_path": str(tmp_path / node / "data" / "node-state.json")},
    )


class AwgKey:
    def __init__(self, key=AWG_KEY):
        self.key = key
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        return self.key + "\n"


def _publisher(settings, tmp_path, node="jeeves", awg=None, xray=XRAY):
    store = InstanceStore(tmp_path / node / "instances", node)
    pub = IdentityPublisher(
        settings, store, node,
        read_awg_private_key=awg or AwgKey(), read_xray=lambda: xray,
    )
    return pub, store


# --- сбор -------------------------------------------------------------------


def test_collect_has_all_static_parts(tmp_path):
    doc = collect_identity(_settings(tmp_path), awg_private_key=AWG_KEY, xray_config=XRAY)
    assert doc["awg"]["private_key"] == AWG_KEY
    assert doc["awg"]["obfuscation"]["jc"] == 5 and doc["awg"]["obfuscation"]["h4"] == 8
    assert doc["reality"] == {
        "inbound_tag": "reality-in", "private_key": "REALPRIV", "short_ids": ["ab12"],
    }
    assert doc["config"]["vpn"]["endpoint_host"] == "1.2.3.4"
    assert "reality" not in doc["config"]["vpn"]
    assert doc["config"]["vpn_reality"]["short_id"] == "ab12"


def test_collect_does_not_include_peers(tmp_path):
    doc = collect_identity(_settings(tmp_path), awg_private_key=AWG_KEY, xray_config=XRAY)
    assert "peers" not in json.dumps(doc).lower()


def test_collect_awg_only_node_needs_no_xray(tmp_path):
    doc = collect_identity(
        _settings(tmp_path, reality=False), awg_private_key=AWG_KEY, xray_config=None
    )
    assert "reality" not in doc


def test_collect_fails_without_secrets(tmp_path):
    with pytest.raises(IdentityError):
        collect_identity(_settings(tmp_path), awg_private_key="", xray_config=XRAY)
    with pytest.raises(IdentityError):
        collect_identity(_settings(tmp_path), awg_private_key=AWG_KEY, xray_config={})


# --- публикация и стабильность ревизии --------------------------------------


async def test_publish_writes_sealed_package_readable_only_by_alfred(tmp_path):
    pub, store = _publisher(_settings(tmp_path), tmp_path)
    assert await pub.publish_if_changed() is True
    data = store.read_package(IDENTITY_SERVICE, "jeeves")
    assert AWG_KEY.encode() not in data and b"REALPRIV" not in data
    source, blob = parse_package(data)
    assert source == "jeeves"
    doc = open_identity(PRIV, blob)
    assert doc["awg"]["private_key"] == AWG_KEY
    other_priv, _ = sealed.generate_keypair()
    with pytest.raises(IdentityError):
        open_identity(other_priv, blob)


async def test_revision_is_stable_while_identity_is_unchanged(tmp_path):
    pub, store = _publisher(_settings(tmp_path), tmp_path)
    await pub.publish_if_changed()
    assert store.refresh(IDENTITY_SERVICE, "jeeves").rev == 1
    first = store.read_package(IDENTITY_SERVICE, "jeeves")

    assert await pub.publish_if_changed() is False
    assert store.read_package(IDENTITY_SERVICE, "jeeves") == first
    assert store.refresh(IDENTITY_SERVICE, "jeeves") is None


async def test_revision_grows_when_identity_changes(tmp_path):
    pub, store = _publisher(_settings(tmp_path), tmp_path)
    await pub.publish_if_changed()
    store.refresh(IDENTITY_SERVICE, "jeeves")
    pub2 = IdentityPublisher(
        _settings(tmp_path, jc=9), store, "jeeves",
        read_awg_private_key=AwgKey(), read_xray=lambda: XRAY,
    )
    assert await pub2.publish_if_changed() is True
    assert store.refresh(IDENTITY_SERVICE, "jeeves").rev == 2


async def test_failed_collection_keeps_previous_package(tmp_path):
    pub, store = _publisher(_settings(tmp_path), tmp_path)
    await pub.publish_if_changed()
    before = store.read_package(IDENTITY_SERVICE, "jeeves")
    broken, _ = _publisher(_settings(tmp_path), tmp_path, xray={})
    assert await broken.publish_if_changed() is False
    assert store.read_package(IDENTITY_SERVICE, "jeeves") == before


def test_canonical_bytes_is_deterministic():
    assert canonical_bytes({"b": 1, "a": 2}) == canonical_bytes({"a": 2, "b": 1})


# --- включение --------------------------------------------------------------


def test_disabled_without_key_or_partner(tmp_path):
    assert publishing_enabled(_settings(tmp_path), True)
    assert not publishing_enabled(_settings(tmp_path, pub=False), True)
    assert not publishing_enabled(_settings(tmp_path, partner=""), True)
    assert not publishing_enabled(_settings(tmp_path), False)


def test_hooks_empty_without_partner_and_passive_only_without_key(tmp_path):
    store = InstanceStore(tmp_path / "i", "jeeves")
    no_partner = _settings(tmp_path, partner="")
    assert replicator_hooks(no_partner, store, "jeeves", vpn_assigned=True) == {}
    hooks = replicator_hooks(_settings(tmp_path, pub=False), store, "jeeves", vpn_assigned=True)
    assert "before_announce" not in hooks and "passive" in hooks
    hooks = replicator_hooks(_settings(tmp_path), store, "jeeves", vpn_assigned=True)
    assert "before_announce" in hooks
    hooks = replicator_hooks(_settings(tmp_path), store, "jeeves", vpn_assigned=False)
    assert "before_announce" not in hooks


def test_bad_recipient_key_disables_publishing_not_the_node(tmp_path):
    s = _settings(tmp_path)
    s.backup.recipient_public_key = "не-ключ"
    store = InstanceStore(tmp_path / "i", "jeeves")
    hooks = replicator_hooks(s, store, "jeeves", vpn_assigned=True)
    assert "before_announce" not in hooks


# --- репликация к напарнику -------------------------------------------------


def _node(tmp_path, node, partner, peers=(), assignments=("vpn",), awg=None, **kw):
    settings = _settings(tmp_path, node=node, partner=partner, **kw)
    store = InstanceStore(tmp_path / node / "instances", node)
    events: list = []

    async def emit(t, d):
        events.append((t, d))

    hooks = replicator_hooks(settings, store, node, vpn_assigned=True)
    if "before_announce" in hooks:  # подменяем чтение секретов
        pub = IdentityPublisher(
            settings, store, node,
            read_awg_private_key=awg or AwgKey(), read_xray=lambda: XRAY,
        )
        hooks["before_announce"] = pub.publish_if_changed
    rep = ConfigReplicator(
        node, store,
        supervisor=Supervisor(list(assignments), None, emit=emit),
        router=NodeRouter(node, peers={p.name: p for p in peers}),
        emit=emit, **hooks,
    )
    return rep, store, events, settings


def _source_peer(rep, store, node="jeeves"):
    payload = rep.package_payload(IDENTITY_SERVICE, node)
    from sa_home_bot.node.instances import InstanceMeta

    meta = InstanceMeta.from_dict(payload["meta"])
    return FakePeer(
        node, state={"instances": rep.local_revisions()},
        package=payload["content"].encode(), meta=meta,
    )


async def test_partner_stores_sealed_copy_passively(tmp_path):
    src, src_store, events, _ = _node(tmp_path, "jeeves", "wooster")
    await src.announce_local_changes()
    assert [e[1]["service"] for e in events] == [IDENTITY_SERVICE]  # объявление без секрета
    assert "content" not in events[0][1]

    peer = _source_peer(src, src_store)
    dst, dst_store, _, dst_settings = _node(
        tmp_path, "wooster", "jeeves", peers=[peer], assignments=("vpn",), pub=False
    )
    await dst.sync_from_peers()

    stored = BackupStore(tmp_path / "wooster" / "data" / "backups").load_identity("jeeves")
    assert stored is not None
    assert stored.meta["source"] == "jeeves" and stored.meta["rev"] == 1
    sealed_file = tmp_path / "wooster/data/backups/jeeves/identity.sealed"
    assert sealed_file.stat().st_mode & 0o777 == 0o600
    doc = open_identity(PRIV, stored.blob)
    assert doc["awg"]["private_key"] == AWG_KEY
    # пассивность: служба vpn на напарнике не тронута, слот не создан
    assert dst_store.read_meta(IDENTITY_SERVICE, "jeeves").rev == 1


async def test_announced_event_triggers_pull_on_partner_only(tmp_path):
    from sa_home_bot.node.replication import EVENT_INSTANCE_CONFIG_CHANGED
    from sa_home_bot.proto.messages import Address, make_event

    src, src_store, events, _ = _node(tmp_path, "jeeves", "wooster")
    await src.announce_local_changes()
    peer = _source_peer(src, src_store)
    env = make_event(
        EVENT_INSTANCE_CONFIG_CHANGED, events[0][1], src=Address(node="jeeves", service="node")
    )

    partner, p_store, _, _ = _node(tmp_path, "wooster", "jeeves", peers=[peer])
    await partner.on_config_changed(env)
    assert p_store.read_package(IDENTITY_SERVICE, "jeeves") is not None

    # alfred (не напарник: partner не задан) и нода с другим партнёром — не тянут
    stranger, s_store, _, _ = _node(tmp_path, "alfred", "", peers=[peer], assignments=("monitor",))
    await stranger.on_config_changed(env)
    other, o_store, _, _ = _node(tmp_path, "mycraft", "someone", peers=[peer])
    await other.on_config_changed(env)
    for store in (s_store, o_store):
        assert store.read_package(IDENTITY_SERVICE, "jeeves") is None
    assert BackupStore(tmp_path / "alfred" / "data" / "backups").load_identity("jeeves") is None


async def test_unchanged_identity_does_not_reannounce_or_restore(tmp_path):
    src, src_store, events, _ = _node(tmp_path, "jeeves", "wooster")
    await src.announce_local_changes()
    events.clear()
    await src.announce_local_changes()
    assert events == []


async def test_rebuilt_source_with_restarted_revisions_overrides_stale_copy(tmp_path):
    """jeeves пересобрали: ревизия снова 1, но свежее — напарник обязан принять."""
    src, src_store, _, _ = _node(tmp_path, "jeeves", "wooster")
    await src.announce_local_changes()
    dst, dst_store, _, _ = _node(
        tmp_path, "wooster", "jeeves", peers=[_source_peer(src, src_store)]
    )
    await dst.sync_from_peers()
    # у напарника «старая» копия с высокой ревизией
    old = dst_store.read_meta(IDENTITY_SERVICE, "jeeves")
    from dataclasses import replace

    dst_store._write_meta(replace(old, rev=9, updated_at="2000-01-01T00:00:00+00:00"))

    # новая сборка jeeves с другой identity (другой ключ) — с нуля, ревизия 1
    import shutil

    shutil.rmtree(tmp_path / "jeeves" / "instances")
    src2, src2_store, _, _ = _node(tmp_path, "jeeves", "wooster", awg=AwgKey("NEWKEY" + "A" * 38))
    await src2.announce_local_changes()
    dst._router.peers["jeeves"] = _source_peer(src2, src2_store)
    await dst.sync_from_peers()

    store = BackupStore(tmp_path / "wooster" / "data" / "backups")
    assert store.load_identity("jeeves").meta["rev"] == 1
    assert open_identity(PRIV, store.load_identity("jeeves").blob)["awg"]["private_key"].startswith(
        "NEWKEY"
    )
    # прежняя копия не потеряна — лежит в истории
    assert len(store.history("jeeves")) == 1


async def test_partner_ignores_packages_of_other_nodes(tmp_path):
    src, src_store, _, _ = _node(tmp_path, "jeeves", "wooster")
    await src.announce_local_changes()
    dst, dst_store, _, _ = _node(
        tmp_path, "wooster", "mycraft", peers=[_source_peer(src, src_store)]
    )
    await dst.sync_from_peers()
    assert dst_store.read_package(IDENTITY_SERVICE, "jeeves") is None


async def test_disabled_publisher_publishes_nothing(tmp_path):
    rep, store, events, _ = _node(tmp_path, "jeeves", "wooster", pub=False)
    await rep.announce_local_changes()
    assert events == [] and store.known() == []
