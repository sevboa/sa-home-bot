from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sa_home_bot.config import Settings
from sa_home_bot.node.fixups import needs
from sa_home_bot.node.fixups.core import Fixup, FixupError
from sa_home_bot.node.fixups.host import NODE_UNIT_NAME
from sa_home_bot.node.fixups.privileged import find_executable
from sa_home_bot.node.fixups.vpn_server import (
    XRAY_BLOCK_OUTBOUND_TAG,
    XRAY_WARP_PROBE_INBOUND_TAG,
    write_xray_config_with_test,
    xray_client_inbound_tags,
    xray_config_path,
)

WARP_API_URL = "https://api.cloudflareclient.com/v0a2158/reg"
WARP_USER_AGENT = "okhttp/3.12.1"
WARP_CLIENT_VERSION = "a-6.10-2158"
WARP_ENDPOINT_PORT = 2408
WARP_TAG = "warp"
WARP_OUTBOUND_PREFIX = "warp-"
WARP_MTU = 1280
WARP_PING_DESTINATION = "https://www.google.com/generate_204"
WARP_PING_INTERVAL = "5m"
WARP_PING_TIMEOUT = "10s"
WARP_QUIC_NETWORK = "udp"
WARP_QUIC_PORT = "443"

PostJson = Callable[[str, dict, dict], dict]
KeyGen = Callable[[], tuple[str, str]]


def warp_key_path(n: int) -> Path:
    return xray_config_path().parent / f"warp-{n}.json"


def warp_legacy_path() -> Path:
    return xray_config_path().parent / "warp.json"


def warp_outbound_tag(n: int) -> str:
    return f"{WARP_OUTBOUND_PREFIX}{n}"


def _endpoint_with_port(endpoint: str) -> str:
    if ":" in endpoint and not endpoint.endswith("]"):
        return endpoint
    return f"{endpoint}:{WARP_ENDPOINT_PORT}"


def warp_outbound(key: dict, tag: str) -> dict:
    return {
        "tag": tag,
        "protocol": "wireguard",
        "settings": {
            "secretKey": key["secretKey"],
            "address": list(key["address"]),
            "peers": [
                {
                    "publicKey": key["peerPublicKey"],
                    "endpoint": _endpoint_with_port(key["endpoint"]),
                }
            ],
            "reserved": list(key["reserved"]),
            "mtu": WARP_MTU,
            "domainStrategy": "ForceIPv4",
        },
    }


def _is_warp_outbound(outbound: dict) -> bool:
    tag = outbound.get("tag", "")
    return tag == WARP_TAG or tag.startswith(WARP_OUTBOUND_PREFIX)


def _is_warp_rule(rule: dict) -> bool:
    return rule.get("balancerTag") == WARP_TAG or rule.get("outboundTag") == WARP_TAG


def warp_probe_inbound(port: int) -> dict:
    return {
        "tag": XRAY_WARP_PROBE_INBOUND_TAG,
        "listen": "127.0.0.1",
        "port": port,
        "protocol": "socks",
        "settings": {"auth": "noauth", "udp": False},
    }


def warp_probe_rule() -> dict:
    return {
        "type": "field",
        "inboundTag": [XRAY_WARP_PROBE_INBOUND_TAG],
        "balancerTag": WARP_TAG,
    }


def _is_probe_rule(rule: dict) -> bool:
    return rule.get("inboundTag") == [XRAY_WARP_PROBE_INBOUND_TAG]


def warp_rule(domains: list[str], inbound_tags: list[str]) -> dict:
    return {
        "type": "field",
        "inboundTag": list(inbound_tags),
        "domain": [f"domain:{d}" for d in domains],
        "balancerTag": WARP_TAG,
    }


def warp_quic_rule(inbound_tags: list[str]) -> dict:
    return {
        "type": "field",
        "inboundTag": list(inbound_tags),
        "network": WARP_QUIC_NETWORK,
        "port": WARP_QUIC_PORT,
        "balancerTag": WARP_TAG,
    }


def _is_quic_rule(rule: dict) -> bool:
    return rule.get("network") == WARP_QUIC_NETWORK and rule.get("port") == WARP_QUIC_PORT


def _same_rule(rule: dict, expected: dict) -> bool:
    return {**rule, "inboundTag": sorted(rule.get("inboundTag", []))} == {
        **expected,
        "inboundTag": sorted(expected["inboundTag"]),
    }


def warp_balancer() -> dict:
    return {
        "tag": WARP_TAG,
        "selector": [WARP_OUTBOUND_PREFIX],
        "strategy": {"type": "leastPing"},
    }


def warp_observatory() -> dict:
    return {
        "subjectSelector": [WARP_OUTBOUND_PREFIX],
        "pingConfig": {
            "destination": WARP_PING_DESTINATION,
            "interval": WARP_PING_INTERVAL,
            "timeout": WARP_PING_TIMEOUT,
        },
    }


def _last_block_index(rules: list[dict]) -> int:
    last = -1
    for i, rule in enumerate(rules):
        if rule.get("outboundTag") == XRAY_BLOCK_OUTBOUND_TAG:
            last = i
    return last


def _quic_sniffing_ok(config: dict, tags: list[str]) -> bool:
    for inbound in config.get("inbounds", []):
        sniffing = inbound.get("sniffing")
        if inbound.get("tag") in tags and sniffing and sniffing.get("enabled"):
            if "quic" not in sniffing.get("destOverride", []):
                return False
    return True


def _add_quic_sniffing(config: dict, tags: list[str]) -> None:
    for inbound in config.get("inbounds", []):
        sniffing = inbound.get("sniffing")
        if inbound.get("tag") in tags and sniffing and sniffing.get("enabled"):
            override = sniffing.setdefault("destOverride", [])
            if "quic" not in override:
                override.append("quic")


def with_warp(
    config: dict,
    keys: list[dict],
    domains: list[str],
    probe_port: int,
    inbound_tags: list[str] | None = None,
) -> dict:
    out = copy.deepcopy(config)
    tags = list(inbound_tags) if inbound_tags is not None else xray_client_inbound_tags(out)
    outbounds = [o for o in out.setdefault("outbounds", []) if not _is_warp_outbound(o)]
    outbounds += [warp_outbound(key, warp_outbound_tag(i)) for i, key in enumerate(keys, 1)]
    out["outbounds"] = outbounds
    routing = out.setdefault("routing", {})
    balancers = [b for b in routing.get("balancers", []) if b.get("tag") != WARP_TAG]
    routing["balancers"] = [*balancers, warp_balancer()]
    rules = [r for r in routing.get("rules", []) if not _is_warp_rule(r)]
    at = _last_block_index(rules) + 1
    rules[at:at] = [warp_probe_rule(), warp_rule(domains, tags), warp_quic_rule(tags)]
    routing["rules"] = rules
    inbounds = [
        i for i in out.setdefault("inbounds", []) if i.get("tag") != XRAY_WARP_PROBE_INBOUND_TAG
    ]
    out["inbounds"] = [*inbounds, warp_probe_inbound(probe_port)]
    out["burstObservatory"] = warp_observatory()
    _add_quic_sniffing(out, tags)
    return out


def warp_applied(config: dict, n: int, domains: list[str], probe_port: int) -> bool:
    warp_tags = sorted(o["tag"] for o in config.get("outbounds", []) if _is_warp_outbound(o))
    if warp_tags != sorted(warp_outbound_tag(i) for i in range(1, n + 1)):
        return False
    routing = config.get("routing") or {}
    if warp_balancer() not in routing.get("balancers", []):
        return False
    if config.get("burstObservatory") != warp_observatory():
        return False
    rules = routing.get("rules", [])
    probe_indexes = [i for i, r in enumerate(rules) if _is_warp_rule(r) and _is_probe_rule(r)]
    quic_indexes = [i for i, r in enumerate(rules) if _is_warp_rule(r) and _is_quic_rule(r)]
    indexes = [
        i
        for i, r in enumerate(rules)
        if _is_warp_rule(r) and not _is_probe_rule(r) and not _is_quic_rule(r)
    ]
    if len(indexes) != 1 or len(probe_indexes) != 1 or len(quic_indexes) != 1:
        return False
    if quic_indexes[0] <= _last_block_index(rules):
        return False
    index = indexes[0]
    if probe_indexes[0] > index or probe_indexes[0] <= _last_block_index(rules):
        return False
    if rules[probe_indexes[0]] != warp_probe_rule():
        return False
    probe_inbounds = [
        i for i in config.get("inbounds", []) if i.get("tag") == XRAY_WARP_PROBE_INBOUND_TAG
    ]
    if probe_inbounds != [warp_probe_inbound(probe_port)]:
        return False
    if index <= _last_block_index(rules):
        return False
    tags = xray_client_inbound_tags(config)
    return (
        _same_rule(rules[index], warp_rule(domains, tags))
        and _same_rule(rules[quic_indexes[0]], warp_quic_rule(tags))
        and _quic_sniffing_ok(config, tags)
    )


def parse_warp_registration(resp: dict, priv: str) -> dict:
    try:
        config = resp["config"]
        addresses = config["interface"]["addresses"]
        peer = config["peers"][0]
        endpoint_host = peer["endpoint"]["v4"].rsplit(":", 1)[0]
        reserved = list(base64.b64decode(config["client_id"]))[:3]
        key = {
            "secretKey": priv,
            "address": [f"{addresses['v4']}/32", f"{addresses['v6']}/128"],
            "peerPublicKey": peer["public_key"],
            "endpoint": f"{endpoint_host}:{WARP_ENDPOINT_PORT}",
            "reserved": reserved,
        }
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise FixupError("ответ регистрации WARP неполный") from exc
    if len(reserved) != 3:
        raise FixupError("ответ регистрации WARP: client_id короче 3 байт")
    return key


def _awg_run(args: list[str], stdin: str | None = None) -> str:
    awg = find_executable("awg")
    if awg is None:
        raise FixupError("нет утилиты awg для генерации ключа WARP")
    result = subprocess.run([awg, *args], input=stdin, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise FixupError(f"awg {args[0]}: код {result.returncode}")
    return result.stdout.strip()


def generate_keypair() -> tuple[str, str]:
    priv = _awg_run(["genkey"])
    return priv, _awg_run(["pubkey"], priv)


def post_json(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _registration_tos() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def register_warp_key(post: PostJson | None = None, keygen: KeyGen | None = None) -> dict:
    priv, pub = (keygen or generate_keypair)()
    payload = {
        "key": pub,
        "install_id": "",
        "fcm_token": "",
        "tos": _registration_tos(),
        "type": "Android",
        "locale": "en_US",
    }
    headers = {"User-Agent": WARP_USER_AGENT, "CF-Client-Version": WARP_CLIENT_VERSION}
    try:
        resp = (post or post_json)(WARP_API_URL, payload, headers)
    except Exception as exc:
        raise FixupError(f"регистрация ключа WARP не удалась ({type(exc).__name__})") from exc
    return parse_warp_registration(resp, priv)


def _write_key_file(path: Path, key: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(key, fh, indent=2)
    os.chmod(path, 0o600)


def adopt_legacy_key() -> None:
    legacy = warp_legacy_path()
    first = warp_key_path(1)
    if legacy.exists() and not first.exists():
        legacy.rename(first)


def ensure_warp_keys(n: int, register: Callable[[], dict] | None = None) -> list[dict]:
    adopt_legacy_key()
    register = register or register_warp_key
    keys: list[dict] = []
    for i in range(1, n + 1):
        path = warp_key_path(i)
        if not path.exists():
            _write_key_file(path, register())
        keys.append(json.loads(path.read_text(encoding="utf-8")))
    return keys


def make_vpn_warp_fixup(settings: Settings) -> Fixup:
    def check() -> bool:
        warp = settings.vpn.warp
        if warp_legacy_path().exists() and not warp_key_path(1).exists():
            return False
        if any(not warp_key_path(i).exists() for i in range(1, warp.keys + 1)):
            return False
        try:
            config = json.loads(xray_config_path().read_text(encoding="utf-8"))
        except Exception:
            return False
        return warp_applied(config, warp.keys, warp.domains, warp.probe_port)

    def apply() -> None:
        warp = settings.vpn.warp
        keys = ensure_warp_keys(warp.keys)
        config_path = xray_config_path()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        write_xray_config_with_test(
            config_path, with_warp(config, keys, warp.domains, warp.probe_port)
        )
        restart = subprocess.run(["systemctl", "--user", "restart", "xray"], check=False)
        if restart.returncode != 0:
            raise FixupError("конфиг записан, но systemctl --user restart xray не сработал")
        subprocess.run(["systemctl", "--user", "restart", NODE_UNIT_NAME], check=False)

    return Fixup(
        id="vpn-warp",
        title=(
            "xray: выбранные домены клиентов VPN через Cloudflare WARP "
            "(рестарт xray и ноды — клиенты вернутся сверкой)"
        ),
        needed=lambda s: needs.vpn(s) and s.vpn.reality is not None and s.vpn.warp is not None,
        check=check,
        apply=apply,
    )
