from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

from sa_home_bot.devlab.agent.paths import matches
from sa_home_bot.devlab.agent.sandbox import Sandbox, SandboxPathError, safe_relative
from sa_home_bot.devlab.agent.spec import AgentSpec

TOOL_LIST_FILES = "list_files"
TOOL_READ_FILE = "read_file"
TOOL_GREP = "grep"
TOOL_EDIT_FILE = "edit_file"
TOOL_WRITE_FILE = "write_file"
TOOL_RUN_COMMAND = "run_command"
TOOL_RUN_TESTS = "run_tests"
TOOL_FINISH = "finish"
MUTATING_TOOLS = frozenset({TOOL_EDIT_FILE, TOOL_WRITE_FILE, TOOL_RUN_COMMAND})

LIST_MAX_ENTRIES = 200
READ_MAX_LINES = 200
GREP_MAX_MATCHES = 50
GREP_MAX_FILE_BYTES = 200_000
GREP_LINE_CHARS = 200


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    mutating: bool = False


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
        f"Читает файл по строкам, не больше {READ_MAX_LINES} строк за вызов.",
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
    _schema(TOOL_RUN_TESTS, "Запускает тесты проекта."),
    _schema(
        TOOL_FINISH,
        "Завершает работу. Принимается только при изменённых файлах и зелёных тестах.",
        summary=_text("Что сделано"),
    ),
]


def _argument(args: dict[str, Any], key: str, kind: type, default: Any = None) -> Any:
    value = args.get(key, default)
    if value is None and default is None and kind is not int:
        raise ToolError(f"не хватает аргумента {key}")
    if value is not None and not isinstance(value, kind):
        raise ToolError(f"аргумент {key}: ожидается {kind.__name__}")
    return value


class ToolBox:
    def __init__(self, sandbox: Sandbox, spec: AgentSpec) -> None:
        self._sandbox = sandbox
        self._spec = spec
        self._handlers = {
            TOOL_LIST_FILES: self._list_files,
            TOOL_READ_FILE: self._read_file,
            TOOL_GREP: self._grep,
            TOOL_EDIT_FILE: self._edit_file,
            TOOL_WRITE_FILE: self._write_file,
            TOOL_RUN_COMMAND: self._run_command,
            TOOL_RUN_TESTS: self._run_tests,
        }

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return TOOL_DEFINITIONS

    async def call(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError(f"неизвестный инструмент {name!r}")
        try:
            text = await handler(args)
        except SandboxPathError as exc:
            raise ToolError(str(exc)) from exc
        return ToolOutcome(text, mutating=name in MUTATING_TOOLS)

    def _check_writable(self, path: str) -> str:
        relative = safe_relative(path)
        if matches(relative, self._spec.forbidden_paths + self._spec.secret_paths):
            raise ToolError(f"путь {relative} закрыт для правок правилами проекта")
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
        end = min(len(lines), _argument(args, "end_line", int, start + READ_MAX_LINES - 1))
        end = min(end, start + READ_MAX_LINES - 1)
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
        await self._sandbox.write_bytes(path, text.replace(old, new, 1).encode())
        return f"правка {path}: заменён один фрагмент"

    async def _write_file(self, args: dict[str, Any]) -> str:
        path = self._check_writable(_argument(args, "path", str))
        content = _argument(args, "content", str)
        await self._sandbox.write_bytes(path, content.encode())
        return f"записан {path}: {len(content)} симв."

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

    async def _run_tests(self, args: dict[str, Any]) -> str:
        if not self._spec.test_argv:
            raise ToolError("команда тестов не задана")
        result = await self._sandbox.exec(self._spec.test_argv, self._spec.test_timeout_s)
        return result.summary(self._spec.output_chars, self._spec.test_timeout_s)
