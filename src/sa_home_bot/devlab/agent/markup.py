from __future__ import annotations

import ast
import json
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import Protocol

STYLE_CB_V4 = "cb_v4"
TOC_TITLE = "## Оглавление файлов задачи"
PLAN_TITLE = "## План"
READS_TITLE = "## Прочитано"
NOTES_TITLE = "## Заметки"
TOC_LIMIT_CHARS = 5000
TOC_MAX_PATHS = 12
TOC_SECTION_CHARS = 1400
TOC_LINE_CHARS = 110
TOC_DIR_DEPTH = 2
TOC_DIR_ENTRIES = 30
JSON_KEYS_MAX = 30
JSON_SUBKEYS_MAX = 8
JSON_KEY_CHARS = 40
READS_LIMIT_CHARS = 1500
READS_CALLS_SHOWN = 3
PLAN_LIMIT_CHARS = 1500
PLAN_TITLE_CHARS = 100
IDLE_CALLS_LIMIT = 12
PATH_SUFFIXES = frozenset(
    {".py", ".json", ".md", ".toml", ".yaml", ".yml", ".txt", ".cfg", ".ini", ".sh", ".sql"}
    | {".html", ".css", ".js", ".ts", ".csv"}
)
BACKTICKED = re.compile(r"`([^`\s]+)`")
PATH_SHAPE = re.compile(r"^[\w./\-]+$")
LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
ALLOWED_PATHS_MARK = "**Менять можно только**"
STEPS_HEADING = re.compile(r"^#{1,6}\s*(?:шаги|steps)\b", re.IGNORECASE)
ANY_HEADING = re.compile(r"^#{1,6}\s")
STEP_LINE = re.compile(r"^\s*(\d+)[.)]\s+(\S.*)$")
READ_AGAIN = (
    "[подсказка] уже читал {path} {start}–{end} на вызове {calls}; если данные в переменной "
    "скрипта — бери оттуда; выводы — в note()"
)
STOP_READING = (
    "[{count} вызовов без записи: хватит читать — оглавление и заметки у тебя есть; "
    "создай/правь файл задачи этим или следующим вызовом]"
)
MISSING = "не найден"
UNREADABLE = "недоступен"
BINARY = "не текстовый"


class FileReader(Protocol):
    async def read_bytes(self, path: str) -> bytes | None: ...

    async def list_files(self) -> list[str]: ...


def task_steps(task: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    inside = False
    for line in task.splitlines():
        if ANY_HEADING.match(line):
            inside = STEPS_HEADING.match(line) is not None
            continue
        match = STEP_LINE.match(line) if inside else None
        if match and all(int(match.group(1)) != number for number, _ in found):
            found.append((int(match.group(1)), match.group(2).strip()))
    return found


def render_plan(steps: Iterable[tuple[int, str]], closed: Collection[int]) -> str:
    lines = [
        f"[{'x' if number in closed else ' '}] {number}. {title[:PLAN_TITLE_CHARS]}"
        for number, title in steps
    ]
    return _fit(lines, PLAN_LIMIT_CHARS)


def task_paths(task: str) -> tuple[str, ...]:
    paths: dict[str, None] = {}
    for line in task.splitlines():
        body = line.lstrip()
        if not body.startswith(ALLOWED_PATHS_MARK):
            continue
        for token in BACKTICKED.finditer(body[len(ALLOWED_PATHS_MARK) :]):
            paths.setdefault(LINE_SUFFIX.sub("", token.group(1)), None)
    return tuple(paths)


def mentioned_paths(task: str) -> list[str]:
    paths: dict[str, None] = {}
    for token in BACKTICKED.findall(task):
        path = LINE_SUFFIX.sub("", token)
        if not PATH_SHAPE.match(path) or path.startswith("/") or ".." in path.split("/"):
            continue
        suffix = path[path.rfind(".") :] if "." in path.rsplit("/", 1)[-1] else ""
        if path.endswith("/") or suffix in PATH_SUFFIXES or "/" in path:
            paths.setdefault(path, None)
    return list(paths)


def _fit(lines: list[str], limit: int) -> str:
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        if used + len(line) + 1 > limit:
            kept.append(f"… ещё {len(lines) - index} строк")
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


def _human_size(size: int) -> str:
    return f"{size} Б" if size < 1024 else f"{size / 1024:.1f} КБ"


def _line_count(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _cut(text: str) -> str:
    body = text.lstrip()
    indent = text[: len(text) - len(body)]
    flat = " ".join(body.split())
    return indent + (flat if len(flat) <= TOC_LINE_CHARS else flat[: TOC_LINE_CHARS - 1] + "…")


def _span(node: ast.stmt) -> str:
    end = getattr(node, "end_lineno", None) or node.lineno
    return f"L{node.lineno}" if end == node.lineno else f"L{node.lineno}-{end}"


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{keyword} {node.name}({ast.unparse(node.args)}){returns}"


def _assigned(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def python_outline(text: str) -> list[str] | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines: list[str] = []
    constants: list[ast.stmt] = []

    def flush() -> None:
        if constants:
            names = ", ".join(name for node in constants for name in _assigned(node))
            end = getattr(constants[-1], "end_lineno", None) or constants[-1].lineno
            first = constants[0].lineno
            span = f"L{first}" if end == first else f"L{first}-{end}"
            lines.append(_cut(f"  {span} {names}"))
            constants.clear()

    for node in tree.body:
        if _assigned(node):
            constants.append(node)
            continue
        flush()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(_cut(f"  {_span(node)} {_signature(node)}"))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            lines.append(_cut(f"  {_span(node)} class {node.name}({bases})"))
            lines += [
                _cut(f"    {_span(member)} {_signature(member)}")
                for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
    flush()
    return lines


def _shape(value: object) -> str:
    if isinstance(value, dict):
        keys = [str(key)[:JSON_KEY_CHARS] for key in list(value)[:JSON_SUBKEYS_MAX]]
        more = f", … (+{len(value) - len(keys)})" if len(value) > len(keys) else ""
        return "{" + ", ".join(keys) + more + "}"
    if isinstance(value, list):
        return f"список[{len(value)}]"
    if isinstance(value, bool):
        return "булево"
    if isinstance(value, (int, float)):
        return "число"
    if isinstance(value, str):
        return "строка"
    return "null"


def json_outline(text: str) -> list[str] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        lines = [
            _cut(f"  {str(key)[:JSON_KEY_CHARS]}: {_shape(value)}")
            for key, value in list(data.items())[:JSON_KEYS_MAX]
        ]
        if len(data) > JSON_KEYS_MAX:
            lines.append(f"  … ещё {len(data) - JSON_KEYS_MAX} ключей")
        return lines
    if isinstance(data, list):
        first = f", элемент: {_shape(data[0])}" if data else ""
        return [f"  список[{len(data)}]{first}"]
    return [f"  {_shape(data)}"]


def _section(lines: list[str]) -> str:
    return _fit(lines, TOC_SECTION_CHARS)


async def _file_section(path: str, data: bytes) -> str:
    try:
        text = data.decode()
    except UnicodeDecodeError:
        return f"{path} ({BINARY}, {_human_size(len(data))})"
    head = f"{path} ({_line_count(text)} стр., {_human_size(len(data))})"
    outline = None
    if path.endswith(".py"):
        outline = python_outline(text)
    elif path.endswith(".json"):
        outline = json_outline(text)
    return _section([head, *(outline or [])])


async def _dir_section(reader: FileReader, path: str, entries: list[str]) -> str:
    prefix = path.rstrip("/") + "/"
    near = [entry for entry in entries if entry[len(prefix) :].count("/") < TOC_DIR_DEPTH]
    shown = near[:TOC_DIR_ENTRIES]
    lines = [f"{prefix} ({len(entries)} файлов)"]
    for entry in shown:
        data = await reader.read_bytes(entry)
        size = _human_size(len(data)) if data is not None else UNREADABLE
        lines.append(_cut(f"  {entry[len(prefix) :]}  {size}"))
    hidden = len(entries) - len(shown)
    if hidden > 0:
        lines.append(f"  … ещё {hidden} файлов глубже или сверх лимита")
    return _section(lines)


async def build_toc(reader: FileReader, task: str) -> str:
    paths = mentioned_paths(task)
    if not paths:
        return ""
    listing: list[str] | None = None
    sections: list[str] = []
    used = 0
    for index, path in enumerate(paths):
        left = len(paths) - index
        if index >= TOC_MAX_PATHS:
            sections.append(f"… ещё {left} путей не показаны")
            break
        try:
            data = None if path.endswith("/") else await reader.read_bytes(path)
            if data is not None:
                section = await _file_section(path, data)
            else:
                if listing is None:
                    listing = await reader.list_files()
                prefix = path.rstrip("/") + "/"
                entries = [entry for entry in listing if entry.startswith(prefix)]
                if entries:
                    section = await _dir_section(reader, path, entries)
                elif path.endswith("/") or path[path.rfind(".") :] in PATH_SUFFIXES:
                    section = f"{path} — {MISSING}"
                else:
                    continue
        except Exception:
            section = f"{path} — {UNREADABLE}"
        if used + len(section) + 1 > TOC_LIMIT_CHARS:
            sections.append(f"… ещё {left} путей не показаны: оглавление упёрлось в лимит")
            break
        sections.append(section)
        used += len(section) + 1
    return "\n".join(sections)


@dataclass
class _Span:
    start: int
    end: int
    calls: list[int]


@dataclass
class _FileReads:
    digest: str
    total: int
    spans: list[_Span] = field(default_factory=list)


class ReadLog:
    def __init__(self) -> None:
        self._files: dict[str, _FileReads] = {}

    def record(
        self, path: str, digest: str, start: int, end: int, total: int, call: int
    ) -> str | None:
        known = self._files.pop(path, None)
        if known is None or known.digest != digest:
            known = _FileReads(digest, total)
        self._files[path] = known
        known.total = total
        covering = next((s for s in known.spans if s.start <= start and end <= s.end), None)
        if covering is not None:
            return READ_AGAIN.format(
                path=path, start=start, end=end, calls=_calls_text(covering.calls)
            )
        calls = {call}
        kept: list[_Span] = []
        for span in known.spans:
            if span.start <= end + 1 and start - 1 <= span.end:
                start, end = min(start, span.start), max(end, span.end)
                calls.update(span.calls)
            else:
                kept.append(span)
        kept.append(_Span(start, end, sorted(calls)))
        known.spans = sorted(kept, key=lambda span: span.start)
        return None

    def render(self) -> str:
        lines = [self._line(path, reads) for path, reads in self._files.items()]
        kept: list[str] = []
        used = 0
        for line in reversed(lines):
            if used + len(line) + 1 > READS_LIMIT_CHARS:
                kept.append(f"… ещё {len(lines) - len(kept)} файлов")
                break
            kept.append(line)
            used += len(line) + 1
        return "\n".join(reversed(kept))

    @staticmethod
    def _line(path: str, reads: _FileReads) -> str:
        calls = sorted({call for span in reads.spans for call in span.calls})
        first = reads.spans[0]
        whole = len(reads.spans) == 1 and first.start == 1 and first.end >= reads.total
        ranges = "целиком" if whole else ", ".join(f"{s.start}–{s.end}" for s in reads.spans)
        return f"{path} {ranges} (вызов {_calls_text(calls)})"


def _calls_text(calls: list[int]) -> str:
    shown = calls[-READS_CALLS_SHOWN:]
    prefix = "…, " if len(calls) > len(shown) else ""
    return prefix + ", ".join(str(call) for call in shown)


class Markup:
    def __init__(self) -> None:
        self.reads = ReadLog()
        self.calls = 0
        self._toc = ""
        self._steps: list[tuple[int, str]] = []
        self._closed: set[int] = set()
        self._idle = 0

    async def open_segment(self, reader: FileReader, task: str) -> None:
        self._steps = task_steps(task)
        self._toc = await build_toc(reader, task)

    def close_steps(self, closed: Collection[int]) -> None:
        self._closed = set(closed)

    def begin_call(self) -> None:
        self.calls += 1

    def record_read(self, path: str, digest: str, start: int, end: int, total: int) -> str:
        return self.reads.record(path, digest, start, end, total, self.calls) or ""

    def after_call(self, mutating: bool) -> str:
        if mutating:
            self._idle = 0
            return ""
        self._idle += 1
        if self._idle % IDLE_CALLS_LIMIT:
            return ""
        return STOP_READING.format(count=self._idle)

    def block(self, user_notes: str) -> str:
        sections = [
            (TOC_TITLE, self._toc),
            (PLAN_TITLE, render_plan(self._steps, self._closed)),
            (READS_TITLE, self.reads.render()),
            (NOTES_TITLE, user_notes),
        ]
        return "\n\n".join(f"{title}\n{body}" for title, body in sections if body)
