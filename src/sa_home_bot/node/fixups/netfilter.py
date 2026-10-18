from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import NamedTuple

from sa_home_bot.node.fixups.core import FixupError
from sa_home_bot.node.fixups.privileged import (
    find_executable,
    install_package,
    install_root_file,
    path_exists,
    run_sudo,
)

IP_FORWARD_PROC_PATH = Path("/proc/sys/net/ipv4/ip_forward")
IP_FORWARD_SYSCTL_PATH = Path("/etc/sysctl.d/99-sa-home-vpn-probe-forward.conf")
IP_FORWARD_SYSCTL_CONTENT = "net.ipv4.ip_forward = 1\n"


class NftChain(NamedTuple):
    family: str
    table: str
    name: str


def nft_output(argv: list[str]) -> str | None:
    result = subprocess.run(["sudo", "-n", "nft", *argv], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def nft_json(argv: list[str]) -> dict:
    output = nft_output(["-j", *argv])
    if output is None:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {}


def nft_ruleset_text() -> str:
    return nft_output(["list", "ruleset"]) or ""


def nft_chain_text(chain: NftChain) -> str:
    return nft_output(["list", "chain", *chain]) or ""


def find_base_chain(hook: str, chain_type: str) -> NftChain | None:
    for item in nft_json(["list", "ruleset"]).get("nftables", []):
        chain = item.get("chain")
        if chain is not None and chain.get("hook") == hook and chain.get("type") == chain_type:
            return NftChain(chain["family"], chain["table"], chain["name"])
    return None


def ensure_nft_installed() -> None:
    if find_executable("nft") is None:
        install_package("nftables", "nft не найден и неизвестен пакетный менеджер для nftables")


def ip_forward_enabled() -> bool:
    try:
        return IP_FORWARD_PROC_PATH.read_text().strip() == "1"
    except OSError:
        return False


def ensure_ip_forward() -> None:
    if ip_forward_enabled():
        return
    if not path_exists(IP_FORWARD_SYSCTL_PATH):
        install_root_file(IP_FORWARD_SYSCTL_PATH, IP_FORWARD_SYSCTL_CONTENT, mode="0644")
    run_sudo(["sysctl", "-w", "net.ipv4.ip_forward=1"])


def default_route_iface() -> str | None:
    ip = find_executable("ip") or "ip"
    try:
        result = subprocess.run(
            [ip, "-4", "route", "show", "default"], capture_output=True, text=True
        )
    except OSError:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if "dev" in parts and parts.index("dev") + 1 < len(parts):
            return parts[parts.index("dev") + 1]
    return None


def ufw_status_text() -> str | None:
    ufw = find_executable("ufw")
    if ufw is None:
        return None
    try:
        result = subprocess.run(["sudo", "-n", ufw, "status"], capture_output=True, text=True)
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def ufw_active(status_text: str) -> bool | None:
    first = status_text.strip().splitlines()[0].lower() if status_text.strip() else ""
    if "inactive" in first or "неактивен" in first:
        return False
    if "active" in first or "активен" in first:
        return True
    return None


def ufw_state() -> str | None:
    text = ufw_status_text()
    if text is None:
        return None
    active = ufw_active(text)
    return None if active is None else ("active" if active else "inactive")


def ufw_allows(status_text: str, spec: str) -> bool:
    port = spec.split("/")[0]
    for line in status_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "ALLOW" and parts[0] in (spec, port):
            return True
    return False


def ufw_in_use() -> bool:
    return find_executable("ufw") is not None and ufw_state() != "inactive"


def ufw_allow_all(specs: list[str], comment: str) -> None:
    ufw = find_executable("ufw")
    if ufw is None:
        raise FixupError("ufw не найден")
    text = ufw_status_text() or ""
    for spec in specs:
        if not ufw_allows(text, spec):
            run_sudo([ufw, "allow", spec, "comment", comment])


def ufw_allows_all(specs: list[str]) -> bool:
    text = ufw_status_text()
    if text is None or ufw_active(text) is not True:
        return False
    return all(ufw_allows(text, spec) for spec in specs)
