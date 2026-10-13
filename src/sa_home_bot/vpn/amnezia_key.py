"""Ключ AmneziaVPN ``vpn://…`` из клиентского конфига AmneziaWG (подэтап 57.12).

Формат взят из исходников amnezia-client (``importController.cpp``,
``extractConfigFromData`` / ``extractWireGuardConfig``): ``vpn://`` +
base64url без ``=`` от ``qCompress(JSON)``, где qCompress = 4 байта длины
несжатых данных (big-endian) + zlib-поток. Клиент разбирает такой ключ как
«Amnezia»-конфиг (``containers`` / ``hostName``); структуру берём ровно ту,
что клиент сам строит при импорте ``.conf`` с AWG-параметрами: контейнер
``amnezia-awg``, в нём ``awg.last_config`` — строка с JSON (в ней же исходный
текст ``.conf``) и ``isThirdPartyConfig: true``. Ключ содержит приватный ключ
клиента — обращаться как с самим ``.conf``.
"""

from __future__ import annotations

import base64
import json
import re
import zlib

PREFIX = "vpn://"
CONTAINER = "amnezia-awg"
PROTOCOL = "awg"
# Параметры AWG, которые клиент переносит из [Interface] как есть (configKey::awgProtocolKeys).
AWG_KEYS = (
    "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
    "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5",
)  # fmt: skip
_DEFAULT_PORT = 51820
_AWG_MTU = "1280"  # protocols::awg::defaultMtu на Android/iOS; клиент всё равно подставляет своё
_IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


class ConfigError(ValueError):
    """Конфиг без обязательных полей (Endpoint, PrivateKey, Address, PublicKey)."""


def _parse_conf(text: str) -> dict[str, str]:
    """Как клиент: строки ``ключ = значение``, заголовки секций пропускаются."""
    out: dict[str, str] = {}
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            continue
        idx = line.find("=")
        if idx > 0:
            out[line[:idx].strip()] = line[idx + 1 :].strip()
    return out


def _split_endpoint(value: str) -> tuple[str, int]:
    host, sep, port = value.strip().rpartition(":")
    if not sep or not host or not port.isdigit():
        return value.strip(), _DEFAULT_PORT
    return host.strip("[]"), int(port)


def build_config(conf_text: str, name: str) -> dict:
    """JSON-конфиг сервера AmneziaVPN для импорта (до сжатия)."""
    conf = _parse_conf(conf_text)
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
    for key in AWG_KEYS:
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
    """``qCompress``: длина несжатого (4 байта, big-endian) + zlib."""
    return len(data).to_bytes(4, "big") + zlib.compress(data, 6)


def q_uncompress(data: bytes) -> bytes:
    size = int.from_bytes(data[:4], "big")
    out = zlib.decompress(data[4:])
    if len(out) != size:
        raise ValueError("длина в заголовке qCompress не совпала")
    return out


def build_key(conf_text: str, name: str) -> str:
    """``vpn://…`` для вставки в AmneziaVPN."""
    raw = json.dumps(build_config(conf_text, name), ensure_ascii=False).encode()
    return PREFIX + base64.urlsafe_b64encode(q_compress(raw)).decode().rstrip("=")


def decode_key(key: str) -> dict:
    """Обратное преобразование (как в клиенте; для проверок и тестов)."""
    body = key.strip().removeprefix(PREFIX)
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    try:
        raw = q_uncompress(raw)
    except (zlib.error, ValueError):
        pass  # клиент при пустом qUncompress берёт данные как есть
    return json.loads(raw)
