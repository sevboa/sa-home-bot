"""Генерация клиентских артефактов VLESS+Reality: полный sing-box конфиг
(основной артефакт для Hiddify), ``vless://``-ссылка и Hiddify deep-link.

Ноль зависимостей — только stdlib. QR тут НЕ рисуем — рендер QR живёт в
вызывающем коде (``deploy/reality-client.py`` для ручной раздачи,
``vpn/service.py`` для reality-транспорта в рое).

Формат конфига — sing-box (Hiddify его понимает). Правила маршрутизации зашиты
в файл (см. ``reality/routing.py``), клиент сам обновляет remote rule-set с
GitHub. Целимся в схему sing-box >= 1.11 (``format: "binary"`` для ``.srs``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import quote

from sa_home_bot.reality.routing import (
    DIRECT_SUFFIXES,
    RULESET_UPDATE_INTERVAL,
    RULESET_URLS,
)

# Порядок тегов в route.rule_set / route.rules осмысленный — см. комментарии
# ниже; тесты фиксируют относительный порядок.
_TAG_DIRECT_MANUAL = "ru-direct-manual"
_TAG_BLOCKED = "ru-blocked"
_TAG_INSIDE = "ru-inside"


@dataclass(frozen=True)
class RealityParams:
    """Параметры сервера Reality. ``config.RealityTransportConfig`` несёт те же
    имена полей → ``render_*`` принимают его по duck-typing без изменений."""

    endpoint_host: str
    port: int
    server_public_key: str
    short_id: str
    sni: str
    flow: str = "xtls-rprx-vision"


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


def render_singbox_config(
    params: RealityParams,
    client_uuid: str,
    *,
    all_proxy: bool = False,
) -> str:
    """Полный sing-box конфиг под одно устройство. Имя профиля клиенту задаёт
    имя файла (``<label>.json``), не содержимое.

    ``all_proxy=False`` (обычный сплит для гостя в РФ): заблокированное → туннель,
    РФ-геолокация и always-direct → напрямую, неопознанное → туннель
    (``final: "proxy"``, чтобы обход не ломался на свежих блокировках).

    ``all_proxy=True`` (одна точка выхода — для гостей вне РФ): всё в туннель,
    напрямую только банки/госуслуги из ``ru-direct-manual``.
    """
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
            "utls": {"enabled": True, "fingerprint": "chrome"},
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

    # DNS: заблокированное резолвим через туннель (обход DNS-спуфинга ТСПУ),
    # остальное — напрямую.
    dns_rules: list[dict] = [{"rule_set": [_TAG_DIRECT_MANUAL], "server": "direct-dns"}]
    if not all_proxy:
        dns_rules.insert(0, {"rule_set": [_TAG_BLOCKED], "server": "proxy-dns"})
        dns_rules.append({"rule_set": [_TAG_INSIDE], "server": "direct-dns"})
    dns_final = "proxy-dns" if all_proxy else "direct-dns"

    # route.rules: always-direct раньше блок-листа, блок-лист раньше РФ-геолок.
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
                    "detour": "direct",
                },
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
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def render_vless_url(params: RealityParams, client_uuid: str, label: str) -> str:
    """``vless://``-ссылка для быстрого импорта / QR. Правил маршрутизации не
    несёт (Hiddify применит свой встроенный пресет «Регион: Россия») — основной
    артефакт всё равно ``render_singbox_config``."""
    query = "&".join(
        f"{k}={v}"
        for k, v in (
            ("encryption", "none"),
            ("flow", params.flow),
            ("security", "reality"),
            ("sni", params.sni),
            ("fp", "chrome"),
            ("pbk", params.server_public_key),
            ("sid", params.short_id),
            ("type", "tcp"),
        )
    )
    return (
        f"vless://{client_uuid}@{params.endpoint_host}:{int(params.port)}"
        f"?{query}#{quote(label, safe='')}"
    )


def render_deep_link(vless_url: str) -> str:
    """Hiddify deep-link: одно нажатие в Telegram открывает Hiddify и
    импортирует профиль. Имя профиля Hiddify берёт из фрагмента ``#label``
    самой ``vless://``-ссылки."""
    return f"hiddify://import/{vless_url}"
