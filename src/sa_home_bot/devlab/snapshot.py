from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sa_home_bot.config import Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.connect import TOKEN_PATH, hub_url
from sa_home_bot.devlab.executor.spec import SPEC_FILE, SpecError, parse_spec

GIT_TIMEOUT_S = 300
SNAPSHOT_MESSAGE = "devlab snapshot"
SNAPSHOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "devlab",
    "GIT_AUTHOR_EMAIL": "devlab@localhost",
    "GIT_COMMITTER_NAME": "devlab",
    "GIT_COMMITTER_EMAIL": "devlab@localhost",
}


class SnapshotError(Exception):
    pass


def _git(
    cwd: Path, *argv: str, env: dict[str, str] | None = None, secret: str = ""
) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    try:
        result = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            env=full_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError(f"git {argv[0]}: {type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if secret:
            detail = detail.replace(secret, "***")
        raise SnapshotError(f"git {argv[0]} завершился с кодом {result.returncode}: {detail}")
    return result


def repo_root(cwd: Path) -> Path:
    try:
        return Path(_git(cwd, "rev-parse", "--show-toplevel").stdout.strip())
    except SnapshotError as exc:
        raise SnapshotError("текущий каталог не git-репозиторий") from exc


def head_commit(root: Path) -> str:
    try:
        return _git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    except SnapshotError as exc:
        raise SnapshotError("в репозитории нет коммитов: снимок строится от HEAD") from exc


def project_name(root: Path, override: str = "") -> str:
    if override:
        return override
    spec_path = root / SPEC_FILE
    if spec_path.is_file():
        try:
            declared = parse_spec(spec_path.read_bytes()).project
        except SpecError as exc:
            raise SnapshotError(str(exc)) from exc
        if declared:
            return declared
    return root.name


def client_name(settings: Settings, override: str = "") -> str:
    return override or settings.node.id or socket.gethostname()


def create_snapshot(root: Path) -> str:
    head = head_commit(root)
    with tempfile.TemporaryDirectory(prefix="devlab-snapshot-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index"), **SNAPSHOT_IDENTITY}
        _git(root, "read-tree", head, env=env)
        _git(root, "add", "-A", env=env)
        tree = _git(root, "write-tree", env=env).stdout.strip()
        return _git(
            root, "commit-tree", tree, "-p", head, "-m", SNAPSHOT_MESSAGE, env=env
        ).stdout.strip()


def branch_name(client: str, commit: str) -> str:
    return f"{protocol.TEST_BRANCH_PREFIX}{client}/{int(time.time())}-{commit[:7]}"


def push_snapshot(root: Path, url: str, token: str, project: str, commit: str, branch: str) -> None:
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
    }
    remote = f"{url}{protocol.HTTP_GIT_PREFIX}{project}.git"
    _git(
        root,
        "push",
        "--no-verify",
        "--quiet",
        remote,
        f"{commit}:refs/heads/{branch}",
        env=env,
        secret=token,
    )


def read_token(token_path: str) -> str:
    path = Path(token_path).expanduser()
    try:
        token = path.read_text().strip()
    except OSError as exc:
        raise SnapshotError(f"токен {path} не прочитан: выполни `nodectl devlab connect`") from exc
    if not token:
        raise SnapshotError(f"токен {path} пуст: выполни `nodectl devlab connect`")
    return token


def snapshot(
    settings: Settings,
    *,
    project: str = "",
    client: str = "",
    cwd: Path | None = None,
    token_path: str = TOKEN_PATH,
) -> int:
    try:
        root = repo_root(cwd or Path.cwd())
        name = project_name(root, project)
        owner = client_name(settings, client)
        token = read_token(token_path)
        commit = create_snapshot(root)
        branch = branch_name(owner, commit)
        push_snapshot(root, hub_url(settings), token, name, commit, branch)
    except SnapshotError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"проект {name}, коммит {commit[:12]}", file=sys.stderr)
    print(branch)
    return 0
