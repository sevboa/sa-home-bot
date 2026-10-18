from __future__ import annotations

import json

from sa_home_bot.reality.client_config import (
    CLIENT_FINGERPRINT,
    RealityParams,
    parse_vless_url,
)

__all__ = ["parse_vless_url", "render_probe_client_config"]


def render_probe_client_config(params: RealityParams, client_uuid: str, *, socks_port: int) -> str:
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
