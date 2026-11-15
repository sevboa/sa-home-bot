from __future__ import annotations

from datetime import date

import pytest

from sa_home_bot.devlab.agent.prompt import (
    FLOW_REMINDER,
    GLM_FLOW_REMINDER,
    GLM_INTRO,
    RULES_TITLE,
    SYSTEM_INTRO,
    build_system,
    glm_setting,
    intro_for,
    reminder_for,
    with_flow_reminder,
)
from sa_home_bot.devlab.agent.tools import (
    TOOL_ASK_OWNER,
    TOOL_EDIT_FILE,
    TOOL_FINISH,
    TOOL_GREP,
    TOOL_READ_FILE,
    TOOL_RUN_COMMAND,
    TOOL_RUN_TESTS,
    TOOL_STEP_DONE,
)

RULES = [("CLAUDE.md", "  правило-один  \n"), ("docs/README.md", "правило-два")]
COMMANDS = ("ruff check", "python -m pytest", "git diff")
TODAY = date(2026, 11, 16)
GLM_MODELS = ["glm-5.3-rco", "GLM-Maya-XS", "project-maya/glm-5.3-iq3"]
OTHER_MODELS = ["qwen3.8-iq3_xxs", "flint", "gpt-oss-120b", ""]


def legacy_system(rules) -> str:
    parts = [SYSTEM_INTRO]
    if rules:
        parts.append(RULES_TITLE)
        parts.extend(f"=== {name} ===\n{text.strip()}" for name, text in rules)
    return "\n\n".join(parts)


def test_build_system_without_model_matches_legacy():
    assert build_system(()) == legacy_system(())
    assert build_system(RULES) == legacy_system(RULES)


@pytest.mark.parametrize("model", OTHER_MODELS)
def test_build_system_with_non_glm_model_matches_legacy(model):
    assert build_system((), model, COMMANDS, TODAY) == legacy_system(())
    assert build_system(RULES, model, COMMANDS, TODAY) == legacy_system(RULES)
    assert intro_for(model) == SYSTEM_INTRO
    assert reminder_for(model) == FLOW_REMINDER


@pytest.mark.parametrize("model", GLM_MODELS)
def test_build_system_with_glm_model_has_intro_setting_and_rules(model):
    setting = glm_setting(COMMANDS, TODAY)
    assert intro_for(model) == GLM_INTRO
    assert reminder_for(model) == GLM_FLOW_REMINDER
    assert build_system((), model, COMMANDS, TODAY) == f"{GLM_INTRO}\n\n{setting}"
    assert build_system(RULES, model, COMMANDS, TODAY) == legacy_system(RULES).replace(
        SYSTEM_INTRO, f"{GLM_INTRO}\n\n{setting}", 1
    )


def test_glm_setting_names_today_and_allowed_commands():
    setting = glm_setting(COMMANDS, TODAY)
    assert setting.startswith("## Обстановка\n")
    assert "Сегодня 2026-11-16." in setting
    assert "`ruff check`, `python -m pytest`, `git diff`" in setting
    assert "PYTHONPATH" in setting
    assert "разрешены команды, которые начинаются так: нет." in glm_setting((), TODAY)


def test_rules_title_and_format_kept_for_glm():
    system = build_system(RULES, "glm-5.3-rco", COMMANDS, TODAY)
    assert f"\n\n{RULES_TITLE}\n\n=== CLAUDE.md ===\nправило-один\n" in system
    assert "=== docs/README.md ===\nправило-два" in system


def test_glm_intro_names_tools_from_constants():
    for name in (
        TOOL_READ_FILE,
        TOOL_GREP,
        TOOL_EDIT_FILE,
        TOOL_RUN_TESTS,
        TOOL_FINISH,
        TOOL_STEP_DONE,
        TOOL_ASK_OWNER,
    ):
        assert name in GLM_INTRO
    assert TOOL_RUN_COMMAND not in GLM_INTRO


def test_glm_intro_is_markdown_with_positive_rules():
    assert GLM_INTRO.startswith("# Роль\n")
    for heading in ("## Как работать", "## Формат хода", "## Процесс анализа"):
        assert heading in GLM_INTRO
    assert "Один инструмент за ход" not in GLM_INTRO
    assert "заканчивается красным тестом" in GLM_INTRO
    assert "нельзя" not in GLM_INTRO


def test_glm_reminder_is_russian_and_rides_on_step_end():
    sent = with_flow_reminder([{"role": "user", "content": "t"}], GLM_FLOW_REMINDER)
    assert sent == [{"role": "user", "content": f"t\n\n{GLM_FLOW_REMINDER}"}]
    assert GLM_FLOW_REMINDER.startswith("[Напоминание]")
    assert "заканчивается красным тестом" in GLM_FLOW_REMINDER
