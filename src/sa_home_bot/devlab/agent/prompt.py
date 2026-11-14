from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sa_home_bot.devlab.agent.tools import (
    TOOL_ASK_OWNER,
    TOOL_EDIT_FILE,
    TOOL_FINISH,
    TOOL_RUN_COMMAND,
    TOOL_RUN_TESTS,
    TOOL_STEP_DONE,
)

SYSTEM_INTRO = (
    "Ты автономный разработчик. Задание — в первом сообщении пользователя (task.md). "
    "Работай только инструментами: читай код, правь его через "
    f"{TOOL_EDIT_FILE}, проверяй через {TOOL_RUN_TESTS} и {TOOL_RUN_COMMAND}. "
    f"Сначала гоняй узкие тесты: {TOOL_RUN_TESTS} с paths для своих тестов; полный набор "
    f"проверит {TOOL_FINISH}. "
    f"Закончив, вызови {TOOL_FINISH} с кратким итогом: он проходит только при непустом диффе, "
    f"зелёных тестах и стражах проекта. Закончив часть работы, вызови {TOOL_STEP_DONE}. "
    f"Если без решения владельца нельзя двигаться дальше, вызови {TOOL_ASK_OWNER}. "
    "Ответ без вызова инструмента не засчитывается."
)
FLOW_REMINDER = (
    "[Reminder] Follow the task's steps («Шаги») in order. When a step's check is green, call "
    f"{TOOL_STEP_DONE} for it and move on to the next step, including the docs step. When all "
    "steps are done, run the task's final check («Финальный прогон») and ruff check on the changed "
    f"files, then call {TOOL_FINISH} in the same reply as soon as they are green."
)
RULES_TITLE = "Правила проекта (обязательны):"
CONTINUATION_TITLE = (
    "Работа уже начата и продолжается после перезапуска. Файлы, изменённые относительно базы "
    "(правки сохранены в рабочей копии, начинать заново не нужно):"
)
STEPS_TITLE = "Журнал шагов (уже сделано и принято):"
REMARKS_TITLE = "Замечания владельца к этой работе:"
CONTINUATION_LIMIT = 100


def build_system(rules: Sequence[tuple[str, str]]) -> str:
    parts = [SYSTEM_INTRO]
    if rules:
        parts.append(RULES_TITLE)
        parts.extend(f"=== {name} ===\n{text.strip()}" for name, text in rules)
    return "\n\n".join(parts)


def build_task(
    task: str,
    changed: Sequence[str],
    steps: Sequence[str] = (),
    remarks: str = "",
) -> str:
    parts = [task]
    if steps:
        parts += [STEPS_TITLE, "\n".join(f"{n}. {title}" for n, title in enumerate(steps, 1))]
    if remarks.strip():
        parts += [REMARKS_TITLE, remarks.strip()]
    if changed:
        shown = [f"- {path}" for path in changed[:CONTINUATION_LIMIT]]
        if len(changed) > CONTINUATION_LIMIT:
            shown.append(f"- ... и ещё {len(changed) - CONTINUATION_LIMIT}")
        parts += [CONTINUATION_TITLE, "\n".join(shown)]
    return "\n\n".join(parts)


def with_flow_reminder(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    view = list(messages)
    if view and isinstance(view[-1].get("content"), str):
        view[-1] = {**view[-1], "content": f"{view[-1]['content']}\n\n{FLOW_REMINDER}"}
    return view
