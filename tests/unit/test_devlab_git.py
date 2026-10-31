from __future__ import annotations

import asyncio
import gzip
import os
import subprocess
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import git_http, http, refpolicy, repos
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.store import HubStore
from tests.unit.support.fakes import FakeNodes

PROJECT = "demo"
ZERO = refpolicy.ZERO_SHA
SHA = "a" * 40


@pytest.fixture
async def store(tmp_path):
    (tmp_path / "hub").mkdir()
    hub_store = HubStore(tmp_path / "hub")
    await hub_store.open()
    await hub_store.add_project(PROJECT)
    yield hub_store
    await hub_store.close()


@pytest.fixture
async def server(store):
    app = http.build_app(store, FakeNodes())
    git_http.add_routes(app)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


@pytest.fixture
async def tokens(store):
    return {
        protocol.ROLE_CLIENT: await store.issue_token("laptop", protocol.ROLE_CLIENT),
        protocol.ROLE_EXECUTOR: await store.issue_token("pc", protocol.ROLE_EXECUTOR),
    }


def run_git(cwd: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def arun_git(cwd: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(run_git, cwd, *argv)


def remote_url(server: TestClient) -> str:
    return str(server.make_url(f"/git/{PROJECT}.git"))


def auth(token: str) -> tuple[str, ...]:
    return ("-c", f"http.extraHeader=Authorization: Bearer {token}")


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    run_git(path, "init", "-q", "-b", "master")
    (path / "f.txt").write_text("x")
    run_git(path, "add", "f.txt")
    run_git(path, "commit", "-q", "-m", "init")
    return path


@pytest.mark.parametrize(
    ("role", "ref", "old", "new", "allowed"),
    [
        (protocol.ROLE_EXECUTOR, "refs/heads/wip/x", ZERO, SHA, True),
        (protocol.ROLE_EXECUTOR, "refs/heads/wip/x", SHA, SHA, True),
        (protocol.ROLE_EXECUTOR, "refs/heads/wip/x", SHA, ZERO, True),
        (protocol.ROLE_EXECUTOR, "refs/heads/agent/x", ZERO, SHA, True),
        (protocol.ROLE_EXECUTOR, "refs/heads/agent/x", SHA, SHA, True),
        (protocol.ROLE_EXECUTOR, "refs/heads/agent/x", SHA, ZERO, False),
        (protocol.ROLE_EXECUTOR, "refs/heads/base/x", ZERO, SHA, False),
        (protocol.ROLE_EXECUTOR, "refs/heads/master", SHA, SHA, False),
        (protocol.ROLE_EXECUTOR, "refs/heads/wip/", ZERO, SHA, False),
        (protocol.ROLE_EXECUTOR, "refs/tags/wip/x", ZERO, SHA, False),
        (protocol.ROLE_CLIENT, "refs/heads/base/x", ZERO, SHA, True),
        (protocol.ROLE_CLIENT, "refs/heads/base/x", SHA, SHA, True),
        (protocol.ROLE_CLIENT, "refs/heads/base/x", SHA, ZERO, True),
        (protocol.ROLE_CLIENT, "refs/heads/agent/x", SHA, ZERO, True),
        (protocol.ROLE_CLIENT, "refs/heads/agent/x", ZERO, SHA, False),
        (protocol.ROLE_CLIENT, "refs/heads/wip/x", ZERO, SHA, False),
        (protocol.ROLE_CLIENT, "refs/heads/main", SHA, SHA, False),
        (protocol.ROLE_CLIENT, "refs/heads/master", SHA, ZERO, False),
        (protocol.ROLE_GATEWAY, "refs/heads/base/x", ZERO, SHA, False),
        ("", "refs/heads/base/x", ZERO, SHA, False),
    ],
)
def test_check_ref(role, ref, old, new, allowed):
    assert (refpolicy.check_ref(role, ref, old, new) is None) is allowed


def test_check_updates_reports_each_bad_line():
    lines = [f"{ZERO} {SHA} refs/heads/base/a", f"{ZERO} {SHA} refs/heads/master", "junk"]
    assert len(refpolicy.check_updates(protocol.ROLE_CLIENT, lines)) == 2


def test_hook_script_calls_shared_module():
    script = repos.hook_script("/opt/my python/bin/python")
    assert script.startswith("#!/bin/sh\n")
    assert "'/opt/my python/bin/python' -m sa_home_bot.devlab.hub.refpolicy" in script


@pytest.mark.parametrize("name", ["", "../x", "a/b", "A", "a.b", "-x", "x y"])
async def test_add_project_rejects_bad_names(store, name):
    with pytest.raises(HubError):
        await store.add_project(name)


async def test_add_project_creates_repo_with_hook(store):
    repo = repos.repo_path(store.git_root, PROJECT)
    hook = repo / "hooks" / repos.HOOK_NAME
    assert hook.is_file() and os.access(hook, os.X_OK)
    with pytest.raises(HubError):
        await store.add_project(PROJECT)
    assert await store.queue.has_project(PROJECT)


async def test_fetch_without_token_is_401(server):
    resp = await server.get(f"/git/{PROJECT}.git/info/refs?service=git-upload-pack")
    assert resp.status == 401


async def test_gateway_cannot_read_repos(server, store):
    token = await store.issue_token("jeeves", protocol.ROLE_GATEWAY)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/git/{PROJECT}.git/info/refs?service=git-upload-pack"
    resp = await server.get(url, headers=headers)
    assert resp.status == 403


async def test_unknown_project_is_404(server, tokens):
    headers = {"Authorization": f"Bearer {tokens[protocol.ROLE_CLIENT]}"}
    resp = await server.get("/git/nope.git/info/refs?service=git-upload-pack", headers=headers)
    assert resp.status == 404


async def test_client_pushes_base_and_clones(server, tokens, work, tmp_path):
    url = remote_url(server)
    client = auth(tokens[protocol.ROLE_CLIENT])
    pushed = await arun_git(work, *client, "push", url, "master:refs/heads/base/x")
    assert pushed.returncode == 0, pushed.stderr
    target = tmp_path / "clone"
    cloned = await arun_git(tmp_path, *client, "clone", "-q", "-b", "base/x", url, str(target))
    assert cloned.returncode == 0, cloned.stderr
    assert (target / "f.txt").read_text() == "x"


async def test_executor_branches_lifecycle(server, tokens, work):
    url = remote_url(server)
    client = auth(tokens[protocol.ROLE_CLIENT])
    executor = auth(tokens[protocol.ROLE_EXECUTOR])
    assert (await arun_git(work, *client, "push", url, "master:refs/heads/base/x")).returncode == 0
    for ref in ("wip/x", "agent/x"):
        pushed = await arun_git(work, *executor, "push", url, f"master:refs/heads/{ref}")
        assert pushed.returncode == 0, pushed.stderr
    deleted = await arun_git(work, *executor, "push", url, ":refs/heads/wip/x")
    assert deleted.returncode == 0, deleted.stderr
    refused = await arun_git(work, *executor, "push", url, ":refs/heads/agent/x")
    assert refused.returncode != 0
    accepted = await arun_git(work, *client, "push", url, ":refs/heads/agent/x")
    assert accepted.returncode == 0, accepted.stderr


@pytest.mark.parametrize(
    ("role", "ref"),
    [
        (protocol.ROLE_CLIENT, "wip/x"),
        (protocol.ROLE_EXECUTOR, "base/x"),
        (protocol.ROLE_CLIENT, "master"),
        (protocol.ROLE_EXECUTOR, "master"),
    ],
)
async def test_push_refused_with_reason(server, tokens, work, role, ref):
    spec = f"master:refs/heads/{ref}"
    pushed = await arun_git(work, *auth(tokens[role]), "push", remote_url(server), spec)
    assert pushed.returncode != 0
    assert "devlab: отклонено" in pushed.stderr


def pkt(text: bytes) -> bytes:
    return b"%04x" % (len(text) + 4) + text


async def test_gzip_request_body_is_accepted(server, tokens, work):
    token = tokens[protocol.ROLE_CLIENT]
    await arun_git(work, *auth(token), "push", remote_url(server), "master:refs/heads/base/x")
    sha = run_git(work, "rev-parse", "HEAD").stdout.strip().encode()
    body = pkt(b"want " + sha + b"\n") + b"0000" + pkt(b"done\n")
    resp = await server.post(
        f"/git/{PROJECT}.git/git-upload-pack",
        data=gzip.compress(body),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-git-upload-pack-request",
            "Content-Encoding": "gzip",
        },
    )
    payload = await resp.read()
    assert resp.status == 200
    assert b"PACK" in payload
