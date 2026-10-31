from __future__ import annotations

import json
from itertools import count
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.app import build_hub_app
from sa_home_bot.devlab.hub.store import HubStore
from tests.unit.test_devlab_git import arun_git, auth, run_git

PROJECT = "demo"
BASE = "base/x"
MODEL = "glm"


@pytest.fixture
async def hub(tmp_path):
    (tmp_path / "hub").mkdir()
    store = HubStore(tmp_path / "hub")
    await store.open()
    async with TestClient(TestServer(build_hub_app(store))) as client:
        yield client, store
    await store.close()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class Mcp:
    def __init__(self, client: TestClient, token: str) -> None:
        self._client = client
        self._headers = bearer(token)
        self._ids = count(1)

    async def call(self, tool: str, **arguments):
        body = {
            "jsonrpc": protocol.MCP_JSONRPC,
            "id": next(self._ids),
            "method": protocol.MCP_METHOD_TOOLS_CALL,
            "params": {"name": tool, "arguments": arguments},
        }
        resp = await self._client.post(protocol.HTTP_MCP_PATH, json=body, headers=self._headers)
        result = (await resp.json())["result"]
        text = result["content"][0]["text"]
        assert not result["isError"], text
        return json.loads(text)


async def executor_call(client: TestClient, token: str, path: str, **body):
    resp = await client.post(path, json=body, headers=bearer(token))
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def test_job_lifecycle_through_hub(hub, tmp_path):
    client, store = hub
    client_token = await store.issue_token("laptop", protocol.ROLE_CLIENT)
    executor_token = await store.issue_token("pc", protocol.ROLE_EXECUTOR)
    mcp = Mcp(client, client_token)
    client_auth = auth(client_token)
    executor_auth = auth(executor_token)

    added = await mcp.call(protocol.TOOL_PROJECT_ADD, name=PROJECT)
    url = str(client.make_url(added["git_path"]))

    work = tmp_path / "work"
    work.mkdir()
    run_git(work, "init", "-q", "-b", "master")
    (work / "f.txt").write_text("base")
    run_git(work, "add", "f.txt")
    run_git(work, "commit", "-q", "-m", "init")
    pushed = await arun_git(work, *client_auth, "push", url, f"master:refs/heads/{BASE}")
    assert pushed.returncode == 0, pushed.stderr

    submitted = await mcp.call(
        protocol.TOOL_SUBMIT, project=PROJECT, model=MODEL, task="# task", base_branch=BASE
    )
    job_id = submitted["job"]["id"]
    assert submitted["job"]["status"] == protocol.STATUS_QUEUED

    await executor_call(client, executor_token, protocol.HTTP_HEARTBEAT_PATH)
    claimed = await executor_call(client, executor_token, protocol.HTTP_CLAIM_PATH, models=[MODEL])
    assert claimed[protocol.FIELD_JOB]["id"] == job_id
    assert claimed[protocol.FIELD_JOB]["base_branch"] == BASE
    status = await mcp.call(protocol.TOOL_STATUS, id=job_id)
    assert status["status"] == protocol.STATUS_RUNNING

    wip = f"{protocol.WIP_BRANCH_PREFIX}{job_id}"
    agent = f"{protocol.AGENT_BRANCH_PREFIX}{job_id}"
    exec_dir = tmp_path / "exec"
    cloned = await arun_git(tmp_path, *executor_auth, "clone", "-q", "-b", BASE, url, str(exec_dir))
    assert cloned.returncode == 0, cloned.stderr
    await arun_git(exec_dir, "checkout", "-q", "-b", wip)
    for step in ("one", "two"):
        (exec_dir / f"{step}.txt").write_text(step)
        await arun_git(exec_dir, "add", f"{step}.txt")
        await arun_git(exec_dir, "commit", "-q", "-m", step)
        pushed = await arun_git(exec_dir, *executor_auth, "push", "origin", wip)
        assert pushed.returncode == 0, pushed.stderr
    await executor_call(
        client,
        executor_token,
        protocol.HTTP_POST_PATH,
        job_id=job_id,
        kind=protocol.MSG_STEP_DONE,
        payload={protocol.PAYLOAD_TEXT: "шаг", protocol.PAYLOAD_TOKENS: 10},
    )
    await executor_call(
        client,
        executor_token,
        protocol.HTTP_POST_PATH,
        job_id=job_id,
        kind=protocol.MSG_QUESTION,
        payload={protocol.PAYLOAD_TEXT: "как назвать?"},
    )
    status = await mcp.call(protocol.TOOL_STATUS, id=job_id)
    assert status["status"] == protocol.STATUS_WAITING_OWNER

    inbox = await mcp.call(protocol.TOOL_INBOX)
    kinds = [m["kind"] for m in inbox[protocol.FIELD_MESSAGES]]
    assert kinds == [protocol.MSG_STEP_DONE, protocol.MSG_QUESTION]
    answered = await mcp.call(protocol.TOOL_ANSWER, id=job_id, text="назови two")
    assert answered["status"] == protocol.STATUS_RUNNING

    polled = await executor_call(client, executor_token, protocol.HTTP_POLL_PATH, since=0)
    (reply,) = polled[protocol.FIELD_MESSAGES]
    assert reply["kind"] == protocol.MSG_ANSWER
    assert reply["payload"][protocol.PAYLOAD_TEXT] == "назови two"

    await arun_git(exec_dir, "checkout", "-q", "-b", agent, f"origin/{BASE}")
    merged = await arun_git(exec_dir, "merge", "--squash", wip)
    assert merged.returncode == 0, merged.stderr
    await arun_git(exec_dir, "commit", "-q", "-m", "squash")
    pushed = await arun_git(exec_dir, *executor_auth, "push", "origin", agent)
    assert pushed.returncode == 0, pushed.stderr
    deleted = await arun_git(exec_dir, *executor_auth, "push", "origin", f":refs/heads/{wip}")
    assert deleted.returncode == 0, deleted.stderr
    await executor_call(
        client,
        executor_token,
        protocol.HTTP_POST_PATH,
        job_id=job_id,
        kind=protocol.MSG_DONE,
        payload={protocol.PAYLOAD_TEXT: "готово"},
    )

    inbox = await mcp.call(protocol.TOOL_INBOX, since=inbox[protocol.FIELD_CURSOR])
    assert [m["kind"] for m in inbox[protocol.FIELD_MESSAGES]] == [protocol.MSG_DONE]
    status = await mcp.call(protocol.TOOL_STATUS, id=job_id)
    assert status["status"] == protocol.STATUS_DONE
    assert status["tokens"] == 10
    acked = await mcp.call(protocol.TOOL_ACK, upto=inbox[protocol.FIELD_CURSOR])
    assert acked["acked"] == 3

    review = tmp_path / "review"
    fetched = await arun_git(tmp_path, *client_auth, "clone", "-q", "-b", agent, url, str(review))
    assert fetched.returncode == 0, fetched.stderr
    for name, content in (("f.txt", "base"), ("one.txt", "one"), ("two.txt", "two")):
        assert (Path(review) / name).read_text() == content
    heads = await arun_git(review, "ls-remote", "--heads", "origin")
    assert wip not in heads.stdout
    removed = await arun_git(review, *client_auth, "push", "origin", f":refs/heads/{agent}")
    assert removed.returncode == 0, removed.stderr
