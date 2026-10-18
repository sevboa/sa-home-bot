from __future__ import annotations

import pytest

from sa_home_bot.node.kind import KIND_SERVER, KIND_VPS, KIND_WORKSTATION, traits_for
from sa_home_bot.node.service import NodeService
from sa_home_bot.node.supervisor import Supervisor


def test_server_and_vps_are_expected_online_workstation_is_not():
    assert traits_for(KIND_SERVER).always_on
    assert traits_for(KIND_VPS).always_on
    assert not traits_for(KIND_WORKSTATION).always_on


def test_only_workstation_is_wakeable():
    assert traits_for(KIND_WORKSTATION).wakeable
    assert not traits_for(KIND_SERVER).wakeable
    assert not traits_for(KIND_VPS).wakeable


def test_lease_priority_prefers_the_machine_more_likely_to_be_on():
    assert (
        traits_for(KIND_SERVER).lease_priority
        > traits_for(KIND_VPS).lease_priority
        > traits_for(KIND_WORKSTATION).lease_priority
    )


def test_unknown_kind_is_conservative():
    unknown = traits_for("")
    assert not unknown.always_on
    assert not unknown.wakeable
    assert unknown.lease_priority == 0
    assert traits_for("нечто") == unknown


def test_node_reports_its_kind_in_hello_and_state():
    svc = NodeService(Supervisor([], None, emit=_noop), node_kind=KIND_VPS)
    assert svc.describe().info.node_kind == KIND_VPS


async def test_get_state_carries_kind():
    svc = NodeService(Supervisor([], None, emit=_noop), node_kind=KIND_VPS)
    assert (await svc.get_state())["kind"] == KIND_VPS


@pytest.mark.parametrize("kind", [KIND_SERVER, KIND_WORKSTATION, KIND_VPS])
def test_config_accepts_every_known_kind(kind):
    from sa_home_bot.config import NodeConfig

    assert NodeConfig(kind=kind).kind == kind


def test_config_rejects_an_unknown_kind():
    from pydantic import ValidationError

    from sa_home_bot.config import NodeConfig

    with pytest.raises(ValidationError):
        NodeConfig(kind="toaster")


def test_config_power_controllable_defaults_to_none():
    from sa_home_bot.config import NodeConfig

    assert NodeConfig().power_controllable is None
    assert NodeConfig(power_controllable=True).power_controllable is True
    assert NodeConfig(power_controllable=False).power_controllable is False


async def _noop(event_type: str, data: dict) -> None:
    return None
