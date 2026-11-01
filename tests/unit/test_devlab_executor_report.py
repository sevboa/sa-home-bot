from __future__ import annotations

from pathlib import Path

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.executor.report import (
    EXCERPT_CHARS,
    MAX_FAILURES,
    build_report,
    parse_junit,
    parse_text,
    tail_of,
)
from sa_home_bot.devlab.executor.spec import (
    DEFAULT_COMMAND_TIMEOUT_S,
    DEFAULT_OUTPUT_CHARS,
    DEFAULT_TIMEOUT_S,
    SPEC_FILE,
    SpecError,
    parse_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def summary(passed=0, failed=0, errors=0, skipped=0):
    return {
        protocol.SUMMARY_PASSED: passed,
        protocol.SUMMARY_FAILED: failed,
        protocol.SUMMARY_ERRORS: errors,
        protocol.SUMMARY_SKIPPED: skipped,
    }


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("....\n12 passed in 0.52s\n", summary(passed=12)),
        ("=== 12 passed in 0.52s (0:00:00) ===\n", summary(passed=12)),
        ("3 failed, 10 passed, 1 skipped in 2.10s\n", summary(passed=10, failed=3, skipped=1)),
        (
            "1 failed, 2 errors, 4 passed, 2 warnings in 1.00s\n",
            summary(passed=4, failed=1, errors=2),
        ),
        ("1 error in 0.10s\n", summary(errors=1)),
        ("no tests ran in 0.01s\n", summary()),
        ("что-то упало без итоговой строки\n", summary()),
    ],
)
def test_text_summary(output, expected):
    assert parse_text(output).summary == expected


def test_text_failures_with_excerpt():
    output = (
        "short test summary info\n"
        "FAILED tests/test_a.py::test_x - assert 1 == 2\n"
        "ERROR tests/test_b.py::test_y\n"
        "2 failed in 0.1s\n"
    )
    names = [
        (f[protocol.FAILURE_NAME], f[protocol.FAILURE_EXCERPT]) for f in parse_text(output).failures
    ]
    assert names == [("tests/test_a.py::test_x", "assert 1 == 2"), ("tests/test_b.py::test_y", "")]


def test_failures_are_capped():
    output = "".join(f"FAILED t.py::t{i} - {'x' * 2000}\n" for i in range(MAX_FAILURES + 20))
    failures = parse_text(output).failures
    assert len(failures) == MAX_FAILURES
    assert len(failures[0][protocol.FAILURE_EXCERPT]) == EXCERPT_CHARS


JUNIT = b"""<?xml version="1.0"?>
<testsuites><testsuite name="pytest" tests="5">
<testcase classname="tests.test_a" name="test_ok"/>
<testcase classname="tests.test_a" name="test_bad"><failure message="boom"/></testcase>
<testcase classname="tests.test_b" name="test_err"><error>setup died</error></testcase>
<testcase classname="tests.test_b" name="test_skip"><skipped message="s"/></testcase>
<testcase classname="tests.test_b" name="test_ok2"/>
</testsuite></testsuites>"""


def test_junit_preferred_over_text():
    report = build_report("99 passed in 1s\n", JUNIT)
    assert report.summary == summary(passed=2, failed=1, errors=1, skipped=1)
    assert [(f[protocol.FAILURE_NAME], f[protocol.FAILURE_EXCERPT]) for f in report.failures] == [
        ("tests.test_a::test_bad", "boom"),
        ("tests.test_b::test_err", "setup died"),
    ]


@pytest.mark.parametrize("junit", [None, b"", b"<broken", b"<testsuites/>"])
def test_text_used_without_usable_junit(junit):
    assert build_report("2 passed in 1s\n", junit).summary == summary(passed=2)


def test_parse_junit_rejects_garbage():
    assert parse_junit(b"not xml") is None


def test_tail_is_decoded_and_bounded():
    assert tail_of(b"a" * 10000 + "ы".encode()).endswith("ы")
    assert len(tail_of(b"a" * 10000)) <= 4096


def test_spec_defaults_without_file():
    section = parse_spec(None).test
    assert (section.cmd, section.args, section.timeout_s) == ("", [], DEFAULT_TIMEOUT_S)


def test_spec_reads_test_section_and_ignores_other_sections():
    data = (
        b'[test]\ncmd = "pytest"\nargs = ["-x"]\ntimeout_s = 60\nimage = "i"\nsetup = ["a"]\n'
        b"[other]\nx = 1\n"
    )
    section = parse_spec(data).test
    assert (section.cmd, section.args, section.timeout_s) == ("pytest", ["-x"], 60)
    assert (section.image, section.setup) == ("i", ["a"])


@pytest.mark.parametrize(
    "data",
    [b"[test", b"[test]\ncmd = 1\n", b"[test]\nbogus = 1\n", b"[agent]\nbogus = 1\n", b"\xff"],
)
def test_spec_errors(data):
    with pytest.raises(SpecError):
        parse_spec(data)


def test_agent_section_defaults_and_spec_from_project():
    project = parse_spec(b'[test]\ncmd = "pytest"\nargs = ["-q"]\ntimeout_s = 90\n')
    assert not project.agent.allow_remote_models and project.agent.rules == []
    spec = AgentSpec.from_project(project)
    assert spec.test_argv == ("pytest", "-q") and spec.test_timeout_s == 90
    assert spec.checks == () and spec.secret_paths == ()
    assert spec.command_timeout_s == DEFAULT_COMMAND_TIMEOUT_S
    assert spec.output_chars == DEFAULT_OUTPUT_CHARS


def test_agent_section_fills_agent_spec():
    data = (
        b"[agent]\nrules = [\"CLAUDE.md\"]\nchecks = [\"ruff check src\", \" \"]\n"
        b'allowed_commands = ["ruff", "python -m pytest"]\nforbidden_paths = ["tests/"]\n'
        b'secret_paths = [".env"]\ndiff_prefixes = ["src/"]\nallow_remote_models = true\n'
        b"command_timeout_s = 12\noutput_chars = 100\n"
    )
    project = parse_spec(data)
    assert project.agent.rules == ["CLAUDE.md"] and project.agent.allow_remote_models
    spec = AgentSpec.from_project(project)
    assert spec.test_argv == ()
    assert spec.checks == (("ruff", "check", "src"),)
    assert spec.allowed_commands == ("ruff", "python -m pytest")
    assert (spec.forbidden_paths, spec.secret_paths, spec.diff_prefixes) == (
        ("tests/",),
        (".env",),
        ("src/",),
    )
    assert (spec.command_timeout_s, spec.output_chars) == (12, 100)


@pytest.mark.parametrize("data", [b"[agent]\ncommand_timeout_s = 0\n", b"[agent]\nchecks = 1\n"])
def test_agent_section_validation(data):
    with pytest.raises(SpecError):
        parse_spec(data)


def test_repository_recipe_is_valid_and_guards_the_tree():
    project = parse_spec((REPO_ROOT / SPEC_FILE).read_bytes())
    spec = AgentSpec.from_project(project)
    assert project.agent.rules == ["CLAUDE.md"]
    assert spec.diff_prefixes == ("src/", "tests/")
    assert any("test_no_comments_in_code" in " ".join(check) for check in spec.checks)
    assert any(check[:2] == ("ruff", "check") for check in spec.checks)
    assert not project.agent.allow_remote_models
