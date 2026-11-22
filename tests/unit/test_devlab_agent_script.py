import asyncio
import sys

import pytest

from sa_home_bot.devlab.agent.prompt_en import SCRIPT_API_EN
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.script import (
    SCRIPT_API,
    api_stubs,
    parse_reply,
    request_payload,
    worker_argv,
)

CONFIG = {
    "closed": [],
    "write_prefixes": [],
    "allowed": [[sys.executable, "-c"]],
    "test_argv": [sys.executable, "-m", "pytest", "-q"],
    "command_timeout": 30,
    "test_timeout": 60,
    "budget": 100,
    "output_chars": 6000,
}


class Session:
    def __init__(self, root, **config):
        self.sandbox = LocalSandbox(root)
        self.argv = worker_argv({**CONFIG, **config}, python=sys.executable)

    async def run(self, code, timeout_s=120, on_call=None):
        reply = await self.sandbox.session_request(
            self.argv, request_payload(code), timeout_s, on_call
        )
        return reply if reply.data is None else parse_reply(reply.data)

    async def close(self):
        await self.sandbox.session_close()


async def run_script(root, code, on_call=None, **config):
    session = Session(root, **config)
    try:
        return await session.run(code, on_call=on_call)
    finally:
        await session.close()


@pytest.fixture
async def session(project):
    opened = Session(project, write_prefixes=["src/"])
    yield opened
    await opened.close()


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("alpha = 1\nbeta = 2\ngamma = 3\n")
    (tmp_path / "src" / "b.py").write_text("beta = 20\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("beta\n")
    return tmp_path


async def test_read_many_and_print_reach_output(project):
    code = "texts = read_many(['src/a.py', 'src/b.py'])\nprint(texts['src/b.py'])\nprint(ls())"
    run = await run_script(project, code)
    assert not run.failed
    assert run.control is None
    assert "beta = 20" in run.output
    assert "1\t" not in run.output
    assert "['src/a.py', 'src/b.py']" in run.output


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("'привет'", "привет"),
        ("['a', 'b']", "a\nb"),
        ("{'x': 'один', 'y': 'два'}", "=== x ===\nодин\n=== y ===\nдва"),
        ("{'x': 1}", "{'x': 1}"),
        ("[1, 2]", "[1, 2]"),
        ("ls()[:1]", "src/a.py"),
        ("None", ""),
        ("value = 5", ""),
        ("'не последняя'\nvalue = 5", ""),
        ("'первая'\nprint('вторая')", "вторая"),
    ],
)
async def test_last_expression_is_printed_like_jupyter(project, code, expected):
    run = await run_script(project, code)
    assert not run.failed
    assert run.output == expected


async def test_last_expression_error_and_signal_keep_their_behaviour(project):
    failed = await run_script(project, "1\nundefined_name")
    assert failed.failed
    assert "NameError" in failed.output
    done = await run_script(project, "finish('готово')")
    assert done.control is not None
    assert done.output == ""


async def test_read_is_plain_text_and_numbered_has_numbers(project):
    run = await run_script(
        project, "print(repr(read('src/a.py', 2, 3)))\nprint(read_numbered('src/a.py', 2, 3))"
    )
    assert run.output.splitlines() == ["'beta = 2\\ngamma = 3\\n'", "2\tbeta = 2", "3\tgamma = 3"]


async def test_grep_with_context_marks_match_and_neighbours(project):
    run = await run_script(project, "print('\\n'.join(grep('^beta', 'src/a', context=1)))")
    assert run.output.splitlines() == [
        "src/a.py-1-alpha = 1",
        "src/a.py:2:beta = 2",
        "src/a.py-3-gamma = 3",
    ]


async def test_exception_marks_script_failed_with_traceback(project):
    run = await run_script(project, "edit('src/a.py', 'alpha', 'b')\nraise LookupError('нет')")
    assert run.failed
    assert "LookupError: нет" in run.output
    assert (project / "src" / "a.py").read_text().startswith("b = 1")


@pytest.mark.parametrize("data", [b"plain\n", b"{broken", b"[1]", b'{"output": 5}'])
def test_reply_without_valid_payload_is_failed(data):
    run = parse_reply(data)
    assert run.failed
    assert run.control is None
    assert run.output == data.decode().rstrip()


@pytest.mark.parametrize(("query", "count"), [("", len(SCRIPT_API)), ("тест", 3), ("grep", 1)])
def test_search_lists_matching_stubs(query, count):
    assert len(api_stubs(query).splitlines()) == count
    assert "find" not in api_stubs()


@pytest.mark.parametrize(
    ("code", "tool", "args"),
    [
        ("finish('итог')", "finish", {"summary": "итог"}),
        ("ask_owner('вопрос?')", "ask_owner", {"question": "вопрос?"}),
    ],
)
async def test_control_calls_end_the_script_even_inside_try(project, code, tool, args):
    run = await run_script(
        project,
        f"print('до')\ntry:\n    {code}\nexcept Exception:\n    print('поймано')\nprint('после')",
    )
    assert not run.failed
    assert run.output == "до"
    assert (run.control.tool, run.control.args) == (tool, args)


async def test_edit_is_immediate_and_edit_error_names_reason(project):
    code = (
        "edit('src/a.py', 'alpha = 1', 'alpha = 5')\n"
        "print(read('src/a.py', 1, 1).strip())\n"
        "try:\n    edit('src/a.py', 'нет такого', 'x')\nexcept EditError as e:\n    print(e)"
    )
    run = await run_script(project, code)
    assert run.output.splitlines()[0] == "alpha = 5"
    assert "найден 0 раз" in run.output


async def test_write_through_open_is_accepted_when_path_allowed(project):
    code = "open('src/c.py', 'w').write('x = 1\\n')"
    run = await run_script(project, code, write_prefixes=["src/"])
    assert not run.failed
    assert (project / "src" / "c.py").read_text() == "x = 1\n"


@pytest.mark.parametrize(
    ("code", "kwargs", "reason"),
    [
        ("open('docs.md', 'w').write('x')", {"write_prefixes": ["src/"]}, "вне путей роли"),
        ("open('src/a.py', 'w').write('x')", {"closed": ["src/a.py"]}, "закрыт для правок"),
    ],
)
async def test_write_through_open_outside_rules_is_reverted(project, code, kwargs, reason):
    before = (project / "src" / "a.py").read_text()
    run = await run_script(project, code, **kwargs)
    assert reason in run.output
    assert (project / "src" / "a.py").read_text() == before
    assert not (project / "docs.md").exists()


async def test_run_tests_returns_result_and_branches(project):
    (project / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (project / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    code = (
        "bad = run_tests(['test_bad.py'])\nok = run_tests(['test_ok.py'])\n"
        "print(bad.ok, ok.ok, bad.code != 0)\n"
        "if ok.ok:\n    finish('зелёное')"
    )
    run = await run_script(project, code)
    assert run.output.splitlines()[0] == "False True True"
    assert run.control.tool == "finish"
    assert run.green is True


@pytest.mark.parametrize(
    ("command", "expected"),
    [(f"{sys.executable} -c 'print(5)'", "[код 0]\n5"), ("ls", "команда не разрешена")],
)
async def test_run_returns_result_for_allowed_commands(project, command, expected):
    run = await run_script(project, f"print(run({command!r}))")
    assert expected in run.output


async def test_variables_and_functions_persist_between_calls(session):
    first = await session.run(
        "import json\nsrc = read('src/a.py')\ndef twice(n):\n    return n * 2"
    )
    second = await session.run("print(json.dumps(twice(len(src.splitlines()))))")
    assert not first.failed
    assert second.output == "6"


async def test_edit_in_second_call_uses_variable_from_first(session, project):
    await session.run("src = read('src/a.py')")
    run = await session.run(
        "edit('src/a.py', src.splitlines()[0], 'alpha = 7')\nread('src/a.py', 1, 1)"
    )
    assert run.output.strip() == "alpha = 7"


@pytest.mark.parametrize("code", ["finish('готово')", "ask_owner('вопрос?')"])
async def test_state_survives_control_signal(session, code):
    await session.run("kept = 41")
    signalled = await session.run(code)
    after = await session.run("kept + 1")
    assert signalled.control is not None
    assert after.output == "42"


async def test_exception_keeps_state_and_next_call_is_clean(session):
    await session.run("kept = 'жив'")
    failed = await session.run("raise ValueError('x')")
    after = await session.run("kept")
    assert failed.failed
    assert after.output == "жив"
    assert not after.failed


async def test_green_flag_does_not_leak_into_next_call(session, project):
    (project / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    first = await session.run("run_tests(['test_ok.py']).ok")
    second = await session.run("1")
    assert first.green is True
    assert second.green is None


async def test_rollback_of_forbidden_write_works_in_persistent_process(project):
    session = Session(project, write_prefixes=["src/"])
    try:
        await session.run("open('src/ok1.py', 'w').write('a = 1\\n')")
        denied = await session.run("open('docs.md', 'w').write('x')")
        again = await session.run("open('docs2.md', 'w').write('x')")
        allowed = await session.run("open('src/ok2.py', 'w').write('b = 1\\n')")
    finally:
        await session.close()
    assert "вне путей роли" in denied.output
    assert "вне путей роли" in again.output
    assert not (project / "docs.md").exists()
    assert not (project / "docs2.md").exists()
    assert (project / "src" / "ok1.py").exists()
    assert (project / "src" / "ok2.py").exists()
    assert "вне путей" not in allowed.output


async def test_project_module_is_reimported_after_edit(session, project):
    (project / "src" / "mod.py").write_text("NAME = 'старое'\n")
    first = await session.run("import mod\nmod.NAME")
    await session.run("edit('src/mod.py', 'старое', 'новое')")
    second = await session.run("import mod\nmod.NAME")
    assert (first.output, second.output) == ("старое", "новое")


async def test_timeout_resets_state_and_process_restarts(session):
    await session.run("kept = 1")
    timed_out = await session.run("import time\ntime.sleep(30)", timeout_s=1)
    assert timed_out.timed_out
    assert timed_out.data is None
    after = await session.run("print('kept' in globals())")
    assert after.output == "False"


@pytest.mark.parametrize(
    "code", ["import os\nos._exit(3)", "import os, signal\nos.kill(os.getpid(), 9)"]
)
async def test_process_death_is_reported_and_restarts(session, code):
    await session.run("kept = 1")
    died = await session.run(code)
    assert died.data is None
    assert not died.timed_out
    after = await session.run("print('kept' in globals())")
    assert after.output == "False"


async def test_sys_exit_does_not_kill_state(session):
    await session.run("kept = 1")
    exited = await session.run("import sys\nsys.exit(2)")
    after = await session.run("kept")
    assert exited.failed
    assert after.output == "1"


async def test_stdout_fd_writes_do_not_corrupt_frames(session):
    run = await session.run("import os\nos.write(1, b'7\\njunk')\nprint('ok')")
    after = await session.run("print('дальше')")
    assert run.output == "ok"
    assert after.output == "дальше"


async def test_close_ends_the_process(project):
    session = Session(project)
    await session.run("import os\nprint(os.getpid())")
    proc = session.sandbox._session._proc
    assert proc.returncode is None
    await session.close()
    assert proc.returncode is not None
    assert session.sandbox._session is None
    again = await session.run("print('новый')")
    await session.close()
    assert again.output == "новый"


async def test_sandbox_stop_and_aclose_close_the_session(project):
    session = Session(project)
    await session.run("1")
    proc = session.sandbox._session._proc
    await session.sandbox.stop()
    assert proc.returncode is not None
    await session.run("1")
    proc = session.sandbox._session._proc
    await session.sandbox.aclose()
    assert proc.returncode is not None


def recording(reply):
    seen = []

    async def on_call(request):
        seen.append(request)
        return reply(request) if callable(reply) else reply

    return seen, on_call


async def test_step_done_accepted_returns_and_script_continues(project):
    seen, on_call = recording({"ok": True})
    run = await run_script(
        project, "print(step_done('1: шаг'))\nprint('после')\nfinish('итог')", on_call
    )
    assert not run.failed
    assert run.output.splitlines() == ["None", "после"]
    assert seen == [{"call": "step_done", "args": {"title": "1: шаг"}}]
    assert (run.control.tool, run.control.args) == ("finish", {"summary": "итог"})


async def test_step_done_rejected_raises_step_rejected_and_stops(project):
    _, on_call = recording({"ok": False, "reason": "шаг не принят: красный тест"})
    run = await run_script(project, "print('до')\nstep_done('шаг')\nprint('после')", on_call)
    assert run.failed
    assert "до" in run.output and "после" not in run.output
    assert "StepRejected: шаг не принят: красный тест" in run.output
    assert run.control is None


async def test_step_rejected_can_be_caught_by_script(project):
    _, on_call = recording({"ok": False, "reason": "нет"})
    code = "try:\n    step_done('шаг')\nexcept StepRejected as e:\n    print('поймано', e)"
    run = await run_script(project, code, on_call)
    assert run.output == "поймано нет"


async def test_host_call_without_handler_is_refused(project):
    run = await run_script(project, "step_done('шаг')")
    assert run.failed
    assert "StepRejected" in run.output


async def test_diff_asks_host_and_returns_text(project):
    seen, on_call = recording({"ok": True, "value": "--- a/x\n+++ b/x"})
    run = await run_script(project, "print(diff())", on_call)
    assert run.output == "--- a/x\n+++ b/x"
    assert seen == [{"call": "diff", "args": {}}]


async def test_diff_failure_is_edit_error(project):
    _, on_call = recording({"ok": False, "reason": "нет базы"})
    run = await run_script(project, "diff()", on_call)
    assert run.failed
    assert "EditError: нет базы" in run.output


async def test_run_tests_appends_comment_notes_and_turns_red(project):
    (project / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    _, on_call = recording({"ok": True, "value": ["комментарии запрещены: src/a.py:2 # да"]})
    code = "r = run_tests(['test_ok.py'])\nprint(r.ok, r.code)\nprint(r)"
    run = await run_script(project, code, on_call)
    lines = run.output.splitlines()
    assert lines[0] == "False 1"
    assert lines[-1] == "комментарии запрещены: src/a.py:2 # да"
    assert run.green is False


async def test_run_tests_without_notes_keeps_result(project):
    (project / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    _, on_call = recording({"ok": True, "value": []})
    run = await run_script(project, "print(run_tests(['test_ok.py']).ok)", on_call)
    assert run.output == "True"
    assert run.green is True


async def test_host_wait_does_not_eat_script_budget(project):
    async def slow(request):
        await asyncio.sleep(1.5)
        return {"ok": True}

    code = f"step_done('шаг')\nprint(run({sys.executable + ' -c print(5)'!r}).ok)"
    run = await run_script(project, code, slow, budget=2.2)
    assert run.output == "True"


async def test_read_beyond_end_is_edit_error_with_line_count(project):
    run = await run_script(
        project, "try:\n    read('src/a.py', 10)\nexcept EditError as e:\n    print(e)"
    )
    assert run.output == "в файле src/a.py 3 строк"


async def test_git_command_is_refused_even_when_allowed(project):
    run = await run_script(project, "print(run('git diff'))", allowed=[["git"]])
    assert run.output == "[код 1]\ngit в песочнице нет: изменения показывает diff()"


def test_english_api_matches_russian_names_and_filters():
    assert [name for name, _, _ in SCRIPT_API_EN] == [name for name, _, _ in SCRIPT_API]
    assert "def grep(" in api_stubs("regular", SCRIPT_API_EN)
    assert api_stubs("регулярное", SCRIPT_API_EN) == ""
