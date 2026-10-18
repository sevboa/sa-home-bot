from __future__ import annotations

import re
from collections.abc import Sequence

_VALID_COMMAND = re.compile(r"^[a-z0-9_]+$")
_MAX_COMMAND_LEN = 32

NODE_PREFIX = "node_"
SVC_PREFIX = "svc_"


def normalize(name: str) -> str | None:
    norm = name.lower().replace("-", "_")
    return norm if _VALID_COMMAND.fullmatch(norm) else None


def _command(prefix: str, *parts: str) -> str | None:
    norms = [normalize(p) for p in parts]
    if any(n is None for n in norms):
        return None
    name = prefix + "_".join(norms)  # type: ignore[arg-type]
    return f"/{name}" if len(name) <= _MAX_COMMAND_LEN else None


def node_command(node_id: str) -> str | None:
    return _command(NODE_PREFIX, node_id)


def svc_command(node_id: str, service: str) -> str | None:
    return _command(SVC_PREFIX, node_id, service)


def resolve_svc_candidates(arg: str, known_ids: Sequence[str]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for nid in known_ids:
        norm = normalize(nid)
        if norm is not None and arg.startswith(norm + "_"):
            candidates.append((nid, arg[len(norm) + 1 :]))
    candidates.sort(key=lambda c: len(c[1]))
    return candidates


def match_name(normalized: str, names: Sequence[str]) -> str | None:
    return next((name for name in names if normalize(name) == normalized), None)
