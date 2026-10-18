from __future__ import annotations

import base64
import json
import re
import zlib

from sa_home_bot.vpn import awg_conf

PREFIX = "vpn://"
CONTAINER = "amnezia-awg"
PROTOCOL = "awg"
_DEFAULT_PORT = 51820
_AWG_MTU = "1280"
_IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


class ConfigError(ValueError):
    pass


def _split_endpoint(value: str) -> tuple[str, int]:
    host, sep, port = value.strip().rpartition(":")
    if not sep or not host or not port.isdigit():
        return value.strip(), _DEFAULT_PORT
    return host.strip("[]"), int(port)


def build_config(conf_text: str, name: str) -> dict:
    conf = awg_conf.parse(conf_text)
    host, port = _split_endpoint(conf.get("Endpoint", ""))
    if not host or not all(conf.get(k) for k in ("PrivateKey", "Address", "PublicKey")):
        raise ConfigError("в конфиге AmneziaWG нет Endpoint/PrivateKey/Address/PublicKey")
    last: dict = {
        "config": conf_text,
        "hostName": host,
        "port": port,
        "client_priv_key": conf["PrivateKey"],
        "client_ip": conf["Address"],
        "server_pub_key": conf["PublicKey"],
    }
    psk = conf.get("PresharedKey") or conf.get("PreSharedKey")
    if psk:
        last["psk_key"] = psk
    if conf.get("PersistentKeepalive"):
        last["persistent_keep_alive"] = conf["PersistentKeepalive"]
    last["allowed_ips"] = [p for p in re.split(r"\s*,\s*", conf.get("AllowedIPs", "")) if p]
    for key in awg_conf.AWG_KEYS:
        if conf.get(key):
            last[key] = conf[key]
    last["mtu"] = conf.get("MTU") or _AWG_MTU
    config: dict = {
        "containers": [
            {
                "container": CONTAINER,
                PROTOCOL: {
                    "last_config": json.dumps(last, ensure_ascii=False, indent=4),
                    "isThirdPartyConfig": True,
                    "port": str(port),
                    "transport_proto": "udp",
                },
            }
        ],
        "defaultContainer": CONTAINER,
        "description": name,
    }
    dns = _IPV4.findall(conf.get("DNS", ""))
    if dns:
        config["dns1"] = dns[0]
        config["dns2"] = dns[1] if len(dns) > 1 else dns[0]
    config["hostName"] = host
    return config


def q_compress(data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + zlib.compress(data, 6)


def q_uncompress(data: bytes) -> bytes:
    size = int.from_bytes(data[:4], "big")
    out = zlib.decompress(data[4:])
    if len(out) != size:
        raise ValueError("длина в заголовке qCompress не совпала")
    return out


def build_key(conf_text: str, name: str) -> str:
    raw = json.dumps(build_config(conf_text, name), ensure_ascii=False).encode()
    return PREFIX + base64.urlsafe_b64encode(q_compress(raw)).decode().rstrip("=")


def decode_key(key: str) -> dict:
    body = key.strip().removeprefix(PREFIX)
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    try:
        raw = q_uncompress(raw)
    except (zlib.error, ValueError):
        pass
    return json.loads(raw)
