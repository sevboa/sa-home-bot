from __future__ import annotations

from sa_home_bot.bot import commands
from sa_home_bot.vpn import protocol as vpn_protocol

SERVICE = vpn_protocol.SERVICE_NAME

ACTION_VPN_CARD = "vpn_card"
ACTION_APK = "apk"
ACTION_USAGE_ALL = "usage_all"

EXPERT_PREFIX = "~"
TRANSPORT_PICK_PREFIX = "t_"
SECRET_TOKEN_PREFIX = "f_"

SCREEN_LIST = "m"
SCREEN_DEVICE = "d"
SCREEN_REISSUE = "r"
SCREEN_DELETE = "x"
SCREEN_PICK = "g"
SCREEN_AWG = "a"
SCREEN_NEW = "n"
SCREEN_LINK = "l"
SCREEN_RESTORE = "R"

KIND_VLESS = "v"
KIND_FILE = "s"
KIND_AWG_COUNTRY = "g"
KIND_NEW = "n"
KIND_MULTI = "m"
KIND_REISSUE_REALITY = "r"
KIND_REISSUE_AWG = "a"
KIND_FIX = "f"
KIND_DELETE = "d"

PROBE_CHAT_ID = 0


def is_probe(chat_id: int) -> bool:
    return chat_id == PROBE_CHAT_ID


def vpn_callback(action: str, value: str | None = None, node: str | None = None) -> str:
    return commands.action_callback(action, value, node_id=node, service=SERVICE)


def card_cb(screen: str = "", key: str = "") -> str:
    return vpn_callback(ACTION_VPN_CARD, f"{screen}{key}")


def home_cb() -> str:
    return card_cb()


def expert_cb(action: str, kind: str, key: str = "", node: str | None = None) -> str:
    return vpn_callback(action, f"{EXPERT_PREFIX}{kind}{key}", node)


def secret_cb(action: str, token: str) -> str:
    return vpn_callback(action, f"{SECRET_TOKEN_PREFIX}{token}")
