from __future__ import annotations

import hashlib
import json
import re
import shlex
import tokenize
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any

from sa_home_bot.devlab.agent.context import CHARS_PER_TOKEN
from sa_home_bot.devlab.agent.markup import STYLE_CB_V4, Markup
from sa_home_bot.devlab.agent.notes import NOTES_UNAVAILABLE, AgentNotes
from sa_home_bot.devlab.agent.paths import matches
from sa_home_bot.devlab.agent.prompt_en import (
    EN,
    EXECUTE_CODE_EN,
    EXECUTE_DESCRIPTION_CB_V5_EN,
    EXECUTE_DESCRIPTION_EN,
    SCRIPT_API_CB_V5_EN,
    SCRIPT_API_EN,
    SCRIPT_API_R2_EN,
    SCRIPT_CLIPPED_HINT_CB_V5_EN,
    SEARCH_DESCRIPTION_EN,
    SEARCH_QUERY_EN,
)
from sa_home_bot.devlab.agent.readstate import STYLE_CB_V5, RepeatLog
from sa_home_bot.devlab.agent.remote import (
    REMOTE_R2,
    SCRIPT_OUTPUT_MAX_CHARS_R2,
    SCRIPT_RUN_OUTPUT_CHARS_R2,
)
from sa_home_bot.devlab.agent.sandbox import (
    Sandbox,
    SandboxPathError,
    clip_head,
    clip_tail,
    safe_relative,
)
from sa_home_bot.devlab.agent.script import (
    SCRIPT_API,
    SCRIPT_API_R2,
    SCRIPT_CLIPPED_HINT,
    SCRIPT_FAILED_HEAD,
    SCRIPT_OUTPUT_CHARS,
    api_functions,
    api_stubs,
    parse_reply,
    request_payload,
    worker_argv,
)
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.utils.code_comments import comment_findings

LOCKED_PATH_TEXT = "путь {path}: файл тестера, не меняй; не согласен — ask_owner"
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
TOOL_SEARCH = "search"
TOOL_EXECUTE = "execute"
READ_ONLY_TOOLS = frozenset({TOOL_LIST_FILES, TOOL_READ_FILE, TOOL_GREP, TOOL_SEARCH})
MUTATING_TOOLS = frozenset({TOOL_EDIT_FILE, TOOL_WRITE_FILE, TOOL_RUN_COMMAND})
SCRIPT_ONLY_TEXT = (
    f"в режиме скрипта этого инструмента нет: пиши код в {TOOL_EXECUTE}, "
    f"а нужные функции ищи через {TOOL_SEARCH}"
)
SEARCH_EMPTY = 'функций с таким словом нет; search("") покажет все'

LIST_MAX_ENTRIES = 200
READ_MIN_LINES = 200
READ_MAX_LINES_CAP = 2000
READ_TOKENS_PER_LINE = 60
SCRIPT_OUTPUT_WINDOW_SHARE = 0.12
SCRIPT_OUTPUT_MAX_CHARS = 40_000
GREP_MAX_MATCHES = 50
GREP_MAX_FILE_BYTES = 200_000
GREP_LINE_CHARS = 200
NEWLINE_ADDED = "; в конец файла дописан перевод строки"
SCRIPT_NO_OUTPUT = (
    "(скрипт ничего не напечатал и последней строкой не было выражения: значения выражений "
    "не в последней строке не выводятся — оберни в print())"
)
SCRIPT_STATE_RESET = (
    "[состояние сброшено: процесс скрипта перезапущен, переменные и импорты прошлых вызовов "
    "потеряны; прочитанное прочитай заново]"
)
SCRIPT_BUDGET_MARGIN_S = 20.0
DIFF_EMPTY = "изменений относительно базы нет"
STEP_GATE_MISSING = "закрыть шаг из скрипта сейчас нельзя"
CALL_UNKNOWN = "неизвестный вызов хоста {name!r}"
SCRIPT_TOOL_NOTE = "[note] {name} выполнен через execute; вызывай функции внутри execute"
COMMENTS_RULE_MARKER = "tests/unit/test_no_comments_in_code.py"
COMMENTS_ROOTS = ("src/", "tests/")
COMMENTS_FORBIDDEN = "комментарии запрещены: {path}:{line} {text}"
COMMENTS_TEXT_CHARS = 80
StepGate = Callable[[dict[str, Any]], Awaitable[str]]
PLAIN_TOOLS = frozenset(
    {TOOL_LIST_FILES, TOOL_READ_FILE, TOOL_GREP, TOOL_EDIT_FILE, TOOL_WRITE_FILE}
    | {TOOL_RUN_COMMAND, TOOL_RUN_TESTS, TOOL_STEP_DONE, TOOL_ASK_OWNER, TOOL_FINISH}
)

def read_max_lines(window_tokens: int) -> int:
    return max(READ_MIN_LINES, min(READ_MAX_LINES_CAP, window_tokens // READ_TOKENS_PER_LINE))


def script_output_chars(
    window_tokens: int, floor: int, ceiling: int = SCRIPT_OUTPUT_MAX_CHARS
) -> int:
    scaled = int(window_tokens * CHARS_PER_TOKEN * SCRIPT_OUTPUT_WINDOW_SHARE)
    return max(floor, min(ceiling, scaled))


def changed_test_files(digests: Mapping[str, str]) -> list[str]:
    return sorted(
        path
        for path, digest in digests.items()
        if digest and _is_test_file(PurePosixPath(path).name)
    )


def _is_test_file(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


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
class ControlRequest:
    tool: str
    args: dict[str, str]
    label: str


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    mutating: bool = False
    green: bool | None = None
    control: ControlRequest | None = None


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

SEARCH_DEFINITION = _schema(
    TOOL_SEARCH,
    "Сигнатуры функций API, которые есть внутри кода execute: те, у которых query в имени или "
    "описании; пустой query — все функции.",
    query=_text("Слово из имени или описания функции; пустая строка — все функции"),
)
EXECUTE_DEFINITION = _schema(
    TOOL_EXECUTE,
    "Исполняет код Python в корне проекта (src в sys.path, пакеты проекта импортируются). "
    "Функции ниже уже определены, import для них не нужен. Код исполняется сразу, по порядку, "
    "как обычный Python. Виден вывод print() и значение последнего выражения, как в Jupyter; "
    f"до {SCRIPT_OUTPUT_CHARS} симв. (начало). Процесс один на задание: переменные, импорты и "
    "функции сохраняются между вызовами, как в Jupyter; прочитанное держи в переменной и не "
    "перечитывай. После таймаута или падения процесса состояние сбрасывается. Новый вызов — "
    "только когда нужно суждение о ещё не виденном тексте; остальное решай условием в этом. "
    "finish() закрывает все шаги задания; finish() и ask_owner() завершают скрипт; "
    "step_done() необязателен и не завершает: принят — скрипт "
    "идёт дальше, отклонён — StepRejected с причиной и остановка. Функции:\n"
    + api_stubs(""),
    code=_text("Код Python"),
)

SEARCH_DEFINITION_EN = _schema(
    TOOL_SEARCH, SEARCH_DESCRIPTION_EN, query=_text(SEARCH_QUERY_EN)
)
EXECUTE_DEFINITION_CB_V5 = _schema(
    TOOL_EXECUTE,
    EXECUTE_DESCRIPTION_CB_V5_EN + api_stubs("", SCRIPT_API_CB_V5_EN),
    code=_text(EXECUTE_CODE_EN),
)
EXECUTE_DEFINITION_EN = _schema(
    TOOL_EXECUTE,
    EXECUTE_DESCRIPTION_EN + api_stubs("", SCRIPT_API_EN),
    code=_text(EXECUTE_CODE_EN),
)


_EXECUTE_HEAD_RU = EXECUTE_DEFINITION["function"]["description"].removesuffix(api_stubs(""))
EXECUTE_DEFINITION_R2 = _schema(
    TOOL_EXECUTE,
    _EXECUTE_HEAD_RU.replace(
        f"до {SCRIPT_OUTPUT_CHARS} симв.", f"до {SCRIPT_OUTPUT_MAX_CHARS_R2} симв."
    )
    + api_stubs("", SCRIPT_API_R2),
    code=_text("Код Python"),
)
EXECUTE_DEFINITION_R2_EN = _schema(
    TOOL_EXECUTE,
    EXECUTE_DESCRIPTION_EN.replace(
        f"up to {SCRIPT_OUTPUT_CHARS} chars", f"up to {SCRIPT_OUTPUT_MAX_CHARS_R2} chars"
    )
    + api_stubs("", SCRIPT_API_R2_EN),
    code=_text(EXECUTE_CODE_EN),
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
    def __init__(
        self,
        sandbox: Sandbox,
        spec: AgentSpec,
        read_lines: int = READ_MIN_LINES,
        notes: AgentNotes | None = None,
        window_tokens: int = 0,
    ) -> None:
        self._notes = notes
        self._window_tokens = window_tokens
        self._sandbox = sandbox
        self._spec = spec
        self._remote_r2 = spec.code_mode and spec.remote_style == REMOTE_R2
        cb_v4 = spec.code_mode and spec.prompt_style == STYLE_CB_V4 and not self._remote_r2
        self._markup = Markup() if cb_v4 else None
        cb_v5 = spec.code_mode and spec.prompt_style == STYLE_CB_V5 and not self._remote_r2
        self._repeats = RepeatLog() if cb_v5 else None
        self._calls = 0
        self._read_lines = read_lines
        self._script_names = api_functions(
            claude_read=self._repeats is not None, compact=self._remote_r2
        )
        self._step_gate: StepGate | None = None
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
            self._handlers = {TOOL_SEARCH: self._search, TOOL_EXECUTE: self._execute}

    def notes_text(self) -> str:
        if self._repeats is not None:
            return self._notes.block_text() if self._notes is not None else ""
        own = self._notes.text() if self._notes is not None else ""
        return self._markup.block(own) if self._markup is not None else own

    async def open_segment(self, task: str) -> None:
        if self._markup is not None:
            await self._markup.open_segment(self._sandbox, task)

    def close_steps(self, closed: Collection[int]) -> None:
        if self._markup is not None:
            self._markup.close_steps(closed)

    def reset_reads(self) -> None:
        if self._repeats is not None:
            self._repeats.reset()

    @property
    def code_mode(self) -> bool:
        return self._spec.code_mode

    @property
    def definitions(self) -> list[dict[str, Any]]:
        if self._remote_r2:
            if self._spec.prompt_lang == EN:
                return [SEARCH_DEFINITION_EN, EXECUTE_DEFINITION_R2_EN]
            return [SEARCH_DEFINITION, EXECUTE_DEFINITION_R2]
        if self._repeats is not None:
            return [SEARCH_DEFINITION_EN, EXECUTE_DEFINITION_CB_V5]
        if self._spec.code_mode:
            if self._spec.prompt_lang == EN:
                return [SEARCH_DEFINITION_EN, EXECUTE_DEFINITION_EN]
            return [SEARCH_DEFINITION, EXECUTE_DEFINITION]
        return TOOL_DEFINITIONS

    async def tree_fingerprint(self) -> str:
        changed = await self._sandbox.changed_digests()
        return hashlib.sha256(json.dumps(changed, sort_keys=True).encode()).hexdigest()

    async def call(
        self, name: str, args: dict[str, Any], step_gate: StepGate | None = None
    ) -> ToolOutcome:
        self._step_gate = step_gate
        if self._spec.code_mode and name in self._script_names:
            return await self._as_script(name, args)
        if self._spec.code_mode and name in PLAIN_TOOLS:
            raise ToolError(SCRIPT_ONLY_TEXT)
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
        if matches(relative, self._spec.locked_paths):
            raise ToolError(LOCKED_PATH_TEXT.format(path=relative))
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

    async def _as_script(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        code = f"print({name}(**{args!r}))"
        outcome = await self._execute({"code": code})
        note = SCRIPT_TOOL_NOTE.format(name=name)
        return replace(outcome, text=f"{note}\n{outcome.text}")

    async def _search(self, args: dict[str, Any]) -> str:
        api = SCRIPT_API_EN if self._spec.prompt_lang == EN else SCRIPT_API
        if self._remote_r2:
            api = SCRIPT_API_R2_EN if self._spec.prompt_lang == EN else SCRIPT_API_R2
        elif self._repeats is not None:
            api = SCRIPT_API_CB_V5_EN
        return api_stubs(_argument(args, "query", str, ""), api) or SEARCH_EMPTY

    def _script_config(self) -> dict[str, Any]:
        spec = self._spec
        config = {
            "closed": list(spec.forbidden_paths + spec.secret_paths),
            "locked": list(spec.locked_paths),
            "write_prefixes": list(spec.write_prefixes),
            "allowed": self._allowed(),
            "test_argv": without_xdist(list(spec.test_argv)),
            "command_timeout": spec.command_timeout_s,
            "test_timeout": spec.narrow_test_timeout_s,
            "budget": spec.script_timeout_s - SCRIPT_BUDGET_MARGIN_S,
            "output_chars": spec.output_chars,
            "markup": self._markup is not None,
            "claude_read": self._repeats is not None,
        }
        if self._remote_r2:
            config.update(
                compact_tests=True,
                checks=[list(argv) for argv in spec.checks],
                output_chars=max(spec.output_chars, SCRIPT_RUN_OUTPUT_CHARS_R2),
            )
        return config

    async def _comment_lines(self) -> list[str]:
        if await self._sandbox.read_bytes(COMMENTS_RULE_MARKER) is None:
            return []
        lines = []
        digests = await self._sandbox.changed_digests()
        for path in sorted(digests):
            if not (digests[path] and path.endswith(".py") and path.startswith(COMMENTS_ROOTS)):
                continue
            data = await self._sandbox.read_bytes(path)
            try:
                findings = comment_findings(data.decode()) if data is not None else []
            except (SyntaxError, tokenize.TokenError, UnicodeDecodeError):
                continue
            lines += [
                COMMENTS_FORBIDDEN.format(path=path, line=line, text=text[:COMMENTS_TEXT_CHARS])
                for line, text in findings
            ]
        return lines

    async def _new_comment_lines(self, args: dict[str, Any]) -> list[str]:
        path, old, new = args.get("path"), args.get("old"), args.get("new")
        if not (isinstance(path, str) and isinstance(new, str)):
            return []
        if not (path.endswith(".py") and path.startswith(COMMENTS_ROOTS)):
            return []
        if await self._sandbox.read_bytes(COMMENTS_RULE_MARKER) is None:
            return []
        try:
            found = comment_findings(new)
            known = Counter(text for _, text in comment_findings(old or ""))
        except (SyntaxError, tokenize.TokenError):
            return []
        lines = []
        for line, text in found:
            if known.get(text):
                known[text] -= 1
                continue
            lines.append(
                COMMENTS_FORBIDDEN.format(path=path, line=line, text=text[:COMMENTS_TEXT_CHARS])
            )
        return lines

    async def _failed_in_base(self, args: dict[str, Any]) -> bool:
        failed = args.get("failed")
        if not isinstance(failed, list):
            return False
        for item in failed:
            if not (isinstance(item, list) and len(item) == 2):
                continue
            if not all(isinstance(part, str) for part in item):
                continue
            path, name = item
            try:
                data = await self._sandbox.base_bytes(path)
            except SandboxPathError:
                continue
            pattern = rf"def {re.escape(name)}\("
            if data is not None and re.search(pattern, data.decode(errors="replace")):
                return True
        return False

    def _serve_notes(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._notes is None:
            return {"ok": False, "reason": NOTES_UNAVAILABLE}
        text = args.get("text")
        text = text if isinstance(text, str) else ""
        op = args.get("op")
        if op == "get":
            return {"ok": True, "value": self._notes.text()}
        refusal = self._notes.add(text) if op == "add" else self._notes.replace(text)
        if refusal is not None:
            return {"ok": False, "reason": refusal}
        return {"ok": True, "value": ""}

    def _serve_reads(self, args: dict[str, Any]) -> dict[str, Any]:
        path, digest = args.get("path"), args.get("digest")
        numbers = [args.get(key) for key in ("start", "end", "total")]
        if self._markup is None or not isinstance(path, str) or not isinstance(digest, str):
            return {"ok": True, "value": ""}
        if not all(type(number) is int for number in numbers):
            return {"ok": True, "value": ""}
        start, end, total = numbers
        return {"ok": True, "value": self._markup.record_read(path, digest, start, end, total)}

    def _serve_seen(self, args: dict[str, Any]) -> dict[str, Any]:
        path, digest = args.get("path"), args.get("digest")
        numbers = [args.get(key) for key in ("start", "end")]
        if self._repeats is None or not isinstance(path, str) or not isinstance(digest, str):
            return {"ok": True, "value": ""}
        if not all(type(number) is int for number in numbers):
            return {"ok": True, "value": ""}
        start, end = numbers
        return {"ok": True, "value": self._repeats.check(path, digest, start, end, self._calls)}

    def _serve_todo(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._notes is None:
            return {"ok": False, "reason": NOTES_UNAVAILABLE}
        if args.get("op") == "get":
            return {"ok": True, "value": self._notes.todo_text()}
        items = args.get("items")
        pairs = [
            (item[0], item[1])
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, list) and len(item) == 2 and all(isinstance(p, str) for p in item)
        ]
        refusal = self._notes.set_todo(pairs)
        if refusal is not None:
            return {"ok": False, "reason": refusal}
        return {"ok": True, "value": ""}

    async def _serve_call(
        self, request: dict[str, Any], gate: StepGate | None, accepted: list[str]
    ) -> dict[str, Any]:
        name = request.get("call")
        args = request.get("args")
        if name == TOOL_STEP_DONE:
            if gate is None or not isinstance(args, dict):
                return {"ok": False, "reason": STEP_GATE_MISSING}
            reason = await gate(args)
            if reason:
                return {"ok": False, "reason": reason}
            accepted.append(str(args.get("title", "")))
            return {"ok": True}
        if name == "notes" and isinstance(args, dict):
            return self._serve_notes(args)
        if name == "seen" and isinstance(args, dict):
            return self._serve_seen(args)
        if name == "todo" and isinstance(args, dict):
            return self._serve_todo(args)
        if name == "reads" and isinstance(args, dict):
            return self._serve_reads(args)
        if name == "changed_tests":
            return {"ok": True, "value": changed_test_files(await self._sandbox.changed_digests())}
        if name == "diff":
            text = await self._sandbox.diff()
            return {"ok": True, "value": text or DIFF_EMPTY}
        if name == "check_comments" and isinstance(args, dict):
            return {"ok": True, "value": await self._new_comment_lines(args)}
        if name == "base_tests" and isinstance(args, dict):
            return {"ok": True, "value": await self._failed_in_base(args)}
        if name == "comments":
            return {"ok": True, "value": await self._comment_lines()}
        return {"ok": False, "reason": CALL_UNKNOWN.format(name=name)}

    async def _execute(self, args: dict[str, Any]) -> ToolOutcome:
        self._calls += 1
        if self._markup is None:
            return await self._run_script(args)
        self._markup.begin_call()
        outcome = await self._run_script(args)
        hint = self._markup.after_call(outcome.mutating)
        if hint and outcome.control is None:
            return replace(outcome, text=f"{outcome.text}\n{hint}")
        return outcome

    async def _run_script(self, args: dict[str, Any]) -> ToolOutcome:
        code = _argument(args, "code", str)
        timeout_s = self._spec.script_timeout_s
        before = await self._sandbox.changed_digests()
        accepted: list[str] = []
        gate = self._step_gate

        async def serve(request: dict[str, Any]) -> dict[str, Any]:
            return await self._serve_call(request, gate, accepted)

        reply = await self._sandbox.session_request(
            worker_argv(self._script_config()), request_payload(code), timeout_s, serve
        )
        mutating = before != await self._sandbox.changed_digests()
        if (reply.timed_out or reply.data is None) and self._repeats is not None:
            self._repeats.reset()
        if reply.timed_out:
            text = f"[таймаут {timeout_s:g} с]\n{SCRIPT_STATE_RESET}"
            return ToolOutcome(text, mutating=mutating)
        if reply.data is None:
            died = f"процесс скрипта завершился ({reply.detail})"
            text = f"{SCRIPT_FAILED_HEAD}\n{died}\n{SCRIPT_STATE_RESET}"
            return ToolOutcome(text, mutating=mutating)
        run = parse_reply(reply.data)
        if self._repeats is not None and run.failed:
            self._repeats.forget_call(self._calls)
        limit = max(self._spec.output_chars, SCRIPT_OUTPUT_CHARS)
        hint = SCRIPT_CLIPPED_HINT
        if self._remote_r2:
            limit = script_output_chars(self._window_tokens, limit, SCRIPT_OUTPUT_MAX_CHARS_R2)
        elif self._repeats is not None:
            limit = script_output_chars(self._window_tokens, limit)
            hint = SCRIPT_CLIPPED_HINT_CB_V5_EN
        if run.failed:
            output = clip_tail(run.output, limit) or SCRIPT_NO_OUTPUT
            return ToolOutcome(
                f"{SCRIPT_FAILED_HEAD}\n{output}", mutating=mutating, green=run.green
            )
        if self._repeats is not None and len(run.output) > limit:
            self._repeats.forget_call(self._calls)
        silent = "" if run.control or accepted else SCRIPT_NO_OUTPUT
        output = clip_head(run.output, limit, hint) or silent
        control = (
            ControlRequest(run.control.tool, run.control.args, run.control.tool)
            if run.control
            else None
        )
        return ToolOutcome(output, mutating=mutating, green=run.green, control=control)
