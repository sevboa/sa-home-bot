from __future__ import annotations

import ipaddress
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sa_home_bot.config import VpnConfig
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ProtoError,
)
from sa_home_bot.vpn.protocol import (
    AWG_PROBE_TRANSPORTS,
    FLOWER_NAMES,
)

log = logging.getLogger("sa_home_bot.vpn.service")

MAX_DEVICE_LABEL_LEN = 64

NODE_SERVICE = "node"
ACTION_TRIGGER_PEERS = "trigger_peers"

CHECK_STALE_FACTOR = 3
CHECK_SPREAD_RATIO = 0.5


def stale_after_s(cfg: VpnConfig, transport: str | None = None) -> float:
    if transport in AWG_PROBE_TRANSPORTS:
        return cfg.awg_check_max_s * CHECK_STALE_FACTOR
    return (cfg.check_interval_s + cfg.check_jitter_s) * CHECK_STALE_FACTOR


def _random_device_label(used: set[str]) -> str:
    pool = [name for name in FLOWER_NAMES if name not in used]
    if pool:
        return random.choice(pool)
    suffix = 2
    while True:
        pool = [
            f"{name}{suffix}"
            for name in FLOWER_NAMES
            if f"{name}{suffix}" not in used and len(f"{name}{suffix}") <= MAX_DEVICE_LABEL_LEN
        ]
        if pool:
            return random.choice(pool)
        suffix += 1


APK_INFO_MEMO_S = 60.0
APK_API_TIMEOUT_S = 15.0
APK_DOWNLOAD_TIMEOUT_S = 90.0
APK_CHUNK_BYTES = 700 * 1024

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


def month_key(when: datetime) -> str:
    return when.strftime("%Y-%m")


def _allocate_address(subnet: str, used: set[str]) -> str:
    network = ipaddress.ip_network(subnet, strict=False)
    hosts = list(network.hosts())
    for host in hosts[1:]:
        addr = str(host)
        if addr not in used:
            return addr
    raise ProtoError(ERR_BAD_REQUEST, "подсеть VPN исчерпана — нет свободных адресов")


def _reality_email(chat_id: int, device_label: str) -> str:
    return f"c{chat_id}-{device_label}"
