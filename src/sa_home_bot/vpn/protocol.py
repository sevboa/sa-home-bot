from __future__ import annotations

import re as _re
import time as _time

SERVICE_NAME = "vpn"

GB = 1_000_000_000
PROBE_CHAT_ID = 0
NODE_SENTINEL_CHAT_ID = PROBE_CHAT_ID

FLOWER_NAMES = (
    "Rose",
    "Lily",
    "Iris",
    "Aster",
    "Poppy",
    "Daisy",
    "Tulip",
    "Lotus",
    "Phlox",
    "Pansy",
    "Sedum",
    "Canna",
    "Hosta",
    "Orchid",
    "Violet",
    "Dahlia",
    "Azalea",
    "Camellia",
    "Jasmine",
    "Lilac",
    "Peony",
    "Zinnia",
    "Yarrow",
    "Crocus",
    "Freesia",
    "Begonia",
    "Petunia",
    "Gerbera",
    "Mallow",
    "Cosmos",
)

TRANSPORT_AWG = "awg"
TRANSPORT_REALITY = "reality"
TRANSPORTS = (TRANSPORT_AWG, TRANSPORT_REALITY)

TRANSPORT_FILE_CODE = {TRANSPORT_AWG: "awg", TRANSPORT_REALITY: "vls"}

_FLAG_FIRST = 0x1F1E6
_FLAG_LAST = 0x1F1FF


def country_code(location: str) -> str:
    letters = [
        chr(ord(c) - _FLAG_FIRST + ord("A"))
        for c in location
        if _FLAG_FIRST <= ord(c) <= _FLAG_LAST
    ]
    return "".join(letters[:2])


def country_flag(location: str) -> str:
    flag = [c for c in location if _FLAG_FIRST <= ord(c) <= _FLAG_LAST]
    return "".join(flag[:2])


_UNSAFE_FILENAME = _re.compile(r"[^a-z0-9]+")
_MAX_TUNNEL_NAME = 15


def _name_prefix(transport: str, location: str) -> str:
    code = TRANSPORT_FILE_CODE.get(transport, transport[:3])
    country = country_code(location).lower()
    return f"{code}_{country}_" if country else f"{code}_"


def secret_filename(transport: str, device_label: str, location: str = "") -> str:
    prefix = _name_prefix(transport, location)
    if transport == TRANSPORT_REALITY:
        slug = _UNSAFE_FILENAME.sub("", device_label.strip().lower()) or "vpn"
        return f"{prefix}{slug}.json"
    slug = _UNSAFE_FILENAME.sub("", device_label.strip().lower()) or "device"
    stamp = str(int(_time.time()))[-3:]
    budget = _MAX_TUNNEL_NAME - len(prefix) - len(stamp) - 1
    return f"{prefix}{slug[:budget]}_{stamp}.conf"

ACTION_PEERS = "peers"
ACTION_ISSUE = "issue"
ACTION_REISSUE = "reissue"
ACTION_REVOKE = "revoke"
ACTION_GET_VLESS = "get_vless"
ACTION_GET_SUBSCRIPTION = "get_subscription"
ACTION_SUB_LINKS = "sub_links"
ACTION_SUB_GEN = "sub_gen"
ACTION_USAGE = "usage"
ACTION_SET_QUOTA = "set_quota"
ACTION_SET_ACCESS = "set_access"
ACTION_GRANT_EXTRA = "grant_extra"
ACTION_REQUEST_EXTRA = "request_extra"
ACTION_RESOLVE_REQUEST = "resolve_request"
ACTION_APK_INFO = "apk_info"
ACTION_APK_CHUNK = "apk_chunk"
ACTION_APK_SET_FILE_ID = "apk_set_file_id"

ACTION_REPORT_CHECK = "report_check"
ACTION_CHECK_NOW = "check_now"
ACTION_CHECK_STATUS = "check_status"

ACTION_PROXY_LINK = "proxy_link"
ACTION_PROXY_ROTATE_SECRET = "proxy_rotate_secret"
ACTION_PROXY_USAGE = "proxy_usage"

ACTION_TELEGRAM_EGRESS = "telegram_egress"
TELEGRAM_EGRESS_TRANSPORT = "reality"
TELEGRAM_EGRESS_TARGET = "https://api.telegram.org"

PROXY_SECRET_SEED = "7p98I6xlxrC7ea7ysIxHCnR3d3cubWljcm9zb2Z0LmNvbQ"

EVENT_VPN_PEER_ISSUED = "vpn_peer_issued"
EVENT_VPN_QUOTA_WARNING = "vpn_quota_warning"
EVENT_VPN_QUOTA_EXCEEDED = "vpn_quota_exceeded"
EVENT_VPN_PEER_BLOCKED = "vpn_peer_blocked"
EVENT_VPN_ACCESS_RESTORED = "vpn_access_restored"
EVENT_VPN_EXTRA_REQUESTED = "vpn_extra_requested"
EVENT_VPN_EXTRA_RESOLVED = "vpn_extra_resolved"
EVENT_VPN_NODE_QUOTA_WARNING = "vpn_node_quota_warning"
EVENT_VPN_CHECK_FAILED = "vpn_check_failed"
EVENT_VPN_CHECK_RECOVERED = "vpn_check_recovered"
EVENT_VPN_SERVER_RESTORED = "vpn_server_restored"

ERR_QUOTA_CEILING = "vpn_quota_ceiling"
