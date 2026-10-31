import os
import subprocess

import pytest

from sa_home_bot.node.fixups import devlab
from tests.unit.fixups_world import make_settings


@pytest.fixture
def sandbox(tmp_path):
    settings = make_settings(["devlab"])
    settings.devlab.root = str(tmp_path / "root")
    job = tmp_path / "root" / "home" / "work" / "job1"
    job.mkdir(parents=True)
    (job / "a.txt").write_text("hi")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "podman"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    script = tmp_path / "wrapper.sh"
    script.write_text(devlab.devlab_wrapper_content(settings))

    def call(*args: str, stdin: bytes = b""):
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        return subprocess.run(["sh", str(script), *args], input=stdin, capture_output=True, env=env)

    return call


def test_create_passes_cpuset(sandbox):
    result = sandbox("create", "job1", "img", "12", "24g", "6-11,18-23")
    assert result.returncode == 0
    lines = result.stdout.decode().split("\n")
    assert lines[lines.index("--cpuset-cpus") + 1] == "6-11,18-23"
    assert "--network" in lines and "none" in lines


def test_create_without_cpuset_has_no_flag(sandbox):
    result = sandbox("create", "job1", "img", "12", "24g")
    assert result.returncode == 0
    assert b"--cpuset-cpus" not in result.stdout


@pytest.mark.parametrize("cpuset", ["", "a", "0-3;id", "$(id)", "-1", "0 1", "0,1\n2"])
def test_create_rejects_bad_cpuset(sandbox, cpuset):
    result = sandbox("create", "job1", "img", "12", "24g", cpuset)
    assert result.returncode == 2
    assert result.stdout == b""


def test_sync_out_single_file(sandbox):
    result = sandbox("sync-out", "job1", "a.txt")
    assert result.returncode == 0
    assert b"a.txt" in result.stdout and b"hi" in result.stdout


def test_sync_out_whole_dir(sandbox):
    assert sandbox("sync-out", "job1").returncode == 0


@pytest.mark.parametrize("file", ["..", ".", "a/b", "../x", "a b", "a;b", "$(id)", ""])
def test_sync_out_rejects_bad_file(sandbox, file):
    result = sandbox("sync-out", "job1", file)
    assert result.returncode == 2
    assert "плохое имя файла" in result.stderr.decode()


def test_build_reads_containerfile_from_file_not_stdin(sandbox, tmp_path):
    result = sandbox("build", "job1", "img", stdin=b"FROM x\n")
    assert result.returncode == 0
    lines = result.stdout.decode().split("\n")
    spec = lines[lines.index("--file") + 1]
    assert spec != "-" and spec.endswith("job1.containerfile")
    assert not (tmp_path / "root" / "home" / "work" / "job1.containerfile").exists()


def test_build_failure_status_is_kept(sandbox, tmp_path):
    (tmp_path / "bin" / "podman").write_text("#!/bin/sh\nexit 7\n")
    assert sandbox("build", "job1", "img", stdin=b"FROM x\n").returncode == 7


def test_name_still_validated(sandbox):
    assert sandbox("sync-out", "../x", "a.txt").returncode == 2
