import asyncio
import io
import json
import sys
import tarfile

import pytest

from sa_home_bot.config import DevlabConfig
from sa_home_bot.devlab.agent.sandbox import (
    ExecResult,
    LocalSandbox,
    PipeSession,
    SandboxPathError,
)
from sa_home_bot.devlab.agent.script import parse_reply, request_payload, worker_argv
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import ToolBox, ToolError
from sa_home_bot.devlab.executor import wrapper
from sa_home_bot.devlab.executor.agent_sandbox import PodmanSandbox
from sa_home_bot.devlab.executor.image import ImageRecipe, image_tag
from sa_home_bot.devlab.executor.runner import JUNIT_FILE
from sa_home_bot.utils.process import ProcResult

RECIPE = ImageRecipe(image="docker.io/library/python:3.13-slim")


def tar_of(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def files_of(archive: bytes) -> dict[str, bytes]:
    return wrapper.extract_files(archive)


class FakeContainer:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.inputs: dict[str, list[bytes]] = {}
        self.files: dict[str, bytes] = {}
        self.on_exec = lambda command, files: (0, b"ok\n")
        self.fail: set[str] = set()
        self.hang_exec = False
        self.images: list[str] = []

    def commands(self) -> list[str]:
        return [argv[5] for argv in self.calls]

    def args_of(self, command: str) -> list[list[str]]:
        return [argv[6:] for argv in self.calls if argv[5] == command]

    async def __call__(self, argv, *, input=None, timeout=None, **kwargs) -> ProcResult:
        self.calls.append(list(argv))
        command = argv[5]
        if command in self.fail:
            return ProcResult(1, b"boom\n", b"")
        if input is not None:
            self.inputs.setdefault(command, []).append(input)
        if command in ("sync-in", "refresh"):
            self.files = files_of(input)
        if command == "images":
            listing = [{"Names": [f"localhost/devlab/{tag}:latest"]} for tag in self.images]
            return ProcResult(0, json.dumps(listing).encode(), b"")
        if command == "sync-out":
            return ProcResult(0, tar_of(self.files), b"")
        if command == "exec":
            if self.hang_exec:
                await asyncio.sleep(3600)
            code, out = self.on_exec(argv[8], self.files)
            return ProcResult(code, out, b"")
        return ProcResult(0, b"", b"")


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print(1)\n")
    (root / "tests").mkdir()
    (root / "tests" / "t.py").write_text("ok\n")
    (root / ".env").write_text("TOKEN=secret\n")
    return root


def make(work, fake, **spec):
    spec.setdefault("secret_paths", (".env",))
    spec.setdefault("forbidden_paths", ("tests/",))
    return PodmanSandbox(DevlabConfig(), "Job-1", work, RECIPE, AgentSpec(**spec), spawn=fake)


async def started(work, fake=None, **spec):
    fake = fake or FakeContainer()
    box = make(work, fake, **spec)
    await box.start()
    return box, fake


async def test_start_argv_and_container_gets_no_secrets(work):
    box, fake = await started(work)
    assert fake.commands() == ["sync-in", "images", "build", "rm", "create"]
    tag = fake.args_of("build")[0][1]
    assert fake.args_of("create")[0] == ["job-1", tag, "6", "12g", "6-11"]
    assert set(fake.files) == {"src/a.py", "tests/t.py"}
    assert fake.calls[0][:5] == ["sudo", "-n", "-u", "devlab", str(wrapper.DEVLAB_WRAPPER_PATH)]


async def test_cached_image_skips_build(work):
    fake = FakeContainer()
    fake.images = [image_tag(RECIPE.image, (), {}, ())]
    await started(work, fake)
    assert "build" not in fake.commands()


async def test_edit_goes_to_host_and_is_synced_before_exec(work):
    box, fake = await started(work)
    await box.write_bytes("src/a.py", b"print(2)\n")
    assert (work / "src" / "a.py").read_bytes() == b"print(2)\n"
    assert await box.changed_paths() == ["src/a.py"]
    result = await box.exec(["echo", "hi there"], 5)
    assert result.ok and result.output == "ok\n"
    assert fake.commands()[-3:] == ["refresh", "exec", "sync-out"]
    assert fake.args_of("exec")[0] == ["job-1", str(wrapper.exec_backstop(5)), "echo 'hi there'"]
    assert fake.files["src/a.py"] == b"print(2)\n"
    assert ".env" not in fake.files


async def test_clean_tree_is_not_resynced(work):
    box, fake = await started(work)
    await box.exec(["true"], 5)
    await box.exec(["true"], 5)
    assert fake.commands().count("refresh") == 0
    await box.write_bytes("src/b.py", b"x\n")
    await box.exec(["true"], 5)
    assert fake.commands().count("refresh") == 1


async def test_changes_made_in_container_come_back_to_host(work):
    box, fake = await started(work)

    def formatter(command, files):
        files["src/a.py"] = b"print( 1 )\n"
        files["src/new.py"] = b"new\n"
        del files["tests/t.py"]
        files[JUNIT_FILE] = b"<x/>"
        files["__pycache__/a.pyc"] = b"\0"
        return 0, b""

    fake.on_exec = formatter
    await box.exec(["ruff", "format"], 5)
    assert (work / "src" / "a.py").read_bytes() == b"print( 1 )\n"
    assert (work / "src" / "new.py").read_bytes() == b"new\n"
    assert (work / "tests" / "t.py").read_text() == "ok\n"
    assert not (work / JUNIT_FILE).exists()
    assert not (work / "__pycache__").exists()
    assert (work / ".env").read_text() == "TOKEN=secret\n"
    assert sorted(await box.changed_paths()) == ["src/a.py", "src/new.py"]


async def test_protected_change_in_container_is_reverted_on_next_exec(work):
    box, fake = await started(work)

    def meddle(command, files):
        files["tests/t.py"] = b"evil\n"
        return 0, b""

    fake.on_exec = meddle
    await box.exec(["x"], 5)
    assert (work / "tests" / "t.py").read_text() == "ok\n"
    fake.on_exec = lambda command, files: (0, b"")
    await box.exec(["x"], 5)
    assert fake.files["tests/t.py"] == b"ok\n"


async def test_deleted_in_container_removes_on_host(work):
    box, fake = await started(work)

    def remove(command, files):
        del files["src/a.py"]
        return 0, b""

    fake.on_exec = remove
    await box.exec(["rm", "src/a.py"], 5)
    assert not (work / "src" / "a.py").exists()


async def test_forbidden_and_secret_paths(work):
    box, _ = await started(work)
    with pytest.raises(SandboxPathError):
        await box.write_bytes("tests/new.py", b"x")
    with pytest.raises(SandboxPathError):
        await box.write_bytes(".env", b"x")
    with pytest.raises(SandboxPathError):
        await box.read_bytes(".env")
    with pytest.raises(SandboxPathError):
        await box.read_bytes("../outside")
    assert await box.read_bytes("tests/t.py") == b"ok\n"
    assert ".env" not in await box.list_files()
    assert (work / ".env").read_text() == "TOKEN=secret\n"


async def test_toolbox_read_of_secret_is_a_tool_error(work):
    box, _ = await started(work)
    tools = ToolBox(box, AgentSpec(secret_paths=(".env",)))
    with pytest.raises(ToolError, match="закрыт"):
        await tools.call("read_file", {"path": ".env"})
    with pytest.raises(ToolError, match="закрыт"):
        await tools.call("write_file", {"path": ".env", "content": "x"})
    assert ".env" not in (await tools.call("grep", {"pattern": "TOKEN"})).text


async def test_exec_timeout_marks_flag_and_container_is_recreated(work):
    box, fake = await started(work)
    fake.on_exec = lambda command, files: (wrapper.TIMEOUT_EXIT_CODE, b"slow\n")
    result = await box.exec(["sleep", "9"], 1)
    assert result.timed_out and not result.ok
    assert fake.commands()[-2:] == ["sync-out", "rm"]
    fake.on_exec = lambda command, files: (0, b"ok\n")
    assert (await box.exec(["true"], 1)).ok
    assert fake.commands().count("create") == 2
    assert fake.commands().count("refresh") == 1
    assert fake.commands().count("build") == 1


async def test_hung_wrapper_times_out_with_grace(work, monkeypatch):
    monkeypatch.setattr(wrapper, "EXEC_GRACE_S", 0.05)
    box, fake = await started(work)
    fake.hang_exec = True
    result = await asyncio.wait_for(box.exec(["x"], 0.01), 5)
    assert result.timed_out
    assert fake.commands()[-1] == "rm"


async def test_command_not_found_exit_code_is_plain_failure(work):
    box, fake = await started(work)
    fake.on_exec = lambda command, files: (127, b"nope\n")
    result = await box.exec(["nope"], 5)
    assert result.exit_code == 127 and not result.timed_out


async def test_stop_and_resume_keeps_workdir_and_resyncs(work):
    box, fake = await started(work)
    await box.stop()
    assert fake.commands()[-1] == "stop"
    await box.resume()
    assert fake.commands()[-1] == "start"
    assert fake.commands().count("create") == 1
    await box.exec(["true"], 5)
    assert fake.commands()[-3:] == ["refresh", "exec", "sync-out"]
    assert (work / "src" / "a.py").exists()


async def test_resume_recreates_when_start_fails(work):
    box, fake = await started(work)
    await box.stop()
    fake.fail = {"start"}
    await box.resume()
    assert fake.commands().count("create") == 2
    assert fake.commands().count("sync-in") == 2


async def test_exec_on_stopped_container_is_an_error(work):
    box, _ = await started(work)
    await box.stop()
    with pytest.raises(wrapper.RunnerError, match="не запущен"):
        await box.exec(["true"], 5)


async def test_aclose_removes_container_and_workdir_copy(work):
    box, fake = await started(work)
    await box.aclose()
    assert fake.commands()[-2:] == ["rm", "clean"]
    assert (work / "src" / "a.py").exists()


async def test_aclose_survives_cancellation(work):
    box, fake = await started(work)
    task = asyncio.ensure_future(box.aclose())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.05)
    assert fake.commands()[-2:] == ["rm", "clean"]


async def test_build_failure_raises(work):
    fake = FakeContainer()
    fake.fail = {"build"}
    box = make(work, fake)
    with pytest.raises(wrapper.RunnerError, match="сборка образа"):
        await box.start()


async def test_bad_job_id_rejected(work):
    with pytest.raises(wrapper.RunnerError):
        PodmanSandbox(DevlabConfig(), "a b", work, RECIPE, AgentSpec())


async def test_baseline_from_base_tree_survives_restart(work, tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "a.txt").write_text("1")
    baseline = LocalSandbox(base).snapshot()
    (work / "a.txt").write_text("2")
    box = make(work, FakeContainer())
    box2 = PodmanSandbox(
        DevlabConfig(), "job-1", work, RECIPE, AgentSpec(), baseline=baseline, spawn=FakeContainer()
    )
    assert "a.txt" in await box2.changed_paths()
    assert "a.txt" not in await box.changed_paths()


async def test_agent_resources_from_config(work):
    fake = FakeContainer()
    box = PodmanSandbox(
        DevlabConfig(agent_cpus=2, agent_memory="4g", agent_cpuset="0-1"),
        "job-1",
        work,
        RECIPE,
        AgentSpec(),
        spawn=fake,
    )
    await box.start()
    assert fake.args_of("create")[0][2:] == ["2", "4g", "0-1"]


SESSION_CONFIG = {
    "closed": [],
    "write_prefixes": [],
    "allowed": [],
    "test_argv": [],
    "command_timeout": 5,
    "test_timeout": 5,
    "budget": 100,
    "output_chars": 6000,
}


def local_session(work, monkeypatch):
    opened = []

    def open_session(self, name, argv):
        opened.append((name, list(argv)))
        return PipeSession(worker_argv(SESSION_CONFIG, python=sys.executable), cwd=str(work))

    monkeypatch.setattr(wrapper.WrapperClient, "open_session", open_session)
    return opened


async def session_text(box, code, timeout_s=60):
    reply = await box.session_request(["python"], request_payload(code), timeout_s)
    return reply if reply.data is None else parse_reply(reply.data)


def test_open_session_argv_goes_through_wrapper_session_command():
    client = wrapper.WrapperClient(DevlabConfig(), FakeContainer())
    session = client.open_session("job-1", ["python", "-c", "print(1)"])
    assert session._argv[-3:] == ["session", "job-1", "python -c 'print(1)'"]


async def test_session_state_persists_and_files_sync_after_each_call(work, monkeypatch):
    opened = local_session(work, monkeypatch)
    box, fake = await started(work)
    first = await session_text(box, "kept = 7")
    second = await session_text(box, "kept")
    assert second.output == "7"
    assert not first.failed
    assert len(opened) == 1
    assert fake.commands().count("sync-out") == 2
    await box.aclose()
    assert box._session is None


async def test_session_timeout_recreates_container_and_drops_state(work, monkeypatch):
    local_session(work, monkeypatch)
    box, fake = await started(work)
    await session_text(box, "kept = 7")
    timed_out = await box.session_request(
        ["python"], request_payload("import time\ntime.sleep(30)"), 1
    )
    assert timed_out.timed_out
    assert fake.commands()[-2:] == ["sync-out", "rm"]
    after = await session_text(box, "print('kept' in globals())")
    assert after.output == "False"
    assert fake.commands().count("create") == 2
    await box.aclose()


async def test_stop_closes_session_process(work, monkeypatch):
    local_session(work, monkeypatch)
    box, _ = await started(work)
    await session_text(box, "1")
    proc = box._session._proc
    await box.stop()
    assert proc.returncode is not None
    assert box._session is None


PIPE_HELPERS = (
    "import json,sys\n"
    "def send(o):\n"
    "    d=json.dumps(o).encode()\n"
    "    sys.stdout.buffer.write(str(len(d)).encode()+b'\\n'+d)\n"
    "    sys.stdout.buffer.flush()\n"
    "def get():\n"
    "    n=int(sys.stdin.buffer.readline())\n"
    "    return json.loads(sys.stdin.buffer.read(n))\n"
)
PYTEST_FAILED = (
    "FAILED tests/test_a.py::test_one - assert 1 == 2\n"
    "ERROR tests/test_b.py::test_two - RuntimeError: boom\n"
    "FAILED tests/test_a.py::test_one - assert 1 == 2\n"
)


def test_failure_summary_lists_failed_tests_before_tail():
    output = "x" * 3000 + "\n" + PYTEST_FAILED + "2 failed in 0.1s"
    text = ExecResult(1, output).failure_summary(1000)
    lines = text.splitlines()
    assert lines[0] == "[код 1]"
    assert lines[1:4] == [
        "упавшие тесты:",
        "FAILED tests/test_a.py::test_one - assert 1 == 2",
        "ERROR tests/test_b.py::test_two - RuntimeError: boom",
    ]
    assert "хвост вывода:" in lines
    assert text.endswith("2 failed in 0.1s")


@pytest.mark.parametrize(
    "result",
    [ExecResult(1, "просто падение\n"), ExecResult(-1, PYTEST_FAILED, timed_out=True)],
)
def test_failure_summary_falls_back_to_plain_summary(result):
    assert result.failure_summary(500, 7) == result.summary(500, 7)


async def test_local_diff_against_base_tree(tmp_path):
    base = tmp_path / "base"
    work = tmp_path / "work"
    for root in (base, work):
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.py").write_text("x = 1\n")
        (root / "src" / "gone.py").write_text("old\n")
    (base / "docs").mkdir()
    (base / "docs" / "n.md").write_text("a\n")
    box = LocalSandbox(work, base_root=base)
    baseline = box.snapshot()
    box = LocalSandbox(work, baseline, base)
    (work / "src" / "a.py").write_text("x = 2\n")
    (work / "src" / "gone.py").unlink()
    (work / "src" / "new.py").write_text("n = 1\n")
    text = await box.diff()
    assert "--- a/src/a.py\n+++ b/src/a.py" in text
    assert "-x = 1\n+x = 2" in text
    assert "--- a/src/gone.py\n+++ /dev/null" in text
    assert "--- /dev/null\n+++ b/src/new.py" in text
    assert "+n = 1" in text
    assert await box.diff(("docs/",)) == ""


async def test_pipe_session_answers_calls_until_final_frame(tmp_path):
    code = PIPE_HELPERS + (
        "get()\n"
        "send({'call':'a','args':{}}); first=get()\n"
        "send({'call':'b','args':{}}); second=get()\n"
        "send({'output': [first, second]})\n"
    )
    seen = []

    async def on_call(request):
        seen.append(request["call"])
        return {"ok": True, "n": len(seen)}

    session = PipeSession([sys.executable, "-c", code])
    reply = await session.request(b"{}", 10, on_call=on_call)
    await session.close()
    assert seen == ["a", "b"]
    assert json.loads(reply.data) == {"output": [{"ok": True, "n": 1}, {"ok": True, "n": 2}]}


async def test_pipe_session_handler_time_is_not_counted_in_timeout():
    code = PIPE_HELPERS + (
        "get()\n"
        "send({'call':'a','args':{}})\n"
        "get()\n"
        "send({'output': 'done'})\n"
    )

    async def slow(request):
        await asyncio.sleep(1.5)
        return {"ok": True}

    session = PipeSession([sys.executable, "-c", code])
    reply = await session.request(b"{}", 1.2, on_call=slow)
    await session.close()
    assert not reply.timed_out
    assert json.loads(reply.data) == {"output": "done"}


async def test_pipe_session_without_handler_refuses_calls():
    code = PIPE_HELPERS + (
        "get()\n"
        "send({'call':'a','args':{}})\n"
        "answer = get()\n"
        "send({'output': answer})\n"
    )
    session = PipeSession([sys.executable, "-c", code])
    reply = await session.request(b"{}", 10)
    await session.close()
    assert json.loads(reply.data)["output"]["ok"] is False


async def test_podman_session_call_pulls_first_and_does_not_deadlock_on_exec(work, monkeypatch):
    local_session(work, monkeypatch)
    box, fake = await started(work)
    fake.files["src/a.py"] = b"print(5)\n"
    seen = []

    async def on_call(request):
        seen.append((request["call"], (work / "src" / "a.py").read_bytes()))
        result = await box.exec(["pytest"], 5)
        return {"ok": result.ok}

    reply = await box.session_request(
        ["python"], request_payload("print(step_done('шаг'))"), 30, on_call
    )
    run = parse_reply(reply.data)
    await box.aclose()
    assert seen == [("step_done", b"print(5)\n")]
    assert run.output == "None"
    assert fake.commands().index("sync-out") < fake.commands().index("exec")


def test_tracked_but_closed_lists_only_matching_tracked_files():
    from sa_home_bot.devlab.agent.paths import tracked_but_closed

    tracked = ["tests/fixtures/seeded.sqlite", "src/a.py", ".env"]
    assert tracked_but_closed(tracked, ["*.sqlite", ".env"]) == [
        "tests/fixtures/seeded.sqlite",
        ".env",
    ]
    assert tracked_but_closed(tracked, []) == []
