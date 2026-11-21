from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from sa_home_bot.devlab.agent.context import FOLDED_OUTPUT_PREFIX
from sa_home_bot.devlab.agent.script import SCRIPT_EXAMPLE
from sa_home_bot.devlab.agent.tools import (
    TOOL_ASK_OWNER,
    TOOL_EDIT_FILE,
    TOOL_FINISH,
    TOOL_GREP,
    TOOL_LIST_FILES,
    TOOL_READ_FILE,
    TOOL_RUN_COMMAND,
    TOOL_RUN_SCRIPT,
    TOOL_RUN_TESTS,
    TOOL_STEP_DONE,
    TOOL_WRITE_FILE,
)

SYSTEM_INTRO = (
    "Ты автономный разработчик. Задание — в первом сообщении пользователя (task.md). "
    "Работай только инструментами: читай код, правь его через "
    f"{TOOL_EDIT_FILE}, проверяй через {TOOL_RUN_TESTS} и {TOOL_RUN_COMMAND}. "
    f"Шаг закрывай сразу {TOOL_STEP_DONE}: он сам прогонит изменённые тесты и стражи; "
    f"{TOOL_RUN_TESTS} с paths зови только чтобы увидеть красный тест до правки или "
    f"разобрать падение; полный набор проверит {TOOL_FINISH}. "
    f"Закончив, вызови {TOOL_FINISH} с кратким итогом: он проходит только при непустом диффе, "
    f"зелёных тестах и стражах проекта. Закончив часть работы, вызови {TOOL_STEP_DONE}. "
    f"Если без решения владельца нельзя двигаться дальше, вызови {TOOL_ASK_OWNER}. "
    "Ответ без вызова инструмента не засчитывается."
)
GLM_INTRO = (
    "# Роль\n"
    "Ты инженер-разработчик. Ты работаешь в песочнице с копией репозитория и действуешь "
    "через предоставленные инструменты. Задание лежит в первом сообщении пользователя (task.md): "
    "цель, файлы, шаги и проверки.\n"
    "\n"
    "## Как работать\n"
    "- Иди по шагам задания («Шаги») по порядку.\n"
    "- Перед правкой прочитай нужный участок и образец рядом через "
    f"{TOOL_READ_FILE} и {TOOL_GREP}.\n"
    f"- Правь код через {TOOL_EDIT_FILE}, новый файл создавай через {TOOL_WRITE_FILE}.\n"
    f"- Шаг сделан — сразу вызывай {TOOL_STEP_DONE}: он сам прогонит изменённые тесты и "
    f"стражи. {TOOL_RUN_TESTS} с paths зови, только когда нужно увидеть красный тест до "
    "правки или разобрать падение.\n"
    "- Шаг готов, когда выполнено его условие «Готово, когда». Условие зелёное: вызови "
    f"{TOOL_STEP_DONE} и переходи к следующему шагу.\n"
    f"- {TOOL_STEP_DONE} прогоняет стражи и изменённые тестовые файлы и принимает только "
    f"зелёное, весь набор проверит {TOOL_FINISH}. Шаг, "
    "который по заданию заканчивается красным тестом, закрывай вместе с правкой: убедись узким "
    "прогоном, что тест красный по нужной причине, сразу делай правку и вызывай "
    f"{TOOL_STEP_DONE}, когда тесты позеленеют.\n"
    "- Все шаги готовы: прогони финальную проверку задания («Финальный прогон») и `ruff check` "
    f"по изменённым файлам, затем вызови {TOOL_FINISH} с кратким итогом на русском. Он проходит "
    "при непустом диффе, зелёных тестах и стражах проекта.\n"
    f"- Решение может принять только владелец: вызови {TOOL_ASK_OWNER}.\n"
    "\n"
    "## Формат хода\n"
    f"- Независимые чтения ({TOOL_READ_FILE}, {TOOL_GREP}, {TOOL_LIST_FILES}) отправляй в одном "
    "ходе.\n"
    "- Правку и проверку делай по одной и смотри результат, прежде чем решать дальше.\n"
    "- Каждый ход заканчивается вызовом инструмента: так засчитывается работа.\n"
    "\n"
    "## Процесс анализа\n"
    "Перед каждой правкой кода кратко обдумай:\n"
    "1. какие файлы и функции затронет изменение;\n"
    "2. не сломает ли оно импорты, сигнатуры и существующие тесты;\n"
    "3. какой узкий тест это проверит.\n"
    "Рассуждай коротко и по делу, затем действуй."
)
GLM_SETTING_TITLE = "## Обстановка"
GLM_SETTING = (
    "- Сегодня {today}. Эту дату ставь у новых находок в `docs/`.\n"
    f"- {TOOL_RUN_COMMAND} запускает команду без оболочки, разрешены команды, которые начинаются "
    "так: {commands}. Песочница уже настроена: команда пишется как есть, без `cd`, пайпов и "
    "переменных окружения вроде `PYTHONPATH`.\n"
    f"- Ты не коммитишь, не деплоишь и не вызываешь скилы: {TOOL_STEP_DONE} и {TOOL_FINISH} сами "
    "сохраняют работу. Правила проекта ниже написаны и для людей; тебя касаются правила про код, "
    "тесты и документацию."
)
FLOW_REMINDER = (
    "[Reminder] Follow the task's steps («Шаги») in order. When a step's check is green, call "
    f"{TOOL_STEP_DONE} for it and move on to the next step, including the docs step. When all "
    "steps are done, run the task's final check («Финальный прогон») and ruff check on the changed "
    f"files, then call {TOOL_FINISH} in the same reply as soon as they are green."
)
GLM_FLOW_REMINDER = (
    "[Напоминание] Иди по шагам задания («Шаги») по порядку. Проверка шага зелёная: вызови "
    f"{TOOL_STEP_DONE} и переходи к следующему, включая шаг документации. Шаг, который по заданию "
    f"заканчивается красным тестом, закрой {TOOL_STEP_DONE} вместе с правкой. Все шаги готовы: "
    "финальный прогон задания и `ruff check` по изменённым файлам, и как только они зелёные, "
    f"вызови {TOOL_FINISH}."
)
CODE_MODE_INTRO = (
    "# Роль\n"
    "Ты инженер-разработчик в песочнице с копией репозитория. Задание лежит в первом сообщении "
    "пользователя (task.md): цель, файлы, шаги и проверки. Работаешь только через "
    f"{TOOL_RUN_SCRIPT}: пишешь код Python, он читает файлы сразу пачкой, а правки, команды и "
    "тесты ставит в очередь, и очередь исполняется после скрипта по порядку.\n"
    "\n"
    "## Цикл шага\n"
    "- Скрипт 1: прочитай всё нужное шагу сразу, read_many([...]) и grep(шаблон, префикс, "
    "context=N) для образцов. Печатай только нужное.\n"
    "- Скрипт 2: все edit() и write() шага и run_tests([...]) в том же скрипте. Когда тесты "
    f"должны позеленеть, в конце этого же скрипта вызови {TOOL_STEP_DONE}(\"итог шага\"): "
    "страж прогонит изменённые тесты и примет шаг только на зелёном. Действие step_done или "
    "finish ставь последним в скрипте.\n"
    "- Ошибку действия (фрагмент не найден, красный тест) чини следующим скриптом, не "
    "перечитывая всё. Виденное не перечитывай.\n"
    "- Шаг, который по заданию заканчивается красным тестом, закрывай вместе с правкой.\n"
    "- Последний шаг: в одном скрипте run(\"ruff check ...\") по изменённым файлам и "
    f"{TOOL_FINISH}(\"итог\"). Он проходит при непустом диффе, зелёных тестах и стражах проекта.\n"
    f"- Решение может принять только владелец: вызови {TOOL_ASK_OWNER}.\n"
    "\n"
    "## Пример шага в одном скрипте\n"
    "Когда нужный текст уже известен, шаг целиком умещается в один скрипт: читать, править, "
    "проверить, закрыть.\n"
    f"{SCRIPT_EXAMPLE}\n"
    "\n"
    "Каждый ход заканчивается вызовом инструмента: так засчитывается работа."
)
CODE_MODE_SETTING = (
    "- Сегодня {today}. Эту дату ставь у новых находок в `docs/`.\n"
    "- run(команда) ставит в очередь команду без оболочки, разрешены команды, которые "
    "начинаются так: {commands}. Песочница уже настроена: команда пишется как есть, без `cd`, "
    "пайпов и переменных окружения вроде `PYTHONPATH`.\n"
    "- Ты не коммитишь и не деплоишь: step_done и finish сами сохраняют работу. Правила "
    "проекта ниже написаны и для людей; тебя касаются правила про код, тесты и документацию."
)
CODE_MODE_REMINDER = (
    "[Напоминание] Работай скриптами. Шаг: одним скриптом прочитай нужное (read_many, grep(..., "
    "context=N)), затем одним скриптом все edit()/write() и run_tests([...]) и в его конце "
    "step_done(\"итог шага\"). Ошибку действия чини следующим скриптом, не перечитывая всё. Шаг, "
    "который по заданию заканчивается красным тестом, закрывай вместе с правкой. Все шаги "
    "готовы: в одном скрипте run(\"ruff check ...\") по изменённым файлам и finish(\"итог\")."
)
RULES_TITLE = "Правила проекта (обязательны):"
CONTINUATION_TITLE = (
    "Работа уже начата и продолжается после перезапуска. Файлы, изменённые относительно базы "
    "(правки сохранены в рабочей копии, начинать заново не нужно):"
)
STEPS_TITLE = "Журнал шагов (уже сделано и принято):"
REMARKS_TITLE = "Замечания владельца к этой работе:"
CONTINUATION_LIMIT = 100


def _is_glm(model: str) -> bool:
    return "glm" in model.lower()


def intro_for(model: str) -> str:
    return GLM_INTRO if _is_glm(model) else SYSTEM_INTRO


def reminder_for(model: str, code_mode: bool = False) -> str:
    if code_mode:
        return CODE_MODE_REMINDER
    return GLM_FLOW_REMINDER if _is_glm(model) else FLOW_REMINDER


def _setting(template: str, commands: Sequence[str], today: date) -> str:
    listed = ", ".join(f"`{command}`" for command in commands) or "нет"
    return f"{GLM_SETTING_TITLE}\n{template.format(today=today.isoformat(), commands=listed)}"


def glm_setting(commands: Sequence[str], today: date) -> str:
    return _setting(GLM_SETTING, commands, today)


def code_mode_setting(commands: Sequence[str], today: date) -> str:
    return _setting(CODE_MODE_SETTING, commands, today)


def build_system(
    rules: Sequence[tuple[str, str]],
    model: str = "",
    commands: Sequence[str] = (),
    today: date | None = None,
    code_mode: bool = False,
) -> str:
    parts = [CODE_MODE_INTRO if code_mode else intro_for(model)]
    if code_mode:
        parts.append(code_mode_setting(commands, today or date.today()))
    elif _is_glm(model):
        parts.append(glm_setting(commands, today or date.today()))
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


def _closes_step(messages: Sequence[dict[str, Any]], index: int) -> bool:
    content = messages[index].get("content")
    if not isinstance(content, str) or content.startswith(FOLDED_OUTPUT_PREFIX):
        return False
    return index == len(messages) - 1 or messages[index + 1].get("role") == "assistant"


def with_flow_reminder(
    messages: Sequence[dict[str, Any]], reminder: str = FLOW_REMINDER
) -> list[dict[str, Any]]:
    return [
        {**message, "content": f"{message['content']}\n\n{reminder}"}
        if _closes_step(messages, index)
        else message
        for index, message in enumerate(messages)
    ]
