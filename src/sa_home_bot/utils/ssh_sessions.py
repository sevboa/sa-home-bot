"""Признаки того, что за mycraft сейчас работают руками — открытые SSH-сессии
и живые tmux-сессии, — и способ их закрыть (node/service.py::
maybe_auto_poweroff_idle, _close_ssh_sessions). Автовыключение по простою
Alfred не должно обрывать чужую работу молча.

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

tmux отдельно от logind-сессий не просто так: сессия tmux переживает разрыв
SSH-соединения (это и есть весь смысл tmux — оставить задачу работать после
отключения) — человек мог отсоединиться и уйти, а его процесс внутри панели
tmux всё ещё считает, что работает. `Remote=yes`-проверка такую сессию не
увидит вообще, поэтому это отдельный, независимый сигнал простоя. На
2026-08-03 tmux на mycraft не установлен — ``list_tmux_sessions`` явно
проглатывает ``FileNotFoundError`` (нет бинарника — трактуем как «нет
сессий», а не роняем проверку), но НЕ в общем ``_run``: для ``loginctl``,
единственного жёсткого источника правды о SSH, отсутствие бинарника должно
падать по-настоящему, а не молча превращаться в «сессий нет» — иначе поломка
systemd-logind сделала бы автовыключение опасно «слепым».
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


async def list_tmux_sessions() -> list[str]:
    """Имена живых tmux-сессий на этой машине — сигнал простоя, независимый
    от SSH (см. докстроку модуля). Пустой список и когда tmux не установлен,
    и когда сервер tmux не поднят вовсе (``list-sessions`` в обоих случаях
    возвращает ненулевой код) — для целей этой проверки разница не важна."""
    try:
        raw = await _run("tmux", "list-sessions", "-F", "#{session_name}")
    except FileNotFoundError:
        return []
    return [line for line in raw.splitlines() if line.strip()]


async def kill_tmux_server() -> None:
    """Погасить tmux целиком — вместе со всеми панелями и тем, что в них
    запущено (см. node/service.py::_close_ssh_sessions: пользователь явно
    подтвердил кнопкой «выключить», а не просто «отсоединить»)."""
    try:
        await _run("tmux", "kill-server")
    except FileNotFoundError:
        pass
