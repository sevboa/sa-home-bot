from __future__ import annotations

import subprocess
from collections.abc import Callable
from getpass import getuser
from pathlib import Path

from sa_home_bot.config import Settings
from sa_home_bot.node.fixups import needs
from sa_home_bot.node.fixups.core import Fixup, FixupError
from sa_home_bot.node.fixups.netfilter import nft_json, nft_output
from sa_home_bot.node.fixups.privileged import (
    find_executable,
    install_from,
    install_package,
    install_sudoers_snippet,
    run_sudo,
    run_user,
    temp_file,
    unit_is_active,
)
from sa_home_bot.node.fixups.toolchain import MTG_BIN_PATH, install_mtg
from sa_home_bot.vpn import protocol as vpn_protocol

PROXY_UNITS_DIR = Path.home() / ".config" / "systemd" / "user"
MTG_UNIT_PATH = PROXY_UNITS_DIR / "mtg.service"
MICROSOCKS_UNIT_PATH = PROXY_UNITS_DIR / "microsocks.service"
PROXY_FIREWALL_SUDOERS_FILE = "50-sa-home-node-proxy-nft"
NFTABLES_CONF_PATH = Path("/etc/nftables.conf")
PROXY_COUNTERS = ("mtg_bytes", "socks_bytes")


def mtg_unit_content(port: int, secret: str) -> str:
    return (
        "[Unit]\n"
        "Description=mtg (MTProto proxy for Telegram)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={MTG_BIN_PATH} simple-run 0.0.0.0:{port} {secret}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def microsocks_unit_content(bind_host: str, port: int, microsocks_path: str) -> str:
    return (
        "[Unit]\n"
        "Description=microsocks (SOCKS5 proxy for sa-home-bot Telegram egress)\n"
        "After=network-online.target tailscaled.service\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={microsocks_path} -i {bind_host} -p {port}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _proxy_units_check() -> bool:
    return (
        MTG_BIN_PATH.is_file()
        and MTG_UNIT_PATH.exists()
        and MICROSOCKS_UNIT_PATH.exists()
        and unit_is_active("mtg.service", user=True)
        and unit_is_active("microsocks.service", user=True)
    )


def _install_microsocks() -> None:
    if find_executable("microsocks") is None:
        install_package("microsocks", "не найден известный пакетный менеджер для microsocks")


def _proxy_units_apply(settings: Settings) -> None:
    install_mtg()
    _install_microsocks()
    if not settings.vpn.socks_host:
        raise FixupError(
            "не настроен [vpn].socks_host — впишите tailscale-адрес jeeves и повторите nodectl fix"
        )
    PROXY_UNITS_DIR.mkdir(parents=True, exist_ok=True)
    if not MTG_UNIT_PATH.exists():
        MTG_UNIT_PATH.write_text(
            mtg_unit_content(settings.vpn.mtg_port, vpn_protocol.PROXY_SECRET_SEED),
            encoding="utf-8",
        )
    if not MICROSOCKS_UNIT_PATH.exists():
        microsocks_path = find_executable("microsocks") or "/usr/bin/microsocks"
        MICROSOCKS_UNIT_PATH.write_text(
            microsocks_unit_content(
                settings.vpn.socks_host, settings.vpn.socks_port, microsocks_path
            ),
            encoding="utf-8",
        )
    run_user(["systemctl", "--user", "daemon-reload"])
    run_user(["systemctl", "--user", "enable", "--now", "mtg.service", "microsocks.service"])


def make_proxy_units_fixup(settings: Settings) -> Fixup:
    return Fixup(
        id="proxy-units",
        title="Поднять mtg (MTProto) + microsocks (SOCKS5) на jeeves",
        needed=needs.vpn_awg,
        check=_proxy_units_check,
        apply=lambda: _proxy_units_apply(settings),
    )


def _filter_table_json() -> dict:
    return nft_json(["list", "table", "inet", "filter"])


def _existing_counter_names(data: dict) -> set[str]:
    return {
        counter["name"]
        for item in data.get("nftables", [])
        if (counter := item.get("counter")) is not None
    }


def _rule_counter_names(data: dict) -> set[str]:
    names: set[str] = set()
    for item in data.get("nftables", []):
        rule = item.get("rule")
        if rule is None or rule.get("chain") != "input":
            continue
        for expr in rule.get("expr", []):
            counter = expr.get("counter")
            if isinstance(counter, str):
                names.add(counter)
            elif isinstance(counter, dict) and counter.get("name"):
                names.add(counter["name"])
    return names


def _proxy_firewall_check() -> bool:
    return set(PROXY_COUNTERS) <= _rule_counter_names(_filter_table_json())


def _nft_check_file(path: Path) -> None:
    nft = find_executable("nft") or "nft"
    try:
        result = subprocess.run(["sudo", nft, "-c", "-f", str(path)])
    except OSError as exc:
        raise FixupError(f"не удалось запустить nft -c: {exc}") from exc
    if result.returncode != 0:
        raise FixupError(
            f"сгенерированный {NFTABLES_CONF_PATH} не проходит `nft -c` "
            "(ошибку см. выше) — файл НЕ изменён"
        )


def _line_index(lines: list[str], predicate: Callable[[str], bool], start: int = 0) -> int | None:
    return next((i for i in range(start, len(lines)) if predicate(lines[i])), None)


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def nftables_conf_with_counters(content: str, settings: Settings) -> str:
    lines = content.splitlines(keepends=True)
    table_i = _line_index(lines, lambda s: s.strip() == "table inet filter {")
    chain_i = (
        _line_index(lines, lambda s: s.strip() == "chain input {", table_i + 1)
        if table_i is not None
        else None
    )
    hook_i = (
        _line_index(lines, lambda s: s.lstrip().startswith("type filter hook input"), chain_i + 1)
        if chain_i is not None
        else None
    )
    if table_i is None or chain_i is None or hook_i is None:
        raise FixupError(
            f"{NFTABLES_CONF_PATH}: не нашёл 'table inet filter {{' / 'chain input {{' / "
            "строку 'type filter hook input' — структура файла не та, что ожидалась; "
            "допишите счётчики вручную (см. node/fixups.py::_append_nftables_conf_counters) "
            "и повторите nodectl fix"
        )
    obj_indent = _indent(lines[chain_i])
    rule_indent = _indent(lines[hook_i])
    counter_objs = (
        f"{obj_indent}counter mtg_bytes {{ }}\n"
        f"{obj_indent}counter socks_bytes {{ }}\n"
    )
    counter_rules = (
        f"{rule_indent}tcp dport {settings.vpn.mtg_port} counter name mtg_bytes accept\n"
        f'{rule_indent}iifname "tailscale0" tcp dport {settings.vpn.socks_port} '
        "counter name socks_bytes accept\n"
    )
    lines.insert(hook_i + 1, counter_rules)
    lines.insert(table_i + 1, counter_objs)
    return "".join(lines)


def _append_nftables_conf_counters(settings: Settings) -> None:
    result = subprocess.run(
        ["sudo", "cat", str(NFTABLES_CONF_PATH)], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise FixupError(f"не удалось прочитать {NFTABLES_CONF_PATH}: {result.stderr.strip()}")
    if "counter mtg_bytes" in result.stdout:
        return
    new_content = nftables_conf_with_counters(result.stdout, settings)
    with temp_file(new_content, ".conf") as tmp:
        _nft_check_file(tmp)
        install_from(tmp, NFTABLES_CONF_PATH, mode="0755")


def _apply_proxy_firewall_live(settings: Settings) -> None:
    data = _filter_table_json()
    existing_counters = _existing_counter_names(data)
    for name in PROXY_COUNTERS:
        if name not in existing_counters:
            run_sudo(["nft", "add", "counter", "inet", "filter", name])
    if set(PROXY_COUNTERS) <= _rule_counter_names(data):
        return
    run_sudo(
        [
            "nft", "insert", "rule", "inet", "filter", "input",
            "tcp", "dport", str(settings.vpn.mtg_port),
            "counter", "name", "mtg_bytes", "accept",
        ]
    )
    run_sudo(
        [
            "nft", "insert", "rule", "inet", "filter", "input",
            "iifname", "tailscale0", "tcp", "dport", str(settings.vpn.socks_port),
            "counter", "name", "socks_bytes", "accept",
        ]
    )


def proxy_firewall_sudoers_content(nft_path: str, user: str) -> str:
    return (
        f"{user} ALL=(root) NOPASSWD: {nft_path} -j list counter inet filter mtg_bytes, "
        f"{nft_path} -j list counter inet filter socks_bytes\n"
    )


def _proxy_firewall_apply(settings: Settings) -> None:
    _append_nftables_conf_counters(settings)
    _apply_proxy_firewall_live(settings)
    nft_path = find_executable("nft") or "/usr/sbin/nft"
    install_sudoers_snippet(
        PROXY_FIREWALL_SUDOERS_FILE, proxy_firewall_sudoers_content(nft_path, getuser())
    )


def _inet_filter_missing() -> bool:
    output = nft_output(["list", "tables"])
    if output is None:
        return False
    return "table inet filter" not in output


def _proxy_firewall_needed(settings: Settings) -> bool:
    return needs.vpn_awg(settings) and not _inet_filter_missing()


def make_proxy_firewall_fixup(settings: Settings) -> Fixup:
    return Fixup(
        id="proxy-firewall",
        title="Счётчики трафика прокси в firewall (точечно, без полного reload)",
        needed=_proxy_firewall_needed,
        check=_proxy_firewall_check,
        apply=lambda: _proxy_firewall_apply(settings),
    )
