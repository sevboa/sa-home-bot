from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sa_home_bot.devlab import protocol

if TYPE_CHECKING:  # pragma: no cover
    from sa_home_bot.config import Settings


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    cli_name: str
    runner: str
    endpoint_attr: str | None = None
    assignable: bool = True
    externally_managed: bool = False
    singleton: bool = False
    instanced: bool = False

    @property
    def cli_args(self) -> list[str]:
        return ["--service", self.cli_name]


_SPECS: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        name="monitor",
        cli_name="monitor",
        runner="sa_home_bot.monitor.app:run_monitor",
        endpoint_attr="monitor.socket",
    ),
    ServiceSpec(
        name="telegram-bot",
        cli_name="bot",
        runner="sa_home_bot.app:run",
        endpoint_attr=None,
        singleton=True,
        instanced=True,
    ),
    ServiceSpec(
        name="apps",
        cli_name="apps",
        runner="sa_home_bot.apps.app:run_apps",
        endpoint_attr="apps.socket",
    ),
    ServiceSpec(
        name="torrents",
        cli_name="torrents",
        runner="sa_home_bot.torrents.app:run_torrents",
        endpoint_attr="torrents.socket",
    ),
    ServiceSpec(
        name="tasks",
        cli_name="tasks",
        runner="sa_home_bot.tasks.app:run_tasks",
        endpoint_attr="tasks.socket",
    ),
    ServiceSpec(
        name="net", cli_name="net", runner="sa_home_bot.net.app:run_net", endpoint_attr="net.socket"
    ),
    ServiceSpec(
        name="memory",
        cli_name="memory",
        runner="sa_home_bot.memory.app:run_memory",
        endpoint_attr="memory.socket",
    ),
    ServiceSpec(
        name="graph_memory",
        cli_name="graph_memory",
        runner="sa_home_bot.graph_memory.app:run_graph_memory",
        endpoint_attr="graph_memory.socket",
    ),
    ServiceSpec(
        name="llm",
        cli_name="llm",
        runner="sa_home_bot.llm.app:run_llm",
        endpoint_attr="llm.socket",
        assignable=False,
        externally_managed=True,
    ),
    ServiceSpec(
        name="devlab_hub",
        cli_name="devlab_hub",
        runner="sa_home_bot.devlab.hub.app:run_hub",
        endpoint_attr="devlab_hub.socket",
        singleton=True,
    ),
    ServiceSpec(
        name="devlab_models",
        cli_name="devlab_models",
        runner="sa_home_bot.devlab.models.app:run_models",
    ),
    ServiceSpec(
        name=protocol.EXECUTOR_SERVICE,
        cli_name=protocol.EXECUTOR_SERVICE,
        runner="sa_home_bot.devlab.executor.app:run_executor",
    ),
    ServiceSpec(
        name="vpn", cli_name="vpn", runner="sa_home_bot.vpn.app:run_vpn", endpoint_attr="vpn.socket"
    ),
    ServiceSpec(
        name="vpn_check",
        cli_name="vpn_check",
        runner="sa_home_bot.vpn_check.app:run_vpn_check",
        endpoint_attr="vpn_check.socket",
    ),
)

SERVICES: dict[str, ServiceSpec] = {s.name: s for s in _SPECS}

NODE_CLI_NAME = "node"
NODE_RUNNER = "sa_home_bot.node.app:run_node"


def spec(name: str) -> ServiceSpec | None:
    return SERVICES.get(name)


def assignable_names() -> tuple[str, ...]:
    return tuple(s.name for s in _SPECS if s.assignable)


def supervised_names() -> tuple[str, ...]:
    return tuple(s.name for s in _SPECS if not s.externally_managed)


def load_runner(cli_name: str):
    target = NODE_RUNNER
    for service in _SPECS:
        if service.cli_name == cli_name:
            target = service.runner
    module_name, _, function_name = target.partition(":")
    return getattr(importlib.import_module(module_name), function_name)


def cli_service_choices() -> tuple[str, ...]:
    return tuple(s.cli_name for s in _SPECS) + (NODE_CLI_NAME,)


def _resolve(settings: Settings, dotted: str) -> str:
    obj: object = settings
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return str(obj)


def service_endpoints(settings: Settings) -> dict[str, str]:
    return {s.name: _resolve(settings, s.endpoint_attr) for s in _SPECS if s.endpoint_attr}
