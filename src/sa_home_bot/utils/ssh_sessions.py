from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sa_home_bot.utils.process import run

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
    result = await run(argv, stderr=asyncio.subprocess.DEVNULL)
    return result.stdout_text


def _parse_props(raw: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    return props


async def list_ssh_sessions() -> list[SshSession]:
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
    for session_id in session_ids:
        await _run("loginctl", "terminate-session", session_id)


async def list_tmux_sessions() -> list[str]:
    try:
        raw = await _run("tmux", "list-sessions", "-F", "#{session_name}")
    except FileNotFoundError:
        return []
    return [line for line in raw.splitlines() if line.strip()]


async def kill_tmux_server() -> None:
    try:
        await _run("tmux", "kill-server")
    except FileNotFoundError:
        pass
