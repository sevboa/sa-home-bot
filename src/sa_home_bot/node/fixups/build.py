from __future__ import annotations

from sa_home_bot.config import Settings
from sa_home_bot.node.fixups import devlab, host, needs, probe_net, probe_tunnel, proxy, vpn_server
from sa_home_bot.node.fixups.core import Fixup


def _probe_fixups(settings: Settings) -> list[Fixup]:
    slots = probe_net.discover_probe_slots(settings)
    supported = [s for s in slots if s.transport in probe_tunnel.SUPPORTED_PROBE_TRANSPORTS]
    return [
        probe_net.make_vpn_probe_legacy_cleanup_fixup(),
        probe_net.make_vpn_probe_forwarding_fixup(supported),
        probe_net.make_vpn_probe_forwarding_persist_fixup(supported),
        *(probe_net.make_vpn_probe_scaffold_fixup(slot) for slot in supported),
        *(probe_tunnel.make_vpn_probe_tunnel_conf_fixup(settings, slot) for slot in supported),
        probe_tunnel.make_vpn_probe_sudoers_fixup(supported),
        probe_net.make_vpn_probe_state_fixup(slots),
    ]


def build_fixups(settings: Settings) -> list[Fixup]:
    fixups = [
        host.INSTALL_SMARTMONTOOLS,
        host.SMARTCTL_SUDOERS,
        host.NODE_UNIT_SMARTCTL_PATH,
        host.JOURNALCTL_GROUP,
        host.POWER_CONTROL_POLKIT,
        host.WOL_ENABLE,
        *devlab.devlab_fixups(settings),
        vpn_server.make_awg_sudoers_fixup(settings),
        vpn_server.make_awg_ufw_fixup(settings),
        vpn_server.make_sub_ufw_fixup(settings),
        vpn_server.make_awg_forward_fixup(settings),
    ]
    if needs.vpn_check(settings):
        fixups += _probe_fixups(settings)
    fixups += [
        proxy.make_proxy_units_fixup(settings),
        proxy.make_proxy_firewall_fixup(settings),
        *(host.make_apps_unit_fixup(app) for app in settings.apps.items),
    ]
    return [f for f in fixups if f.needed(settings)]
