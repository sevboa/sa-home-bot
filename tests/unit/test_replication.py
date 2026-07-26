"""Репликация пакетов настроек: объявление правок, приём, догон, рестарт."""

from __future__ import annotations

from sa_home_bot.node.instances import InstanceMeta, InstanceStore, content_hash
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.replication import (
    ACTION_GET_INSTANCE_CONFIG,
    EVENT_INSTANCE_CONFIG_CHANGED,
    ConfigReplicator,
)
from sa_home_bot.node.service import NodeService
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.proto.messages import MSG_GET_STATE, Address, Envelope, make_event

PACKAGE = b'[telegram]\ntoken = "111:aaa"\n'
NEWER = b'[telegram]\ntoken = "222:bbb"\n'


class FakePeer:
    """Сосед, отвечающий заранее заданным состоянием и пакетом."""

    def __init__(self, node_id: str, *, state: dict | None = None, package: bytes | None = None,
                 meta: InstanceMeta | None = None, alive: bool = True):
        self.name = node_id
        self.endpoint = f"tcp://{node_id}:8710"
        self.alive = alive
        self.node_kind = "server"
        self._state = state or {}
        self._package = package
        self._meta = meta
        self.requests: list[str] = []

    def downtime_s(self):
        return None if self.alive else 999.0

    async def forward(self, env: Envelope) -> Envelope:
        if env.type == MSG_GET_STATE:
            self.requests.append("get_state")
            return Envelope(type="response", id=env.id, ok=True, payload=self._state)
        action = env.payload.get("action")
        self.requests.append(action)
        if action == ACTION_GET_INSTANCE_CONFIG and self._package is not None:
            return Envelope(
                type="response",
                id=env.id,
                ok=True,
                payload={"meta": self._meta.to_dict(), "content": self._package.decode()},
            )
        return Envelope(
            type="response", id=env.id, ok=False, error={"code": "bad_request", "message": "нет"}
        )


def _meta(rev: int, data: bytes, *, node: str, at: str = "2026-07-26T10:00:00+00:00"):
    return InstanceMeta(
        service="telegram-bot",
        instance="alfred",
        rev=rev,
        hash=content_hash(data),
        updated_at=at,
        origin_node=node,
    )


def _replicator(tmp_path, *, assignments: list[str], peers=(), node="jeeves"):
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    supervisor = Supervisor(assignments, None, emit=emit)
    router = NodeRouter(node, peers={p.name: p for p in peers})
    store = InstanceStore(tmp_path / "instances", node)
    rep = ConfigReplicator(node, store, supervisor=supervisor, router=router, emit=emit)
    return rep, store, supervisor, events


# --- объявление локальных правок --------------------------------------------


async def test_local_edit_is_announced_to_the_swarm(tmp_path):
    rep, store, _, events = _replicator(tmp_path, assignments=["telegram-bot@alfred"])
    path = store.package_path("telegram-bot", "alfred")
    path.parent.mkdir(parents=True)
    path.write_bytes(PACKAGE)

    await rep.announce_local_changes()
    assert [e[0] for e in events] == [EVENT_INSTANCE_CONFIG_CHANGED]
    assert events[0][1]["rev"] == 1


async def test_untouched_package_is_not_announced(tmp_path):
    rep, store, _, events = _replicator(tmp_path, assignments=["telegram-bot@alfred"])
    path = store.package_path("telegram-bot", "alfred")
    path.parent.mkdir(parents=True)
    path.write_bytes(PACKAGE)
    await rep.announce_local_changes()
    events.clear()

    await rep.announce_local_changes()
    assert events == []


# --- приём объявления -------------------------------------------------------


def _changed_event(meta: InstanceMeta, src: str) -> Envelope:
    return make_event(
        EVENT_INSTANCE_CONFIG_CHANGED, meta.to_dict(), src=Address(node=src, service="node")
    )


async def test_announced_revision_is_pulled_when_the_instance_is_ours(tmp_path):
    meta = _meta(3, PACKAGE, node="alfred")
    peer = FakePeer("alfred", package=PACKAGE, meta=meta)
    rep, store, _, _ = _replicator(
        tmp_path, assignments=["telegram-bot@alfred:standby"], peers=[peer]
    )

    await rep.on_config_changed(_changed_event(meta, "alfred"))
    assert store.read_package("telegram-bot", "alfred") == PACKAGE
    assert ACTION_GET_INSTANCE_CONFIG in peer.requests


async def test_standby_gets_the_settings_too(tmp_path):
    """Смысл резерва в том и состоит: он держит те же настройки, что активная."""
    meta = _meta(1, PACKAGE, node="alfred")
    peer = FakePeer("alfred", package=PACKAGE, meta=meta)
    rep, store, _, _ = _replicator(
        tmp_path, assignments=["telegram-bot@alfred:standby"], peers=[peer]
    )
    await rep.on_config_changed(_changed_event(meta, "alfred"))
    assert store.read_meta("telegram-bot", "alfred").rev == 1


async def test_foreign_instance_is_not_pulled(tmp_path):
    """Чужой бот — не наше дело: его токен нам незачем даже получать."""
    meta = _meta(3, PACKAGE, node="alfred")
    peer = FakePeer("alfred", package=PACKAGE, meta=meta)
    rep, store, _, _ = _replicator(tmp_path, assignments=["monitor"], peers=[peer])

    await rep.on_config_changed(_changed_event(meta, "alfred"))
    assert peer.requests == []
    assert store.read_package("telegram-bot", "alfred") is None


async def test_older_announced_revision_is_not_pulled(tmp_path):
    peer = FakePeer("alfred", package=PACKAGE, meta=_meta(1, PACKAGE, node="alfred"))
    rep, store, _, _ = _replicator(
        tmp_path, assignments=["telegram-bot@alfred:standby"], peers=[peer]
    )
    store.apply(_meta(5, NEWER, node="jeeves"), NEWER)
    peer.requests.clear()

    await rep.on_config_changed(_changed_event(_meta(1, PACKAGE, node="alfred"), "alfred"))
    assert peer.requests == []
    assert store.read_package("telegram-bot", "alfred") == NEWER


# --- догон ------------------------------------------------------------------


async def test_catch_up_pulls_newer_revision_from_a_peer(tmp_path):
    """Нода была выключена, когда настройки правили: событие до неё не доехало,
    но при следующем цикле она сама спросит соседей."""
    meta = _meta(7, NEWER, node="alfred")
    peer = FakePeer(
        "alfred", state={"instances": [meta.to_dict()]}, package=NEWER, meta=meta
    )
    rep, store, _, _ = _replicator(
        tmp_path, assignments=["telegram-bot@alfred:standby"], peers=[peer]
    )
    store.apply(_meta(2, PACKAGE, node="alfred"), PACKAGE)

    await rep.sync_from_peers()
    assert store.read_package("telegram-bot", "alfred") == NEWER
    assert store.read_meta("telegram-bot", "alfred").rev == 7


async def test_catch_up_skips_a_sleeping_peer(tmp_path):
    meta = _meta(7, NEWER, node="winpc")
    peer = FakePeer(
        "winpc", state={"instances": [meta.to_dict()]}, package=NEWER, meta=meta, alive=False
    )
    rep, _, _, _ = _replicator(
        tmp_path, assignments=["telegram-bot@alfred:standby"], peers=[peer]
    )
    await rep.sync_from_peers()
    assert peer.requests == []


async def test_catch_up_does_nothing_without_instanced_assignments(tmp_path):
    peer = FakePeer("alfred", state={"instances": []})
    rep, _, _, _ = _replicator(tmp_path, assignments=["monitor"], peers=[peer])
    await rep.sync_from_peers()
    assert peer.requests == []


# --- применение к работающей службе -----------------------------------------


async def test_updated_package_restarts_the_running_service(tmp_path):
    """Конфиг читается один раз при старте (ARCH §9 п. 7) — иначе новые
    настройки просто не вступят в силу."""
    meta = _meta(2, NEWER, node="alfred")
    peer = FakePeer("alfred", package=NEWER, meta=meta)
    rep, store, supervisor, _ = _replicator(
        tmp_path, assignments=["telegram-bot@alfred"], peers=[peer]
    )
    slot = supervisor.services["telegram-bot@alfred"]
    restarted: list[bool] = []

    async def fake_restart() -> None:
        restarted.append(True)

    slot.restart = fake_restart
    slot._status = "running"

    await rep.on_config_changed(_changed_event(meta, "alfred"))
    assert restarted == [True]


async def test_standby_is_not_restarted(tmp_path):
    meta = _meta(2, NEWER, node="alfred")
    peer = FakePeer("alfred", package=NEWER, meta=meta)
    rep, _, supervisor, _ = _replicator(
        tmp_path, assignments=["telegram-bot@alfred:standby"], peers=[peer]
    )
    slot = supervisor.services["telegram-bot@alfred"]
    restarted: list[bool] = []

    async def fake_restart() -> None:
        restarted.append(True)

    slot.restart = fake_restart
    slot._status = "running"  # даже если почему-то запущена

    await rep.on_config_changed(_changed_event(meta, "alfred"))
    assert restarted == []


# --- действия ноды ----------------------------------------------------------


async def test_node_serves_the_package_to_a_peer(tmp_path):
    rep, store, supervisor, _ = _replicator(tmp_path, assignments=["telegram-bot@alfred"])
    path = store.package_path("telegram-bot", "alfred")
    path.parent.mkdir(parents=True)
    path.write_bytes(PACKAGE)
    await rep.announce_local_changes()

    svc = NodeService(supervisor, replicator=rep, node_id="alfred")
    payload = await svc.run_command(
        ACTION_GET_INSTANCE_CONFIG, {"service": "telegram-bot", "instance": "alfred"}
    )
    assert payload["content"].encode() == PACKAGE
    assert payload["meta"]["rev"] == 1


async def test_node_state_lists_package_revisions(tmp_path):
    rep, store, supervisor, _ = _replicator(tmp_path, assignments=["telegram-bot@alfred"])
    path = store.package_path("telegram-bot", "alfred")
    path.parent.mkdir(parents=True)
    path.write_bytes(PACKAGE)
    await rep.announce_local_changes()

    svc = NodeService(supervisor, replicator=rep, node_id="alfred")
    state = await svc.get_state()
    assert [(i["instance"], i["rev"]) for i in state["instances"]] == [("alfred", 1)]


async def test_node_without_packages_reports_none(tmp_path):
    svc = NodeService(Supervisor([], None, emit=_noop), node_id="jeeves")
    assert (await svc.get_state())["instances"] == []


async def _noop(event_type: str, data: dict) -> None:
    return None
