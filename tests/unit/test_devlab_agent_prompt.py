from __future__ import annotations

import pytest

from sa_home_bot.devlab.agent.prompt import (
    GLM_INTRO,
    RULES_TITLE,
    SYSTEM_INTRO,
    build_system,
    intro_for,
)
from sa_home_bot.devlab.agent.tools import (
    TOOL_ASK_OWNER,
    TOOL_EDIT_FILE,
    TOOL_FINISH,
    TOOL_RUN_COMMAND,
    TOOL_RUN_TESTS,
    TOOL_STEP_DONE,
)

RULES = [("CLAUDE.md", "  правило-один  \n"), ("docs/README.md", "правило-два")]
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
    assert build_system((), model) == legacy_system(())
    assert build_system(RULES, model) == legacy_system(RULES)


@pytest.mark.parametrize("model", GLM_MODELS)
def test_build_system_with_glm_model_starts_with_glm_intro(model):
    assert intro_for(model) == GLM_INTRO
    assert build_system((), model) == GLM_INTRO
    assert build_system(RULES, model).startswith(GLM_INTRO)
    assert build_system(RULES, model) == legacy_system(RULES).replace(
        SYSTEM_INTRO, GLM_INTRO, 1
    )


def test_intro_for_empty_model_is_system_intro():
    assert intro_for("") == SYSTEM_INTRO


def test_rules_title_and_format_kept_for_glm():
    system = build_system(RULES, "glm-5.3-rco")
    assert f"\n\n{RULES_TITLE}\n\n=== CLAUDE.md ===\nправило-один\n" in system
    assert "=== docs/README.md ===\nправило-два" in system


def test_glm_intro_names_tools_from_constants():
    for name in (
        TOOL_EDIT_FILE,
        TOOL_RUN_TESTS,
        TOOL_RUN_COMMAND,
        TOOL_FINISH,
        TOOL_STEP_DONE,
        TOOL_ASK_OWNER,
    ):
        assert name in GLM_INTRO


def test_glm_intro_is_markdown_with_positive_rules():
    assert GLM_INTRO.startswith("# Роль\n")
    assert "## Как работать" in GLM_INTRO
    assert "## Формат шага" in GLM_INTRO
    assert "## Процесс анализа" in GLM_INTRO
    assert "Один инструмент за ход" in GLM_INTRO
    assert "нельзя" not in GLM_INTRO
