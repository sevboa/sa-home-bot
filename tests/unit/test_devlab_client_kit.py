from __future__ import annotations

import asyncio
import re
import tomllib
from pathlib import Path

import pytest
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
