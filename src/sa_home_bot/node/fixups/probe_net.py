from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
from pathlib import Path

from sa_home_bot.bot import vpn_nodes
from sa_home_bot.config import Settings
from sa_home_bot.node import vpn_probe_state
from sa_home_bot.node.fixups import needs
from sa_home_bot.node.fixups.core import Fixup, FixupError
from sa_home_bot.node.fixups.netfilter import (
    NftChain,
    ensure_ip_forward,
    ensure_nft_installed,
    find_base_chain,
    ip_forward_enabled,
    nft_chain_text,
    nft_ruleset_text,
)
from sa_home_bot.node.fixups.privileged import (
    enable_now,
    file_matches,
    find_executable,
    install_root_file,
    path_exists,
    read_text_if_exists,
    replace_unit,
    run_sudo,
    unit_is_active,
)
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import resolve_endpoint

log = logging.getLogger(__name__)

VPN_PROBE_SCAFFOLD_UNIT_DIR = Path("/etc/systemd/system")
VPN_PROBE_SUBNET_BASE = "10.200.200"
VPN_PROBE_NFT_TABLE = "sa_vpn_probe"
VPN_PROBE_FORWARD_SCRIPT_PATH = Path("/usr/local/lib/sa-home-bot/vpn-probe-forward-reapply.sh")
VPN_PROBE_FORWARD_UNIT_FILE = Path("/etc/systemd/system/sa-home-vpn-probe-forward.service")
VPN_PROBE_LEGACY_UNIT = "sa-home-vpn-probe.service"
VPN_PROBE_LEGACY_NETNS = "vpn-probe"
VPN_PROBE_LEGACY_VETH = "vprobe-veth0"
REALITY_SOCKS_PORT_BASE = 11080
SLOT_SUBNET_SIZE = 4

WAIT_TABLE_FUNCTION = [
    "wait_table() {",
    "  i=0",
    "  while [ $i -lt 60 ]; do",
    "    nft list table \"$1\" \"$2\" >/dev/null 2>&1 && return 0",
    "    i=$((i + 1))",
    "    sleep 1",
    "  done",
    "  return 1",
    "}",
]
FORWARD_CHAIN_DEFINITION = "{ type filter hook forward priority filter ; policy accept ; }"
NAT_CHAIN_DEFINITION = "{ type nat hook postrouting priority srcnat ; policy accept ; }"


def probe_slot_from_target(index: int, target: vpn_nodes.ProbeTarget) -> vpn_probe_state.ProbeSlot:
    base = index * SLOT_SUBNET_SIZE
    is_awg = target.transport == "awg"
    return vpn_probe_state.ProbeSlot(
        server=target.server,
        transport=target.transport,
        netns=f"vpn-probe-{target.server}-{target.transport}",
        veth_host=f"vprobe{index}h0",
        veth_ns=f"vprobe{index}n0",
        veth_host_addr=f"{VPN_PROBE_SUBNET_BASE}.{base + 1}/30",
        veth_ns_addr=f"{VPN_PROBE_SUBNET_BASE}.{base + 2}/30",
        subnet=f"{VPN_PROBE_SUBNET_BASE}.{base}/30",
        iface=f"awg-probe{index}" if is_awg else None,
        socks_port=None if is_awg else REALITY_SOCKS_PORT_BASE + index,
    )


async def _fetch_probe_slots(settings: Settings) -> list[vpn_probe_state.ProbeSlot]:
    client = ProtoClient(resolve_endpoint(settings.node.socket), token=settings.swarm.token)
    try:
        await client.connect()
        targets = await vpn_nodes.probe_targets(client, exclude=socket.gethostname())
    finally:
        await client.close()
    return [probe_slot_from_target(i, t) for i, t in enumerate(targets)]


def discover_probe_slots(settings: Settings) -> list[vpn_probe_state.ProbeSlot]:
    try:
        return asyncio.run(_fetch_probe_slots(settings))
    except Exception as exc:
        log.warning("nodectl fix: не удалось обнаружить VPN-серверы для пробника: %s", exc)
        return []


def scaffold_unit_path(slot: vpn_probe_state.ProbeSlot) -> Path:
    name = f"sa-home-vpn-probe-scaffold-{slot.server}-{slot.transport}.service"
    return VPN_PROBE_SCAFFOLD_UNIT_DIR / name


def vpn_probe_scaffold_unit_content(slot: vpn_probe_state.ProbeSlot, ip_path: str) -> str:
    host_addr_only = slot.veth_host_addr.split("/")[0]
    pre_steps = [
        f"-{ip_path} netns add {slot.netns}",
        f"-{ip_path} link add {slot.veth_host} type veth peer name {slot.veth_ns}",
        f"-{ip_path} link set {slot.veth_ns} netns {slot.netns}",
        f"-{ip_path} addr add {slot.veth_host_addr} dev {slot.veth_host}",
        f"{ip_path} link set {slot.veth_host} up",
        f"-{ip_path} netns exec {slot.netns} {ip_path} addr add {slot.veth_ns_addr} "
        f"dev {slot.veth_ns}",
        f"{ip_path} netns exec {slot.netns} {ip_path} link set {slot.veth_ns} up",
        f"{ip_path} netns exec {slot.netns} {ip_path} link set lo up",
        f"-{ip_path} netns exec {slot.netns} {ip_path} route add default via {host_addr_only}",
    ]
    pre_lines = "".join(f"ExecStartPre={step}\n" for step in pre_steps)
    return (
        "[Unit]\n"
        "Description=sa-home-bot: netns-скаффолд VPN-пробника "
        f"({slot.server}/{slot.transport}, netns {slot.netns})\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"{pre_lines}"
        "ExecStart=/bin/true\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _scaffold_check(slot: vpn_probe_state.ProbeSlot) -> bool:
    unit_path = scaffold_unit_path(slot)
    ip_path = find_executable("ip")
    if ip_path is None:
        return False
    expected = vpn_probe_scaffold_unit_content(slot, ip_path)
    return file_matches(unit_path, expected) and unit_is_active(unit_path.name)


def _scaffold_apply(slot: vpn_probe_state.ProbeSlot) -> None:
    ip_path = find_executable("ip")
    if ip_path is None:
        raise FixupError("ip (iproute2) не найден в PATH")
    unit_path = scaffold_unit_path(slot)
    replace_unit(unit_path, vpn_probe_scaffold_unit_content(slot, ip_path))
    enable_now(unit_path.name)


def make_vpn_probe_scaffold_fixup(slot: vpn_probe_state.ProbeSlot) -> Fixup:
    return Fixup(
        id=f"vpn-probe-scaffold-{slot.server}-{slot.transport}",
        title=f"Поднять netns-скаффолд пробника к {slot.server}/{slot.transport} (netns+veth)",
        needed=needs.vpn_check,
        check=lambda: _scaffold_check(slot),
        apply=lambda: _scaffold_apply(slot),
    )


def _legacy_present() -> bool:
    if path_exists(VPN_PROBE_SCAFFOLD_UNIT_DIR / VPN_PROBE_LEGACY_UNIT):
        return True
    netns = subprocess.run(["sudo", "-n", "ip", "netns", "list"], capture_output=True, text=True)
    if netns.returncode == 0 and any(
        line.split()[0] == VPN_PROBE_LEGACY_NETNS for line in netns.stdout.splitlines() if line
    ):
        return True
    link = subprocess.run(
        ["ip", "link", "show", VPN_PROBE_LEGACY_VETH], capture_output=True, text=True
    )
    return link.returncode == 0


def _legacy_cleanup() -> None:
    unit_path = VPN_PROBE_SCAFFOLD_UNIT_DIR / VPN_PROBE_LEGACY_UNIT
    if path_exists(unit_path):
        run_sudo(["systemctl", "disable", "--now", VPN_PROBE_LEGACY_UNIT])
        run_sudo(["rm", "-f", str(unit_path)])
        run_sudo(["systemctl", "daemon-reload"])
    subprocess.run(["sudo", "ip", "netns", "del", VPN_PROBE_LEGACY_NETNS], capture_output=True)
    subprocess.run(["sudo", "ip", "link", "del", VPN_PROBE_LEGACY_VETH], capture_output=True)
    if _legacy_present():
        raise FixupError(
            f"остатки старого пробника ({VPN_PROBE_LEGACY_NETNS}/{VPN_PROBE_LEGACY_VETH}) "
            "не убрались — снесите вручную, иначе первый слот новой схемы будет глухим"
        )


def make_vpn_probe_legacy_cleanup_fixup() -> Fixup:
    return Fixup(
        id="vpn-probe-legacy-cleanup",
        title="Снести одиночный пробник прежней схемы (netns vpn-probe, vprobe-veth0)",
        needed=needs.vpn_check,
        check=lambda: not _legacy_present(),
        apply=_legacy_cleanup,
    )


def _create_own_chain(family: str, name: str, chain_type: str, hook: str, priority: str) -> NftChain:
    run_sudo(["nft", "add", "table", family, VPN_PROBE_NFT_TABLE])
    run_sudo(
        [
            "nft", "add", "chain", family, VPN_PROBE_NFT_TABLE, name,
            "{", "type", chain_type, "hook", hook, "priority", priority, ";",
            "policy", "accept", ";", "}",
        ]
    )
    return NftChain(family, VPN_PROBE_NFT_TABLE, name)


def resolve_probe_forward_targets() -> tuple[NftChain, NftChain]:
    ensure_nft_installed()
    forward = find_base_chain("forward", "filter") or _create_own_chain(
        "inet", "forward", "filter", "forward", "filter"
    )
    nat = find_base_chain("postrouting", "nat") or _create_own_chain(
        "ip", "postrouting", "nat", "postrouting", "srcnat"
    )
    return forward, nat


def _forwarding_check(slots: list[vpn_probe_state.ProbeSlot]) -> bool:
    if not ip_forward_enabled():
        return False
    if not slots:
        return True
    text = nft_ruleset_text()
    return all(slot.veth_host in text and slot.subnet in text for slot in slots)


def _forwarding_apply(slots: list[vpn_probe_state.ProbeSlot]) -> None:
    ensure_ip_forward()
    if not slots:
        return
    forward, nat = resolve_probe_forward_targets()
    forward_text = nft_chain_text(forward)
    for slot in slots:
        for rule in (f"iifname {slot.veth_host} accept", f"oifname {slot.veth_host} accept"):
            if rule not in forward_text:
                run_sudo(["nft", "insert", "rule", *forward, *rule.split()])
    nat_text = nft_chain_text(nat)
    for slot in slots:
        rule = f"ip saddr {slot.subnet} masquerade"
        if rule not in nat_text:
            run_sudo(["nft", "insert", "rule", *nat, *rule.split()])


def make_vpn_probe_forwarding_fixup(slots: list[vpn_probe_state.ProbeSlot]) -> Fixup:
    return Fixup(
        id="vpn-check-probe-forwarding",
        title="Пропустить трафик veth-пар пробников наружу (ip_forward + forward + NAT)",
        needed=needs.vpn_check,
        check=lambda: _forwarding_check(slots),
        apply=lambda: _forwarding_apply(slots),
    )


def _ensure_chain_line(chain: NftChain, definition: str) -> str:
    if chain.table != VPN_PROBE_NFT_TABLE:
        return (
            f"wait_table {chain.family} {chain.table} || {{ "
            f'echo "нет таблицы {chain.family} {chain.table} — некуда ставить правила пробников" >&2; '
            f"exit 1; }}"
        )
    return (
        f"nft list table {chain.family} {chain.table} >/dev/null 2>&1 || {{ "
        f"nft add table {chain.family} {chain.table}; "
        f"nft add chain {chain.family} {chain.table} {chain.name} '{definition}'; }}"
    )


def _ensure_rule_line(chain: NftChain, match: str, rule: str) -> str:
    return (
        f"nft list chain {chain.family} {chain.table} {chain.name} 2>/dev/null | "
        f"grep -qF '{match}' || nft insert rule {chain.family} {chain.table} {chain.name} {rule}"
    )


def vpn_probe_forward_script_content(
    forward: NftChain, nat: NftChain, slots: list[vpn_probe_state.ProbeSlot]
) -> str:
    lines = [
        "#!/bin/sh",
        "# sa-home-bot: сгенерировано nodectl fix — не редактировать руками,",
        "# перезапишется при следующем nodectl fix.",
        "set -e",
        "",
        "# Чужую таблицу (напр. ip filter, которую заводит tailscaled) на чистой",
        "# загрузке можно застать ещё не созданной — юнит стартует раньше её",
        "# владельца. Живой инцидент на alfred 2026-09-23: ребут → юнит упал с",
        "# \"Could not process rule: No such file or directory\" на первом же",
        "# insert, set -e прибил остальное, и forward/NAT пробников не появились",
        "# ВООБЩЕ — все проверки с этой ноды молча таймаутили до ручного",
        "# nodectl fix. Поэтому ждём появления таблицы, а не падаем сразу.",
        *WAIT_TABLE_FUNCTION,
        _ensure_chain_line(forward, FORWARD_CHAIN_DEFINITION),
        _ensure_chain_line(nat, NAT_CHAIN_DEFINITION),
    ]
    for slot in slots:
        host = slot.veth_host
        lines.append(_ensure_rule_line(forward, f'iifname "{host}" accept', f"iifname {host} accept"))
        lines.append(_ensure_rule_line(forward, f'oifname "{host}" accept', f"oifname {host} accept"))
        masquerade = f"ip saddr {slot.subnet} masquerade"
        lines.append(_ensure_rule_line(nat, masquerade, masquerade))
    return "\n".join(lines) + "\n"


def vpn_probe_forward_unit_content(
    script_path: Path, slots: list[vpn_probe_state.ProbeSlot]
) -> str:
    scaffold_units = " ".join(scaffold_unit_path(s).name for s in slots)
    after = "network-online.target tailscaled.service" + (
        f" {scaffold_units}" if scaffold_units else ""
    )
    return (
        "[Unit]\n"
        "Description=sa-home-bot: forward/NAT для veth-пар VPN-пробников "
        "(переживает ребут)\n"
        f"After={after}\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        f"ExecStart=/bin/sh {script_path}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _forwarding_persist_check(slots: list[vpn_probe_state.ProbeSlot]) -> bool:
    expected_unit = vpn_probe_forward_unit_content(VPN_PROBE_FORWARD_SCRIPT_PATH, slots)
    return (
        path_exists(VPN_PROBE_FORWARD_SCRIPT_PATH)
        and file_matches(VPN_PROBE_FORWARD_UNIT_FILE, expected_unit)
        and unit_is_active(VPN_PROBE_FORWARD_UNIT_FILE.name)
    )


def _forwarding_persist_apply(slots: list[vpn_probe_state.ProbeSlot]) -> None:
    ensure_ip_forward()
    forward, nat = resolve_probe_forward_targets()
    script_content = vpn_probe_forward_script_content(forward, nat, slots)
    if read_text_if_exists(VPN_PROBE_FORWARD_SCRIPT_PATH) != script_content:
        install_root_file(
            VPN_PROBE_FORWARD_SCRIPT_PATH, script_content, mode="0755", make_parents=True
        )
    replace_unit(
        VPN_PROBE_FORWARD_UNIT_FILE,
        vpn_probe_forward_unit_content(VPN_PROBE_FORWARD_SCRIPT_PATH, slots),
    )
    enable_now(VPN_PROBE_FORWARD_UNIT_FILE.name)


def make_vpn_probe_forwarding_persist_fixup(slots: list[vpn_probe_state.ProbeSlot]) -> Fixup:
    return Fixup(
        id="vpn-check-probe-forwarding-persist",
        title="Пережить перезагрузку: nft forward/NAT пробников переигрываются systemd-юнитом",
        needed=needs.vpn_check,
        check=lambda: _forwarding_persist_check(slots),
        apply=lambda: _forwarding_persist_apply(slots),
    )


def _state_apply(slots: list[vpn_probe_state.ProbeSlot]) -> None:
    install_root_file(
        vpn_probe_state.STATE_PATH, vpn_probe_state.render(slots), mode="0644", make_parents=True
    )


def make_vpn_probe_state_fixup(slots: list[vpn_probe_state.ProbeSlot]) -> Fixup:
    return Fixup(
        id="vpn-check-probe-state",
        title="Записать список настроенных пробников (что реально проверяется с этой ноды)",
        needed=needs.vpn_check,
        check=lambda: file_matches(vpn_probe_state.STATE_PATH, vpn_probe_state.render(slots)),
        apply=lambda: _state_apply(slots),
    )
