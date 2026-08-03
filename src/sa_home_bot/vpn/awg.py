"""Обёртка над `awg` (amneziawg-tools) — единственное место, где служба vpn
зовёт внешний бинарник.

Долгоживущий процесс сам не имеет root: команды, меняющие интерфейс, идут
через `sudo -n` на узкий sudoers-снипет (см. node/fixups.py::AWG_SUDOERS —
NOPASSWD ровно на `awg show *` и `awg set <iface> peer *`, не на весь
`awg-quick`, который исполняет произвольные PostUp). Отказ sudo превращается
в ProtoError(ERR_NEEDS_PRIVILEGE) — тот же приём, что apps/service.py::
_run_systemctl, с той же подсказкой «выполните nodectl fix».

Генерация ключей (`awg genkey`/`awg pubkey`) root не требует — приватный
ключ никогда не должен всплывать в sudo-логе.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Protocol

from sa_home_bot.proto.messages import ERR_INTERNAL, ERR_NEEDS_PRIVILEGE, ProtoError
from sa_home_bot.utils.requirements import looks_like_permission_error

log = logging.getLogger(__name__)


class AwgBackend(Protocol):
    async def server_public_key(self) -> str: ...

    async def add_peer(self, public_key: str, address: str) -> None: ...

    async def remove_peer(self, public_key: str) -> None: ...

    async def transfer(self) -> dict[str, tuple[int, int]]:
        """pubkey → (rx_bytes, tx_bytes) с момента поднятия интерфейса."""
        ...

    async def latest_handshakes(self) -> dict[str, int]:
        """pubkey → unix-timestamp последнего хендшейка (0 — никогда)."""
        ...

    async def generate_keypair(self) -> tuple[str, str]:
        """(приватный, публичный) — без sudo."""
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
    """Настоящая реализация — зовёт `sudo -n awg ...` на jeeves."""

    def __init__(self, interface: str) -> None:
        self._interface = interface

    def _resolved_path(self) -> str:
        # Резолвим при каждом вызове (не кэшируем): так фикс sudoers,
        # применённый ПОСЛЕ старта службы (nodectl fix), подхватится без
        # рестарта. Тот же приём, что node/fixups.py::_which.
        return shutil.which("awg") or "awg"

    async def _sudo_awg(self, *args: str) -> str:
        path = self._resolved_path()
        proc = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr_raw = await proc.communicate()
        if proc.returncode != 0:
            stderr = stderr_raw.decode(errors="replace").strip()
            if looks_like_permission_error(stderr) or "a password is required" in stderr.lower():
                raise ProtoError(
                    ERR_NEEDS_PRIVILEGE,
                    "нужны права для управления awg — по SSH выполните: nodectl fix",
                )
            raise ProtoError(ERR_INTERNAL, f"awg {' '.join(args)} завершился ошибкой: {stderr}")
        return stdout.decode()

    async def server_public_key(self) -> str:
        return (await self._sudo_awg("show", self._interface, "public-key")).strip()

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
        gen = await asyncio.create_subprocess_exec(
            "awg", "genkey", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr_raw = await gen.communicate()
        if gen.returncode != 0:
            raise ProtoError(
                ERR_INTERNAL, f"awg genkey: {stderr_raw.decode(errors='replace').strip()}"
            )
        private_key = stdout.decode().strip()

        pub = await asyncio.create_subprocess_exec(
            "awg",
            "pubkey",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout2, stderr2_raw = await pub.communicate(private_key.encode())
        if pub.returncode != 0:
            raise ProtoError(
                ERR_INTERNAL, f"awg pubkey: {stderr2_raw.decode(errors='replace').strip()}"
            )
        return private_key, stdout2.decode().strip()
