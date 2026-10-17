"""xray-core клиентский конфиг с SOCKS5-инбаундом — для VPN-пробника
(39.0.7(e)), НЕ для гостя.

Гостю ``vpn/service.py::_issue`` отдаёт sing-box-конфиг
(``reality/client_config.py::render_singbox_config``) — TUN-based, весь
трафик приложения заворачивается через виртуальный интерфейс, плюс сплит-
туннель по правилам ``reality/routing.py``. Пробнику это не подходит:
внутри netns нет TUN, нужен только один локальный SOCKS5-порт, через
который curl проверяет доступность (``curl --socks5 127.0.0.1:<port>``, не
полагаясь на переписанный default route — см. IMPLEMENTATION_PLAN.md,
39.0.7). xray-core умеет это нативной схемой inbounds/outbounds без
sing-box поверх.

Параметры сервера (``RealityParams``) и UUID клиента пробник получает не из
своего конфига (это ДРУГАЯ нода — сервер), а из ``vless://``-ссылки,
которую ``vpn/service.py::_issue`` уже отдаёт любому клиенту, включая
пробника (``chat_id=0``, см. node/fixups.py::VPN_PROBE_CHAT_ID) —
``parse_vless_url`` ниже обратна уже существующей
``client_config.py::render_vless_url``.
"""

from __future__ import annotations

import json

from sa_home_bot.reality.client_config import (
    CLIENT_FINGERPRINT,
    RealityParams,
    parse_vless_url,
)

__all__ = ["parse_vless_url", "render_probe_client_config"]


def render_probe_client_config(params: RealityParams, client_uuid: str, *, socks_port: int) -> str:
    """xray-core конфиг (не sing-box): один SOCKS5-инбаунд на localhost,
    один VLESS+Reality outbound. ``udp=true`` — DNS-запросы через curl
    (``getaddrinfo``) идут тем же SOCKS-туннелем, не мимо."""
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": socks_port,
                "protocol": "socks",
                "settings": {"udp": True},
            }
        ],
        "outbounds": [
            {
                "tag": "reality-out",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": params.endpoint_host,
                            "port": int(params.port),
                            "users": [
                                {
                                    "id": client_uuid,
                                    "encryption": "none",
                                    "flow": params.flow,
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "serverName": params.sni,
                        "publicKey": params.server_public_key,
                        "shortId": params.short_id,
                        "fingerprint": CLIENT_FINGERPRINT,
                    },
                },
            }
        ],
    }
    return json.dumps(config, indent=2) + "\n"
