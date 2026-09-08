"""Обёртка над бинарником ``xray`` — единственное место, где служба
``reality`` зовёт внешний процесс.

Управление юзерами и учёт трафика идут через gRPC API самого xray
(``xray api adu``/``rmu``/``inbounduser``/``statsquery`` на локальном
``127.0.0.1:10085``) — это обычный пользовательский вызов, **без sudo и без
рестарта юнита** (в отличие от ``vpn/awg.py``, которому нужен ``sudo -n awg``).
Поэтому и отдельного sudoers-снипета у ``reality`` нет.

``xray api adu`` меняет только ЖИВОЙ инстанс, не файл ``config.json`` — после
рестарта xray список юзеров пуст, а БД помнит всех активных. Свести их
обратно — задача ``RealityService.reconcile()`` при старте (тот же приём, что
``VpnService.reconcile()`` после рестарта ``awg0``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError

log = logging.getLogger(__name__)


class XrayBackend(Protocol):
    async def add_client(self, client_uuid: str, email: str, flow: str) -> None: ...

    async def remove_client(self, email: str) -> None: ...

    async def list_clients(self) -> set[str]:
        """email'ы юзеров, реально сидящих в inbound прямо сейчас."""
        ...

    async def stats(self) -> dict[str, tuple[int, int]]:
        """email → (uplink, downlink) в байтах с момента старта xray (или с
        последнего ``-reset``; мы не сбрасываем — дельту считает сервис)."""
        ...


def _parse_stats(payload: str) -> dict[str, tuple[int, int]]:
    """Разбор вывода ``xray api statsquery`` (JSON ``{"stat": [{name, value}]}``).

    Имена вида ``user>>>bob@x>>>traffic>>>uplink`` — email может содержать
    ``>>>``? Нет: xml-подобный разделитель зарезервирован, email его не несёт.
    """
    try:
        data = json.loads(payload or "{}")
    except ValueError as exc:
        raise ProtoError(ERR_INTERNAL, f"xray statsquery: не JSON — {exc}") from exc
    acc: dict[str, list[int]] = {}
    for item in data.get("stat") or []:
        name = str(item.get("name") or "")
        parts = name.split(">>>")
        if len(parts) != 4 or parts[0] != "user" or parts[2] != "traffic":
            continue
        email, direction = parts[1], parts[3]
        try:
            value = int(item.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        slot = acc.setdefault(email, [0, 0])
        if direction == "uplink":
            slot[0] = value
        elif direction == "downlink":
            slot[1] = value
    return {email: (up, down) for email, (up, down) in acc.items()}


def _parse_clients(payload: str) -> set[str]:
    try:
        data = json.loads(payload or "{}")
    except ValueError as exc:
        raise ProtoError(ERR_INTERNAL, f"xray inbounduser: не JSON — {exc}") from exc
    return {str(u.get("email")) for u in (data.get("users") or []) if u.get("email")}


class RealXrayBackend:
    """Настоящая реализация — зовёт ``xray api ...`` на локальном gRPC-порту."""

    def __init__(self, api_addr: str, inbound_tag: str) -> None:
        self._api = api_addr
        self._tag = inbound_tag

    def _bin(self) -> str:
        # Резолвим при каждом вызове (не кэшируем) — как node/fixups.py::_which:
        # если xray доставили/обновили после старта службы, подхватится без
        # рестарта.
        return shutil.which("xray") or "/usr/local/bin/xray"

    async def _run(self, *args: str, stdin: bytes | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            self._bin(),
            "api",
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr_raw = await proc.communicate(stdin)
        if proc.returncode != 0:
            stderr = stderr_raw.decode(errors="replace").strip()
            raise ProtoError(
                ERR_INTERNAL,
                f"xray api {args[0] if args else ''} завершился ошибкой: {stderr}",
            )
        return stdout.decode()

    async def add_client(self, client_uuid: str, email: str, flow: str) -> None:
        snippet = {
            "inbounds": [
                {
                    "tag": self._tag,
                    "protocol": "vless",
                    "settings": {"clients": [{"id": client_uuid, "email": email, "flow": flow}]},
                }
            ]
        }
        # ``xray api adu`` принимает только пути к файлам, не stdin.
        tmp = Path(tempfile.mkstemp(prefix="reality-adu-", suffix=".json")[1])
        try:
            tmp.write_text(json.dumps(snippet), encoding="utf-8")
            await self._run("adu", f"--server={self._api}", str(tmp))
        finally:
            tmp.unlink(missing_ok=True)

    async def remove_client(self, email: str) -> None:
        try:
            await self._run("rmu", f"--server={self._api}", f"-tag={self._tag}", email)
        except ProtoError as exc:
            # Юзера уже нет в живом инстансе (рестарт xray, ручная правка) —
            # для реконсайлера это успех, а не ошибка.
            if "not found" in str(exc).lower():
                log.info("reality: rmu %s — юзера уже нет, пропускаю", email)
                return
            raise

    async def list_clients(self) -> set[str]:
        return _parse_clients(
            await self._run("inbounduser", f"--server={self._api}", f"-tag={self._tag}")
        )

    async def stats(self) -> dict[str, tuple[int, int]]:
        return _parse_stats(
            await self._run("statsquery", f"--server={self._api}", "-pattern=user>>>")
        )
