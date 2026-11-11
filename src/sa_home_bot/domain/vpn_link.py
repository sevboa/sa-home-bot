from __future__ import annotations

from dataclasses import dataclass

LEVEL_OK = "ok"
LEVEL_PARTIAL = "partial"
LEVEL_DOWN = "down"


def link_level(total: int, bad: int) -> str:
    if total <= 0:
        return LEVEL_DOWN
    if bad <= 0:
        return LEVEL_OK
    if bad >= total:
        return LEVEL_DOWN
    return LEVEL_PARTIAL


@dataclass(frozen=True)
class LinkState:
    level: str
    candidate: str
    candidate_count: int
    announced: str


@dataclass(frozen=True)
class LinkTransition:
    from_announced: str
    to_announced: str


def reconcile_link(
    known: LinkState | None, level: str, threshold: int
) -> tuple[LinkState, LinkTransition | None]:
    if known is None:
        announced = LEVEL_DOWN if level == LEVEL_DOWN else LEVEL_OK
        return LinkState(level, level, 0, announced), None

    if level == known.level:
        return LinkState(known.level, known.level, 0, known.announced), None

    count = known.candidate_count + 1 if level == known.candidate else 1
    if count < threshold:
        return LinkState(known.level, level, count, known.announced), None

    if level in (LEVEL_OK, LEVEL_DOWN) and level != known.announced:
        return LinkState(level, level, 0, level), LinkTransition(known.announced, level)
    return LinkState(level, level, 0, known.announced), None
