from __future__ import annotations

from getpass import getuser
from pathlib import Path

from sa_home_bot.config import Settings
from sa_home_bot.node.fixups import needs
from sa_home_bot.node.fixups.core import Fixup, FixupError
from sa_home_bot.node.fixups.netfilter import (
    default_route_iface,
    ensure_ip_forward,
    ufw_allow_all,
    ufw_allows_all,
    ufw_in_use,
    ufw_state,
)
from sa_home_bot.node.fixups.privileged import (
    SUDOERS_DIR,
    daemon_reload,
    enable_now,
    find_executable,
    install_root_file,
    install_sudoers_snippet,
    path_exists,
    unit_is_active,
)

AWG_SUDOERS_FILE = "50-sa-home-node-awg"
SUB_UFW_COMMENT = "sa-home vpn: подписка Hiddify / http-01"
TCP_IP_HEADER_BYTES = 40


def awg_sudoers_content(awg_path: str, interface: str, user: str) -> str:
    return (
        f"{user} ALL=(root) NOPASSWD: {awg_path} show *, "
        f"{awg_path} set {interface} peer *\n"
    )


def make_awg_sudoers_fixup(settings: Settings) -> Fixup:
    def apply() -> None:
        path = find_executable("awg")
        if path is None:
            raise FixupError(
                "awg не найден в PATH — сначала поставьте amneziawg-tools "
                "(см. deploy/setup-awg-jeeves.sh)"
            )
        install_sudoers_snippet(
            AWG_SUDOERS_FILE, awg_sudoers_content(path, settings.vpn.interface, getuser())
        )

    return Fixup(
        id="awg-sudoers",
        title="Разрешить управление awg без пароля (sudoers, только show/set peer)",
        needed=needs.vpn_awg,
        check=lambda: (SUDOERS_DIR / AWG_SUDOERS_FILE).exists(),
        apply=apply,
    )


def make_awg_ufw_fixup(settings: Settings) -> Fixup:
    specs = [f"{settings.vpn.endpoint_port}/udp"]
    comment = f"amneziawg {settings.vpn.interface} server (sa-home vpn)"
    return Fixup(
        id="awg-ufw-port",
        title=f"ufw: открыть UDP-порт awg-сервера ({settings.vpn.endpoint_port}/udp)",
        needed=lambda s: needs.vpn_awg(s) and ufw_in_use(),
        check=lambda: ufw_allows_all(specs),
        apply=lambda: ufw_allow_all(specs, comment),
    )


def sub_ufw_specs(settings: Settings) -> list[str]:
    vpn = settings.vpn
    if vpn.sub_port <= 0 or vpn.reality is None:
        return []
    return [f"{vpn.sub_port}/tcp", "80/tcp"]


def make_sub_ufw_fixup(settings: Settings) -> Fixup:
    specs = sub_ufw_specs(settings)
    return Fixup(
        id="sub-ufw-ports",
        title="ufw: порт страницы подписки Hiddify и 80/tcp для сертификата",
        needed=lambda s: bool(sub_ufw_specs(s)) and ufw_in_use(),
        check=lambda: ufw_allows_all(specs),
        apply=lambda: ufw_allow_all(specs, SUB_UFW_COMMENT),
    )


def awg_forward_paths(interface: str) -> tuple[Path, Path]:
    return (
        Path(f"/usr/local/lib/sa-home-bot/{interface}-forward-reapply.sh"),
        Path(f"/etc/systemd/system/sa-home-{interface}-forward.service"),
    )


def awg_forward_script_content(interface: str, subnet: str, out_iface: str, mtu: int) -> str:
    mss = mtu - TCP_IP_HEADER_BYTES
    fwd = "nft list chain ip filter FORWARD 2>/dev/null"
    nat = "nft list chain ip nat POSTROUTING 2>/dev/null"
    return (
        "#!/bin/sh\n"
        "# sa-home-bot: сгенерировано nodectl fix (39.0.8(f)) — forward+NAT для\n"
        f"# VPN-сервера {interface}; nft-правила ребут не переживают, переигрываем\n"
        "# при каждом старте. Идемпотентно (grep перед add).\n"
        "set -e\n"
        "wait_table() {\n"
        "  i=0\n"
        "  while [ $i -lt 60 ]; do\n"
        '    nft list table "$1" "$2" >/dev/null 2>&1 && return 0\n'
        "    i=$((i + 1))\n"
        "    sleep 1\n"
        "  done\n"
        "  return 1\n"
        "}\n"
        "wait_table ip filter\n"
        "wait_table ip nat\n"
        f'{fwd} | grep -qF "iifname \\"{interface}\\" accept" '
        f"|| nft add rule ip filter FORWARD iifname {interface} accept\n"
        f'{fwd} | grep -qF "oifname \\"{interface}\\" accept" '
        f"|| nft add rule ip filter FORWARD oifname {interface} accept\n"
        f'{nat} | grep -qF "ip saddr {subnet} oifname \\"{out_iface}\\" masquerade" '
        f"|| nft add rule ip nat POSTROUTING ip saddr {subnet} "
        f"oifname {out_iface} masquerade\n"
        "# MSS-clamp (path MTU из РФ, см. 39.0.3); insert — до терминирующих accept.\n"
        f'if ! {fwd} | grep -qF "maxseg size set {mss}"; then\n'
        + "".join(
            f"  nft insert rule ip filter FORWARD {d}ifname {interface} tcp flags syn / syn,rst "
            f"tcp option maxseg size gt {mss} tcp option maxseg size set {mss}\n"
            for d in ("o", "i")
        )
        + "fi\n"
    )


def awg_forward_unit_content(interface: str, script_path: Path) -> str:
    return (
        "[Unit]\n"
        f"Description=sa-home-bot: forward/NAT для VPN-сервера {interface} (переживает ребут)\n"
        f"After=network-online.target awg-quick@{interface}.service "
        "ufw.service tailscaled.service\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"ExecStart=/bin/sh {script_path}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def make_awg_forward_fixup(settings: Settings) -> Fixup:
    interface = settings.vpn.interface
    script, unit = awg_forward_paths(interface)

    def check() -> bool:
        return path_exists(unit) and unit_is_active(unit.name)

    def apply() -> None:
        out_iface = default_route_iface()
        if out_iface is None:
            raise FixupError("не определён внешний интерфейс (нет маршрута по умолчанию)")
        ensure_ip_forward()
        files = [
            (
                script,
                awg_forward_script_content(
                    interface, settings.vpn.subnet, out_iface, settings.vpn.mtu
                ),
                "0755",
            ),
            (unit, awg_forward_unit_content(interface, script), "0644"),
        ]
        for path, content, mode in files:
            if not path_exists(path):
                install_root_file(path, content, mode=mode, make_parents=True)
        daemon_reload()
        enable_now(unit.name)

    return Fixup(
        id="awg-forward-persist",
        title=f"Пережить перезагрузку: nft forward/NAT для {interface} (systemd-юнит)",
        needed=lambda s: needs.vpn_awg(s) and ufw_state() == "active",
        check=check,
        apply=apply,
    )
