from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError
from sa_home_bot.utils.process import run

log = logging.getLogger(__name__)


class XrayBackend(Protocol):
    async def add_client(self, client_uuid: str, email: str, flow: str) -> None: ...

    async def remove_client(self, email: str) -> None: ...

    async def list_clients(self) -> set[str]:
        ...

    async def stats(self) -> dict[str, tuple[int, int]]:
        ...


def _parse_stats(payload: str) -> dict[str, tuple[int, int]]:
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
    def __init__(self, api_addr: str, inbound_tag: str, port: int) -> None:
        self._api = api_addr
        self._tag = inbound_tag
        self._port = port

    def _bin(self) -> str:
        return shutil.which("xray") or "/usr/local/bin/xray"

    async def _run(self, *args: str, stdin: bytes | None = None) -> str:
        result = await run([self._bin(), "api", *args], input=stdin)
        if result.returncode != 0:
            raise ProtoError(
                ERR_INTERNAL,
                f"xray api {args[0] if args else ''} завершился ошибкой: {result.stderr_text}",
            )
        return result.stdout.decode()

    async def add_client(self, client_uuid: str, email: str, flow: str) -> None:
        snippet = {
            "inbounds": [
                {
                    "tag": self._tag,
                    "port": self._port,
                    "protocol": "vless",
                    "settings": {
                        "decryption": "none",
                        "clients": [{"id": client_uuid, "email": email, "flow": flow}],
                    },
                }
            ]
        }
        tmp = Path(tempfile.mkstemp(prefix="reality-adu-", suffix=".json")[1])
        try:
            tmp.write_text(json.dumps(snippet), encoding="utf-8")
            out = await self._run("adu", f"--server={self._api}", str(tmp))
        finally:
            tmp.unlink(missing_ok=True)
        if "Added 0 user(s)" in out or "failed to build config" in out:
            raise ProtoError(
                ERR_INTERNAL,
                f"xray api adu не добавил клиента {email}: {out.strip()}",
            )

    async def remove_client(self, email: str) -> None:
        try:
            await self._run("rmu", f"--server={self._api}", f"-tag={self._tag}", email)
        except ProtoError as exc:
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
