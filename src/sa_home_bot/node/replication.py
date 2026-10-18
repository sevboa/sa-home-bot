from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from sa_home_bot.node.assignments import Assignment
from sa_home_bot.node.events import EVENT_INSTANCE_CONFIG_CHANGED
from sa_home_bot.node.instances import (
    InstanceMeta,
    InstanceStore,
    base_instance,
    is_satellite,
    slot_key,
)
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.supervisor import EventEmitter, Supervisor
from sa_home_bot.proto.addresses import NODE_SERVICE
from sa_home_bot.proto.messages import (
    MSG_COMMAND,
    MSG_GET_STATE,
    Address,
    Envelope,
    ProtoError,
    make_request,
)
from sa_home_bot.utils.lifespan import cancel_task

log = logging.getLogger(__name__)

ACTION_LIST_INSTANCES = "list_instances"
ACTION_GET_INSTANCE_CONFIG = "get_instance_config"

POLL_INTERVAL_S = 30.0
PEER_TIMEOUT_S = 10.0


def _encode_package(data: bytes) -> str:
    return data.decode("utf-8")


def _decode_package(text: str) -> bytes:
    return text.encode("utf-8")


class ConfigReplicator:
    def __init__(
        self,
        node_id: str,
        store: InstanceStore,
        *,
        supervisor: Supervisor,
        router: NodeRouter | None,
        emit: EventEmitter,
        poll_interval_s: float = POLL_INTERVAL_S,
        passive: Callable[[str, str], bool] | None = None,
        before_announce: Callable[[], Awaitable[object]] | None = None,
        on_applied: Callable[[InstanceMeta, bytes], None] | None = None,
    ) -> None:
        self._node_id = node_id
        self._store = store
        self._supervisor = supervisor
        self._router = router
        self._emit = emit
        self._poll_interval_s = poll_interval_s
        self._task: asyncio.Task | None = None
        self._passive = passive
        self._before_announce = before_announce
        self._on_applied = on_applied

    def _my_instances(self) -> list[Assignment]:
        return [
            svc.assignment for svc in self._supervisor.services.values() if svc.assignment.instance
        ]

    def _cares_about(self, service: str, instance: str) -> bool:
        if self._passive is not None and self._passive(service, instance):
            return True
        base = base_instance(instance)
        return any(a.service == service and a.instance == base for a in self._my_instances())

    def _is_passive(self, service: str, instance: str) -> bool:
        return self._passive is not None and self._passive(service, instance)

    def _wins(self, meta: InstanceMeta, local: InstanceMeta | None) -> bool:
        if self._is_passive(meta.service, meta.instance) and meta.origin_node == meta.instance:
            if local is None:
                return True
            return meta.hash != local.hash and meta.updated_at > local.updated_at
        return meta.wins_over(local)

    def local_revisions(self) -> list[dict]:
        return [m.to_dict() for m in self._store.known()]

    async def announce_local_changes(self) -> list[InstanceMeta]:
        if self._before_announce is not None:
            try:
                await self._before_announce()
            except Exception:
                log.exception("Репликация: ошибка подготовки локальных пакетов")
        changed = self._store.refresh_all()
        for meta in changed:
            await self._emit(EVENT_INSTANCE_CONFIG_CHANGED, meta.to_dict())
            if not is_satellite(meta.instance):
                await self._apply_to_running_service(meta)
        return changed

    async def on_config_changed(self, env: Envelope) -> None:
        data = env.payload.get("data", {})
        meta = InstanceMeta.from_dict(data)
        if not meta.service or not meta.instance:
            return
        if not self._cares_about(meta.service, meta.instance):
            return
        local = self._store.read_meta(meta.service, meta.instance)
        if not self._wins(meta, local):
            return
        source = meta.origin_node or (env.src.node if env.src else "")
        if not source or source == self._node_id:
            return
        await self._pull(meta, source)

    async def sync_from_peers(self) -> None:
        if self._router is None:
            return
        if not self._my_instances() and self._passive is None:
            return
        for peer_id, link in list(self._router.peers.items()):
            if not link.alive:
                continue
            state = await self._peer_state(peer_id, link)
            if state is None:
                continue
            for raw in state.get("instances", []):
                meta = InstanceMeta.from_dict(raw)
                if not self._cares_about(meta.service, meta.instance):
                    continue
                local = self._store.read_meta(meta.service, meta.instance)
                if self._wins(meta, local):
                    await self._pull(meta, peer_id)

    async def _peer_state(self, peer_id: str, link) -> dict | None:
        request = make_request(
            MSG_GET_STATE,
            src=Address(node=self._node_id, service=NODE_SERVICE),
            dst=Address(node=peer_id, service=NODE_SERVICE),
            timeout_s=PEER_TIMEOUT_S,
        )
        try:
            response = await asyncio.wait_for(link.forward(request), PEER_TIMEOUT_S)
        except (ProtoError, TimeoutError, ConnectionError, OSError) as exc:
            log.debug("Репликация: нода %s не ответила о своих пакетах (%s)", peer_id, exc)
            return None
        return response.payload if response.ok else None

    async def _pull(self, meta: InstanceMeta, source: str) -> None:
        if self._router is None:
            return
        link = self._router.peers.get(source)
        if link is None or not link.alive:
            return
        request = make_request(
            MSG_COMMAND,
            {
                "action": ACTION_GET_INSTANCE_CONFIG,
                "args": {"service": meta.service, "instance": meta.instance},
            },
            src=Address(node=self._node_id, service=NODE_SERVICE),
            dst=Address(node=source, service=NODE_SERVICE),
            timeout_s=PEER_TIMEOUT_S,
        )
        try:
            response = await asyncio.wait_for(link.forward(request), PEER_TIMEOUT_S)
        except (ProtoError, TimeoutError, ConnectionError, OSError) as exc:
            log.warning(
                "Репликация: не удалось забрать пакет %s с ноды %s (%s) — попробую позже",
                slot_key(meta.service, meta.instance),
                source,
                exc,
            )
            return
        if not response.ok:
            log.warning(
                "Репликация: нода %s отказала в пакете %s (%s)",
                source,
                slot_key(meta.service, meta.instance),
                (response.error or {}).get("message", "?"),
            )
            return
        payload = response.payload
        content = payload.get("content")
        if not isinstance(content, str):
            return
        remote_meta = InstanceMeta.from_dict(payload.get("meta", meta.to_dict()))
        data = _decode_package(content)
        local = self._store.read_meta(remote_meta.service, remote_meta.instance)
        force = self._is_passive(remote_meta.service, remote_meta.instance) and self._wins(
            remote_meta, local
        )
        if self._store.apply(remote_meta, data, force=force):
            if self._on_applied is not None:
                try:
                    self._on_applied(remote_meta, data)
                except Exception:
                    log.exception("Репликация: ошибка обработки принятого пакета")
            await self._apply_to_running_service(remote_meta)

    async def _apply_to_running_service(self, meta: InstanceMeta) -> None:
        slot = self._supervisor.get(slot_key(meta.service, base_instance(meta.instance)))
        if slot is None or slot.assignment.standby:
            return
        if slot.status != "running":
            return
        log.info(
            "Пакет %s обновлён до ревизии %d — перезапускаю службу",
            slot.name,
            meta.rev,
        )
        await slot.restart()

    def package_payload(self, service: str, instance: str) -> dict | None:
        data = self._store.read_package(service, instance)
        meta = self._store.read_meta(service, instance)
        if data is None or meta is None:
            return None
        return {"meta": meta.to_dict(), "content": _encode_package(data)}

    async def tick(self) -> None:
        await self.announce_local_changes()
        await self.sync_from_peers()

    async def start(self) -> None:
        with contextlib.suppress(Exception):
            await self.tick()
        self._task = asyncio.create_task(self._run(), name="config-replicator")

    async def stop(self) -> None:
        await cancel_task(self._task)
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                await self.tick()
            except Exception:
                log.exception("Репликация: ошибка цикла синхронизации")
