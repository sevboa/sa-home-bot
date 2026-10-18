from __future__ import annotations

from typing import Any

AWG_KEYS = (
    "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
    "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5",
)  # fmt: skip
ISSUED_OBFUSCATION_KEYS = ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4")


def render(cfg: Any, private_key: str, address: str, server_public_key: str) -> str:
    obfuscation = "".join(
        f"{key} = {getattr(cfg, key.lower())}\n" for key in ISSUED_OBFUSCATION_KEYS
    )
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {address}/32\n"
        f"DNS = {cfg.dns}\n"
        f"MTU = {cfg.mtu}\n"
        f"{obfuscation}"
        "\n"
        "[Peer]\n"
        f"PublicKey = {server_public_key}\n"
        f"Endpoint = {cfg.endpoint_host}:{cfg.endpoint_port}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "PersistentKeepalive = 25\n"
    )


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            continue
        idx = line.find("=")
        if idx > 0:
            out[line[:idx].strip()] = line[idx + 1 :].strip()
    return out
