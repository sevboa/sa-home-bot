from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

from sa_home_bot.devlab.agent.paths import matches
from sa_home_bot.devlab.agent.sandbox import Sandbox, SandboxPathError, clip_tail, safe_relative
from sa_home_bot.devlab.agent.script import (
    ACTION_EDIT,
    ACTION_RUN_TESTS,
    ACTION_WRITE,
    SCRIPT_FAILED_HEAD,
    parse_output,
    script_argv,
)
from sa_home_bot.devlab.agent.spec import AgentSpec

TOOL_LIST_FILES = "list_files"
TOOL_READ_FILE = "read_file"
TOOL_GREP = "grep"
TOOL_EDIT_FILE = "edit_file"
TOOL_WRITE_FILE = "write_file"
TOOL_RUN_COMMAND = "run_command"
TOOL_RUN_TESTS = "run_tests"
TOOL_FINISH = "finish"
TOOL_ASK_OWNER = "ask_owner"
TOOL_STEP_DONE = "step_done"
TOOL_RUN_SCRIPT = "run_script"
READ_ONLY_TOOLS = frozenset({TOOL_LIST_FILES, TOOL_READ_FILE, TOOL_GREP})
MUTATING_TOOLS = frozenset({TOOL_EDIT_FILE, TOOL_WRITE_FILE, TOOL_RUN_COMMAND})

LIST_MAX_ENTRIES = 200
READ_MIN_LINES = 200
READ_MAX_LINES_CAP = 2000
READ_TOKENS_PER_LINE = 60
GREP_MAX_MATCHES = 50
GREP_MAX_FILE_BYTES = 200_000
GREP_LINE_CHARS = 200
NEWLINE_ADDED = "; в конец файла дописан перевод строки"
SCRIPT_ACTION_TOOLS = {
    ACTION_EDIT: TOOL_EDIT_FILE,
    ACTION_WRITE: TOOL_WRITE_FILE,
    ACTION_RUN_TESTS: TOOL_RUN_TESTS,
}
SCRIPT_NO_OUTPUT = "(скрипт ничего не напечатал)"
SCRIPT_ACTIONS_SKIPPED = "действия скрипта не исполнены"
SCRIPT_DIRECT_WRITES = (
    "скрипт сам изменил файлы: {paths}; действия не исполнены. Файлы меняй только через "
    f"edit()/write() в скрипте; изменённое верни через {TOOL_EDIT_FILE}/{TOOL_WRITE_FILE}"
)


def read_max_lines(window_tokens: int) -> int:
    return max(READ_MIN_LINES, min(READ_MAX_LINES_CAP, window_tokens // READ_TOKENS_PER_LINE))


def _with_final_newline(data: bytes) -> bytes | None:
    if not data or data.endswith(b"\n") or b"\0" in data:
        return None
    try:
        data.decode()
    except UnicodeDecodeError:
        return None
    crlf = b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    return data + (b"\r\n" if crlf else b"\n")


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    mutating: bool = False
    green: bool | None = None


def _schema(name: str, description: str, **properties: dict[str, str]) -> dict[str, Any]:
    required = [key for key, spec in properties.items() if not spec.pop("optional", None)]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _text(description: str, optional: bool = False) -> dict[str, Any]:
    return {
        "type": "string",
        "description": description,
        **({"optional": True} if optional else {}),
    }


def _integer(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description, "optional": True}


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _schema(
        TOOL_LIST_FILES,
        "Список файлов проекта (относительные пути).",
        prefix=_text("Только пути, начинающиеся с этого префикса", optional=True),
    ),
    _schema(
        TOOL_READ_FILE,
        "Читает файл по строкам; за вызов отдаёт сколько поместится, в ответе видно, сколько "
        "строк всего.",
        path=_text("Путь файла"),
        start_line=_integer("Первая строка, с 1"),
        end_line=_integer("Последняя строка включительно"),
    ),
    _schema(
        TOOL_GREP,
        "Поиск регулярного выражения по текстовым файлам проекта.",
        pattern=_text("Регулярное выражение"),
        path_prefix=_text("Искать только в путях с этим префиксом", optional=True),
    ),
    _schema(
        TOOL_EDIT_FILE,
        "Заменяет в файле один фрагмент old на new. Фрагмент должен встречаться ровно один раз.",
        path=_text("Путь файла"),
        old=_text("Заменяемый фрагмент"),
        new=_text("Новый фрагмент"),
    ),
    _schema(
        TOOL_WRITE_FILE,
        "Создаёт файл или полностью перезаписывает его.",
        path=_text("Путь файла"),
        content=_text("Содержимое файла"),
    ),
    _schema(
        TOOL_RUN_COMMAND,
        "Запускает разрешённую команду без оболочки.",
        command=_text("Команда одной строкой"),
    ),
    _schema(
        TOOL_RUN_TESTS,
        "Запускает указанные тесты проекта (узкий прогон). paths обязателен: весь набор "
        "проверит finish.",
        paths=_text("Пути тестов через пробел; paths обязателен"),
    ),
    _schema(
        TOOL_STEP_DONE,
        "Фиксирует законченный шаг. Принимается только при зелёных тестах и стражах проекта.",
        title=_text("Что сделано на этом шаге, одной строкой"),
    ),
    _schema(
        TOOL_ASK_OWNER,
        "Задаёт вопрос владельцу и ждёт ответа (может занять часы). Только если без ответа "
        "нельзя двигаться дальше.",
        question=_text("Вопрос с нужным контекстом"),
    ),
    _schema(
        TOOL_FINISH,
        "Завершает работу. Принимается только при изменённых файлах и зелёных тестах.",
        summary=_text("Что сделано"),
    ),
]

RUN_SCRIPT_DEFINITION = _schema(
    TOOL_RUN_SCRIPT,
    "Исполняет код Python в корне проекта. Доступны ls(prefix), read(path, start, end), "
    "read_many(paths), grep(pattern, prefix, context), edit(path, old, new), write(path, content), "
    "run_tests(paths). Нужное печатай через print. Собирай в одном скрипте всё нужное для шага: "
    "читай сразу все связанные файлы. edit/write/run_tests исполняются после скрипта по порядку.",
    code=_text("Код Python"),
)


def _argument(args: dict[str, Any], key: str, kind: type, default: Any = None) -> Any:
    value = args.get(key, default)
    if value is None and default is None and kind is not int:
        raise ToolError(f"не хватает аргумента {key}")
    if value is not None and not isinstance(value, kind):
        raise ToolError(f"аргумент {key}: ожидается {kind.__name__}")
    return value


XDIST_VALUE_FLAGS = frozenset({"-n", "--numprocesses"})
XDIST_INLINE = re.compile(r"^(-n.+|--numprocesses=.+)$")


def without_xdist(argv: list[str]) -> list[str]:
    kept: list[str] = []
    skip_value = False
    for arg in argv:
        if skip_value:
            skip_value = False
        elif arg in XDIST_VALUE_FLAGS:
            skip_value = True
        elif not XDIST_INLINE.match(arg):
            kept.append(arg)
    return kept


class ToolBox:
    def __init__(self, sandbox: Sandbox, spec: AgentSpec, read_lines: int = READ_MIN_LINES) -> None:
        self._sandbox = sandbox
        self._spec = spec
        self._read_lines = read_lines
        self._handlers = {
            TOOL_LIST_FILES: self._list_files,
            TOOL_READ_FILE: self._read_file,
            TOOL_GREP: self._grep,
            TOOL_EDIT_FILE: self._edit_file,
            TOOL_WRITE_FILE: self._write_file,
            TOOL_RUN_COMMAND: self._run_command,
            TOOL_RUN_TESTS: self._run_tests,
        }
        if spec.code_mode:
            self._handlers[TOOL_RUN_SCRIPT] = self._run_script

    @property
    def definitions(self) -> list[dict[str, Any]]:
        if self._spec.code_mode:
            return [*TOOL_DEFINITIONS, RUN_SCRIPT_DEFINITION]
        return TOOL_DEFINITIONS

    async def tree_fingerprint(self) -> str:
        changed = await self._sandbox.changed_digests()
        return hashlib.sha256(json.dumps(changed, sort_keys=True).encode()).hexdigest()

    async def call(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError(f"неизвестный инструмент {name!r}")
        try:
            result = await handler(args)
        except SandboxPathError as exc:
            raise ToolError(str(exc)) from exc
        if isinstance(result, ToolOutcome):
            return result
        return ToolOutcome(result, mutating=name in MUTATING_TOOLS)

    def _check_writable(self, path: str) -> str:
        relative = safe_relative(path)
        if matches(relative, self._spec.forbidden_paths + self._spec.secret_paths):
            raise ToolError(f"путь {relative} закрыт для правок правилами проекта")
        if self._spec.write_prefixes and not any(
            relative.startswith(prefix) for prefix in self._spec.write_prefixes
        ):
            raise ToolError(
                f"путь {relative} вне путей роли: {', '.join(self._spec.write_prefixes)}"
            )
        return relative

    async def _read_text(self, path: str) -> str:
        data = await self._sandbox.read_bytes(path)
        if data is None:
            raise ToolError(f"файл {path} не найден")
        try:
            return data.decode()
        except UnicodeDecodeError as exc:
            raise ToolError(f"файл {path} не текстовый") from exc

    async def _list_files(self, args: dict[str, Any]) -> str:
        prefix = _argument(args, "prefix", str, "")
        files = [f for f in await self._sandbox.list_files() if f.startswith(prefix)]
        shown = files[:LIST_MAX_ENTRIES]
        tail = f"\n... ещё {len(files) - len(shown)}" if len(files) > len(shown) else ""
        return "\n".join(shown) + tail if shown else "файлов нет"

    async def _read_file(self, args: dict[str, Any]) -> str:
        path = _argument(args, "path", str)
        lines = (await self._read_text(path)).splitlines()
        start = max(1, _argument(args, "start_line", int, 1))
        end = min(len(lines), _argument(args, "end_line", int, start + self._read_lines - 1))
        end = min(end, start + self._read_lines - 1)
        if start > len(lines):
            raise ToolError(f"в файле {path} только {len(lines)} строк")
        body = "\n".join(f"{n}\t{lines[n - 1]}" for n in range(start, end + 1))
        return f"{path}, строки {start}-{end} из {len(lines)}\n{body}"

    async def _grep(self, args: dict[str, Any]) -> str:
        try:
            pattern = re.compile(_argument(args, "pattern", str))
        except re.error as exc:
            raise ToolError(f"регулярное выражение: {exc}") from exc
        prefix = _argument(args, "path_prefix", str, "")
        matches: list[str] = []
        for path in await self._sandbox.list_files():
            if not path.startswith(prefix):
                continue
            data = await self._sandbox.read_bytes(path)
            if data is None or len(data) > GREP_MAX_FILE_BYTES:
                continue
            try:
                text = data.decode()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    matches.append(f"{path}:{number}:{line[:GREP_LINE_CHARS]}")
                    if len(matches) >= GREP_MAX_MATCHES:
                        return "\n".join(matches) + f"\n... показаны первые {GREP_MAX_MATCHES}"
        return "\n".join(matches) if matches else "совпадений нет"

    async def _edit_file(self, args: dict[str, Any]) -> str:
        path = self._check_writable(_argument(args, "path", str))
        old = _argument(args, "old", str)
        new = _argument(args, "new", str)
        text = await self._read_text(path)
        found = text.count(old)
        if not old or found != 1:
            raise ToolError(
                f"фрагмент old найден {found} раз в {path}: нужно ровно один, добавь контекста"
            )
        data = text.replace(old, new, 1).encode()
        fixed = _with_final_newline(data)
        await self._sandbox.write_bytes(path, fixed or data)
        return f"правка {path}: заменён один фрагмент" + (NEWLINE_ADDED if fixed else "")

    async def _write_file(self, args: dict[str, Any]) -> str:
        path = self._check_writable(_argument(args, "path", str))
        content = _argument(args, "content", str)
        data = content.encode()
        fixed = _with_final_newline(data)
        await self._sandbox.write_bytes(path, fixed or data)
        return f"записан {path}: {len(content)} симв." + (NEWLINE_ADDED if fixed else "")

    async def _run_command(self, args: dict[str, Any]) -> str:
        try:
            argv = shlex.split(_argument(args, "command", str))
        except ValueError as exc:
            raise ToolError(f"команда не разобрана: {exc}") from exc
        if not any(argv[: len(allowed)] == allowed for allowed in self._allowed()):
            raise ToolError(
                f"команда не разрешена; разрешены: {', '.join(self._spec.allowed_commands)}"
            )
        result = await self._sandbox.exec(argv, self._spec.command_timeout_s)
        return result.summary(self._spec.output_chars, self._spec.command_timeout_s)

    def _allowed(self) -> list[list[str]]:
        return [shlex.split(entry) for entry in self._spec.allowed_commands if entry.strip()]

    async def _run_tests(self, args: dict[str, Any]) -> ToolOutcome:
        if not self._spec.test_argv:
            raise ToolError("команда тестов не задана")
        tokens = _argument(args, "paths", str, "").split()
        if not tokens:
            raise ToolError(
                "paths обязателен: укажи свои тесты, весь набор проверит finish"
            )
        if any(t.startswith("-") for t in tokens):
            raise ToolError("paths — только пути тестов, без флагов pytest")
        paths = [safe_relative(t) for t in tokens]
        argv = without_xdist([*self._spec.test_argv, *paths])
        timeout_s = self._spec.narrow_test_timeout_s
        result = await self._sandbox.exec(argv, timeout_s)
        text = result.summary(self._spec.output_chars, timeout_s)
        return ToolOutcome(text, green=result.ok)

    async def _run_script(self, args: dict[str, Any]) -> ToolOutcome:
        code = _argument(args, "code", str)
        timeout_s = self._spec.command_timeout_s
        before = await self._sandbox.changed_digests()
        result = await self._sandbox.exec(script_argv(code), timeout_s)
        after = await self._sandbox.changed_digests()
        touched = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
        if touched:
            return ToolOutcome(SCRIPT_DIRECT_WRITES.format(paths=", ".join(touched)), mutating=True)
        if result.timed_out:
            return ToolOutcome(result.summary(self._spec.output_chars, timeout_s))
        run = parse_output(result.output)
        output = clip_tail(run.output, self._spec.output_chars) or SCRIPT_NO_OUTPUT
        if run.failed:
            skipped = f"\n{SCRIPT_ACTIONS_SKIPPED}" if run.actions else ""
            return ToolOutcome(f"{SCRIPT_FAILED_HEAD}\n{output}{skipped}")
        lines = [output]
        mutating = False
        green: bool | None = None
        failed = False
        for number, action in enumerate(run.actions, 1):
            tool = SCRIPT_ACTION_TOOLS.get(action.tool)
            label = f"действие {number} {tool or action.tool}"
            if failed:
                lines.append(f"{label}: пропущено после ошибки")
                continue
            try:
                if tool is None:
                    raise ToolError(f"неизвестное действие {action.tool!r}")
                outcome = await self._handlers[tool](action.args)
            except (ToolError, SandboxPathError) as exc:
                lines.append(f"{label}: ошибка: {exc}")
                failed = True
                continue
            if isinstance(outcome, ToolOutcome):
                green = outcome.green if outcome.green is not None else green
                outcome = outcome.text
            mutating = mutating or tool in MUTATING_TOOLS
            lines.append(f"{label}: {outcome}")
        return ToolOutcome("\n".join(lines), mutating=mutating, green=green)
