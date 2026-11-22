from __future__ import annotations

import re
from datetime import date

import pytest

from sa_home_bot.devlab.agent.prompt import (
    CODE_MODE_INTRO,
    CODE_MODE_INTRO_REMOTE,
    CODE_MODE_REMINDER,
    CODE_MODE_REMINDER_REMOTE,
    FLOW_REMINDER,
    GLM_FLOW_REMINDER,
    GLM_INTRO,
    RULES_TITLE,
    SYSTEM_INTRO,
    build_system,
    code_mode_setting,
    glm_setting,
    intro_for,
    reminder_for,
    with_flow_reminder,
)
from sa_home_bot.devlab.agent.prompt_en import (
    CODE_MODE_INTRO_EN,
    CODE_MODE_INTRO_REMOTE_EN,
    CODE_MODE_REMINDER_EN,
    CODE_MODE_REMINDER_REMOTE_EN,
    RULES_TITLE_EN,
)
from sa_home_bot.devlab.agent.tools import (
    EXECUTE_DEFINITION,
    TOOL_ASK_OWNER,
    TOOL_EDIT_FILE,
    TOOL_EXECUTE,
    TOOL_FINISH,
    TOOL_GREP,
    TOOL_READ_FILE,
    TOOL_RUN_COMMAND,
    TOOL_RUN_TESTS,
    TOOL_SEARCH,
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



@pytest.mark.parametrize(
    ("model", "intro", "reminder"),
    [
        *[(model, CODE_MODE_INTRO, CODE_MODE_REMINDER) for model in GLM_MODELS],
        *[(model, CODE_MODE_INTRO_REMOTE, CODE_MODE_REMINDER_REMOTE) for model in OTHER_MODELS],
    ],
)
def test_code_mode_prompt_depends_on_glm_only(model, intro, reminder):
    system = build_system(RULES, model, COMMANDS, TODAY, code_mode=True)
    assert system.startswith(intro)
    assert reminder_for(model, code_mode=True) == reminder
    assert "Сегодня 2026-11-16." in system
    assert "`ruff check`, `python -m pytest`, `git diff`" in system
    assert "=== CLAUDE.md ===\nправило-один" in system


@pytest.mark.parametrize("model", [*GLM_MODELS, *OTHER_MODELS])
def test_without_code_mode_build_system_and_reminder_are_unchanged(model):
    assert build_system(RULES, model, COMMANDS, TODAY, code_mode=False) == build_system(
        RULES, model, COMMANDS, TODAY
    )
    assert reminder_for(model, code_mode=False) == reminder_for(model)


RU_CODE_MODE_TEXTS = [
    CODE_MODE_INTRO,
    CODE_MODE_INTRO_REMOTE,
    CODE_MODE_REMINDER,
    CODE_MODE_REMINDER_REMOTE,
]


@pytest.mark.parametrize("text", RU_CODE_MODE_TEXTS)
def test_code_mode_texts_name_no_hidden_tools(text):
    for hidden in (TOOL_READ_FILE, TOOL_EDIT_FILE, TOOL_RUN_COMMAND, "write_file", "list_files"):
        assert hidden not in text
    assert not re.search(r"\b(grep|run_tests)\b(?!\()", text)
    assert "по одной" not in text
    assert "обдумай" not in text


@pytest.mark.parametrize("text", RU_CODE_MODE_TEXTS)
def test_code_mode_texts_have_no_queue_after_script(text):
    assert "очеред" not in text
    assert "после скрипта" not in text


@pytest.mark.parametrize(
    "text",
    [CODE_MODE_INTRO, CODE_MODE_INTRO_REMOTE, EXECUTE_DEFINITION["function"]["description"]],
)
def test_code_mode_texts_say_state_persists_between_calls(text):
    assert "не сохраняются" not in text
    assert "новый процесс" not in text
    assert "Jupyter" in text or "Переменные держатся" in text


def test_code_mode_second_call_example_uses_variable_from_first():
    assert 'src = read("src/pkg/a.py"' in CODE_MODE_INTRO
    assert "src.splitlines" in CODE_MODE_INTRO


@pytest.mark.parametrize(
    ("text", "portions", "stale", "plan"),
    [
        (CODE_MODE_INTRO, "Пиши порциями", "от трёх до шести", "Код в размышлении не пиши"),
        (
            CODE_MODE_INTRO_EN,
            "Write in portions",
            "three to six",
            "Do not write code in your thinking",
        ),
        (
            CODE_MODE_REMINDER,
            "пиши порциями",
            "Порция работы = два вызова",
            "короткий план без кода",
        ),
        (
            CODE_MODE_REMINDER_EN,
            "write in portions",
            "A portion of work = two calls",
            "short plan without code",
        ),
    ],
)
def test_code_mode_texts_write_in_portions(text, portions, stale, plan):
    assert portions in text
    assert plan in text
    assert stale not in text
    assert "в том же вызове: правка теста" not in text


@pytest.mark.parametrize("text", [CODE_MODE_INTRO, CODE_MODE_INTRO_EN])
def test_code_mode_example_splits_test_and_code_calls(text):
    assert text.count('red = run_tests(["') == 1
    assert text.index('red = run_tests(["') < text.index('r = run_tests(["')
    assert text.index("old_test = next") < text.index('old = next(r for r in src')


@pytest.mark.parametrize("intro", [CODE_MODE_INTRO, CODE_MODE_INTRO_REMOTE])
def test_code_mode_intro_names_script_and_control_calls(intro):
    for name in (TOOL_SEARCH, TOOL_EXECUTE, TOOL_STEP_DONE, TOOL_FINISH, TOOL_ASK_OWNER):
        assert name in intro
    assert 'step_done("1–2: тест ловил ошибку, load возвращает 2")' not in intro
    assert 'step_done("3: docs/pkg.md дополнен")' not in intro
    assert "закрывает все шаги задания" in intro
    assert "необязательная промежуточная проверка" in intro
    assert 'finish("load возвращает 2; тесты, ruff и docs готовы")' in intro
    assert "список — в описании execute" in intro


@pytest.mark.parametrize(
    ("text", "batch", "single_call", "budget"),
    [
        (CODE_MODE_INTRO_REMOTE, "думай подробно", "одним вызовом", "Запросы ограничены"),
        (
            CODE_MODE_INTRO_REMOTE_EN,
            "Think in detail",
            "in one call",
            "Requests are limited",
        ),
        (CODE_MODE_REMINDER_REMOTE, "Думай подробно", "одним вызовом", "Запросы ограничены"),
        (CODE_MODE_REMINDER_REMOTE_EN, "Think in detail", "in one call", "Requests are limited"),
    ],
    ids=["intro-ru", "intro-en", "reminder-ru", "reminder-en"],
)
def test_remote_code_mode_texts_batch_edits_and_save_requests(text, batch, single_call, budget):
    assert batch.lower() in text.lower()
    assert single_call in text
    assert budget in text
    assert "порциями" not in text
    assert "in portions" not in text
    assert "короткий план без кода" not in text
    assert "short plan without code" not in text


@pytest.mark.parametrize(
    ("intro", "header"),
    [
        (CODE_MODE_INTRO_REMOTE, "три вызова\n"),
        (CODE_MODE_INTRO_REMOTE_EN, "in three calls\n"),
    ],
    ids=["ru", "en"],
)
def test_remote_code_mode_example_edits_test_and_code_in_one_script(intro, header):
    assert header in intro
    red = intro.index('red = run_tests(["')
    assert intro.count('red = run_tests(["') == 1
    test_edit = intro.index('edit("tests/test_a.py"')
    code_edit = intro.index('edit("src/pkg/a.py"')
    run = intro.index('r = run_tests(["')
    ruff = intro.index("ruff check src")
    assert test_edit < red < code_edit < run < ruff


def test_english_lang_swaps_code_mode_texts_and_titles():
    system = build_system(RULES, "glm", COMMANDS, TODAY, code_mode=True, lang="en")
    assert system.startswith(CODE_MODE_INTRO_EN)
    remote = build_system(RULES, "other", COMMANDS, TODAY, code_mode=True, lang="en")
    assert remote.startswith(CODE_MODE_INTRO_REMOTE_EN)
    assert reminder_for("other", code_mode=True, lang="en") == CODE_MODE_REMINDER_REMOTE_EN
    assert "## Setting\n- Today is 2026-11-16." in system
    assert f"\n\n{RULES_TITLE_EN}\n\n=== CLAUDE.md ===" in system
    assert RULES_TITLE not in system
    assert reminder_for("glm", code_mode=True, lang="en") == CODE_MODE_REMINDER_EN
    assert "## Setting" in code_mode_setting((), TODAY, "en")
    setting = code_mode_setting((), TODAY, "en")
    assert "- run(command) accepts commands starting with: none." in setting


def test_english_lang_without_code_mode_changes_nothing():
    assert build_system(RULES, "glm", COMMANDS, TODAY, lang="en") == build_system(
        RULES, "glm", COMMANDS, TODAY
    )
