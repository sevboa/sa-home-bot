from __future__ import annotations

import asyncio
import json
import re
import tomllib
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.config import Settings
from sa_home_bot.devlab import connect as devlab_connect
from sa_home_bot.devlab import protocol, snapshot
from sa_home_bot.devlab.executor.spec import parse_spec
from sa_home_bot.devlab.hub.app import build_hub_app
from sa_home_bot.devlab.hub.mcp_tools import TOOLS_BY_NAME
from sa_home_bot.devlab.hub.store import HubStore
from tests.unit.support.fakes import FakeNodes
from tests.unit.test_devlab_git import run_git

PROJECT = "demo"
CLIENT = "laptop"
ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "src/sa_home_bot/devlab/skills/devlab/SKILL.md"
TEMPLATES = ROOT / "docs/devlab/devlab-toml.md"


@pytest.fixture
async def hub(tmp_path):
    (tmp_path / "hub").mkdir()
    store = HubStore(tmp_path / "hub")
    await store.open()
    await store.add_project(PROJECT)
    token = await store.issue_token(CLIENT, protocol.ROLE_CLIENT)
    async with TestClient(TestServer(build_hub_app(store, FakeNodes()))) as client:
        yield client, store, token
    await store.close()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    run_git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("one")
    run_git(path, "add", "a.txt")
    run_git(path, "commit", "-q", "-m", "init")
    return path


def settings_for(client: TestClient) -> Settings:
    url = client.make_url("/")
    return Settings.model_validate(
        {
            "swarm": {"token": "x"},
            "node": {"id": CLIENT},
            "devlab_hub": {"hub_node": url.host, "port": url.port},
        }
    )


async def run_branch(hub, repo, tmp_path, action, names, **kwargs):
    client, _, token = hub
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    return await asyncio.to_thread(
        snapshot.branch_command,
        settings_for(client),
        action,
        names,
        project=PROJECT,
        cwd=repo,
        token_path=str(token_file),
        **kwargs,
    )


async def test_base_fetch_drop_cycle(hub, repo, tmp_path, capsys, monkeypatch):
    _, store, token = hub
    argvs = []
    original = snapshot.subprocess.run

    def spy(argv, **kwargs):
        argvs.append(list(argv))
        return original(argv, **kwargs)

    monkeypatch.setattr(snapshot.subprocess, "run", spy)
    bare = store.git_root / f"{PROJECT}.git"

    assert await run_branch(hub, repo, tmp_path, "base", ["task1"]) == 0
    assert capsys.readouterr().out.strip() == "base/task1"
    head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert run_git(bare, "rev-parse", "base/task1").stdout.strip() == head

    run_git(bare, "update-ref", "refs/heads/agent/j1", head)
    assert await run_branch(hub, repo, tmp_path, "fetch", ["j1"]) == 0
    assert capsys.readouterr().out.strip() == "refs/devlab/agent/j1"
    assert run_git(repo, "rev-parse", "refs/devlab/agent/j1").stdout.strip() == head

    assert await run_branch(hub, repo, tmp_path, "drop", ["agent/j1", "base/task1"]) == 0
    refs = run_git(bare, "for-each-ref", "refs/heads").stdout
    assert "agent/j1" not in refs and "base/task1" not in refs
    assert all(token not in part for argv in argvs for part in argv)


async def test_base_uses_given_ref(hub, repo, tmp_path, capsys):
    _, store, _ = hub
    first = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("two")
    run_git(repo, "commit", "-q", "-am", "second")
    assert await run_branch(hub, repo, tmp_path, "base", ["old"], ref=first) == 0
    bare = store.git_root / f"{PROJECT}.git"
    assert run_git(bare, "rev-parse", "base/old").stdout.strip() == first


@pytest.mark.parametrize(
    ("action", "names"),
    [
        ("base", ["Bad Name"]),
        ("base", ["../x"]),
        ("fetch", ["a/b"]),
        ("drop", ["master"]),
        ("drop", ["test/laptop/1"]),
        ("base", []),
    ],
)
async def test_branch_command_rejects_bad_names(hub, repo, tmp_path, capsys, action, names):
    assert await run_branch(hub, repo, tmp_path, action, names) == 1
    assert "Ошибка" in capsys.readouterr().err


async def test_branch_command_errors_hide_token(hub, repo, tmp_path, capsys):
    _, _, token = hub
    assert await run_branch(hub, repo, tmp_path, "fetch", ["nothere"]) == 1
    err = capsys.readouterr().err
    assert "Ошибка" in err
    assert token not in err


def test_branch_command_without_token_points_to_connect(repo, tmp_path, capsys):
    code = snapshot.branch_command(
        Settings.model_validate({"swarm": {"token": "x"}}),
        "base",
        ["x"],
        cwd=repo,
        token_path=str(tmp_path / "none"),
    )
    assert code == 1
    assert "nodectl devlab connect" in capsys.readouterr().err


async def test_connect_installs_both_skills(tmp_path, capsys):
    async def fetch(url, swarm_token):
        return "dlh_token"

    skills = tmp_path / "skills"
    code = await devlab_connect.connect(
        Settings.model_validate({"swarm": {"token": "x"}}),
        token_path=str(tmp_path / "token"),
        skills_dir=str(skills),
        fetch=fetch,
        runner=lambda argv: 0,
        which=lambda name: "/bin/claude",
    )
    assert code == 0
    assert devlab_connect.SKILL_NAMES == ("devlab-test", "devlab")
    for name in devlab_connect.SKILL_NAMES:
        installed = skills / name / devlab_connect.SKILL_FILE
        assert installed.read_text() == devlab_connect.skill_source(name)
        assert installed.read_text().startswith(f"---\nname: {name}\n")


async def run_connect(tmp_path, runner):
    async def fetch(url, swarm_token):
        return "dlh_secret"

    return await devlab_connect.connect(
        Settings.model_validate({"swarm": {"token": "x"}}),
        token_path=str(tmp_path / "token"),
        skills_dir=str(tmp_path / "skills"),
        fetch=fetch,
        runner=runner,
        which=lambda name: "/bin/claude",
    )


async def test_connect_adds_mcp_once_when_server_absent(tmp_path, capsys):
    calls = []

    def runner(argv):
        calls.append(argv[:3])
        return 0

    assert await run_connect(tmp_path, runner) == 0
    assert calls == [["claude", "mcp", "add"]]
    assert "переподключите" not in capsys.readouterr().out


async def test_connect_replaces_existing_server(tmp_path, capsys):
    calls = []

    def runner(argv):
        calls.append(argv[:3])
        return 1 if len(calls) == 1 else 0

    assert await run_connect(tmp_path, runner) == 0
    assert [c[2] for c in calls] == ["add", "remove", "add"]
    out = capsys.readouterr().out
    assert "переподключите devlab" in out
    assert "dlh_secret" not in out


async def test_connect_add_failure_prints_manual_hint(tmp_path, capsys):
    assert await run_connect(tmp_path, lambda argv: 1) == 1
    out = capsys.readouterr().out
    assert "$(cat " in out
    assert "dlh_secret" not in out


@pytest.mark.parametrize("name", devlab_connect.SKILL_NAMES)
def test_repo_skill_symlink_is_the_packaged_one(name):
    link = ROOT / ".claude/skills" / name
    assert link.is_symlink()
    assert (link / "SKILL.md").read_text() == devlab_connect.skill_source(name)


def test_skill_mentions_only_existing_hub_tools():
    mentioned = set(re.findall(r"\bdevlab_[a-z]+(?:_[a-z]+)*", SKILL.read_text()))
    assert mentioned
    assert mentioned <= set(TOOLS_BY_NAME)
    for required in (
        protocol.TOOL_MODELS,
        protocol.TOOL_SUBMIT,
        protocol.TOOL_INBOX,
        protocol.TOOL_ACK,
        protocol.TOOL_ANSWER,
        protocol.TOOL_RESUME,
        protocol.TOOL_STOP,
        protocol.TOOL_RELEASE,
        protocol.TOOL_TEST,
    ):
        assert required in mentioned


def test_skill_and_usage_tell_that_owner_may_answer_from_telegram():
    usage = ROOT / "src/sa_home_bot/devlab/hub/resources/usage.md"
    for text in (SKILL.read_text(), usage.read_text()):
        assert "владелец из Telegram" in text


def test_skill_params_exist_on_tools():
    found = re.findall(r"\b(devlab_[a-z_]+) ((?:[a-z_]+=\S+ ?)+)", SKILL.read_text())
    assert found
    for tool, params in found:
        known = {p.name for p in TOOLS_BY_NAME[tool].params}
        assert {p.split("=")[0] for p in params.split()} <= known, tool


def template_blocks():
    return re.findall(r"```toml\n(.*?)```", TEMPLATES.read_text(), re.S)


def test_templates_parse_with_real_parser():
    blocks = template_blocks()
    assert len(blocks) == 3
    for block in blocks:
        tomllib.loads(block)
        spec = parse_spec(block.encode())
        assert spec.project
        assert spec.test.cmd
        assert spec.agent.rules == ["CLAUDE.md"]
        assert spec.agent.checks
        assert ".devlab.toml" in spec.agent.forbidden_paths


def test_sa_home_bot_spec_uses_same_sections_as_templates():
    own = parse_spec((ROOT / ".devlab.toml").read_bytes())
    template = parse_spec(template_blocks()[0].encode())
    assert own.agent.rules == template.agent.rules
    assert set(own.agent.forbidden_paths) >= set(template.agent.forbidden_paths[:2])


def test_hub_url_on_the_hub_node_uses_the_tailscale_address():
    settings = Settings.model_validate(
        {"swarm": {"token": "x"}, "node": {"id": "alfred"}, "devlab_hub": {"hub_node": "alfred"}}
    )
    assert devlab_connect.hub_url(settings, local_address=lambda: "100.73.52.92") == (
        "http://100.73.52.92:8720"
    )
    assert devlab_connect.hub_url(settings, local_address=lambda: None) == "http://alfred:8720"


def test_hub_url_on_another_node_keeps_the_hub_name():
    settings = Settings.model_validate(
        {"swarm": {"token": "x"}, "node": {"id": "mycraft"}, "devlab_hub": {"hub_node": "alfred"}}
    )

    def forbidden():
        raise AssertionError("tailscale не должен спрашиваться")

    assert devlab_connect.hub_url(settings, local_address=forbidden) == "http://alfred:8720"


def wait_token(tmp_path, token):
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    return str(token_file)


async def queue_test_job(store):
    return (await store.queue.submit_test(CLIENT, PROJECT, "test/laptop/1")).id


async def run_wait(hub, tmp_path, *, token=None, poll=None, **kwargs):
    client, _, real = hub
    sleeps = []
    clock = {"now": 0.0}

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    options = {"sleep": sleep, "clock": lambda: clock["now"]}
    if poll is not None:
        options["poll"] = poll
    code = await asyncio.to_thread(
        snapshot.wait_command,
        settings_for(client),
        kwargs.pop("job_id"),
        token_path=wait_token(tmp_path, token or real),
        **options,
        **kwargs,
    )
    return code, sleeps


async def test_wait_returns_result_after_test_done(hub, tmp_path, capsys):
    _, store, _ = hub
    job_id = await queue_test_job(store)
    polls = []
    real_poll = snapshot.call_status

    def poll(url, tok, jid):
        polls.append(jid)
        if len(polls) == 3:
            asyncio.run_coroutine_threadsafe(finish(store, job_id), loop).result(5)
        return real_poll(url, tok, jid)

    loop = asyncio.get_running_loop()
    code, sleeps = await run_wait(hub, tmp_path, job_id=job_id, poll=poll, interval=7)
    assert code == 0
    assert len(polls) == 3
    assert sleeps == [7, 7]
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert last["id"] == job_id
    assert last["status"] == protocol.STATUS_DONE
    assert last["result"][protocol.PAYLOAD_EXIT_CODE] == 0


async def finish(store, job_id):
    job = await store.queue.claim("pc", [], [protocol.JOB_KIND_TEST])
    assert job is not None and job.id == job_id
    await store.queue.post_from_executor(
        job.id,
        protocol.MSG_TEST_DONE,
        {
            protocol.PAYLOAD_EXIT_CODE: 0,
            protocol.PAYLOAD_DURATION_S: 1.0,
            protocol.PAYLOAD_SUMMARY: dict.fromkeys(protocol.SUMMARY_KEYS, 0),
        },
        executor="pc",
    )


async def test_wait_times_out_with_code_124(hub, tmp_path, capsys):
    _, store, token = hub
    job_id = await queue_test_job(store)
    code, sleeps = await run_wait(hub, tmp_path, job_id=job_id, interval=10, timeout=35)
    assert code == 124
    assert sleeps == [10, 10, 10, 5]
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert last == {"id": job_id, "status": protocol.STATUS_QUEUED}


async def test_wait_never_prints_token(hub, tmp_path, capsys):
    _, store, token = hub
    job_id = await queue_test_job(store)
    code, _ = await run_wait(hub, tmp_path, job_id=job_id, interval=10, timeout=15)
    out = capsys.readouterr()
    assert code == 124
    assert token not in out.out + out.err


async def test_wait_fails_after_three_hub_failures(hub, tmp_path, capsys):
    calls = []

    def poll(url, tok, jid):
        calls.append(jid)
        raise snapshot.HubUnavailable("ConnectionRefusedError")

    code, sleeps = await run_wait(hub, tmp_path, job_id="j1", poll=poll, interval=5)
    assert code == 1
    assert len(calls) == 3
    assert len(sleeps) == 2
    assert "недоступен" in capsys.readouterr().err


async def test_wait_recovers_when_hub_comes_back(hub, tmp_path, capsys):
    answers = iter([snapshot.HubUnavailable("x"), snapshot.HubUnavailable("x"), {"status": "done"}])

    def poll(url, tok, jid):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    code, _ = await run_wait(hub, tmp_path, job_id="j1", poll=poll, interval=5)
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"id": "j1", "status": "done"}


async def test_wait_unknown_job_is_error(hub, tmp_path, capsys):
    code, sleeps = await run_wait(hub, tmp_path, job_id="nope", interval=5)
    assert code == 1
    assert sleeps == []
    assert "Ошибка" in capsys.readouterr().err


async def test_wait_stops_on_waiting_owner(hub, tmp_path, capsys):
    def poll(url, tok, jid):
        return {"status": protocol.STATUS_WAITING_OWNER}

    code, _ = await run_wait(hub, tmp_path, job_id="j1", poll=poll)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == protocol.STATUS_WAITING_OWNER


async def test_wait_bad_token_reports_error(hub, tmp_path, capsys):
    code, _ = await run_wait(hub, tmp_path, token="wrong-token", job_id="j1")
    assert code == 1
    err = capsys.readouterr().err
    assert "nodectl devlab connect" in err
    assert "wrong-token" not in err


def test_wait_without_token_points_to_connect(tmp_path, capsys):
    code = snapshot.wait_command(
        Settings.model_validate({"swarm": {"token": "x"}}),
        "j1",
        token_path=str(tmp_path / "none"),
    )
    assert code == 1
    assert "nodectl devlab connect" in capsys.readouterr().err


async def test_connect_advises_wait_zero_and_nodectl_wait(tmp_path, capsys):
    async def fetch(url, swarm):
        return "tok"

    await devlab_connect.connect(
        Settings.model_validate({"swarm": {"token": "x"}}),
        token_path=str(tmp_path / "t"),
        skills_dir=str(tmp_path / "s"),
        fetch=fetch,
        runner=lambda argv: 0,
        which=lambda name: None,
    )
    out = capsys.readouterr().out
    assert "wait=0" in out
    assert "nodectl devlab wait" in out


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonrpc": "2.0", "id": 1, "error": "boom"},
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": "[1]"}]}},
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": 5}]}},
        {"jsonrpc": "2.0", "id": 1, "result": {"content": "x"}},
        [1, 2],
    ],
)
async def test_call_status_garbage_shape_is_hub_unavailable(payload):
    async def handler(request):
        return web.json_response(payload)

    app = web.Application()
    app.router.add_post(protocol.HTTP_MCP_PATH, handler)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("")).rstrip("/")
        if isinstance(payload, dict) and "error" in payload:
            with pytest.raises(snapshot.SnapshotError):
                await asyncio.to_thread(snapshot.call_status, url, "t", "j")
        else:
            with pytest.raises(snapshot.HubUnavailable):
                await asyncio.to_thread(snapshot.call_status, url, "t", "j")


async def test_call_status_does_not_follow_redirects():
    seen = []

    async def target(request):
        seen.append(request.headers.get("Authorization"))
        return web.json_response({})

    async def handler(request):
        raise web.HTTPTemporaryRedirect("/other")

    app = web.Application()
    app.router.add_post(protocol.HTTP_MCP_PATH, handler)
    app.router.add_post("/other", target)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("")).rstrip("/")
        with pytest.raises(snapshot.HubUnavailable):
            await asyncio.to_thread(snapshot.call_status, url, "secret", "j")
    assert seen == []
