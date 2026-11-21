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
from sa_home_bot.node.fixups.host import NODE_UNIT_NAME
from sa_home_bot.node.fixups.netfilter import (
    NFTABLES_CONF_PATH,
    default_route_iface,
    ensure_ip_forward,
    filter_table_json,
    indent,
    line_index,
    nft_check_file,
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
    install_from,
    install_root_file,
    install_sudoers_snippet,
    path_exists,
    read_text,
    run_sudo,
    temp_file,
    unit_is_active,
)

AWG_SUDOERS_FILE = "50-sa-home-node-awg"
NFT_PORT_COMMENT = "# sa-home vpn (nodectl fix)"
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


def build_xhttp_inbound(config: dict, xhttp) -> dict:
    reality_in = None
    for inbound in config.get("inbounds", []):
        if inbound.get("tag") == "reality-in":
            reality_in = inbound
            break
    if reality_in is None:
        raise FixupError("в конфиге xray нет входа reality-in")
    inbound = copy.deepcopy(reality_in)
    inbound["tag"] = xhttp.inbound_tag
    inbound["port"] = xhttp.port
    inbound["settings"]["clients"] = []
    ss = inbound["streamSettings"]
    ss["network"] = "xhttp"
    ss["xhttpSettings"] = {"path": xhttp.path, "mode": xhttp.mode}
    rs = inbound["streamSettings"]["realitySettings"]
    rs["dest"] = xhttp.effective_dest
    rs["serverNames"] = [xhttp.sni]
    return inbound


def xhttp_inbound_matches(config: dict, xhttp) -> bool:
    for inbound in config.get("inbounds", []):
        if inbound.get("tag") == xhttp.inbound_tag:
            if inbound.get("port") != xhttp.port:
                return False
            ss = inbound.get("streamSettings", {})
            if ss.get("network") != "xhttp":
                return False
            xs = ss.get("xhttpSettings", {})
            if xs.get("path") != xhttp.path:
                return False
            if xs.get("mode") != xhttp.mode:
                return False
            rs = ss.get("realitySettings", {})
            if rs.get("dest") != xhttp.effective_dest:
                return False
            if rs.get("serverNames") != [xhttp.sni]:
                return False
            return True
    return False


def _xray_api(args: list[str]) -> subprocess.CompletedProcess:
    xray = find_executable("xray") or "/usr/local/bin/xray"
    return subprocess.run([xray, "api", *args], capture_output=True, text=True, timeout=30)


def write_xray_config_with_test(config_path: Path, config: dict) -> None:
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
            write_xray_config_with_test(config_path, config)

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


def reality_xhttp_ufw_specs(settings: Settings) -> list[str]:
    xhttp = getattr(settings.vpn.reality, "xhttp", None)
    if xhttp is None:
        return []
    return [f"{xhttp.port}/tcp"]


def make_reality_xhttp_inbound_fixup(settings: Settings) -> Fixup:
    def check() -> bool:
        xhttp = settings.vpn.reality.xhttp
        api_addr = settings.vpn.reality.api_addr
        try:
            config_path = xray_config_path()
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not xhttp_inbound_matches(config, xhttp):
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
            if inbound.get("tag") == xhttp.inbound_tag:
                return True
        return False

    def apply() -> None:
        xhttp = settings.vpn.reality.xhttp
        api_addr = settings.vpn.reality.api_addr
        config_path = xray_config_path()
        config = json.loads(config_path.read_text(encoding="utf-8"))

        existing = None
        for inbound in config.get("inbounds", []):
            if inbound.get("tag") == xhttp.inbound_tag:
                existing = inbound
                break

        if existing is not None:
            if not xhttp_inbound_matches(config, xhttp):
                raise FixupError("вход уже есть с другими настройками, менять вручную")
        else:
            inbound = build_xhttp_inbound(config, xhttp)
            config["inbounds"].append(inbound)
            write_xray_config_with_test(config_path, config)

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
        has_tag = any(
            inbound.get("tag") == xhttp.inbound_tag for inbound in data.get("inbounds", [])
        )
        if has_tag:
            return

        inbound = build_xhttp_inbound(config, xhttp)
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
        id="reality-xhttp-inbound",
        title="Вход Reality с транспортом XHTTP в xray через API без рестарта",
        needed=lambda s: needs.vpn(s) and getattr(s.vpn.reality, "xhttp", None) is not None,
        check=check,
        apply=apply,
    )


XRAY_API_INBOUND_TAG = "api-in"
XRAY_WARP_PROBE_INBOUND_TAG = "warp-probe"
XRAY_BLOCK_OUTBOUND_TAG = "block"
XRAY_ROUTING_DOMAIN_STRATEGY = "IPIfNonMatch"
XRAY_BLOCKED_DEST_IPS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)
XRAY_BLOCKED_DEST_DOMAINS = (
    "full:localhost",
    "domain:localhost",
    "domain:ts.net",
    "domain:local",
    "domain:internal",
    "domain:lan",
)


def xray_client_inbound_tags(config: dict) -> list[str]:
    return [
        inbound["tag"]
        for inbound in config.get("inbounds", [])
        if inbound.get("tag")
        and inbound.get("tag") not in (XRAY_API_INBOUND_TAG, XRAY_WARP_PROBE_INBOUND_TAG)
    ]


def xray_private_block_rules(config: dict) -> list[dict]:
    tags = xray_client_inbound_tags(config)
    return [
        {
            "type": "field",
            "inboundTag": tags,
            "outboundTag": XRAY_BLOCK_OUTBOUND_TAG,
            "domain": list(XRAY_BLOCKED_DEST_DOMAINS),
        },
        {
            "type": "field",
            "inboundTag": tags,
            "outboundTag": XRAY_BLOCK_OUTBOUND_TAG,
            "ip": list(XRAY_BLOCKED_DEST_IPS),
        },
    ]


def _normalized_rule(rule: dict) -> dict:
    out = dict(rule)
    if "inboundTag" in out:
        out["inboundTag"] = sorted(out["inboundTag"])
    return out


def xray_blocks_private(config: dict) -> bool:
    routing = config.get("routing") or {}
    if routing.get("domainStrategy") != XRAY_ROUTING_DOMAIN_STRATEGY:
        return False
    rules = [_normalized_rule(r) for r in routing.get("rules", [])]
    return all(_normalized_rule(r) in rules for r in xray_private_block_rules(config))


def with_private_block(config: dict) -> dict:
    out = copy.deepcopy(config)
    outbounds = out.setdefault("outbounds", [])
    if not any(o.get("tag") == XRAY_BLOCK_OUTBOUND_TAG for o in outbounds):
        outbounds.append({"tag": XRAY_BLOCK_OUTBOUND_TAG, "protocol": "blackhole"})
    routing = out.setdefault("routing", {})
    routing["domainStrategy"] = XRAY_ROUTING_DOMAIN_STRATEGY
    kept = [r for r in routing.get("rules", []) if r.get("outboundTag") != XRAY_BLOCK_OUTBOUND_TAG]
    api_rules = [r for r in kept if XRAY_API_INBOUND_TAG in r.get("inboundTag", [])]
    other_rules = [r for r in kept if r not in api_rules]
    routing["rules"] = api_rules + xray_private_block_rules(out) + other_rules
    return out


def make_xray_private_block_fixup(settings: Settings) -> Fixup:
    def check() -> bool:
        try:
            config = json.loads(xray_config_path().read_text(encoding="utf-8"))
        except Exception:
            return False
        return xray_blocks_private(config)

    def apply() -> None:
        config_path = xray_config_path()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        write_xray_config_with_test(config_path, with_private_block(config))
        restart = subprocess.run(["systemctl", "--user", "restart", "xray"], check=False)
        if restart.returncode != 0:
            raise FixupError("конфиг записан, но systemctl --user restart xray не сработал")
        subprocess.run(["systemctl", "--user", "restart", NODE_UNIT_NAME], check=False)

    return Fixup(
        id="xray-private-block",
        title=(
            "xray: клиентам VPN закрыты внутренние адреса сервера и Tailscale "
            "(рестарт xray и ноды — клиенты вернутся сверкой)"
        ),
        needed=lambda s: needs.vpn(s) and s.vpn.reality is not None,
        check=check,
        apply=apply,
    )


def make_reality_xhttp_ufw_fixup(settings: Settings) -> Fixup:
    specs = reality_xhttp_ufw_specs(settings)
    return Fixup(
        id="reality-xhttp-ufw-port",
        title=f"ufw: открыть TCP-порт входа XHTTP Reality ({specs[0] if specs else 'нет'})",
        needed=lambda s: bool(reality_xhttp_ufw_specs(s)) and ufw_in_use(),
        check=lambda: ufw_allows_all(specs),
        apply=lambda: ufw_allow_all(specs, "sa-home vpn: вход Reality с транспортом XHTTP"),
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


def make_awg_interface_fixup(settings: Settings) -> Fixup:
    iface = settings.vpn.interface
    conf_path = AWG_CONF_DIR / f"{iface}.conf"
    unit_name = f"awg-quick@{iface}"

    def needed(s: Settings) -> bool:
        return needs.vpn_awg(s) and path_exists(conf_path) and not unit_is_active(unit_name)

    def check() -> bool:
        return not path_exists(conf_path) or unit_is_active(unit_name)

    def apply() -> None:
        enable_now(unit_name)

    return Fixup(
        id="awg-interface",
        title=f"Подключить интерфейс AmneziaWG ({iface})",
        needed=needed,
        check=check,
        apply=apply,
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


def vpn_server_port_specs(settings: Settings) -> list[str]:
    vpn = settings.vpn
    specs = [f"{vpn.endpoint_port}/udp"]
    if vpn.awg_alt is not None:
        specs.append(f"{vpn.awg_alt.port}/udp")
    if vpn.reality is not None:
        specs.append(f"{vpn.reality.port}/tcp")
    specs += reality_alt_ufw_specs(settings)
    specs += reality_xhttp_ufw_specs(settings)
    specs += sub_ufw_specs(settings)
    return list(dict.fromkeys(specs))


def _split_port_spec(spec: str) -> tuple[str, int]:
    port, _, proto = spec.partition("/")
    return proto, int(port)


def _input_chain_bounds(lines: list[str]) -> tuple[int, int]:
    table_i = line_index(lines, lambda s: s.strip() == "table inet filter {")
    chain_i = (
        line_index(lines, lambda s: s.strip() == "chain input {", table_i + 1)
        if table_i is not None
        else None
    )
    hook_i = (
        line_index(lines, lambda s: s.lstrip().startswith("type filter hook input"), chain_i + 1)
        if chain_i is not None
        else None
    )
    if chain_i is None or hook_i is None:
        raise FixupError(
            f"{NFTABLES_CONF_PATH}: не нашёл 'table inet filter {{' / 'chain input {{' / "
            "строку 'type filter hook input' — структура файла не та, что ожидалась; "
            "допишите порты вручную и повторите nodectl fix"
        )
    chain_indent = indent(lines[chain_i])
    end_i = line_index(lines, lambda s: s.strip() == "}" and indent(s) == chain_indent, hook_i + 1)
    return hook_i, len(lines) if end_i is None else end_i


def _conf_opens(lines: list[str], proto: str, port: int) -> bool:
    prefix = [proto, "dport", str(port)]
    for line in lines:
        words = line.split("#", 1)[0].split()
        if words[:3] != prefix:
            continue
        rest = words[3:]
        if rest[:2] == ["counter", "name"]:
            rest = rest[3:]
        if rest == ["accept"]:
            return True
    return False


def nftables_conf_with_ports(content: str, specs: list[str]) -> str:
    lines = content.splitlines(keepends=True)
    hook_i, end_i = _input_chain_bounds(lines)
    chain_lines = lines[hook_i + 1 : end_i]
    rule_indent = indent(lines[hook_i])
    new_rules = []
    for spec in specs:
        proto, port = _split_port_spec(spec)
        if not _conf_opens(chain_lines, proto, port):
            new_rules.append(f"{rule_indent}{proto} dport {port} accept\t{NFT_PORT_COMMENT}\n")
    if not new_rules:
        return content
    lines[hook_i + 1 : hook_i + 1] = new_rules
    return "".join(lines)


def _live_open_ports(data: dict) -> set[tuple[str, int]]:
    opened: set[tuple[str, int]] = set()
    for item in data.get("nftables", []):
        rule = item.get("rule")
        if rule is None or rule.get("chain") != "input":
            continue
        matches = []
        accepted = False
        other = False
        for expr in rule.get("expr", []):
            if "match" in expr:
                matches.append(expr["match"])
            elif "accept" in expr:
                accepted = True
            elif "counter" not in expr:
                other = True
        if len(matches) != 1 or not accepted or other:
            continue
        match = matches[0]
        payload = (match.get("left") or {}).get("payload") or {}
        right = match.get("right")
        if (
            match.get("op") == "=="
            and payload.get("field") == "dport"
            and payload.get("protocol") in ("tcp", "udp")
            and isinstance(right, int)
        ):
            opened.add((payload["protocol"], right))
    return opened


def _nft_ports_live_missing(specs: list[str]) -> list[str]:
    opened = _live_open_ports(filter_table_json())
    return [spec for spec in specs if _split_port_spec(spec) not in opened]


def _nft_ports_file_content() -> str | None:
    try:
        return read_text(NFTABLES_CONF_PATH)
    except FixupError:
        return None


def _nft_ports_needed(settings: Settings) -> bool:
    if not needs.vpn_awg(settings) or ufw_in_use():
        return False
    content = _nft_ports_file_content()
    return content is not None and "table inet filter" in content


def _nft_ports_check(settings: Settings) -> bool:
    specs = vpn_server_port_specs(settings)
    content = _nft_ports_file_content()
    if content is None or _nft_ports_live_missing(specs):
        return False
    return nftables_conf_with_ports(content, specs) == content


def _nft_ports_apply(settings: Settings) -> None:
    specs = vpn_server_port_specs(settings)
    content = read_text(NFTABLES_CONF_PATH)
    new_content = nftables_conf_with_ports(content, specs)
    if new_content != content:
        with temp_file(new_content, ".conf") as tmp:
            nft_check_file(tmp)
            install_from(tmp, NFTABLES_CONF_PATH, mode="0755")
    for spec in _nft_ports_live_missing(specs):
        proto, port = _split_port_spec(spec)
        rule = ["inet", "filter", "input", proto, "dport", str(port), "accept"]
        run_sudo(["nft", "insert", "rule", *rule])


def make_vpn_nft_ports_fixup(settings: Settings) -> Fixup:
    return Fixup(
        id="vpn-nft-ports",
        title="nftables: открыть порты VPN-сервера в /etc/nftables.conf и живом наборе (без ufw)",
        needed=_nft_ports_needed,
        check=lambda: _nft_ports_check(settings),
        apply=lambda: _nft_ports_apply(settings),
    )
