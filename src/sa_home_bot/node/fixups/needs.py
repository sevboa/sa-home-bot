from __future__ import annotations

from sa_home_bot.config import Settings
from sa_home_bot.node import assignments
from sa_home_bot.node import kind as node_kinds
from sa_home_bot.vpn import protocol as vpn_protocol


def monitor(settings: Settings) -> bool:
    return assignments.has_service(settings.node.assignments, "monitor")


def monitor_disks(settings: Settings) -> bool:
    return monitor(settings) and settings.sensors.disks.enabled


def apps(settings: Settings) -> bool:
    return assignments.has_service(settings.node.assignments, "apps")


def power_controllable(settings: Settings) -> bool:
    return node_kinds.traits_for(settings.node.kind).power_controllable


def wakeable(settings: Settings) -> bool:
    return node_kinds.traits_for(settings.node.kind).wakeable


def vpn(settings: Settings) -> bool:
    return assignments.has_service(settings.node.assignments, "vpn")


def vpn_awg(settings: Settings) -> bool:
    return vpn(settings) and (
        vpn_protocol.TRANSPORT_AWG in (settings.vpn.transports or [vpn_protocol.TRANSPORT_AWG])
    )


def vpn_check(settings: Settings) -> bool:
    return assignments.has_service(settings.node.assignments, "vpn_check")


def devlab(settings: Settings) -> bool:
    return any(
        assignments.has_service(settings.node.assignments, name)
        for name in ("devlab", "devlab_models")
    )
