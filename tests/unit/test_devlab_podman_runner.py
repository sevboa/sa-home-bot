import asyncio
import io
import json
import tarfile

import pytest

from sa_home_bot.config import DevlabConfig
from sa_home_bot.devlab.executor import image as images
from sa_home_bot.devlab.executor import podman, wrapper
from sa_home_bot.devlab.executor.app import build_runner
from sa_home_bot.devlab.executor.runner import JUNIT_FILE, RunRequest
from sa_home_bot.node.fixups.devlab import DEVLAB_WRAPPER_PATH
from sa_home_bot.utils.process import ProcResult

WRAPPER = str(DEVLAB_WRAPPER_PATH)
IMAGE = "docker.io/library/python:3.13-slim"


def tar_with(name: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeWrapper:
    def __init__(
        self,
        exec_code: int = 0,
        exec_out: bytes = b"ok\n",
        images: list[str] | None = None,
        junit: bytes | None = None,
        block_exec: bool = False,
        fail: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.exec_code = exec_code
        self.exec_out = exec_out
        self.images = images or []
        self.junit = junit
        self.block_exec = block_exec
        self.fail = fail or set()
        self.exec_started = asyncio.Event()
        self.killed = asyncio.Event()

    def commands(self) -> list[str]:
        return [argv[5] for argv, _ in self.calls]

    def args_of(self, command: str) -> list[list[str]]:
        return [argv[5:] for argv, _ in self.calls if argv[5] == command]

    async def __call__(self, argv, *, input=None, timeout=None, **kwargs) -> ProcResult:
        self.calls.append((list(argv), input))
        command = argv[5]
        if command in self.fail:
            return ProcResult(1, b"boom\n", b"")
        if command == "images":
            listing = [{"Names": [f"localhost/devlab/{tag}:latest"]} for tag in self.images]
            return ProcResult(0, json.dumps(listing).encode(), b"")
        if command == "exec":
            self.exec_started.set()
            if self.block_exec:
                await self.killed.wait()
                return ProcResult(137, b"", b"")
            return ProcResult(self.exec_code, self.exec_out, b"")
        if command == "rm" and self.exec_started.is_set():
            self.killed.set()
        if command == "sync-out":
            if self.junit is None:
                return ProcResult(2, b"", b"")
            return ProcResult(0, tar_with(JUNIT_FILE, self.junit), b"")
        return ProcResult(0, b"", b"")


def make_runner(fake: FakeWrapper, **cfg) -> podman.PodmanTestRunner:
    return podman.PodmanTestRunner(DevlabConfig(**cfg), spawn=fake)


def request(tmp_path, **kwargs) -> RunRequest:
    (tmp_path / "pyproject.toml").write_text("x = 1\n")
    base = dict(
        job_id="abc123",
        workdir=tmp_path,
        argv=("python", "-m", "pytest", "-q"),
        timeout_s=60,
        setup=("pip install pytest",),
        setup_files=("pyproject.toml",),
    )
    base.update(kwargs)
    return RunRequest(**base)


async def test_full_pipeline_argv_and_junit(tmp_path):
    fake = FakeWrapper(junit=b"<testsuites/>")
    result = await make_runner(fake).run(request(tmp_path))
    assert result.exit_code == 0 and result.output == b"ok\n" and not result.timed_out
    assert fake.commands() == [
        "sync-in",
        "images",
        "build",
        "rm",
        "create",
        "exec",
        "sync-out",
        "rm",
        "clean",
    ]
    assert fake.calls[0][0][:5] == ["sudo", "-n", "-u", "devlab", WRAPPER]
    tag = fake.args_of("build")[0][2]
    assert fake.args_of("create")[0][1:] == ["abc123", tag, "12", "24g", "6-11,18-23"]
    backstop = str(wrapper.exec_backstop(60))
    assert fake.args_of("exec")[0][1:] == ["abc123", backstop, "python -m pytest -q"]
    assert fake.args_of("sync-out")[0][1:] == ["abc123", JUNIT_FILE]
    assert (tmp_path / JUNIT_FILE).read_bytes() == b"<testsuites/>"
    dockerfile = fake.calls[fake.commands().index("build")][1].decode()
    assert dockerfile.startswith(f"FROM {IMAGE}\n")
    assert 'RUN ["sh", "-c", "pip install pytest"]' in dockerfile


async def test_cached_image_skips_build(tmp_path):
    req = request(tmp_path)
    files = images.read_setup_files(tmp_path, req.setup_files)
    tag = images.image_tag(IMAGE, req.setup, files, ())
    fake = FakeWrapper(images=[tag])
    await make_runner(fake).run(req)
    assert "build" not in fake.commands()


def test_image_tag_depends_on_content():
    base = images.image_tag("img", ["a"], {"f": b"1"}, ())
    assert base == images.image_tag("img", ["a"], {"f": b"1"}, ())
    assert base != images.image_tag("img2", ["a"], {"f": b"1"}, ())
    assert base != images.image_tag("img", ["b"], {"f": b"1"}, ())
    assert base != images.image_tag("img", ["a"], {"f": b"2"}, ())
    assert base != images.image_tag("img", ["a"], {"f": b"1"}, (("K", "v"),))


async def test_timeout_inside_container_sets_flag(tmp_path):
    fake = FakeWrapper(exec_code=124)
    result = await make_runner(fake).run(request(tmp_path))
    assert result.timed_out


async def test_outer_timeout_kills_container(tmp_path, monkeypatch):
    monkeypatch.setattr(wrapper, "EXEC_GRACE_S", 0.05)
    fake = FakeWrapper(block_exec=True)
    result = await make_runner(fake).run(request(tmp_path, timeout_s=0.01))
    assert result.timed_out
    assert fake.commands()[-2:] == ["rm", "clean"]
    commands = fake.commands()
    assert "rm" in commands[commands.index("exec") : commands.index("sync-out")]


async def test_cancel_kills_container_and_run_returns(tmp_path):
    fake = FakeWrapper(block_exec=True)
    runner = make_runner(fake)
    task = asyncio.create_task(runner.run(request(tmp_path)))
    await fake.exec_started.wait()
    await runner.cancel("abc123")
    result = await asyncio.wait_for(task, 5)
    assert result.exit_code == podman.CANCELLED_EXIT_CODE
    assert fake.commands()[-2:] == ["rm", "clean"]
    assert "sync-out" not in fake.commands()
    await runner.cancel("abc123")


async def test_cancel_unknown_job_is_noop():
    fake = FakeWrapper()
    await make_runner(fake).cancel("nope")
    assert fake.calls == []


async def test_outer_cancellation_propagates_and_cleans(tmp_path):
    fake = FakeWrapper(block_exec=True)
    runner = make_runner(fake)
    task = asyncio.create_task(runner.run(request(tmp_path)))
    await fake.exec_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.commands()[-2:] == ["rm", "clean"]


async def test_build_failure_raises_and_cleans(tmp_path):
    fake = FakeWrapper(fail={"build"})
    with pytest.raises(wrapper.RunnerError, match="сборка образа"):
        await make_runner(fake).run(request(tmp_path))
    assert fake.commands()[-2:] == ["rm", "clean"]
    assert "exec" not in fake.commands()


async def test_missing_junit_is_not_an_error(tmp_path):
    result = await make_runner(FakeWrapper(exec_code=1)).run(request(tmp_path))
    assert result.exit_code == 1
    assert not (tmp_path / JUNIT_FILE).exists()


async def test_config_overrides_reach_create(tmp_path):
    fake = FakeWrapper()
    await make_runner(fake, test_cpuset="0-3", test_cpus=4, test_memory="8g").run(request(tmp_path))
    assert fake.args_of("create")[0][3:] == ["4", "8g", "0-3"]


@pytest.mark.parametrize("job_id", ["", "-x", "A b", "a/b", "a;b"])
def test_job_name_rejects_bad_ids(job_id):
    with pytest.raises(wrapper.RunnerError):
        wrapper.job_name(job_id)


@pytest.mark.parametrize("name", ["/etc/passwd", "../x", "missing.toml", ""])
def test_setup_files_rejects_escapes(tmp_path, name):
    with pytest.raises(wrapper.RunnerError):
        images.read_setup_files(tmp_path, [name])


@pytest.mark.parametrize("image", ["a b", "a\nRUN x", "-x", ""])
def test_image_rejects_injection(image):
    with pytest.raises(wrapper.RunnerError):
        images.check_image(image)


def test_env_key_validated():
    with pytest.raises(wrapper.RunnerError):
        images.containerfile("img", [], {}, (("A B", "1"),))


def test_build_runner_returns_podman_runner():
    assert isinstance(build_runner(DevlabConfig()), podman.PodmanTestRunner)


@pytest.mark.parametrize("field", ["test_cpuset", "test_memory"])
def test_config_validates_limits(field):
    with pytest.raises(ValueError):
        DevlabConfig(**{field: "1; rm -rf /"})
