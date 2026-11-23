import sys

import pytest

from sa_home_bot.config import DevlabProfileConfig
from sa_home_bot.devlab.agent import tools as tools_module
from sa_home_bot.devlab.agent.prompt import (
    CODE_MODE_INTRO_R2,
    CODE_MODE_INTRO_REMOTE,
    CODE_MODE_REMINDER_R2,
    CODE_MODE_REMINDER_REMOTE,
    build_system,
    reminder_for,
)
from sa_home_bot.devlab.agent.prompt_en import (
    CODE_MODE_INTRO_R2_EN,
    CODE_MODE_INTRO_REMOTE_EN,
)
from sa_home_bot.devlab.agent.remote import REMOTE_R2, SCRIPT_OUTPUT_MAX_CHARS_R2
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.script import SCRIPT_OUTPUT_CHARS, worker_argv
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import (
    EXECUTE_DEFINITION,
    EXECUTE_DEFINITION_R2,
    EXECUTE_DEFINITION_R2_EN,
    SCRIPT_OUTPUT_MAX_CHARS,
    TOOL_EXECUTE,
    ToolBox,
    script_output_chars,
)

MODEL = "nvidia/nemotron-3-ultra"


@pytest.fixture(autouse=True)
def current_python(monkeypatch):
    monkeypatch.setattr(
        tools_module, "worker_argv", lambda config: worker_argv(config, python=sys.executable)
    )


@pytest.fixture
def project(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_a():\n    assert 1\n")
    (tmp_path / "tests" / "test_bad.py").write_text(
        "def test_b():\n    assert 1 == 2\n\n\ndef test_c():\n    assert True\n"
    )
    return tmp_path


def box_for(root, remote_style=REMOTE_R2, **fields):
    spec = AgentSpec(
        code_mode=True,
        remote_style=remote_style,
        test_argv=(sys.executable, "-m", "pytest", "-q"),
        allow_no_changes=True,
        **fields,
    )
    return ToolBox(LocalSandbox(root), spec, window_tokens=262_144)


async def run(box, code):
    return (await box.call(TOOL_EXECUTE, {"code": code})).text


def test_profile_remote_style_default_is_r1():
    assert DevlabProfileConfig().remote_style == "r1"
    assert DevlabProfileConfig(remote_style="r2").remote_style == "r2"
    assert AgentSpec(remote_style="r2", prompt_style="cb_v3").chat_style == "r2"
    assert AgentSpec(prompt_style="cb_v4").chat_style == "cb_v4"


@pytest.mark.parametrize(
    "lang, r1, r2",
    [
        ("ru", CODE_MODE_INTRO_REMOTE, CODE_MODE_INTRO_R2),
        ("en", CODE_MODE_INTRO_REMOTE_EN, CODE_MODE_INTRO_R2_EN),
    ],
)
def test_prompt_by_remote_style(lang, r1, r2):
    for style in ("cb_v1", "cb_v3"):
        assert build_system([], MODEL, code_mode=True, lang=lang, style=style) == build_system(
            [], MODEL, code_mode=True, lang=lang
        )
    default = build_system([], MODEL, code_mode=True, lang=lang)
    remote = build_system([], MODEL, code_mode=True, lang=lang, style=REMOTE_R2)
    assert r1 in default and r2 not in default
    assert r2 in remote and r1 not in remote
    assert r1 != r2


def test_reminders_and_cost_rules():
    assert reminder_for(MODEL, True, "ru") == CODE_MODE_REMINDER_REMOTE
    assert reminder_for(MODEL, True, "ru", REMOTE_R2) == CODE_MODE_REMINDER_R2
    for needle in ("ВСЁ нужное", "finish()", "check()", str(SCRIPT_OUTPUT_MAX_CHARS_R2)):
        assert needle in CODE_MODE_INTRO_R2
    assert "check(" not in CODE_MODE_INTRO_REMOTE


def test_definitions_and_limit(project):
    assert box_for(project).definitions[1] is EXECUTE_DEFINITION_R2
    assert box_for(project, prompt_lang="en").definitions[1] is EXECUTE_DEFINITION_R2_EN
    description = EXECUTE_DEFINITION_R2["function"]["description"]
    assert "def check(" in description
    assert f"до {SCRIPT_OUTPUT_MAX_CHARS_R2} симв." in description
    assert f"до {SCRIPT_OUTPUT_CHARS} симв." in EXECUTE_DEFINITION["function"]["description"]
    assert box_for(project, remote_style="r1").definitions[1] is EXECUTE_DEFINITION
    assert script_output_chars(262_144, 6000, SCRIPT_OUTPUT_MAX_CHARS_R2) == (
        SCRIPT_OUTPUT_MAX_CHARS_R2
    )
    assert script_output_chars(262_144, 6000) == SCRIPT_OUTPUT_MAX_CHARS
    assert SCRIPT_OUTPUT_MAX_CHARS_R2 > SCRIPT_OUTPUT_MAX_CHARS


async def test_output_limit_r2_exceeds_r1(project):
    code = "print('x' * 60000)"
    assert len(await run(box_for(project), code)) > 50_000
    assert len(await run(box_for(project, remote_style="r1"), code)) < 41_000


async def test_green_run_is_one_line_and_red_has_no_progress(project):
    box = box_for(project)
    green = await run(box, "print(run_tests(['tests/test_ok.py']))")
    assert green.splitlines()[0] == "[код 0]" and len(green.splitlines()) == 2
    assert "passed" in green
    red = await run(box, "print(run_tests(['tests/test_bad.py']))")
    assert "FAILED tests/test_bad.py::test_b" in red
    assert "assert 1 == 2" in red
    assert not any(line.strip() and set(line.strip()) <= set(".F") for line in red.splitlines())


async def test_r1_run_tests_keeps_raw_output(project):
    out = await run(box_for(project, remote_style="r1"), "print(run_tests(['tests/test_ok.py']))")
    assert "check" not in out
    gone = await run(box_for(project, remote_style="r1"), "check()")
    assert "NameError" in gone


async def test_check_summarizes_tests_and_guards(project):
    box = box_for(project, checks=((sys.executable, "-c", "print('guard ok')"),))
    out = await run(box, "print(check(['tests/test_ok.py']))")
    assert "OK тесты tests/test_ok.py" in out and "guard ok" in out
    assert "[код 0]" in out
    failing = box_for(
        project, checks=((sys.executable, "-c", "print('boom'); raise SystemExit(1)"),)
    )
    out = await run(failing, "r = check(['tests/test_bad.py']); print(r.ok); print(r)")
    assert out.startswith("False") and "FAIL тесты tests/test_bad.py" in out
    assert "FAIL" in out and "boom" in out


async def test_check_without_paths_asks_host_for_changed_tests(project):
    box = box_for(project)
    out = await run(box, "print(check())")
    assert "нечего проверять" in out
