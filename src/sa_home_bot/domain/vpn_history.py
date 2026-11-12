from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sa_home_bot.domain.vpn_link import LEVEL_DOWN, LEVEL_PARTIAL, link_level


@dataclass(frozen=True)
class Sample:
    at: datetime
    node: str
    server: str
    transport: str
    total: int
    bad: int
    median_ms: int | None


def bucket_start(at: datetime, bucket_s: int) -> datetime:
    return datetime.fromtimestamp(int(at.timestamp()) // bucket_s * bucket_s, tz=at.tzinfo)


def median_ms(values: Iterable[int]) -> int | None:
    collected = list(values)
    if not collected:
        return None
    return int(round(statistics.median(collected)))


def aggregate(samples: Iterable[Sample], bucket_s: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[datetime, list[Sample]]] = {}
    for sample in samples:
        buckets = groups.setdefault((sample.server, sample.transport, sample.node), {})
        buckets.setdefault(bucket_start(sample.at, bucket_s), []).append(sample)

    series: list[dict[str, Any]] = []
    for key in sorted(groups):
        server, transport, node = key
        buckets = groups[key]
        points = []
        for start in sorted(buckets):
            group = buckets[start]
            levels = [link_level(sample.total, sample.bad) for sample in group]
            points.append(
                {
                    "at": start.isoformat(),
                    "reports": len(group),
                    "down": levels.count(LEVEL_DOWN),
                    "partial": levels.count(LEVEL_PARTIAL),
                    "median_ms": median_ms(
                        sample.median_ms for sample in group if sample.median_ms is not None
                    ),
                }
            )
        series.append({"node": node, "server": server, "transport": transport, "points": points})
    return series
