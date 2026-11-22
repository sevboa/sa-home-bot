from __future__ import annotations

import json
from dataclasses import dataclass

from sa_home_bot.config import Settings
from sa_home_bot.metrics import protocol
from sa_home_bot.proto import endpoints


@dataclass(frozen=True)
class ScrapeTarget:
    node: str
    host: str


def peer_host(candidates: list[str]) -> str | None:
    best: tuple[tuple[bool, int], str] | None = None
    for raw in candidates:
        try:
            endpoint = endpoints.parse_endpoint(raw)
        except ValueError:
            continue
        if not isinstance(endpoint, endpoints.TcpEndpoint) or endpoints.is_loopback(endpoint):
            continue
        if endpoints.is_unspecified(endpoint):
            continue
        rank = endpoints.endpoint_rank(endpoint)
        key = (rank != endpoints.RANK_OVERLAY, rank)
        if best is None or key < best[0]:
            best = (key, endpoint.host)
    return best[1] if best else None


def scrape_targets(settings: Settings, own_host: str | None) -> list[ScrapeTarget]:
    own_id = settings.node.id
    found: dict[str, str] = {}
    for node in settings.swarm.nodes:
        if node.id == own_id:
            continue
        host = peer_host([node.endpoint, *node.endpoints])
        if host:
            found.setdefault(node.id, host)
    if own_id and own_host:
        found[own_id] = own_host
    return [ScrapeTarget(node, found[node]) for node in sorted(found)]


def scrape_config_text(targets: list[ScrapeTarget], interval: str, port: int) -> str:
    lines = [
        "global:",
        f"  scrape_interval: {json.dumps(interval)}",
        "scrape_configs:",
        f"  - job_name: {json.dumps(protocol.JOB_NAME)}",
        f"    metrics_path: {json.dumps(protocol.METRICS_PATH)}",
        "    static_configs:",
    ]
    if not targets:
        lines[-1] = "    static_configs: []"
    for target in targets:
        lines.append(f"      - targets: [{json.dumps(f'{target.host}:{port}')}]")
        lines.append("        labels:")
        lines.append(f"          {protocol.LABEL_NODE}: {json.dumps(target.node)}")
    return "\n".join(lines) + "\n"
