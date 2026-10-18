from __future__ import annotations

from pathlib import Path

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


class ProbeState(BaseModel):
    slots: list[ProbeSlot] = Field(default_factory=list)


def reality_conf_path(slot: ProbeSlot) -> Path:
    return REALITY_CONF_DIR / f"{slot.netns}.json"


def render(slots: list[ProbeSlot]) -> str:
    return ProbeState(slots=slots).model_dump_json(indent=2) + "\n"


def parse(text: str) -> list[ProbeSlot]:
    return ProbeState.model_validate_json(text).slots


def load(path: Path = STATE_PATH) -> list[ProbeSlot]:
    if not path.exists():
        return []
    return parse(path.read_text())
