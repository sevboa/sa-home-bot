from __future__ import annotations

from collections.abc import Sequence

from sa_home_bot.devlab.agent.tools import (
    TOOL_EDIT_FILE,
    TOOL_FINISH,
    TOOL_RUN_COMMAND,
    TOOL_RUN_TESTS,
)

SYSTEM_INTRO = (
    "Ты автономный разработчик. Задание — в первом сообщении пользователя (task.md). "
    "Работай только инструментами: читай код, правь его через "
    f"{TOOL_EDIT_FILE}, проверяй через {TOOL_RUN_TESTS} и {TOOL_RUN_COMMAND}. "
    f"Закончив, вызови {TOOL_FINISH} с кратким итогом: он проходит только при непустом диффе, "
    "зелёных тестах и стражах проекта. Ответ без вызова инструмента не засчитывается."
)
RULES_TITLE = "Правила проекта (обязательны):"
CONTINUATION_TITLE = (
    "Работа уже начата и продолжается после перезапуска. Файлы, изменённые относительно базы "
    "(правки сохранены в рабочей копии, начинать заново не нужно):"
)
CONTINUATION_LIMIT = 100


def build_system(rules: Sequence[tuple[str, str]]) -> str:
    parts = [SYSTEM_INTRO]
    if rules:
        parts.append(RULES_TITLE)
        parts.extend(f"=== {name} ===\n{text.strip()}" for name, text in rules)
    return "\n\n".join(parts)


def build_task(task: str, changed: Sequence[str]) -> str:
    if not changed:
        return task
    shown = [f"- {path}" for path in changed[:CONTINUATION_LIMIT]]
    if len(changed) > CONTINUATION_LIMIT:
        shown.append(f"- ... и ещё {len(changed) - CONTINUATION_LIMIT}")
    return "\n\n".join([task, CONTINUATION_TITLE, "\n".join(shown)])
