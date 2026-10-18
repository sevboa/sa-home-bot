from __future__ import annotations

import logging
from typing import Protocol

from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError
from sa_home_bot.utils.process import run
from sa_home_bot.vpn.privileged import sudo_stdout

log = logging.getLogger(__name__)


class AwgBackend(Protocol):
    async def server_public_key(self) -> str: ...

    async def add_peer(self, public_key: str, address: str) -> None: ...

    async def remove_peer(self, public_key: str) -> None: ...

    async def transfer(self) -> dict[str, tuple[int, int]]:
        ...

    async def latest_handshakes(self) -> dict[str, int]:
        ...

    async def generate_keypair(self) -> tuple[str, str]:
        ...


def _parse_transfer(output: str) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            result[parts[0]] = (int(parts[1]), int(parts[2]))
        except ValueError:
            continue
    return result


def _parse_handshakes(output: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            result[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return result


class RealAwgBackend:
    def __init__(self, interface: str) -> None:
        self._interface = interface

    async def _sudo_awg(self, *args: str) -> str:
        return await sudo_stdout(
            "awg",
            *args,
            needs_privilege_text="нужны права для управления awg — по SSH выполните: nodectl fix",
        )

    async def server_public_key(self) -> str:
        return (await self._sudo_awg("show", self._interface, "public-key")).strip()

    async def server_private_key(self) -> str:
        return (await self._sudo_awg("show", self._interface, "private-key")).strip()

    async def add_peer(self, public_key: str, address: str) -> None:
        await self._sudo_awg(
            "set", self._interface, "peer", public_key, "allowed-ips", f"{address}/32"
        )

    async def remove_peer(self, public_key: str) -> None:
        await self._sudo_awg("set", self._interface, "peer", public_key, "remove")

    async def transfer(self) -> dict[str, tuple[int, int]]:
        return _parse_transfer(await self._sudo_awg("show", self._interface, "transfer"))

    async def latest_handshakes(self) -> dict[str, int]:
        return _parse_handshakes(
            await self._sudo_awg("show", self._interface, "latest-handshakes")
        )

    async def generate_keypair(self) -> tuple[str, str]:
        generated = await run(["awg", "genkey"])
        if generated.returncode != 0:
            raise ProtoError(ERR_INTERNAL, f"awg genkey: {generated.stderr_text}")
        private_key = generated.stdout.decode().strip()
        derived = await run(["awg", "pubkey"], input=private_key.encode())
        if derived.returncode != 0:
            raise ProtoError(ERR_INTERNAL, f"awg pubkey: {derived.stderr_text}")
        return private_key, derived.stdout.decode().strip()
