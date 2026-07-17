"""Самообновление ноды через pipx — механика без сети, где это возможно.

pipx (в отличие от `nodectl fix`/sudo) не требует root — переустановку можно
делать прямо из долгоживущего процесса ноды, без отдельного короткоживущего
процесса с интерактивным терминалом. Уже загруженный в память код текущего
процесса переустановка файлов на диске не трогает — «увидеть» новую версию
можно только после рестарта, который выполняет человек (`restart_node`).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from sa_home_bot.proto.client import DEFAULT_TIMEOUT
from sa_home_bot.utils.version import version_key

log = logging.getLogger(__name__)

PACKAGE_NAME = "sa-home-bot"

_TAG_RE = re.compile(r"v\d+(?:\.\d+)*")

# ls-remote — короткая read-only сетевая операция, вызывается синхронно внутри
# check_update/update и ждёт ответа клиент (nodectl/бот) с таймаутом
# DEFAULT_TIMEOUT протокола. Держим заметно меньше него — иначе клиент
# сдаётся раньше, чем сервер успевает честно ответить "сеть не сработала", и
# вместо аккуратной ошибки прилетает голый TimeoutError на всё соединение.
LS_REMOTE_TIMEOUT_S = DEFAULT_TIMEOUT - 3
# pipx install — git clone + сборка пакета, но это фоновая задача
# (asyncio.create_task в node/service.py) — клиент её не ждёт, таймаут можно
# держать большим.
PIPX_INSTALL_TIMEOUT_S = 300


def installed_version() -> str | None:
    """Версия пакета НА ДИСКЕ прямо сейчас (не то, что выполняется в памяти).

    Перечитывается каждый раз — после `pipx install --force` увидит новую
    версию, пока сам процесс не перезапущен (см. `__version__` — то, что
    реально исполняется, зафиксировано при импорте и не меняется).
    """
    importlib.invalidate_caches()
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def origin_repo_url() -> str | None:
    """Git-репозиторий, из которого поставлен этот пакет (PEP 610).

    None — editable-установка (dev-чекаут: `pip install -e .`), пакет не
    найден, или он поставлен не из git — обновлять через pipx нечего.
    """
    try:
        dist = importlib.metadata.distribution(PACKAGE_NAME)
        raw = dist.read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if data.get("dir_info", {}).get("editable"):
        return None
    vcs_info = data.get("vcs_info") or {}
    if vcs_info.get("vcs") != "git":
        return None
    return data.get("url")


def _parse_tags(ls_remote_output: str) -> list[str]:
    """`git ls-remote --tags --refs` → список имён тегов вида vX.Y.Z…"""
    tags = []
    for line in ls_remote_output.splitlines():
        _, _, ref = line.partition("refs/tags/")
        if ref and _TAG_RE.fullmatch(ref):
            tags.append(ref)
    return tags


def _latest_tag_sync(repo_url: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", repo_url],
            capture_output=True,
            text=True,
            timeout=LS_REMOTE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git ls-remote %s не сработал: %s", repo_url, exc)
        return None
    if out.returncode != 0:
        log.warning("git ls-remote %s вернул код %s: %s", repo_url, out.returncode, out.stderr)
        return None
    tags = _parse_tags(out.stdout)
    if not tags:
        return None
    return max(tags, key=lambda t: version_key(t.lstrip("v")))


async def latest_tag(repo_url: str) -> str | None:
    """Самый свежий тег `vX.Y.Z…` в репозитории (read-only, без токена)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _latest_tag_sync, repo_url)


def _pipx_home() -> str:
    """PIPX_HOME, вычисленный от РЕАЛЬНО работающего интерпретатора, а не
    унаследованный от аккаунта, который зовёт pipx.

    pipx-венв всегда имеет структуру ``<PIPX_HOME>/venvs/<pkg>/{bin,Scripts}/
    python[.exe]`` — четвёртый родитель ``sys.executable`` и есть PIPX_HOME.

    Живой баг 2026-07-17: Windows-служба идёт от LocalSystem, у которого
    СВОЙ %LOCALAPPDATA% (системный профиль), отличный от того, откуда
    реально запущен процесс. Без явного PIPX_HOME `pipx install --force`
    молча ставит пакет в venv ПОД LocalSystem — "успешно", но мимо venv'а,
    из которого служба фактически работает; `installed_version()` после
    такого "обновления" честно продолжает видеть старую версию.
    """
    return str(Path(sys.executable).parents[3])


async def pipx_reinstall(repo_url: str, ref: str) -> tuple[bool, str]:
    """`pipx install --force git+<repo_url>@<ref>` — без sudo, без TTY.

    Возвращает (успех, хвост вывода для диагностики). Не бросает исключений
    наружу — вызывающий код (фоновая задача) сам решает, что делать с провалом.
    """
    spec = f"git+{repo_url}@{ref}"
    env = {**os.environ, "PIPX_HOME": _pipx_home()}
    try:
        proc = await asyncio.create_subprocess_exec(
            "pipx",
            "install",
            "--force",
            spec,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=PIPX_INSTALL_TIMEOUT_S
        )
    except (OSError, TimeoutError) as exc:
        return False, str(exc)
    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        return False, output[-2000:]  # хвост — обычно там суть ошибки
    return True, output[-2000:]
