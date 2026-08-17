"""Обёртка над mtg/microsocks на jeeves — единственное место, где служба vpn
трогает эти демоны напрямую (по образцу vpn/awg.py).

Счётчики трафика читаются через `sudo -n nft -j list counter inet filter
<name>` — узкий sudoers-снипет (node/fixups.py::make_proxy_firewall_fixup,
NOPASSWD ровно на чтение двух конкретных именованных счётчиков, не на весь
`nft`). Секрет mtg меняется БЕЗ sudo — юзер-юнит `~/.config/systemd/user/
mtg.service` принадлежит тому же пользователю, что и сама служба vpn
(node/fixups.py::make_proxy_units_fixup ставит mtg в /usr/local/bin, но сам
юнит — обычный файл в $HOME).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Protocol

from sa_home_bot.proto.messages import ERR_INTERNAL, ERR_NEEDS_PRIVILEGE, ProtoError
from sa_home_bot.utils.requirements import looks_like_permission_error

log = logging.getLogger(__name__)

MTG_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / "mtg.service"
# ExecStart=/usr/local/bin/mtg simple-run 0.0.0.0:443 <secret> — секрет
# всегда последний токен строки simple-run, независимо от пути к бинарю.
_SECRET_RE = re.compile(r"^(ExecStart=.*\bsimple-run\s+\S+\s+)(\S+)\s*$", re.MULTILINE)

_COUNTER_NAMES = {"mtg": "mtg_bytes", "socks": "socks_bytes"}


class ProxyBackend(Protocol):
    async def counters(self) -> dict[str, int]:
        """{"mtg": bytes, "socks": bytes} — счётчик nft с момента его создания."""
        ...

    async def generate_secret(self, domain: str) -> str:
        """Новый секрет fake-TLS для ``domain`` — не sudo, локальная утилита."""
        ...

    async def rotate_secret(self, new_secret: str) -> None:
        """Переписать ExecStart юнита mtg новым секретом и перезапустить демон."""
        ...


async def _run(argv: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr_raw = await proc.communicate()
    return proc.returncode or 0, stdout.decode(), stderr_raw.decode(errors="replace").strip()


async def _sudo_nft(*args: str) -> str:
    path = shutil.which("nft") or "nft"
    code, stdout, stderr = await _run(["sudo", "-n", path, *args])
    if code != 0:
        if looks_like_permission_error(stderr) or "a password is required" in stderr.lower():
            raise ProtoError(
                ERR_NEEDS_PRIVILEGE,
                "нужны права для чтения счётчиков прокси — по SSH выполните: nodectl fix",
            )
        raise ProtoError(ERR_INTERNAL, f"nft {' '.join(args)} завершился ошибкой: {stderr}")
    return stdout


def _parse_counter_bytes(output: str) -> int:
    data = json.loads(output)
    for item in data.get("nftables", []):
        counter = item.get("counter")
        if counter is not None:
            return int(counter.get("bytes", 0))
    return 0


class RealProxyBackend:
    """Настоящая реализация — зовёт `sudo -n nft` и правит юзер-юнит mtg."""

    async def counters(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for key, counter_name in _COUNTER_NAMES.items():
            output = await _sudo_nft("-j", "list", "counter", "inet", "filter", counter_name)
            result[key] = _parse_counter_bytes(output)
        return result

    async def generate_secret(self, domain: str) -> str:
        mtg_path = shutil.which("mtg") or "/usr/local/bin/mtg"
        code, stdout, stderr = await _run([mtg_path, "generate-secret", domain])
        if code != 0:
            raise ProtoError(ERR_INTERNAL, f"mtg generate-secret: {stderr}")
        secret = stdout.strip()
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
        code, _, stderr = await _run(["systemctl", "--user", "restart", "mtg.service"])
        if code != 0:
            raise ProtoError(ERR_INTERNAL, f"systemctl --user restart mtg.service: {stderr}")
