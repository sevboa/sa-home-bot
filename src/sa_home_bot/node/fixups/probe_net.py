from __future__ import annotations

import asyncio
import logging
import re
import socket
import subprocess
from dataclasses import dataclass
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
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

VPN_PROBE_SCAFFOLD_UNIT_DIR = Path("/etc/systemd/system")
VPN_PROBE_SUBNET_BASE = "10.200"
VPN_PROBE_FIRST_THIRD_OCTET = 200
SLOT_INDEX_LIMIT = 128
VPN_PROBE_NFT_TABLE = "sa_vpn_probe"
VPN_PROBE_FORWARD_SCRIPT_PATH = Path("/usr/local/lib/sa-home-bot/vpn-probe-forward-reapply.sh")
VPN_PROBE_FORWARD_UNIT_FILE = Path("/etc/systemd/system/sa-home-vpn-probe-forward.service")
VPN_PROBE_LEGACY_UNIT = "sa-home-vpn-probe.service"
VPN_PROBE_LEGACY_NETNS = "vpn-probe"
VPN_PROBE_LEGACY_VETH = "vprobe-veth0"
REALITY_SOCKS_PORT_BASE = 11080
SLOT_SUBNET_SIZE = 4
ALT_SLOT_INDEX_BASE = 32
XHTTP_SLOT_INDEX_BASE = 56
AWG_ALT_SLOT_INDEX_BASE = 48
ALT_SLOT_INDEX_BASES = {
    vpn_protocol.PROBE_TRANSPORT_REALITY_ALT: ALT_SLOT_INDEX_BASE,
    vpn_protocol.PROBE_TRANSPORT_AWG_ALT: AWG_ALT_SLOT_INDEX_BASE,
    vpn_protocol.PROBE_TRANSPORT_REALITY_XHTTP: XHTTP_SLOT_INDEX_BASE,
}

SCAFFOLD_UNIT_GLOB = "sa-home-vpn-probe-scaffold-*.service"
PROBE_NETNS_PREFIX = "vpn-probe-"
PROBE_VETH_HOST_LINE = re.compile(r"^\d+:\s+(vprobe\d+h0)[@:]")

WAIT_TABLE_FUNCTION = [
    "wait_table() {",
    "  i=0",
    "  while [ $i -lt 60 ]; do",
    '    nft list table "$1" "$2" >/dev/null 2>&1 && return 0',
    "    i=$((i + 1))",
    "    sleep 1",
    "  done",
    "  return 1",
    "}",
]
FORWARD_CHAIN_DEFINITION = "{ type filter hook forward priority filter ; policy accept ; }"
NAT_CHAIN_DEFINITION = "{ type nat hook postrouting priority srcnat ; policy accept ; }"


def probe_slot_from_target(index: int, target: vpn_nodes.ProbeTarget) -> vpn_probe_state.ProbeSlot:
    offset = index * SLOT_SUBNET_SIZE
    third_octet = VPN_PROBE_FIRST_THIRD_OCTET + offset // 256
    prefix = f"{VPN_PROBE_SUBNET_BASE}.{third_octet}"
    base = offset % 256
    is_awg = target.transport in vpn_protocol.AWG_PROBE_TRANSPORTS
    return vpn_probe_state.ProbeSlot(
        server=target.server,
        transport=target.transport,
        netns=f"vpn-probe-{target.server}-{target.transport}",
        veth_host=f"vprobe{index}h0",
        veth_ns=f"vprobe{index}n0",
        veth_host_addr=f"{prefix}.{base + 1}/30",
        veth_ns_addr=f"{prefix}.{base + 2}/30",
        subnet=f"{prefix}.{base}/30",
        iface=f"awg-probe{index}" if is_awg else None,
        socks_port=None if is_awg else REALITY_SOCKS_PORT_BASE + index,
    )


@dataclass(frozen=True)
class ProbePlan:
    slots: list[vpn_probe_state.ProbeSlot]
    indices: dict[str, int]


def _first_free_index(transport: str, taken: set[int]) -> int:
    start = ALT_SLOT_INDEX_BASES.get(transport, 0)
    for index in [*range(start, SLOT_INDEX_LIMIT), *range(start)]:
        if index not in taken:
            return index
    raise FixupError(f"закончились номера слотов пробников (предел {SLOT_INDEX_LIMIT})")


def assign_indices(targets: list[vpn_nodes.ProbeTarget], known: dict[str, int]) -> dict[str, int]:
    indices = dict(known)
    taken = set(indices.values())
    for target in sorted(targets):
        key = vpn_probe_state.slot_key(target.server, target.transport)
        if key not in indices:
            indices[key] = _first_free_index(target.transport, taken)
            taken.add(indices[key])
    return indices


def probe_slots_from_targets(
    targets: list[vpn_nodes.ProbeTarget], known: dict[str, int] | None = None
) -> list[vpn_probe_state.ProbeSlot]:
    indices = assign_indices(targets, known or {})
    ordered = [t for t in targets if t.transport not in ALT_SLOT_INDEX_BASES] + [
        t for t in targets if t.transport in ALT_SLOT_INDEX_BASES
    ]
    return [
        probe_slot_from_target(indices[vpn_probe_state.slot_key(t.server, t.transport)], t)
        for t in ordered
    ]


async def _fetch_probe_targets(settings: Settings) -> list[vpn_nodes.ProbeTarget]:
    client = ProtoClient(resolve_endpoint(settings.node.socket), token=settings.swarm.token)
    try:
        await client.connect()
        return await vpn_nodes.probe_targets(client, exclude=socket.gethostname())
    finally:
        await client.close()


def discover_probe_plan(settings: Settings) -> ProbePlan:
    known = vpn_probe_state.load_indices()
    try:
        targets = asyncio.run(_fetch_probe_targets(settings))
    except Exception as exc:
        log.warning("nodectl fix: не удалось обнаружить VPN-серверы для пробника: %s", exc)
        return ProbePlan(slots=[], indices=known)
    return ProbePlan(
        slots=probe_slots_from_targets(targets, known),
        indices=assign_indices(targets, known),
    )


def scaffold_unit_path(slot: vpn_probe_state.ProbeSlot) -> Path:
    name = f"sa-home-vpn-probe-scaffold-{slot.server}-{slot.transport}.service"
    return VPN_PROBE_SCAFFOLD_UNIT_DIR / name


def vpn_probe_scaffold_unit_content(slot: vpn_probe_state.ProbeSlot, ip_path: str) -> str:
    host_addr_only = slot.veth_host_addr.split("/")[0]
    pre_steps = [
        f"-{ip_path} netns add {slot.netns}",
        f"-{ip_path} link del {slot.veth_host}",
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


@dataclass(frozen=True)
class StaleProbeArtifacts:
    units: list[str]
    netns: list[str]
    veths: list[str]

    def __bool__(self) -> bool:
        return bool(self.units or self.netns or self.veths)


def _split_key(key: str) -> tuple[str, str]:
    server, _, transport = key.partition("/")
    return server, transport


def _expected_artifacts(indices: dict[str, int]) -> tuple[set[str], set[str], set[str]]:
    units, netns, veths = set(), set(), set()
    for key, index in indices.items():
        server, transport = _split_key(key)
        units.add(f"sa-home-vpn-probe-scaffold-{server}-{transport}.service")
        netns.add(f"{PROBE_NETNS_PREFIX}{server}-{transport}")
        veths.add(f"vprobe{index}h0")
    return units, netns, veths


def _command_lines(argv: list[str]) -> list[str]:
    result = subprocess.run(argv, capture_output=True, text=True)
    return result.stdout.splitlines() if result.returncode == 0 else []


def _installed_scaffold_units() -> list[str]:
    lines = _command_lines(
        ["systemctl", "list-unit-files", SCAFFOLD_UNIT_GLOB, "--no-legend", "--plain"]
    )
    return [line.split()[0] for line in lines if line.strip()]


def _host_probe_netns() -> list[str]:
    lines = _command_lines(["sudo", "-n", "ip", "netns", "list"])
    names = [line.split()[0] for line in lines if line.strip()]
    return [name for name in names if name.startswith(PROBE_NETNS_PREFIX)]


def _host_probe_veths() -> list[str]:
    lines = _command_lines(["ip", "-o", "link", "show", "type", "veth"])
    matches = (PROBE_VETH_HOST_LINE.match(line) for line in lines)
    return [m.group(1) for m in matches if m]


def find_stale_probe_artifacts(indices: dict[str, int]) -> StaleProbeArtifacts:
    units, netns, veths = _expected_artifacts(indices)
    return StaleProbeArtifacts(
        units=sorted(set(_installed_scaffold_units()) - units),
        netns=sorted(set(_host_probe_netns()) - netns),
        veths=sorted(set(_host_probe_veths()) - veths),
    )


def _stale_cleanup_apply(indices: dict[str, int]) -> None:
    stale = find_stale_probe_artifacts(indices)
    for unit in stale.units:
        run_sudo(["systemctl", "disable", "--now", unit])
        run_sudo(["rm", "-f", str(VPN_PROBE_SCAFFOLD_UNIT_DIR / unit)])
    if stale.units:
        run_sudo(["systemctl", "daemon-reload"])
    for netns in stale.netns:
        subprocess.run(["sudo", "ip", "netns", "del", netns], capture_output=True)
    for veth in stale.veths:
        subprocess.run(["sudo", "ip", "link", "del", veth], capture_output=True)
    if find_stale_probe_artifacts(indices):
        raise FixupError("остатки слотов пробников, которых нет в рое, не убрались")


def make_vpn_probe_stale_cleanup_fixup(plan: ProbePlan) -> Fixup:
    return Fixup(
        id="vpn-probe-stale-cleanup",
        title="Снести юниты, netns и veth слотов пробников, которых нет в рое и в состоянии",
        needed=needs.vpn_check,
        check=lambda: not plan.slots or not find_stale_probe_artifacts(plan.indices),
        apply=lambda: _stale_cleanup_apply(plan.indices),
    )


def _create_own_chain(
    family: str, name: str, chain_type: str, hook: str, priority: str
) -> NftChain:
    run_sudo(["nft", "add", "table", family, VPN_PROBE_NFT_TABLE])
    run_sudo(
        [
            "nft",
            "add",
            "chain",
            family,
            VPN_PROBE_NFT_TABLE,
            name,
            "{",
            "type",
            chain_type,
            "hook",
            hook,
            "priority",
            priority,
            ";",
            "policy",
            "accept",
            ";",
            "}",
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
            f'echo "нет таблицы {chain.family} {chain.table} — '
            'некуда ставить правила пробников" >&2; '
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
        '# "Could not process rule: No such file or directory" на первом же',
        "# insert, set -e прибил остальное, и forward/NAT пробников не появились",
        "# ВООБЩЕ — все проверки с этой ноды молча таймаутили до ручного",
        "# nodectl fix. Поэтому ждём появления таблицы, а не падаем сразу.",
        *WAIT_TABLE_FUNCTION,
        _ensure_chain_line(forward, FORWARD_CHAIN_DEFINITION),
        _ensure_chain_line(nat, NAT_CHAIN_DEFINITION),
    ]
    for slot in slots:
        host = slot.veth_host
        lines.append(
            _ensure_rule_line(forward, f'iifname "{host}" accept', f"iifname {host} accept")
        )
        lines.append(
            _ensure_rule_line(forward, f'oifname "{host}" accept', f"oifname {host} accept")
        )
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


def _slots_with_sni(slots: list[vpn_probe_state.ProbeSlot]) -> list[vpn_probe_state.ProbeSlot]:
    result = []
    for slot in slots:
        if slot.transport in vpn_protocol.REALITY_PROBE_TRANSPORTS:
            conf = read_text_if_exists(vpn_probe_state.reality_conf_path(slot))
            sni = vpn_probe_state.reality_conf_sni(conf) if conf else None
            if sni:
                slot = slot.model_copy(update={"sni": sni})
        result.append(slot)
    return result


def _state_check(slots: list[vpn_probe_state.ProbeSlot], indices: dict[str, int] | None) -> bool:
    expected = vpn_probe_state.render(_slots_with_sni(slots), indices)
    return file_matches(vpn_probe_state.STATE_PATH, expected)


def _state_apply(slots: list[vpn_probe_state.ProbeSlot], indices: dict[str, int] | None) -> None:
    install_root_file(
        vpn_probe_state.STATE_PATH,
        vpn_probe_state.render(_slots_with_sni(slots), indices),
        mode="0644",
        make_parents=True,
    )


def make_vpn_probe_state_fixup(
    slots: list[vpn_probe_state.ProbeSlot], indices: dict[str, int] | None = None
) -> Fixup:
    return Fixup(
        id="vpn-check-probe-state",
        title="Записать список настроенных пробников (что реально проверяется с этой ноды)",
        needed=needs.vpn_check,
        check=lambda: _state_check(slots, indices),
        apply=lambda: _state_apply(slots, indices),
    )
