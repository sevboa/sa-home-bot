from __future__ import annotations

import shlex
import stat
import sys
from pathlib import Path

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.utils.process import run

GIT_TIMEOUT_S = 60.0
HOOK_NAME = "pre-receive"
HOOK_MODULE = "sa_home_bot.devlab.hub.refpolicy"
DEFAULT_BRANCH = "master"
REPO_CONFIG = {
    "http.receivepack": "true",
    "receive.denyDeletes": "false",
    "receive.denyNonFastForwards": "false",
}


def repo_path(git_root: Path, name: str) -> Path:
    return git_root / f"{name}{protocol.GIT_REPO_SUFFIX}"


def hook_script(python: str = sys.executable) -> str:
    return f"#!/bin/sh\nexec {shlex.quote(python)} -m {HOOK_MODULE}\n"


def install_hook(repo: Path, python: str = sys.executable) -> None:
    hook = repo / "hooks" / HOOK_NAME
    hook.write_text(hook_script(python))
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


async def _git(*argv: str) -> None:
    result = await run(["git", *argv], timeout=GIT_TIMEOUT_S)
    if result.returncode != 0:
        raise HubError(f"git {argv[0]}: {result.stderr_text or result.returncode}")


async def init_bare(git_root: Path, name: str) -> Path:
    repo = repo_path(git_root, name)
    if repo.exists():
        raise HubError(f"репозиторий {repo.name} уже существует")
    git_root.mkdir(parents=True, exist_ok=True)
    await _git("init", "--bare", f"--initial-branch={DEFAULT_BRANCH}", str(repo))
    for key, value in REPO_CONFIG.items():
        await _git("-C", str(repo), "config", key, value)
    install_hook(repo)
    return repo
