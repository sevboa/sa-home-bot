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


class GitRunner:
    def __init__(self, token: str) -> None:
        self._token = token

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {self._token}",
            **(extra or {}),
        }

    async def run(
        self,
        *argv: str,
        git_dir: Path,
        work_tree: Path | None = None,
        stdout=None,
        extra_env: dict[str, str] | None = None,
    ) -> bytes:
        command = ["git", f"--git-dir={git_dir}"]
        if work_tree is not None:
            command.append(f"--work-tree={work_tree}")
        command.extend(argv)
        kwargs = {} if stdout is None else {"stdout": stdout}
        result = await run(command, timeout=GIT_TIMEOUT_S, env=self.env(extra_env), **kwargs)
        if result.returncode != 0:
            message = result.stderr_text.replace(self._token, SECRET_MASK)
            raise GitError(f"git {argv[0]}: {message}")
        return result.stdout


class ProjectMirror:
    def __init__(self, git_dir: Path, remote_url: str, token: str) -> None:
        self._git_dir = git_dir
        self._remote_url = remote_url
        self._runner = GitRunner(token)

    @property
    def git_dir(self) -> Path:
        return self._git_dir

    @property
    def url(self) -> str:
        return self._remote_url

    @property
    def runner(self) -> GitRunner:
        return self._runner

    async def _git(self, *argv: str, stdout=None) -> bytes:
        return await self._runner.run(*argv, git_dir=self._git_dir, stdout=stdout)

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
