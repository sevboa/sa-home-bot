from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from getpass import getuser
from pathlib import Path

from sa_home_bot.bot import vpn_nodes
from sa_home_bot.config import Settings
from sa_home_bot.node import vpn_probe_state
from sa_home_bot.node.fixups import needs
from sa_home_bot.node.fixups.core import Fixup, FixupError
from sa_home_bot.node.fixups.privileged import (
    SUDOERS_DIR,
    find_executable,
    install_root_file,
    install_sudoers_snippet,
    path_exists,
    read_text,
    read_text_if_exists,
)
from sa_home_bot.node.fixups.toolchain import build_amneziawg_tools, ensure_xray_binary
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import resolve_endpoint
from sa_home_bot.reality import probe_client_config
from sa_home_bot.vpn import protocol as vpn_protocol

VPN_PROBE_AWG_CONF_DIR = Path("/etc/amnezia/amneziawg")
VPN_PROBE_SUDOERS_FILE = "50-sa-home-node-vpn-probe"
VPN_PROBE_CHAT_ID = 0
SUPPORTED_PROBE_TRANSPORTS = ("awg", "reality")
ISSUE_TIMEOUT_S = 20.0


@asynccontextmanager
async def _node_client(settings: Settings) -> AsyncIterator[ProtoClient]:
    client = ProtoClient(resolve_endpoint(settings.node.socket), token=settings.swarm.token)
    try:
        await client.connect()
        yield client
    finally:
        await client.close()


def prepare_probe_conf(config_text: str) -> str:
    lines = [
        line
        for line in config_text.splitlines()
        if not line.strip().startswith("DNS ") and not line.strip().startswith("Table")
    ]
    return "\n".join(lines) + "\n"


def awg_conf_path(slot: vpn_probe_state.ProbeSlot) -> Path:
    return VPN_PROBE_AWG_CONF_DIR / f"{slot.iface}.conf"


async def _fetch_probe_config(settings: Settings, slot: vpn_probe_state.ProbeSlot) -> str:
    async with _node_client(settings) as client:
        dst = await vpn_nodes.resolve_vpn_dst(client, server=slot.server)
        if dst is None or dst.node != slot.server:
            raise FixupError(f"vpn@{slot.server} сейчас недоступна — конфиг пробника взять негде")
        result = await client.command(
            vpn_protocol.ACTION_ISSUE,
            {"chat_id": VPN_PROBE_CHAT_ID, "transport": slot.transport},
            dst=dst,
            timeout=ISSUE_TIMEOUT_S,
        )
    if slot.transport == "awg":
        config_text = result.get("config_text")
        if not config_text:
            raise FixupError(f"vpn@{slot.server} не вернул config_text")
        return str(config_text)
    share_url = result.get("share_url")
    if not share_url:
        raise FixupError(f"vpn@{slot.server} не вернул share_url")
    params, client_uuid = probe_client_config.parse_vless_url(str(share_url))
    return probe_client_config.render_probe_client_config(
        params, client_uuid, socks_port=slot.socks_port
    )


async def _fetch_server_public_key(
    settings: Settings, slot: vpn_probe_state.ProbeSlot
) -> str | None:
    try:
        async with _node_client(settings) as client:
            dst = await vpn_nodes.resolve_vpn_dst(client, server=slot.server)
            if dst is None or dst.node != slot.server:
                return None
            state = await client.get_state(dst=dst)
    except Exception:
        return None
    key = state.get("server_public_key")
    return str(key) if key else None


def fetch_probe_config_or_fail(settings: Settings, slot: vpn_probe_state.ProbeSlot, what: str) -> str:
    try:
        return asyncio.run(_fetch_probe_config(settings, slot))
    except Exception as exc:
        raise FixupError(
            f"не удалось получить {what} у vpn@{slot.server} ({exc}) — "
            "проверьте, что сервер доступен и служба vpn запущена"
        ) from exc


def conf_peer_public_key(config_text: str) -> str | None:
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("PublicKey"):
            _, _, value = stripped.partition("=")
            return value.strip() or None
    return None


def probe_conf_stale(settings: Settings, slot: vpn_probe_state.ProbeSlot, config_text: str) -> bool:
    peer_key = conf_peer_public_key(config_text)
    if peer_key is None:
        return False
    server_key = asyncio.run(_fetch_server_public_key(settings, slot))
    if server_key is None:
        return False
    return peer_key != server_key


def _read_if_exists_or_none(path: Path) -> str | None:
    if not path_exists(path):
        return None
    try:
        return read_text(path)
    except FixupError:
        return None


def _awg_conf_check(slot: vpn_probe_state.ProbeSlot, settings: Settings | None) -> bool:
    current = _read_if_exists_or_none(awg_conf_path(slot))
    if current is None:
        return False
    if any(line.strip().startswith("Table") for line in current.splitlines()):
        return False
    return not (settings is not None and probe_conf_stale(settings, slot, current))


def _reality_conf_check(slot: vpn_probe_state.ProbeSlot) -> bool:
    current = _read_if_exists_or_none(vpn_probe_state.reality_conf_path(slot))
    if current is None:
        return False
    try:
        return int(json.loads(current)["inbounds"][0]["port"]) == slot.socks_port
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
        return False


def _probe_conf_check(slot: vpn_probe_state.ProbeSlot, settings: Settings | None = None) -> bool:
    if slot.transport == "awg":
        return _awg_conf_check(slot, settings)
    return _reality_conf_check(slot)


def _awg_conf_apply(settings: Settings, slot: vpn_probe_state.ProbeSlot) -> None:
    if find_executable("awg-quick") is None:
        build_amneziawg_tools()
        if find_executable("awg-quick") is None:
            raise FixupError("awg-quick не нашёлся после сборки amneziawg-tools")
    conf_path = awg_conf_path(slot)
    existing = read_text_if_exists(conf_path)
    if existing is not None and not probe_conf_stale(settings, slot, existing):
        raw_config_text = existing
    else:
        raw_config_text = fetch_probe_config_or_fail(settings, slot, "конфиг")
    install_root_file(conf_path, prepare_probe_conf(raw_config_text), mode="0600", make_parents=True)


def _reality_conf_apply(settings: Settings, slot: vpn_probe_state.ProbeSlot) -> None:
    ensure_xray_binary()
    conf_path = vpn_probe_state.reality_conf_path(slot)
    if path_exists(conf_path):
        return
    conf_text = fetch_probe_config_or_fail(settings, slot, "reality-конфиг")
    install_root_file(conf_path, conf_text, mode="0600", make_parents=True)


def _probe_conf_apply(settings: Settings, slot: vpn_probe_state.ProbeSlot) -> None:
    if slot.transport == "awg":
        _awg_conf_apply(settings, slot)
    else:
        _reality_conf_apply(settings, slot)


def make_vpn_probe_tunnel_conf_fixup(settings: Settings, slot: vpn_probe_state.ProbeSlot) -> Fixup:
    return Fixup(
        id=f"vpn-probe-conf-{slot.server}-{slot.transport}",
        title=f"Получить и установить конфиг {slot.transport}-пробника к {slot.server}",
        needed=needs.vpn_check,
        check=lambda: _probe_conf_check(slot, settings),
        apply=lambda: _probe_conf_apply(settings, slot),
    )


def vpn_probe_sudoers_content(
    slots: list[vpn_probe_state.ProbeSlot],
    ip_path: str,
    awg_quick_path: str,
    xray_path: str,
    user: str,
) -> str:
    cmds: list[str] = []
    for slot in slots:
        cmds.append(f"{ip_path} netns exec {slot.netns} curl *")
        if slot.transport == "awg":
            cmds.append(f"{ip_path} netns exec {slot.netns} {ip_path} route get *")
            cmds.append(f"{ip_path} netns exec {slot.netns} {awg_quick_path} up {slot.iface}")
            cmds.append(f"{ip_path} netns exec {slot.netns} {awg_quick_path} down {slot.iface}")
        elif slot.transport == "reality":
            conf_path = vpn_probe_state.reality_conf_path(slot)
            cmds.append(
                f"{ip_path} netns exec {slot.netns} timeout * {xray_path} run -c {conf_path}"
            )
    return f"{user} ALL=(root) NOPASSWD: " + ", ".join(cmds) + "\n"


def _sudoers_check(slots: list[vpn_probe_state.ProbeSlot]) -> bool:
    path = SUDOERS_DIR / VPN_PROBE_SUDOERS_FILE
    if not path_exists(path):
        return False
    ip_path = find_executable("ip")
    if ip_path is None:
        return False
    needs_awg = any(s.transport == "awg" for s in slots)
    needs_reality = any(s.transport == "reality" for s in slots)
    awg_quick_path = find_executable("awg-quick") if needs_awg else ""
    if needs_awg and not awg_quick_path:
        return False
    xray_path = find_executable("xray") if needs_reality else ""
    if needs_reality and not xray_path:
        return False
    expected = vpn_probe_sudoers_content(
        slots, ip_path, awg_quick_path or "", xray_path or "", getuser()
    )
    return read_text(path) == expected


def _required_tool(name: str, transport_present: bool) -> str:
    if not transport_present:
        return ""
    path = find_executable(name) or ""
    if not path:
        raise FixupError(f"{name} не найден — сначала должен пройти vpn-probe-conf-* фикс")
    return path


def _sudoers_apply(slots: list[vpn_probe_state.ProbeSlot]) -> None:
    ip_path = find_executable("ip")
    if ip_path is None:
        raise FixupError("ip (iproute2) не найден в PATH")
    awg_quick_path = _required_tool("awg-quick", any(s.transport == "awg" for s in slots))
    xray_path = _required_tool("xray", any(s.transport == "reality" for s in slots))
    content = vpn_probe_sudoers_content(slots, ip_path, awg_quick_path, xray_path, getuser())
    install_sudoers_snippet(VPN_PROBE_SUDOERS_FILE, content)


def make_vpn_probe_sudoers_fixup(slots: list[vpn_probe_state.ProbeSlot]) -> Fixup:
    return Fixup(
        id="vpn-check-probe-sudoers",
        title="Разрешить vpn_check ходить в netns пробников и поднимать/гасить awg без пароля",
        needed=needs.vpn_check,
        check=lambda: _sudoers_check(slots),
        apply=lambda: _sudoers_apply(slots),
    )
