from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.config import Settings
from sa_home_bot.devlab import connect as devlab_connect
from sa_home_bot.devlab import protocol, snapshot
from sa_home_bot.devlab.hub.app import build_hub_app
from sa_home_bot.devlab.hub.store import HubStore
from tests.unit.support.fakes import FakeNodes
from tests.unit.test_devlab_git import run_git

PROJECT = "demo"
CLIENT = "laptop"


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
    (path / ".gitignore").write_text("ignored.txt\n")
    (path / "tracked.txt").write_text("v1")
    (path / "staged.txt").write_text("s1")
    run_git(path, "add", ".gitignore", "tracked.txt", "staged.txt")
    run_git(path, "commit", "-q", "-m", "init")
    (path / "tracked.txt").write_text("v2 unstaged")
    (path / "staged.txt").write_text("s2 staged")
    run_git(path, "add", "staged.txt")
    (path / "staged.txt").write_text("s3 after stage")
    (path / "new.txt").write_text("untracked")
    (path / "ignored.txt").write_text("secret")
    return path


def state(repo: Path) -> dict[str, str]:
    return {
        "head": run_git(repo, "rev-parse", "HEAD").stdout,
        "branch": run_git(repo, "symbolic-ref", "HEAD").stdout,
        "status": run_git(repo, "status", "--porcelain=v2", "--branch").stdout,
        "diff": run_git(repo, "diff").stdout,
        "cached": run_git(repo, "diff", "--cached").stdout,
        "branches": run_git(repo, "branch", "-a").stdout,
        "stash": run_git(repo, "stash", "list").stdout,
        "files": "|".join(
            f"{p.name}={p.read_text()}" for p in sorted(repo.iterdir()) if p.is_file()
        ),
    }


def test_snapshot_commit_holds_uncommitted_and_leaves_repo_untouched(repo):
    before = state(repo)
    commit = snapshot.create_snapshot(repo)
    assert state(repo) == before
    assert run_git(repo, "rev-parse", f"{commit}^").stdout == before["head"]
    def show(name: str) -> str:
        return run_git(repo, "show", f"{commit}:{name}").stdout

    assert show("tracked.txt") == "v2 unstaged"
    assert show("staged.txt") == "s3 after stage"
    assert show("new.txt") == "untracked"
    assert run_git(repo, "cat-file", "-e", f"{commit}:ignored.txt").returncode != 0


def test_snapshot_without_commits_is_an_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    run_git(empty, "init", "-q")
    with pytest.raises(snapshot.SnapshotError, match="нет коммитов"):
        snapshot.create_snapshot(empty)


@pytest.mark.parametrize(
    ("spec", "override", "expected"),
    [
        (None, "", "repo"),
        ('project = "named"\n', "", "named"),
        ('project = "named"\n', "forced", "forced"),
    ],
)
def test_project_name(repo, spec, override, expected):
    if spec:
        (repo / ".devlab.toml").write_text(spec)
    assert snapshot.project_name(repo, override) == expected


def settings_for(hub_client: TestClient) -> Settings:
    url = hub_client.make_url("/")
    return Settings.model_validate(
        {
            "swarm": {"token": "x"},
            "node": {"id": CLIENT},
            "devlab_hub": {"hub_node": url.host, "port": url.port},
        }
    )


async def test_snapshot_pushes_to_hub_without_token_in_argv(
    hub, repo, tmp_path, capsys, monkeypatch
):
    client, store, token = hub
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    argvs = []
    original = snapshot.subprocess.run

    def spy(argv, **kwargs):
        argvs.append(list(argv))
        return original(argv, **kwargs)

    monkeypatch.setattr(snapshot.subprocess, "run", spy)
    before = state(repo)
    code = await asyncio.to_thread(
        snapshot.snapshot, settings_for(client), cwd=repo, token_path=str(token_file),
        project=PROJECT,
    )
    out = capsys.readouterr()
    assert code == 0
    assert state(repo) == before
    ref = out.out.strip()
    assert re.fullmatch(rf"test/{CLIENT}/\d+-[0-9a-f]{{7}}", ref)
    assert token not in out.out + out.err
    assert all(token not in part for argv in argvs for part in argv)
    pushed = run_git(store.git_root / f"{PROJECT}.git", "show", f"{ref}:new.txt")
    assert pushed.stdout == "untracked"


async def test_snapshot_push_error_hides_token(hub, repo, tmp_path, capsys):
    client, _, token = hub
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    code = await asyncio.to_thread(
        snapshot.snapshot, settings_for(client), cwd=repo, token_path=str(token_file),
        project="missing",
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "Ошибка" in err
    assert token not in err


async def test_snapshot_foreign_namespace_is_rejected(hub, repo, tmp_path, capsys):
    client, _, token = hub
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    code = await asyncio.to_thread(
        snapshot.snapshot, settings_for(client), cwd=repo, token_path=str(token_file),
        project=PROJECT, client="other",
    )
    assert code == 1
    assert "отклонено" in capsys.readouterr().err


def test_snapshot_without_token_file_points_to_connect(repo, tmp_path, capsys):
    code = snapshot.snapshot(
        Settings.model_validate({"swarm": {"token": "x"}}),
        cwd=repo,
        token_path=str(tmp_path / "none"),
    )
    assert code == 1
    assert "nodectl devlab connect" in capsys.readouterr().err


def test_install_skill_installs_updates_and_keeps_current(tmp_path):
    skills = str(tmp_path / "skills")
    target = tmp_path / "skills" / devlab_connect.SKILL_NAMES[0] / devlab_connect.SKILL_FILE
    assert devlab_connect.install_skill(skills) == "installed"
    assert devlab_connect.install_skill(skills) == "current"
    target.write_text("old")
    assert devlab_connect.install_skill(skills) == "updated"
    assert target.read_text() == devlab_connect.skill_source()


def test_repo_skill_is_the_packaged_one():
    repo_skill = Path(".claude/skills/devlab-test/SKILL.md")
    assert repo_skill.read_text() == devlab_connect.skill_source()
    assert devlab_connect.skill_source().startswith("---\nname: devlab-test\n")
