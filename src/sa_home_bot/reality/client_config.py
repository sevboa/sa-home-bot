from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from sa_home_bot.reality.routing import (
    DIRECT_SUFFIXES,
    RULESET_UPDATE_INTERVAL,
    RULESET_URLS,
)

_TAG_DIRECT_MANUAL = "ru-direct-manual"
_TAG_BLOCKED = "ru-blocked"
_TAG_INSIDE = "ru-inside"

DEFAULT_FLOW = "xtls-rprx-vision"
CLIENT_FINGERPRINT = "chrome"


@dataclass(frozen=True)
class RealityParams:
    endpoint_host: str
    port: int
    server_public_key: str
    short_id: str
    sni: str
    flow: str = DEFAULT_FLOW

    @classmethod
    def from_config(cls, cfg: Any) -> RealityParams:
        return cls(
            endpoint_host=cfg.endpoint_host,
            port=cfg.port,
            server_public_key=cfg.server_public_key,
            short_id=cfg.short_id,
            sni=cfg.sni,
            flow=cfg.flow,
        )


def _remote_ruleset(tag: str) -> dict:
    return {
        "type": "remote",
        "tag": tag,
        "format": "binary",
        "url": RULESET_URLS[tag],
        "download_detour": "direct",
        "update_interval": RULESET_UPDATE_INTERVAL,
    }


def _inline_direct_ruleset() -> dict:
    return {
        "type": "inline",
        "tag": _TAG_DIRECT_MANUAL,
        "rules": [{"domain_suffix": list(DIRECT_SUFFIXES)}],
    }


def _singbox_config(params: RealityParams, client_uuid: str, *, all_proxy: bool) -> dict:
    proxy_out = {
        "type": "vless",
        "tag": "proxy",
        "server": params.endpoint_host,
        "server_port": int(params.port),
        "uuid": client_uuid,
        "flow": params.flow,
        "tls": {
            "enabled": True,
            "server_name": params.sni,
            "utls": {"enabled": True, "fingerprint": CLIENT_FINGERPRINT},
            "reality": {
                "enabled": True,
                "public_key": params.server_public_key,
                "short_id": params.short_id,
            },
        },
    }

    rule_sets: list[dict] = [_inline_direct_ruleset()]
    if not all_proxy:
        rule_sets.append(_remote_ruleset(_TAG_BLOCKED))
        rule_sets.append(_remote_ruleset(_TAG_INSIDE))

    dns_rules: list[dict] = [{"rule_set": [_TAG_DIRECT_MANUAL], "server": "direct-dns"}]
    if not all_proxy:
        dns_rules.insert(0, {"rule_set": [_TAG_BLOCKED], "server": "proxy-dns"})
        dns_rules.append({"rule_set": [_TAG_INSIDE], "server": "direct-dns"})
    dns_final = "proxy-dns" if all_proxy else "direct-dns"

    route_rules: list[dict] = [
        {"action": "sniff"},
        {"protocol": "dns", "action": "hijack-dns"},
        {"ip_is_private": True, "outbound": "direct"},
        {"rule_set": [_TAG_DIRECT_MANUAL], "outbound": "direct"},
    ]
    if not all_proxy:
        route_rules.append({"rule_set": [_TAG_BLOCKED], "outbound": "proxy"})
        route_rules.append({"rule_set": [_TAG_INSIDE], "outbound": "direct"})

    config = {
        "log": {"level": "warn", "timestamp": True},
        "dns": {
            "servers": [
                {
                    "tag": "proxy-dns",
                    "address": "https://1.1.1.1/dns-query",
                    "detour": "proxy",
                },
                {
                    "tag": "direct-dns",
                    "address": "https://common.dot.dns.yandex.net/dns-query",
                    "address_resolver": "bootstrap-dns",
                    "detour": "direct",
                },
                {"tag": "bootstrap-dns", "address": "77.88.8.8", "detour": "direct"},
            ],
            "rules": dns_rules,
            "final": dns_final,
            "strategy": "prefer_ipv4",
        },
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "address": ["172.19.0.1/28"],
                "auto_route": True,
                "strict_route": True,
                "stack": "mixed",
                "mtu": 1400,
            }
        ],
        "outbounds": [
            proxy_out,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "rules": route_rules,
            "rule_set": rule_sets,
            "final": "proxy",
            "auto_detect_interface": True,
        },
    }
    return config


def _dump_singbox(config: dict) -> str:
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def render_singbox_config(
    params: RealityParams,
    client_uuid: str,
    *,
    all_proxy: bool = False,
) -> str:
    return _dump_singbox(_singbox_config(params, client_uuid, all_proxy=all_proxy))


def render_vless_url(params: RealityParams, client_uuid: str, label: str) -> str:
    query = "&".join(
        f"{k}={v}"
        for k, v in (
            ("encryption", "none"),
            ("flow", params.flow),
            ("security", "reality"),
            ("sni", params.sni),
            ("fp", CLIENT_FINGERPRINT),
            ("pbk", params.server_public_key),
            ("sid", params.short_id),
            ("type", "tcp"),
        )
    )
    return (
        f"vless://{client_uuid}@{params.endpoint_host}:{int(params.port)}"
        f"?{query}#{quote(label, safe='')}"
    )


def parse_vless_url(url: str) -> tuple[RealityParams, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "vless":
        raise ValueError(f"не vless:// ссылка: {url!r}")
    client_uuid = parsed.username
    if not client_uuid:
        raise ValueError("vless:// без uuid перед @")
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        raise ValueError("vless:// без host:port")
    query = parse_qs(parsed.query)

    def _param(name: str, *, default: str | None = None) -> str:
        values = query.get(name)
        if values:
            return values[0]
        if default is not None:
            return default
        raise ValueError(f"vless:// без обязательного параметра {name}")

    params = RealityParams(
        endpoint_host=host,
        port=port,
        server_public_key=_param("pbk"),
        short_id=_param("sid"),
        sni=_param("sni"),
        flow=_param("flow", default=DEFAULT_FLOW),
    )
    return params, client_uuid


def render_deep_link(vless_url: str) -> str:
    return f"hiddify://import/{vless_url}"


def render_singbox_multi(
    servers: list[tuple[str, RealityParams, str]],
    *,
    all_proxy: bool = False,
) -> str:
    if not servers:
        raise ValueError("нужна хотя бы одна страна")
    _first_name, first_params, first_uuid = servers[0]
    config = _singbox_config(first_params, first_uuid, all_proxy=all_proxy)
    outbounds: list[dict] = []
    for name, params, client_uuid in servers:
        single = _singbox_config(params, client_uuid, all_proxy=all_proxy)
        outbounds.append({**single["outbounds"][0], "tag": name})
    names = [o["tag"] for o in outbounds]
    outbounds.append(
        {
            "type": "urltest",
            "tag": "auto",
            "outbounds": names,
            "url": "https://www.gstatic.com/generate_204",
            "interval": "3m",
            "tolerance": 100,
        }
    )
    outbounds.append(
        {"type": "selector", "tag": "proxy", "outbounds": ["auto", *names], "default": "auto"}
    )
    outbounds.append({"type": "direct", "tag": "direct"})
    config["outbounds"] = outbounds
    return _dump_singbox(config)
