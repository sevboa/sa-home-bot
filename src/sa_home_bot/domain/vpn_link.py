from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

LEVEL_OK = "ok"
LEVEL_PARTIAL = "partial"
LEVEL_DOWN = "down"

LAYER_NET = "net"
LAYER_NODE = "node"
LAYER_VPN = "vpn"
LAYERS = (LAYER_NET, LAYER_NODE, LAYER_VPN)


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


SCOPE_GLOBAL = "global"
SCOPE_RUSSIA = "russia"
SCOPES = (SCOPE_GLOBAL, SCOPE_RUSSIA)


def scope_of(node: str, russia: Collection[str], ignored: Collection[str]) -> str | None:
    if node in ignored:
        return None
    return SCOPE_RUSSIA if node in russia else SCOPE_GLOBAL


def scope_level(levels: Iterable[str]) -> str | None:
    collected = list(levels)
    if not collected:
        return None
    if LEVEL_OK in collected:
        return LEVEL_OK
    if all(level == LEVEL_DOWN for level in collected):
        return LEVEL_DOWN
    return LEVEL_PARTIAL


def scope_levels(
    links: Iterable[tuple[str, str, str]],
    russia: Collection[str],
    ignored: Collection[str],
) -> dict[str, dict[str, str]]:
    levels: dict[str, dict[str, list[str]]] = {scope: {} for scope in SCOPES}
    for node, server, level in links:
        scope = scope_of(node, russia, ignored)
        if scope is None:
            continue
        levels[scope].setdefault(server, []).append(level)

    return {
        scope: {server: scope_level(server_levels[server]) for server in sorted(server_levels)}
        for scope, server_levels in levels.items()
    }


def resource_bad_targets(rows: Iterable[tuple[tuple[str, str, str], str, bool]]) -> set[str]:
    by_link: dict[tuple[str, str, str], dict[str, bool]] = {}
    for link, target, ok in rows:
        by_link.setdefault(link, {})[target] = ok
    working = [targets for targets in by_link.values() if any(targets.values())]
    if len(working) < 2:
        return set()
    seen: dict[str, list[bool]] = {}
    for targets in working:
        for target, ok in targets.items():
            seen.setdefault(target, []).append(ok)
    return {target for target, oks in seen.items() if len(oks) >= 2 and not any(oks)}


def effective_scopes(
    vpn: dict[str, dict[str, str]],
    net: dict[str, dict[str, str]],
    node: dict[str, str],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for scope in SCOPES:
        levels = dict(vpn.get(scope, {}))
        for server, level in net.get(scope, {}).items():
            if level == LEVEL_DOWN:
                levels[server] = LEVEL_DOWN
        for server, level in node.items():
            if level == LEVEL_DOWN:
                levels[server] = LEVEL_DOWN
        result[scope] = {server: levels[server] for server in sorted(levels)}
    return result


def layer_table(
    vpn: dict[str, dict[str, str]],
    net: dict[str, dict[str, str]],
    node: dict[str, str],
) -> dict[str, dict[str, dict[str, str | None]]]:
    table: dict[str, dict[str, dict[str, str | None]]] = {}
    for scope in SCOPES:
        servers = set(vpn.get(scope, {})) | set(net.get(scope, {})) | set(node)
        table[scope] = {
            server: {
                LAYER_NET: net.get(scope, {}).get(server),
                LAYER_NODE: node.get(server),
                LAYER_VPN: vpn.get(scope, {}).get(server),
            }
            for server in sorted(servers)
        }
    return table
