from __future__ import annotations

import logging
import os
import re
import tarfile
from pathlib import Path

from sa_home_bot.devlab import protocol
from sa_home_bot.utils.process import run

log = logging.getLogger(__name__)

PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MIRROR_REFS = "refs/hub/"
FETCH_REFSPEC = f"+{protocol.HEADS_REF_PREFIX}*:{MIRROR_REFS}*"
GIT_TIMEOUT_S = 300.0
ARCHIVE_NAME = ".devlab-src.tar"
SECRET_MASK = "***"


class GitError(Exception):
    pass


class ProjectMirror:
    def __init__(self, git_dir: Path, remote_url: str, token: str) -> None:
        self._git_dir = git_dir
        self._remote_url = remote_url
        self._token = token

    def _env(self) -> dict[str, str]:
        return {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {self._token}",
        }

    async def _git(self, *argv: str, stdout=None) -> bytes:
        command = ["git", f"--git-dir={self._git_dir}", *argv]
        kwargs = {} if stdout is None else {"stdout": stdout}
        result = await run(command, timeout=GIT_TIMEOUT_S, env=self._env(), **kwargs)
        if result.returncode != 0:
            message = result.stderr_text.replace(self._token, SECRET_MASK)
            raise GitError(f"git {argv[0]}: {message}")
        return result.stdout

    async def fetch(self) -> None:
        if not (self._git_dir / "HEAD").exists():
            self._git_dir.parent.mkdir(parents=True, exist_ok=True)
            await self._git("init", "--bare", "-q", str(self._git_dir))
        await self._git("fetch", "-q", "--prune", "--no-tags", self._remote_url, FETCH_REFSPEC)

    async def resolve(self, ref: str) -> str:
        for candidate in (f"{MIRROR_REFS}{ref}", ref):
            try:
                out = await self._git("rev-parse", "--verify", "-q", f"{candidate}^{{commit}}")
            except GitError:
                continue
            return out.decode().strip()
        raise GitError(f"ref {ref!r} не найден в репозитории проекта")

    async def read_file(self, sha: str, path: str) -> bytes | None:
        try:
            return await self._git("show", f"{sha}:{path}")
        except GitError:
            return None

    async def export(self, sha: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        archive = dest.parent / f"{dest.name}{ARCHIVE_NAME}"
        try:
            with archive.open("wb") as handle:
                await self._git("archive", "--format=tar", sha, stdout=handle)
            with tarfile.open(archive) as tar:
                tar.extractall(dest, filter="data")
        finally:
            archive.unlink(missing_ok=True)

    async def delete_branch(self, branch: str) -> None:
        await self._git("push", "-q", self._remote_url, f":{protocol.HEADS_REF_PREFIX}{branch}")


def check_project_name(name: str) -> str:
    if not PROJECT_NAME.match(name):
        raise GitError(f"имя проекта {name!r} недопустимо")
    return name


def remote_url(hub_url: str, project: str) -> str:
    return f"{hub_url.rstrip('/')}{protocol.HTTP_GIT_PREFIX}{project}{protocol.GIT_REPO_SUFFIX}"
