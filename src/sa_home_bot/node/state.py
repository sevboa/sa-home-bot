from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from sa_home_bot.config import SwarmNodeConfig
from sa_home_bot.utils.files import load_model, save_model


class NodeState(BaseModel):
    assignments: list[str] = Field(default_factory=list)
    peers: list[SwarmNodeConfig] = Field(default_factory=list)
    last_known_version: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> NodeState:
        return load_model(cls, path)

    def save(self, path: str | Path) -> None:
        save_model(self, path)

    def remember_peer(self, node_id: str, endpoint: str, endpoints: Sequence[str] = ()) -> None:
        known = self._peer(node_id)
        others = [p for p in self.peers if p.id != node_id]
        self.peers = [
            *others,
            SwarmNodeConfig(
                id=node_id,
                endpoint=endpoint,
                endpoints=[e for e in endpoints if e != endpoint],
                kind=known.kind if known is not None else "",
            ),
        ]

    def record_peer_kind(
        self, node_id: str, endpoint: str, endpoints: Sequence[str], kind: str
    ) -> bool:
        known = self._peer(node_id)
        if known is None:
            self.peers.append(
                SwarmNodeConfig(
                    id=node_id,
                    endpoint=endpoint,
                    endpoints=[e for e in endpoints if e != endpoint],
                    kind=kind,
                )
            )
            return True
        if known.kind != kind:
            known.kind = kind
            return True
        return False

    def peer_kind(self, node_id: str) -> str:
        known = self._peer(node_id)
        return known.kind if known is not None else ""

    def _peer(self, node_id: str) -> SwarmNodeConfig | None:
        return next((p for p in self.peers if p.id == node_id), None)
