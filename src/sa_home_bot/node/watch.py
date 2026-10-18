from __future__ import annotations

import asyncio
import logging

from sa_home_bot.node.events import EVENT_NODE_DOWN, EVENT_NODE_RETURNED, EVENT_NODE_UP
from sa_home_bot.node.kind import traits_for
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import EventEmitter
from sa_home_bot.utils.lifespan import cancel_task

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 30.0


class PresenceWatcher:
    def __init__(
        self,
        node_id: str,
        router: NodeRouter,
        *,
        emit: EventEmitter,
        state: NodeState | None = None,
        state_path: str | None = None,
        down_after_s: float = 300.0,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._node_id = node_id
        self._router = router
        self._emit = emit
        self._state = state
        self._state_path = state_path
        self._down_after_s = down_after_s
        self._poll_interval_s = poll_interval_s
        self._reported_down: set[str] = set()
        self._seen_down: set[str] = set()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="presence-watcher")

    async def stop(self) -> None:
        await cancel_task(self._task)
        self._task = None

    def _is_announcer(self) -> bool:
        alive = [link.name for link in self._router.peers.values() if link.alive]
        return all(self._node_id < name for name in alive)

    def _remember_kinds(self) -> None:
        if self._state is None or self._state_path is None:
            return
        changed = False
        for link in self._router.peers.values():
            if link.node_kind and self._state.record_peer_kind(
                link.name, str(link.endpoint), link.endpoints, link.node_kind
            ):
                changed = True
        if changed:
            self._state.save(self._state_path)

    def _known_kind(self, peer_id: str) -> str:
        link = self._router.peers.get(peer_id)
        if link is not None and link.node_kind:
            return link.node_kind
        if self._state is not None:
            return self._state.peer_kind(peer_id)
        return ""

    async def check_once(self) -> None:
        self._remember_kinds()
        announcer = self._is_announcer()
        for link in self._router.peers.values():
            down_s = link.downtime_s()
            if down_s is None:
                was_down = link.name in self._seen_down
                self._seen_down.discard(link.name)
                if link.name in self._reported_down:
                    self._reported_down.discard(link.name)
                    if announcer:
                        await self._emit(
                            EVENT_NODE_UP,
                            {"node": link.name, "kind": self._known_kind(link.name)},
                        )
                elif was_down and announcer:
                    await self._emit(
                        EVENT_NODE_RETURNED,
                        {"node": link.name, "kind": self._known_kind(link.name)},
                    )
                continue
            self._seen_down.add(link.name)
            if link.name in self._reported_down:
                continue
            if down_s < self._down_after_s:
                continue
            if link.left:
                continue
            if not traits_for(self._known_kind(link.name)).always_on:
                continue
            self._reported_down.add(link.name)
            if announcer:
                await self._emit(
                    EVENT_NODE_DOWN,
                    {
                        "node": link.name,
                        "kind": self._known_kind(link.name),
                        "down_s": round(down_s),
                    },
                )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                await self.check_once()
            except Exception:
                log.exception("PresenceWatcher: ошибка проверки присутствия")
