from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

NodeKind = Literal["server", "workstation", "vps"]

KIND_SERVER: NodeKind = "server"
KIND_WORKSTATION: NodeKind = "workstation"
KIND_VPS: NodeKind = "vps"


def known_kinds() -> tuple[str, ...]:
    return get_args(NodeKind)


@dataclass(frozen=True)
class NodeTraits:
    always_on: bool
    wakeable: bool
    lease_priority: int

    @property
    def power_controllable(self) -> bool:
        return not self.always_on


TRAITS: dict[str, NodeTraits] = {
    KIND_SERVER: NodeTraits(always_on=True, wakeable=False, lease_priority=100),
    KIND_VPS: NodeTraits(always_on=True, wakeable=False, lease_priority=50),
    KIND_WORKSTATION: NodeTraits(always_on=False, wakeable=True, lease_priority=10),
}

_UNKNOWN = NodeTraits(always_on=False, wakeable=False, lease_priority=0)


def traits_for(kind: str | None) -> NodeTraits:
    if not kind:
        return _UNKNOWN
    return TRAITS.get(kind, _UNKNOWN)
