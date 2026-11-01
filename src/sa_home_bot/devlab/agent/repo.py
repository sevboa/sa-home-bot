from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.executor.mirror import GitError, ProjectMirror

log = logging.getLogger(__name__)

AUTHOR_EMAIL = "devlab-agent@localhost"
AUTHOR_LABEL = "devlab-agent"
MILESTONE_PREFIX = "веха: "
LEFTOVER_MESSAGE = "правки перед завершением"
ABANDONED_PREFIX = "не завершено: "
PUSH_INTERVAL_S = 30.0
SUBJECT_LIMIT = 200
BASE_REF = "refs/devlab/base"
HEAD_REF = "HEAD"


@dataclass(frozen=True)
class CommitAuthor:
    name: str
    email: str = AUTHOR_EMAIL


def agent_author(model: str) -> CommitAuthor:
    return CommitAuthor(f"{AUTHOR_LABEL} ({model})")


class AgentRepo:
    def __init__(
        self,
        mirror: ProjectMirror,
        workdir: Path,
        git_dir: Path,
        job_id: str,
        author: CommitAuthor,
        push_interval_s: float = PUSH_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mirror = mirror
        self._workdir = workdir
        self._git_dir = git_dir
        self._job_id = job_id
        self._author = author
        self._interval = push_interval_s
        self._clock = clock
        self._wip_branch = f"{protocol.WIP_BRANCH_PREFIX}{job_id}"
        self._agent_branch = f"{protocol.AGENT_BRANCH_PREFIX}{job_id}"
        self._wip_ref = f"{protocol.HEADS_REF_PREFIX}{self._wip_branch}"
        self._lock = asyncio.Lock()
        self._base = ""
        self._pushed = ""
        self._last_push = float("-inf")
        self._timer: asyncio.Task[None] | None = None

    @property
    def workdir(self) -> Path:
        return self._workdir

    @property
    def base(self) -> str:
        return self._base

    @property
    def pushed(self) -> str:
        return self._pushed

    async def _git(self, *argv: str, extra_env: dict[str, str] | None = None) -> str:
        out = await self._mirror.runner.run(
            *argv, git_dir=self._git_dir, work_tree=self._workdir, extra_env=extra_env
        )
        return out.decode().strip()

    def _author_env(self) -> dict[str, str]:
        return {
            "GIT_AUTHOR_NAME": self._author.name,
            "GIT_AUTHOR_EMAIL": self._author.email,
            "GIT_COMMITTER_NAME": self._author.name,
            "GIT_COMMITTER_EMAIL": self._author.email,
        }

    async def _head(self) -> str:
        try:
            return await self._git("rev-parse", "--verify", "-q", f"{HEAD_REF}^{{commit}}")
        except GitError:
            return ""

    async def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        try:
            await self._git("merge-base", "--is-ancestor", ancestor, descendant)
        except GitError:
            return False
        return True

    async def open(self) -> str:
        async with self._lock:
            await self._mirror.fetch()
            self._base = await self._mirror.resolve(f"{protocol.BASE_BRANCH_PREFIX}{self._job_id}")
            try:
                hub_tip = await self._mirror.resolve(self._wip_branch)
            except GitError:
                hub_tip = ""
            if not (self._git_dir / "HEAD").exists():
                self._git_dir.parent.mkdir(parents=True, exist_ok=True)
                await self._mirror.runner.run("init", "--bare", "-q", git_dir=self._git_dir)
                await self._git("config", "core.bare", "false")
            self._workdir.mkdir(parents=True, exist_ok=True)
            wanted = [self._base, *([hub_tip] if hub_tip else [])]
            await self._git("fetch", "-q", str(self._mirror.git_dir), *wanted)
            await self._git("update-ref", BASE_REF, self._base)
            await self._git("symbolic-ref", HEAD_REF, self._wip_ref)
            head = await self._head()
            floor = hub_tip or self._base
            if not (head and await self._is_ancestor(floor, head)):
                await self._git("update-ref", self._wip_ref, floor)
                await self._git("reset", "-q", "--hard")
                await self._git("clean", "-fdxq")
            self._pushed = hub_tip
            return floor

    async def _commit(self, message: str, allow_empty: bool) -> str | None:
        await self._git("add", "-A")
        if not allow_empty and not await self._git("status", "--porcelain"):
            return None
        subject = " ".join(message.split())[:SUBJECT_LIMIT] or "изменение"
        args = ["commit", "-q", "--no-gpg-sign", "-m", subject]
        if allow_empty:
            args.append("--allow-empty")
        await self._git(*args, extra_env=self._author_env())
        return await self._head()

    async def _push(self) -> bool:
        head = await self._head()
        if not head or head == (self._pushed or self._base):
            return False
        await self._mirror.runner.run(
            "push",
            "-q",
            self._mirror.url,
            f"{head}:{self._wip_ref}",
            git_dir=self._git_dir,
            work_tree=self._workdir,
        )
        self._pushed = head
        self._last_push = self._clock()
        return True

    def _cancel_timer(self) -> None:
        if self._timer is not None and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None

    async def on_change(self, description: str) -> None:
        async with self._lock:
            if await self._commit(description, allow_empty=False) is None:
                return
            wait = self._interval - (self._clock() - self._last_push)
            if wait <= 0:
                self._cancel_timer()
                await self._push_quietly()
            elif self._timer is None:
                self._timer = asyncio.create_task(self._push_later(wait))

    async def _push_quietly(self) -> None:
        try:
            await self._push()
        except GitError as error:
            log.warning("push wip/%s отложен: %s", self._job_id, error)

    async def _push_later(self, wait: float) -> None:
        await asyncio.sleep(wait)
        async with self._lock:
            self._timer = None
            await self._push_quietly()

    async def flush(self) -> bool:
        async with self._lock:
            self._cancel_timer()
            return await self._push()

    async def milestone(self, title: str) -> str:
        async with self._lock:
            self._cancel_timer()
            sha = await self._commit(f"{MILESTONE_PREFIX}{title}", allow_empty=True)
            await self._push()
            return sha or ""

    async def _milestones(self) -> list[str]:
        out = await self._git("log", "--reverse", "--format=%s", f"{self._base}..{HEAD_REF}")
        return [
            line[len(MILESTONE_PREFIX) :]
            for line in out.splitlines()
            if line.startswith(MILESTONE_PREFIX)
        ]

    async def _compose(self, headline: str) -> str:
        lines = [
            headline.strip() or f"Задание {self._job_id}",
            "",
            f"Задание: {self._job_id}",
            f"Автор: {self._author.name}",
        ]
        milestones = await self._milestones()
        if milestones:
            lines += ["", "Вехи:", *(f"- {title}" for title in milestones)]
        return "\n".join(lines) + "\n"

    async def _squash(self, headline: str) -> str | None:
        await self._commit(LEFTOVER_MESSAGE, allow_empty=False)
        await self._push()
        tree = await self._git("rev-parse", f"{HEAD_REF}^{{tree}}")
        if tree == await self._git("rev-parse", f"{self._base}^{{tree}}"):
            return None
        message = await self._compose(headline)
        sha = await self._git(
            "commit-tree", tree, "-p", self._base, "-m", message, extra_env=self._author_env()
        )
        await self._mirror.runner.run(
            "push",
            "-q",
            self._mirror.url,
            f"+{sha}:{protocol.HEADS_REF_PREFIX}{self._agent_branch}",
            git_dir=self._git_dir,
            work_tree=self._workdir,
        )
        return sha

    async def _drop_wip(self) -> None:
        if self._pushed:
            await self._mirror.delete_branch(self._wip_branch)
            self._pushed = ""
        await asyncio.to_thread(shutil.rmtree, self._git_dir, True)

    async def _close(self, headline: str) -> str | None:
        async with self._lock:
            self._cancel_timer()
            sha = await self._squash(headline)
            await self._drop_wip()
            return sha

    async def finish(self, summary: str) -> str | None:
        return await self._close(summary)

    async def abandon(self, reason: str) -> str | None:
        return await self._close(f"{ABANDONED_PREFIX}{reason}")

    async def aclose(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer
