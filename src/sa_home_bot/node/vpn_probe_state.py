from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

STATE_PATH = Path("/etc/sa-home-bot/vpn-probe-state.json")
REALITY_CONF_DIR = Path("/etc/sa-home-bot/vpn-probe")


class ProbeSlot(BaseModel):
    server: str
    transport: str
    netns: str
    veth_host: str
    veth_ns: str
    veth_host_addr: str
    veth_ns_addr: str
    subnet: str
    iface: str | None = None
    socks_port: int | None = None
    sni: str | None = None


class ProbeState(BaseModel):
    slots: list[ProbeSlot] = Field(default_factory=list)
    indices: dict[str, int] = Field(default_factory=dict)


VETH_HOST_INDEX = re.compile(r"^vprobe(\d+)h0$")


def slot_key(server: str, transport: str) -> str:
    return f"{server}/{transport}"


def slot_index(slot: ProbeSlot) -> int | None:
    match = VETH_HOST_INDEX.match(slot.veth_host)
    return int(match.group(1)) if match else None


def slot_indices(slots: list[ProbeSlot]) -> dict[str, int]:
    indices = {}
    for slot in slots:
        index = slot_index(slot)
        if index is not None:
            indices[slot_key(slot.server, slot.transport)] = index
    return indices


def reality_conf_path(slot: ProbeSlot) -> Path:
    return REALITY_CONF_DIR / f"{slot.netns}.json"


def render(slots: list[ProbeSlot], indices: dict[str, int] | None = None) -> str:
    merged = {**slot_indices(slots), **(indices or {})}
    return ProbeState(slots=slots, indices=merged).model_dump_json(indent=2) + "\n"


def parse(text: str) -> list[ProbeSlot]:
    return ProbeState.model_validate_json(text).slots


def load(path: Path = STATE_PATH) -> list[ProbeSlot]:
    if not path.exists():
        return []
    return parse(path.read_text())


def load_indices(path: Path = STATE_PATH) -> dict[str, int]:
    try:
        state = ProbeState.model_validate_json(path.read_text())
    except (OSError, ValueError):
        return {}
    return {**slot_indices(state.slots), **state.indices}


def reality_conf_sni(conf_text: str) -> str | None:
    try:
        doc: Any = json.loads(conf_text)
        sni = doc["outbounds"][0]["streamSettings"]["realitySettings"]["serverName"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return str(sni) if sni else None
