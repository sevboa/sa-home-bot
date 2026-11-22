from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sa_home_bot.config import Settings, SwarmNodeConfig
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


def scrape_targets(
    settings: Settings, own_host: str | None, peers: Sequence[SwarmNodeConfig] = ()
) -> list[ScrapeTarget]:
    own_id = settings.node.id
    found: dict[str, str] = {}
    for node in [*settings.swarm.nodes, *peers]:
        if node.id == own_id:
            continue
        host = peer_host([node.endpoint, *node.endpoints])
        if host:
            found.setdefault(node.id, host)
    if own_id and own_host:
        found[own_id] = own_host
    return [ScrapeTarget(node, found[node]) for node in sorted(found)]


@dataclass(frozen=True)
class ScrapeJob:
    name: str
    port: int
    targets: list[ScrapeTarget]


def vpn_scrape_targets(
    targets: list[ScrapeTarget], known_servers: Collection[str]
) -> list[ScrapeTarget]:
    if not known_servers:
        return targets
    return [target for target in targets if target.node in known_servers]


def _job_lines(job: ScrapeJob) -> list[str]:
    lines = [
        f"  - job_name: {json.dumps(job.name)}",
        f"    metrics_path: {json.dumps(protocol.METRICS_PATH)}",
        "    static_configs:",
    ]
    if not job.targets:
        lines[-1] = "    static_configs: []"
    for target in job.targets:
        lines.append(f"      - targets: [{json.dumps(f'{target.host}:{job.port}')}]")
        lines.append("        labels:")
        lines.append(f"          {protocol.LABEL_NODE}: {json.dumps(target.node)}")
    return lines


def scrape_config_text(jobs: list[ScrapeJob], interval: str) -> str:
    lines = ["global:", f"  scrape_interval: {json.dumps(interval)}", "scrape_configs:"]
    for job in jobs:
        lines.extend(_job_lines(job))
    return "\n".join(lines) + "\n"
