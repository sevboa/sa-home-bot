"""Персистентное состояние ноды (assignments, пиры) — не TOML, без БД.

Ноду можно донастраивать в рантайме (assign/unassign службы — этап 17; join
к рою — этап 18) без правки конфига и без рестарта процесса. Новое состояние
переживает рестарт через этот файл. TOML (`node.assignments`,
`[[swarm.nodes]]`) остаётся стартовым источником, не единственным — см.
`node/app.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from sa_home_bot.config import SwarmNodeConfig
from sa_home_bot.utils.files import load_model, save_model


class NodeState(BaseModel):
    assignments: list[str] = Field(default_factory=list)
    peers: list[SwarmNodeConfig] = Field(default_factory=list)
    # Версия, под которой нода стартовала в ПРОШЛЫЙ раз — переживает рестарт
    # именно поэтому персистится здесь, а не в памяти процесса (node/app.py
    # сравнивает с текущим __version__ на старте и эмитит restart_applied,
    # если версия сменилась). None — до сих пор не было ни одного старта с
    # записанной версией (свежий стейт-файл или обновление с версии без
    # этого поля) — событие в этом случае не эмитится, только запоминается
    # текущая версия, иначе первый же старт бота на всём рое дал бы ложный
    # "applied" без реального рестарта после update.
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
