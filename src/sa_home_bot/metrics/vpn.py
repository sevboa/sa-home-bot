from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from sa_home_bot.domain.vpn_link import LEVEL_DOWN, LEVEL_OK, LEVEL_PARTIAL, scope_of
from sa_home_bot.metrics import protocol
from sa_home_bot.metrics.exposition import gauge_family, sample

LINK_LEVEL_VALUES = {LEVEL_DOWN: 0, LEVEL_PARTIAL: 1, LEVEL_OK: 2}


@dataclass(frozen=True)
class CheckRow:
    observer: str
    server: str
    transport: str
    target: str
    ok: bool
    latency_ms: int | None


@dataclass(frozen=True)
class LinkRow:
    observer: str
    server: str
    transport: str
    level: str


def _pair_labels(observer: str, server: str, transport: str, scope: str) -> dict[str, str]:
    return {
        protocol.LABEL_OBSERVER: observer,
        protocol.LABEL_SERVER: server,
        protocol.LABEL_TRANSPORT: transport,
        protocol.LABEL_SCOPE: scope,
    }


def render_vpn_metrics(
    checks: Iterable[CheckRow],
    links: Iterable[LinkRow],
    russia: Collection[str],
    ignored: Collection[str],
) -> str:
    check_samples: list[str] = []
    latency_samples: list[str] = []
    totals: dict[tuple[str, str, str, str], list[int]] = {}
    for row in sorted(checks, key=lambda r: (r.server, r.transport, r.observer, r.target)):
        scope = scope_of(row.observer, russia, ignored)
        if scope is None:
            continue
        labels = {
            **_pair_labels(row.observer, row.server, row.transport, scope),
            protocol.LABEL_TARGET: row.target,
        }
        check_samples.append(sample(protocol.METRIC_VPN_CHECK_OK, labels, int(row.ok)))
        if row.latency_ms is not None:
            latency_samples.append(
                sample(protocol.METRIC_VPN_CHECK_LATENCY_MS, labels, row.latency_ms)
            )
        key = (row.observer, row.server, row.transport, scope)
        totals.setdefault(key, []).append(int(row.ok))
    ratio_samples = [
        sample(
            protocol.METRIC_VPN_CHECK_OK_RATIO,
            _pair_labels(*key),
            sum(values) / len(values),
        )
        for key, values in sorted(totals.items())
    ]
    link_samples: list[str] = []
    for link in sorted(links, key=lambda r: (r.server, r.transport, r.observer)):
        scope = scope_of(link.observer, russia, ignored)
        if scope is None or link.level not in LINK_LEVEL_VALUES:
            continue
        link_samples.append(
            sample(
                protocol.METRIC_VPN_LINK_LEVEL,
                _pair_labels(link.observer, link.server, link.transport, scope),
                LINK_LEVEL_VALUES[link.level],
            )
        )
    lines: list[str] = []
    lines += gauge_family(
        protocol.METRIC_VPN_CHECK_OK,
        "последняя проверка цели прошла (1) или нет (0)",
        check_samples,
    )
    lines += gauge_family(
        protocol.METRIC_VPN_CHECK_LATENCY_MS,
        "задержка последней проверки цели, мс",
        latency_samples,
    )
    lines += gauge_family(
        protocol.METRIC_VPN_CHECK_OK_RATIO,
        "доля успешных целей наблюдателя по серверу и способу, 0..1",
        ratio_samples,
    )
    lines += gauge_family(
        protocol.METRIC_VPN_LINK_LEVEL,
        "уровень связки: 2 работает, 1 частично, 0 не работает",
        link_samples,
    )
    return "\n".join(lines) + "\n"
