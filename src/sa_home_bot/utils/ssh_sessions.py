"""Открытые SSH-сессии на этой машине (systemd-logind) — узнать, кто зашёл
руками, и при необходимости выгнать (node/service.py::maybe_auto_poweroff_idle,
_close_ssh_sessions — автовыключение mycraft по простою Alfred не должно
обрывать чужую работу за терминалом молча).

``loginctl`` уже обязателен в проекте (polkit-правила питания в
node/fixups.py на systemd-logind и так завязаны) — отдельная зависимость не
нужна. Сессия считается SSH, если у неё ``Remote=yes`` — так PAM помечает
вход именно по сети (в отличие от локальной консоли/GUI); живая проверка
2026-08-03 на mycraft подтвердила: обычная `systemd --user` linger-сессия
даёт ``Remote=no``, интерактивный `ssh mycraft` — ``Remote=yes``.

Разбор через построчный ``Key=Value`` (без ``--value``): проверено вживую —
при нескольких ``-p`` подряд ``show-session ... --value`` отдаёт значения в
СВОЁМ внутреннем порядке свойств, а не в порядке аргументов ``-p``, что при
позиционном разборе тихо перепутывает поля. Построчный формат самоописан и
от этого не зависит.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

_SESSION_PROPS = ("Remote", "Name", "TTY", "Timestamp")


@dataclass(frozen=True)
class SshSession:
    id: str
    user: str
    tty: str
    since: str

    def describe(self) -> str:
        tty = self.tty or "?"
        since = self.since or "?"
        return f"{self.user}, {tty}, с {since}"


async def _run(*argv: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode(errors="replace")


def _parse_props(raw: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    return props


async def list_ssh_sessions() -> list[SshSession]:
    """Все logind-сессии этой машины, пришедшие по сети (SSH)."""
    listing = await _run("loginctl", "list-sessions", "--no-legend")
    ids = [line.split()[0] for line in listing.splitlines() if line.split()]
    sessions = []
    for session_id in ids:
        raw = await _run(
            "loginctl",
            "show-session",
            session_id,
            *[arg for p in _SESSION_PROPS for arg in ("-p", p)],
        )
        props = _parse_props(raw)
        if props.get("Remote") != "yes":
            continue
        sessions.append(
            SshSession(
                id=session_id,
                user=props.get("Name", "?"),
                tty=props.get("TTY", ""),
                since=props.get("Timestamp", ""),
            )
        )
    return sessions


async def terminate_sessions(session_ids: list[str]) -> None:
    """Закрыть перечисленные logind-сессии (и всё, что под ними запущено)."""
    for session_id in session_ids:
        await _run("loginctl", "terminate-session", session_id)
