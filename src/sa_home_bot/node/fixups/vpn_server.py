from __future__ import annotations

import copy
import ipaddress
import json
import os
import stat
import subprocess
import tempfile
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
    file_matches,
    find_executable,
    install_root_file,
    install_sudoers_snippet,
    path_exists,
    unit_is_active,
)

AWG_SUDOERS_FILE = "50-sa-home-node-awg"
SUB_UFW_COMMENT = "sa-home vpn: подписка Hiddify / http-01"
TCP_IP_HEADER_BYTES = 40
AWG_CONF_DIR = Path("/etc/amnezia/amneziawg")


def awg_alt_conf_content(alt, private_key: str, mtu: int, out_iface: str) -> str:
    subnet = alt.subnet
    network = ipaddress.ip_network(subnet, strict=False)
    address = f"{next(network.hosts())}/{network.prefixlen}"
    lines = [
        "[Interface]",
        f"Address = {address}",
        f"ListenPort = {alt.port}",
        f"PrivateKey = {private_key}",
        f"MTU = {mtu}",
        f"Jc = {alt.jc}",
        f"Jmin = {alt.jmin}",
        f"Jmax = {alt.jmax}",
        f"S1 = {alt.s1}",
        f"S2 = {alt.s2}",
        f"S3 = {alt.s3}",
        f"S4 = {alt.s4}",
        f"H1 = {alt.h1}",
        f"H2 = {alt.h2}",
        f"H3 = {alt.h3}",
        f"H4 = {alt.h4}",
    ]
    for i in range(1, 6):
        val = getattr(alt, f"i{i}")
        if val:
            lines.append(f"I{i} = {val}")
    post_up = (
        f"iptables -A FORWARD -i {alt.interface} -j ACCEPT; "
        f"iptables -A FORWARD -o {alt.interface} -j ACCEPT; "
        f"iptables -t nat -A POSTROUTING -s {subnet} -o {out_iface} -j MASQUERADE; "
        f"iptables -t mangle -A FORWARD -o {alt.interface} -p tcp --tcp-flags SYN,RST SYN "
        f"-j TCPMSS --set-mss {mtu - TCP_IP_HEADER_BYTES}; "
        f"iptables -t mangle -A FORWARD -i {alt.interface} -p tcp --tcp-flags SYN,RST SYN "
        f"-j TCPMSS --set-mss {mtu - TCP_IP_HEADER_BYTES}"
    )
    post_down = (
        f"iptables -D FORWARD -i {alt.interface} -j ACCEPT; "
        f"iptables -D FORWARD -o {alt.interface} -j ACCEPT; "
        f"iptables -t nat -D POSTROUTING -s {subnet} -o {out_iface} -j MASQUERADE; "
        f"iptables -t mangle -D FORWARD -o {alt.interface} -p tcp --tcp-flags SYN,RST SYN "
        f"-j TCPMSS --set-mss {mtu - TCP_IP_HEADER_BYTES}; "
        f"iptables -t mangle -D FORWARD -i {alt.interface} -p tcp --tcp-flags SYN,RST SYN "
        f"-j TCPMSS --set-mss {mtu - TCP_IP_HEADER_BYTES}"
    )
    lines.append(f"PostUp = {post_up}")
    lines.append(f"PostDown = {post_down}")
    return "\n".join(lines) + "\n"


def xray_config_path() -> Path:
    return Path.home() / ".config/xray/config.json"


def build_alt_inbound(config: dict, alt) -> dict:
    reality_in = None
    for inbound in config.get("inbounds", []):
        if inbound.get("tag") == "reality-in":
            reality_in = inbound
            break
    if reality_in is None:
        raise FixupError("в конфиге xray нет входа reality-in")
    inbound = copy.deepcopy(reality_in)
    inbound["tag"] = alt.inbound_tag
    inbound["port"] = alt.port
    inbound["settings"]["clients"] = []
    rs = inbound["streamSettings"]["realitySettings"]
    rs["dest"] = alt.effective_dest
    rs["serverNames"] = [alt.sni]
    return inbound


def alt_inbound_matches(config: dict, alt) -> bool:
    for inbound in config.get("inbounds", []):
        if inbound.get("tag") == alt.inbound_tag:
            if inbound.get("port") != alt.port:
                return False
            rs = inbound.get("streamSettings", {}).get("realitySettings", {})
            if rs.get("dest") != alt.effective_dest:
                return False
            if rs.get("serverNames") != [alt.sni]:
                return False
            return True
    return False


def _xray_api(args: list[str]) -> subprocess.CompletedProcess:
    xray = find_executable("xray") or "/usr/local/bin/xray"
    return subprocess.run([xray, "api", *args], capture_output=True, text=True, timeout=30)


def make_reality_alt_inbound_fixup(settings: Settings) -> Fixup:
    def check() -> bool:
        alt = settings.vpn.reality.alt
        api_addr = settings.vpn.reality.api_addr
        try:
            config_path = xray_config_path()
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not alt_inbound_matches(config, alt):
            return False
        try:
            result = _xray_api(["lsi", f"--server={api_addr}", "--isOnlyTags=true"])
        except Exception:
            return False
        if result.returncode != 0:
            return False
        try:
            data = json.loads(result.stdout)
        except Exception:
            return False
        for inbound in data.get("inbounds", []):
            if inbound.get("tag") == alt.inbound_tag:
                return True
        return False

    def apply() -> None:
        alt = settings.vpn.reality.alt
        api_addr = settings.vpn.reality.api_addr
        config_path = xray_config_path()
        config = json.loads(config_path.read_text(encoding="utf-8"))

        existing = None
        for inbound in config.get("inbounds", []):
            if inbound.get("tag") == alt.inbound_tag:
                existing = inbound
                break

        if existing is not None:
            if not alt_inbound_matches(config, alt):
                raise FixupError("вход уже есть с другими настройками, менять вручную")
        else:
            inbound = build_alt_inbound(config, alt)
            config["inbounds"].append(inbound)
            fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".json")
            try:
                os.write(fd, json.dumps(config, indent=2).encode("utf-8"))
                os.close(fd)
                os.chmod(tmp_path, 0o600)
                xray_bin = find_executable("xray") or "/usr/local/bin/xray"
                test_result = subprocess.run(
                    [xray_bin, "run", "-test", "-c", tmp_path],
                    capture_output=True,
                    timeout=30,
                )
                if test_result.returncode != 0:
                    os.unlink(tmp_path)
                    raise FixupError(f"xray run -test: код {test_result.returncode}")
                old_stat = config_path.stat()
                os.chmod(tmp_path, stat.S_IMODE(old_stat.st_mode))
                os.replace(tmp_path, config_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

        try:
            result = _xray_api(["lsi", f"--server={api_addr}", "--isOnlyTags=true"])
        except Exception as exc:
            raise FixupError("xray api lsi не ответил") from exc
        if result.returncode != 0:
            raise FixupError("xray api lsi не ответил")
        try:
            data = json.loads(result.stdout)
        except Exception as exc:
            raise FixupError("xray api lsi не ответил") from exc
        has_tag = any(inbound.get("tag") == alt.inbound_tag for inbound in data.get("inbounds", []))
        if has_tag:
            return

        inbound = build_alt_inbound(config, alt)
        fd, tmp_path = tempfile.mkstemp(suffix=".json")
        try:
            os.write(fd, json.dumps({"inbounds": [inbound]}, indent=2).encode("utf-8"))
            os.close(fd)
            os.chmod(tmp_path, 0o600)
            try:
                adi_result = _xray_api(["adi", f"--server={api_addr}", tmp_path])
                if adi_result.returncode != 0:
                    raise FixupError(f"xray api adi: код {adi_result.returncode}")
            finally:
                os.unlink(tmp_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    return Fixup(
        id="reality-alt-inbound",
        title="Второй вход Reality (alt) в xray через API без рестарта",
        needed=lambda s: needs.vpn(s) and getattr(s.vpn.reality, "alt", None) is not None,
        check=check,
        apply=apply,
    )


def reality_alt_ufw_specs(settings: Settings) -> list[str]:
    alt = getattr(settings.vpn.reality, "alt", None)
    if alt is None:
        return []
    return [f"{alt.port}/tcp"]


def make_reality_alt_ufw_fixup(settings: Settings) -> Fixup:
    specs = reality_alt_ufw_specs(settings)
    return Fixup(
        id="reality-alt-ufw-port",
        title=f"ufw: открыть TCP-порт второго входа Reality ({specs[0] if specs else 'нет'})",
        needed=lambda s: bool(reality_alt_ufw_specs(s)) and ufw_in_use(),
        check=lambda: ufw_allows_all(specs),
        apply=lambda: ufw_allow_all(specs, "sa-home vpn: второй вход Reality (alt)"),
    )


def awg_sudoers_content(
    awg_path: str, interface: str, user: str, extra_interfaces: tuple[str, ...] = ()
) -> str:
    parts = [f"{awg_path} show *", f"{awg_path} set {interface} peer *"]
    for iface in extra_interfaces:
        parts.append(f"{awg_path} set {iface} peer *")
    return f"{user} ALL=(root) NOPASSWD: {', '.join(parts)}\n"


def make_awg_sudoers_fixup(settings: Settings) -> Fixup:
    def apply() -> None:
        path = find_executable("awg")
        if path is None:
            raise FixupError(
                "awg не найден в PATH — сначала поставьте amneziawg-tools "
                "(см. deploy/setup-awg-jeeves.sh)"
            )
        extra = ()
        if settings.vpn.awg_alt is not None:
            extra = (settings.vpn.awg_alt.interface,)
        install_sudoers_snippet(
            AWG_SUDOERS_FILE, awg_sudoers_content(path, settings.vpn.interface, getuser(), extra)
        )

    def check() -> bool:
        if settings.vpn.awg_alt is None:
            return (SUDOERS_DIR / AWG_SUDOERS_FILE).exists()
        path = find_executable("awg")
        if path is None:
            return False
        extra = (settings.vpn.awg_alt.interface,)
        expected = awg_sudoers_content(path, settings.vpn.interface, getuser(), extra)
        return file_matches(SUDOERS_DIR / AWG_SUDOERS_FILE, expected)

    return Fixup(
        id="awg-sudoers",
        title="Разрешить управление awg без пароля (sudoers, только show/set peer)",
        needed=needs.vpn_awg,
        check=check,
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


def make_awg_alt_interface_fixup(settings: Settings) -> Fixup:
    alt = settings.vpn.awg_alt
    if alt is None:
        return Fixup(
            id="awg-alt-interface",
            title="Второй интерфейс AmneziaWG (не настроен)",
            needed=lambda s: False,
            check=lambda: True,
            apply=lambda: None,
        )
    conf_path = AWG_CONF_DIR / f"{alt.interface}.conf"
    unit_name = f"awg-quick@{alt.interface}"

    def check() -> bool:
        return path_exists(conf_path) and unit_is_active(unit_name)

    def apply() -> None:
        if not path_exists(conf_path):
            result = subprocess.run(["awg", "genkey"], capture_output=True, text=True)
            if result.returncode != 0:
                raise FixupError("awg genkey не сработал")
            private_key = result.stdout.strip()
            out_iface = default_route_iface()
            if out_iface is None:
                raise FixupError("не определён внешний интерфейс (нет маршрута по умолчанию)")
            content = awg_alt_conf_content(alt, private_key, settings.vpn.mtu, out_iface)
            install_root_file(conf_path, content, mode="0600", make_parents=True)
        ensure_ip_forward()
        daemon_reload()
        enable_now(unit_name)

    return Fixup(
        id="awg-alt-interface",
        title=f"Второй интерфейс AmneziaWG ({alt.interface})",
        needed=lambda s: needs.vpn_awg(s) and s.vpn.awg_alt is not None,
        check=check,
        apply=apply,
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


def make_awg_alt_ufw_fixup(settings: Settings) -> Fixup:
    alt = settings.vpn.awg_alt
    if alt is None:
        return Fixup(
            id="awg-alt-ufw-port",
            title="ufw: открыть UDP-порт второго awg-сервера (не настроен)",
            needed=lambda s: False,
            check=lambda: True,
            apply=lambda: None,
        )
    specs = [f"{alt.port}/udp"]
    comment = f"amneziawg {alt.interface} server (sa-home vpn)"
    return Fixup(
        id="awg-alt-ufw-port",
        title=f"ufw: открыть UDP-порт второго awg-сервера ({alt.port}/udp)",
        needed=lambda s: needs.vpn_awg(s) and s.vpn.awg_alt is not None and ufw_in_use(),
        check=lambda: ufw_allows_all(specs),
        apply=lambda: ufw_allow_all(specs, comment),
    )


def _awg_forward_fixup(
    fixup_id: str, title: str, interface: str, subnet: str, mtu: int, needed
) -> Fixup:
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
                awg_forward_script_content(interface, subnet, out_iface, mtu),
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
        id=fixup_id,
        title=title,
        needed=needed,
        check=check,
        apply=apply,
    )


def make_awg_forward_fixup(settings: Settings) -> Fixup:
    interface = settings.vpn.interface
    return _awg_forward_fixup(
        fixup_id="awg-forward-persist",
        title=f"Пережить перезагрузку: nft forward/NAT для {interface} (systemd-юнит)",
        interface=interface,
        subnet=settings.vpn.subnet,
        mtu=settings.vpn.mtu,
        needed=lambda s: needs.vpn_awg(s) and ufw_state() == "active",
    )


def make_awg_alt_forward_fixup(settings: Settings) -> Fixup:
    alt = settings.vpn.awg_alt
    if alt is None:
        return Fixup(
            id="awg-alt-forward-persist",
            title="Пережить перезагрузку: nft forward/NAT для второго awg (не настроен)",
            needed=lambda s: False,
            check=lambda: True,
            apply=lambda: None,
        )
    return _awg_forward_fixup(
        fixup_id="awg-alt-forward-persist",
        title=f"Пережить перезагрузку: nft forward/NAT для {alt.interface} (systemd-юнит)",
        interface=alt.interface,
        subnet=alt.subnet,
        mtu=settings.vpn.mtu,
        needed=lambda s: (
            needs.vpn_awg(s) and s.vpn.awg_alt is not None and ufw_state() == "active"
        ),
    )
