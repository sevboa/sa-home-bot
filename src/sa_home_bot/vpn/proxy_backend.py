from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Protocol

from sa_home_bot.proto.messages import ERR_INTERNAL, ERR_NEEDS_PRIVILEGE, ProtoError
from sa_home_bot.utils.process import run
from sa_home_bot.vpn.privileged import sudo_stdout

log = logging.getLogger(__name__)

MTG_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / "mtg.service"
_SECRET_RE = re.compile(r"^(ExecStart=.*\bsimple-run\s+\S+\s+)(\S+)\s*$", re.MULTILINE)

_COUNTER_NAMES = {"mtg": "mtg_bytes", "socks": "socks_bytes"}


class ProxyBackend(Protocol):
    async def counters(self) -> dict[str, int]:
        ...

    async def generate_secret(self, domain: str) -> str:
        ...

    async def rotate_secret(self, new_secret: str) -> None:
        ...


async def _sudo_nft(*args: str) -> str:
    return await sudo_stdout(
        "nft",
        *args,
        needs_privilege_text=(
            "нужны права для чтения счётчиков прокси — по SSH выполните: nodectl fix"
        ),
    )


def _parse_counter_bytes(output: str) -> int:
    data = json.loads(output)
    for item in data.get("nftables", []):
        counter = item.get("counter")
        if counter is not None:
            return int(counter.get("bytes", 0))
    return 0


class RealProxyBackend:
    async def counters(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for key, counter_name in _COUNTER_NAMES.items():
            output = await _sudo_nft("-j", "list", "counter", "inet", "filter", counter_name)
            result[key] = _parse_counter_bytes(output)
        return result

    async def generate_secret(self, domain: str) -> str:
        mtg_path = shutil.which("mtg") or "/usr/local/bin/mtg"
        generated = await run([mtg_path, "generate-secret", domain])
        if generated.returncode != 0:
            raise ProtoError(ERR_INTERNAL, f"mtg generate-secret: {generated.stderr_text}")
        secret = generated.stdout.decode().strip()
        if not secret:
            raise ProtoError(ERR_INTERNAL, "mtg generate-secret вернул пустой секрет")
        return secret

    async def rotate_secret(self, new_secret: str) -> None:
        if not MTG_UNIT_PATH.exists():
            raise ProtoError(
                ERR_NEEDS_PRIVILEGE, "юнит mtg не найден — по SSH на jeeves выполните: nodectl fix"
            )
        content = MTG_UNIT_PATH.read_text(encoding="utf-8")
        new_content, count = _SECRET_RE.subn(rf"\g<1>{new_secret}", content)
        if count != 1:
            raise ProtoError(ERR_INTERNAL, "не удалось найти секрет в mtg.service для замены")
        MTG_UNIT_PATH.write_text(new_content, encoding="utf-8")
        restarted = await run(["systemctl", "--user", "restart", "mtg.service"])
        if restarted.returncode != 0:
            raise ProtoError(
                ERR_INTERNAL, f"systemctl --user restart mtg.service: {restarted.stderr_text}"
            )
