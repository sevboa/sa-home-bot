from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.refpolicy import ZERO_SHA
from sa_home_bot.utils.process import run

GIT_TIMEOUT_S = 60.0
HOOK_NAME = "pre-receive"
HOOK_MODULE = "sa_home_bot.devlab.hub.refpolicy"
DEFAULT_BRANCH = "master"
COMMITTER = "devlab-hub"
COMMITTER_EMAIL = "devlab-hub@localhost"
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


async def branch_exists(git_root: Path, project: str, branch: str) -> bool:
    repo = repo_path(git_root, project)
    if not repo.is_dir():
        return False
    ref = protocol.HEADS_REF_PREFIX + branch
    result = await run(
        ["git", "--git-dir", str(repo), "rev-parse", "--verify", "--quiet", ref],
        timeout=GIT_TIMEOUT_S,
    )
    return result.returncode == 0


async def ref_exists(git_root: Path, project: str, ref: str) -> bool:
    repo = repo_path(git_root, project)
    if not repo.is_dir() or ref.startswith("-"):
        return False
    result = await run(
        ["git", "--git-dir", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        timeout=GIT_TIMEOUT_S,
    )
    return result.returncode == 0


def _git_dir(git_root: Path, project: str) -> list[str]:
    return ["git", "--git-dir", str(repo_path(git_root, project))]


def _ref(branch: str) -> str:
    return f"{protocol.HEADS_REF_PREFIX}{branch}"


async def _out(argv: list[str], what: str, env: dict[str, str] | None = None) -> str:
    result = await run(argv, timeout=GIT_TIMEOUT_S, env=env)
    if result.returncode != 0:
        raise HubError(f"git {what}: {result.stderr_text or result.stdout_text.strip()}")
    return result.stdout_text.strip()


async def rev(git_root: Path, project: str, branch: str) -> str:
    return await _out(
        [*_git_dir(git_root, project), "rev-parse", "--verify", _ref(branch)],
        "rev-parse",
    )


async def create_branch(git_root: Path, project: str, branch: str, start: str) -> str:
    sha = await rev(git_root, project, start)
    await _out(
        [*_git_dir(git_root, project), "update-ref", _ref(branch), sha, ZERO_SHA],
        "update-ref",
    )
    return sha


async def delete_branch(git_root: Path, project: str, branch: str) -> None:
    await run(
        [*_git_dir(git_root, project), "update-ref", "-d", _ref(branch)],
        timeout=GIT_TIMEOUT_S,
    )


async def tests_listing(git_root: Path, project: str, branch: str) -> list[str]:
    out = await _out(
        [*_git_dir(git_root, project), "ls-tree", "-r", "--name-only", _ref(branch), "tests/"],
        "ls-tree",
    )
    return out.splitlines()


async def _is_ancestor(git_root: Path, project: str, older: str, newer: str) -> bool:
    result = await run(
        [*_git_dir(git_root, project), "merge-base", "--is-ancestor", older, newer],
        timeout=GIT_TIMEOUT_S,
    )
    return result.returncode == 0


async def merge_branch(
    git_root: Path, project: str, target: str, source: str, message: str
) -> str:
    old = await rev(git_root, project, target)
    new = await rev(git_root, project, source)
    if old == new or await _is_ancestor(git_root, project, new, old):
        return old
    if not await _is_ancestor(git_root, project, old, new):
        tree_run = await run(
            [*_git_dir(git_root, project), "merge-tree", "--write-tree", old, new],
            timeout=GIT_TIMEOUT_S,
        )
        if tree_run.returncode != 0:
            raise HubError(f"слияние {source} в {target}: конфликт")
        tree = tree_run.stdout_text.splitlines()[0]
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": COMMITTER,
            "GIT_AUTHOR_EMAIL": COMMITTER_EMAIL,
            "GIT_COMMITTER_NAME": COMMITTER,
            "GIT_COMMITTER_EMAIL": COMMITTER_EMAIL,
        }
        new = await _out(
            [
                *_git_dir(git_root, project),
                "commit-tree",
                tree,
                "-p",
                old,
                "-p",
                new,
                "-m",
                message,
            ],
            "commit-tree",
            env,
        )
    await _out(
        [*_git_dir(git_root, project), "update-ref", _ref(target), new, old],
        "update-ref",
    )
    return new


async def list_wip_branches(git_root: Path, project: str) -> list[str]:
    repo = repo_path(git_root, project)
    if not repo.is_dir():
        return []
    result = await run(
        [
            "git",
            "--git-dir",
            str(repo),
            "for-each-ref",
            "--format=%(refname:short)",
            f"{protocol.HEADS_REF_PREFIX}{protocol.WIP_BRANCH_PREFIX}",
        ],
        timeout=GIT_TIMEOUT_S,
    )
    if result.returncode != 0:
        return []
    branches = []
    for line in result.stdout_text.splitlines():
        branch = line.strip()
        if branch.startswith(protocol.WIP_BRANCH_PREFIX):
            branches.append(branch[len(protocol.WIP_BRANCH_PREFIX) :])
    return branches
