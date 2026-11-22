from __future__ import annotations

from collections.abc import Iterable

from sa_home_bot.db.store import Store
from sa_home_bot.domain.host import HostMetricState, instance_of, metric_spec
from sa_home_bot.domain.models import HealthState
from sa_home_bot.metrics import protocol
from sa_home_bot.metrics.exposition import METRIC_NAME, Render, gauge_family, sample


def render_metrics(health: Iterable[HealthState], host_states: Iterable[HostMetricState]) -> str:
    lines = gauge_family(
        protocol.METRIC_UP, "служба monitor жива", [sample(protocol.METRIC_UP, {}, 1)]
    )
    by_metric: dict[str, list[HostMetricState]] = {}
    for state in host_states:
        if METRIC_NAME.match(state.metric):
            by_metric.setdefault(state.metric, []).append(state)
    for metric in sorted(by_metric):
        name = protocol.HOST_METRIC_PREFIX + metric
        spec = metric_spec(metric)
        title = f"{spec.label}, {spec.unit}".rstrip(", ") if spec else metric
        label_name = spec.instance_label if spec else None
        samples = []
        for state in by_metric[metric]:
            instance = instance_of(state.component_id, metric)
            labels = {label_name: instance} if label_name and instance is not None else {}
            samples.append(sample(name, labels, state.value))
        lines.extend(gauge_family(name, title, samples))
    temperatures = sorted(health, key=lambda s: (s.kind, s.component_id))
    if temperatures:
        lines.extend(
            gauge_family(
                protocol.METRIC_TEMPERATURE,
                "температура компонента, °C",
                (
                    sample(
                        protocol.METRIC_TEMPERATURE,
                        {
                            protocol.LABEL_KIND: state.kind,
                            protocol.LABEL_COMPONENT: state.component_id,
                        },
                        state.temperature_c,
                    )
                    for state in temperatures
                ),
            )
        )
    return "\n".join(lines) + "\n"


def store_renderer(store: Store) -> Render:
    async def render() -> str:
        return render_metrics(await store.get_all_states(), await store.get_all_host_states())

    return render
