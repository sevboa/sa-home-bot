from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sa_home_bot.config import Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.connect import TOKEN_PATH, hub_url
from sa_home_bot.devlab.executor.spec import SPEC_FILE, SpecError, parse_spec

GIT_TIMEOUT_S = 300
BRANCH_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
WAIT_DONE_STATUSES = (*protocol.TERMINAL_STATUSES, protocol.STATUS_WAITING_OWNER)
WAIT_INTERVAL_S = 20.0
WAIT_TIMEOUT_S = 1800.0
WAIT_REQUEST_TIMEOUT_S = 15
WAIT_MAX_HUB_FAILURES = 3
EXIT_TIMEOUT = 124
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


def hub_env(token: str) -> dict[str, str]:
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
    }


def hub_remote(url: str, project: str) -> str:
    return f"{url}{protocol.HTTP_GIT_PREFIX}{project}.git"


def push_snapshot(root: Path, url: str, token: str, project: str, commit: str, branch: str) -> None:
    _git(
        root,
        "push",
        "--no-verify",
        "--quiet",
        hub_remote(url, project),
        f"{commit}:refs/heads/{branch}",
        env=hub_env(token),
        secret=token,
    )


def check_branch_name(branch: str, prefix: str) -> str:
    name = branch.removeprefix(prefix)
    if not BRANCH_NAME.fullmatch(name):
        raise SnapshotError(f"имя {branch!r}: допустимы a-z, 0-9, точка, дефис, подчёркивание")
    return prefix + name


def push_base(root: Path, url: str, token: str, project: str, name: str, ref: str) -> str:
    branch = check_branch_name(name, protocol.BASE_BRANCH_PREFIX)
    commit = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()
    _git(
        root,
        "push",
        "--no-verify",
        "--quiet",
        "--force",
        hub_remote(url, project),
        f"{commit}:refs/heads/{branch}",
        env=hub_env(token),
        secret=token,
    )
    return branch


def fetch_agent(root: Path, url: str, token: str, project: str, job_id: str) -> str:
    branch = check_branch_name(job_id, protocol.AGENT_BRANCH_PREFIX)
    local = f"refs/devlab/{branch}"
    _git(
        root,
        "fetch",
        "--quiet",
        hub_remote(url, project),
        f"+refs/heads/{branch}:{local}",
        env=hub_env(token),
        secret=token,
    )
    return local


def drop_branches(root: Path, url: str, token: str, project: str, branches: list[str]) -> None:
    refspecs = []
    for branch in branches:
        prefix = next(
            (
                p
                for p in (protocol.AGENT_BRANCH_PREFIX, protocol.BASE_BRANCH_PREFIX)
                if branch.startswith(p)
            ),
            "",
        )
        if not prefix:
            raise SnapshotError(f"удалять можно только agent/* и base/*, не {branch!r}")
        refspecs.append(f":refs/heads/{check_branch_name(branch, prefix)}")
    _git(
        root,
        "push",
        "--no-verify",
        "--quiet",
        hub_remote(url, project),
        *refspecs,
        env=hub_env(token),
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


def branch_command(
    settings: Settings,
    action: str,
    names: list[str],
    *,
    project: str = "",
    ref: str = "HEAD",
    cwd: Path | None = None,
    token_path: str = TOKEN_PATH,
) -> int:
    try:
        if not names:
            raise SnapshotError("не указано имя ветки")
        root = repo_root(cwd or Path.cwd())
        name = project_name(root, project)
        token = read_token(token_path)
        url = hub_url(settings)
        if action == "base":
            result = [push_base(root, url, token, name, names[0], ref)]
        elif action == "fetch":
            result = [fetch_agent(root, url, token, name, names[0])]
        else:
            drop_branches(root, url, token, name, names)
            result = []
    except SnapshotError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"проект {name}", file=sys.stderr)
    for line in result:
        print(line)
    return 0


class HubUnavailable(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HUB_OPENER = urllib.request.build_opener(NoRedirect)


def call_status(url: str, token: str, job_id: str) -> dict[str, Any]:
    body = {
        "jsonrpc": protocol.MCP_JSONRPC,
        "id": 1,
        "method": protocol.MCP_METHOD_TOOLS_CALL,
        "params": {"name": protocol.TOOL_STATUS, "arguments": {"id": job_id}},
    }
    request = urllib.request.Request(
        url + protocol.HTTP_MCP_PATH,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with HUB_OPENER.open(request, timeout=WAIT_REQUEST_TIMEOUT_S) as resp:
            reply = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise SnapshotError(
                f"хаб отклонил токен (HTTP {exc.code}): `nodectl devlab connect`"
            ) from exc
        raise HubUnavailable(f"HTTP {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise HubUnavailable(type(exc).__name__) from exc
    if not isinstance(reply, dict):
        raise HubUnavailable("ответ хаба не объект")
    if protocol.FIELD_ERROR in reply:
        error = reply[protocol.FIELD_ERROR]
        message = error.get("message") if isinstance(error, dict) else None
        raise SnapshotError(f"хаб: {message or 'ошибка JSON-RPC'}")
    result = reply.get("result") or {}
    try:
        text = result["content"][0]["text"]
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise HubUnavailable("неожиданный ответ хаба") from exc
    if not isinstance(text, str):
        raise HubUnavailable("неожиданный ответ хаба")
    if result.get("isError") is True:
        raise SnapshotError(f"хаб: {text}")
    try:
        view = json.loads(text)
    except ValueError as exc:
        raise HubUnavailable("ответ хаба не JSON") from exc
    if not isinstance(view, dict):
        raise HubUnavailable("ответ хаба не объект")
    return view


def wait_summary(job_id: str, view: dict[str, Any]) -> str:
    line: dict[str, Any] = {"id": job_id, "status": view.get("status") or "unknown"}
    if view.get("result") is not None:
        line["result"] = view["result"]
    return json.dumps(line, ensure_ascii=False)


def wait_command(
    settings: Settings,
    job_id: str,
    *,
    interval: float = WAIT_INTERVAL_S,
    timeout: float = WAIT_TIMEOUT_S,
    token_path: str = TOKEN_PATH,
    poll: Callable[[str, str, str], dict[str, Any]] = call_status,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    try:
        if not job_id:
            raise SnapshotError("не указан id задания")
        if interval <= 0 or timeout <= 0:
            raise SnapshotError("--interval и --timeout должны быть больше нуля")
        token = read_token(token_path)
        url = hub_url(settings)
    except SnapshotError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    deadline = clock() + timeout
    failures = 0
    last: dict[str, Any] = {}
    while True:
        try:
            last = poll(url, token, job_id)
            failures = 0
        except HubUnavailable as exc:
            failures += 1
            if failures >= WAIT_MAX_HUB_FAILURES:
                print(f"Ошибка: хаб недоступен {failures} опроса подряд: {exc}", file=sys.stderr)
                return 1
        except SnapshotError as exc:
            print(f"Ошибка: {str(exc).replace(token, '***')}", file=sys.stderr)
            return 1
        else:
            if last.get("status") in WAIT_DONE_STATUSES:
                print(wait_summary(job_id, last))
                return 0
        remaining = deadline - clock()
        if remaining <= 0:
            print(f"Тайм-аут {timeout:g} с: задание не завершилось", file=sys.stderr)
            print(wait_summary(job_id, last))
            return EXIT_TIMEOUT
        sleep(min(interval, remaining))
