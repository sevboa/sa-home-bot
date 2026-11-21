from __future__ import annotations

import json
from dataclasses import dataclass

from sa_home_bot.devlab.agent.sandbox import IGNORED_DIRS

ACTIONS_MARKER = "DEVLAB_ACTIONS "
CONTROL_STEP_DONE = "step_done"
CONTROL_FINISH = "finish"
CONTROL_ASK_OWNER = "ask_owner"
SCRIPT_GREP_MAX_MATCHES = 200
SCRIPT_GREP_MAX_FILE_BYTES = 200_000
SCRIPT_FILENAME = "<script>"
SCRIPT_FAILED_HEAD = "[скрипт упал]"
SCRIPT_OUTPUT_CHARS = 8_000
SCRIPT_CLIPPED_HINT = (
    "; печатай только нужное: grep() и ls() возвращают список строк, срез [:n] режет строки"
)
PROCESS_FORBIDDEN = "процессы запускай через run()/run_tests(): subprocess и os.system запрещены"
SCRIPT_API: tuple[tuple[str, str, str], ...] = (
    ("ls", 'def ls(prefix: str = "") -> list[str]: ...', "пути файлов проекта с префиксом"),
    (
        "read",
        "def read(path: str, start: int = 1, end: int | None = None) -> str: ...",
        "чистый текст файла (срез строк start..end), его можно подставлять в old для edit()",
    ),
    (
        "read_numbered",
        "def read_numbered(path: str, start: int = 1, end: int | None = None) -> str: ...",
        "строки с номерами «n<TAB>текст», только для просмотра",
    ),
    (
        "read_many",
        "def read_many(paths: list[str]) -> dict[str, str]: ...",
        "словарь путь → чистый текст файла",
    ),
    (
        "grep",
        'def grep(pattern: str, prefix: str = "", context: int = 0) -> list[str]: ...',
        "совпадения регулярного выражения: path:n:строка",
    ),
    (
        "edit",
        "def edit(path: str, old: str, new: str) -> None: ...",
        "сразу заменить единственный фрагмент old на new; иначе исключение EditError",
    ),
    (
        "write",
        "def write(path: str, content: str) -> None: ...",
        "сразу создать или перезаписать файл; запрещённый путь — EditError",
    ),
    (
        "run",
        "def run(command: str) -> Result: ...",
        "сразу выполнить разрешённую команду без оболочки",
    ),
    (
        "run_tests",
        "def run_tests(paths: list[str]) -> Result: ...",
        "сразу выполнить узкий прогон тестов по путям",
    ),
    (
        CONTROL_STEP_DONE,
        "def step_done(text: str) -> NoReturn: ...",
        "закрыть шаг с итогом text и завершить скрипт; страж проверит шаг",
    ),
    (
        CONTROL_FINISH,
        "def finish(summary: str) -> NoReturn: ...",
        "завершить задание с итогом summary и завершить скрипт; страж проверит дифф и проект",
    ),
    (
        CONTROL_ASK_OWNER,
        "def ask_owner(question: str) -> NoReturn: ...",
        "задать вопрос владельцу и завершить скрипт",
    ),
    (
        "Result",
        "class Result: ...",
        "итог run()/run_tests(): поля code: int, output: str, ok: bool (code == 0); "
        "str(r) = «[код N]» и вывод",
    ),
    (
        "EditError",
        "class EditError(Exception): ...",
        "правка отклонена: фрагмент не найден или найден не один раз, путь закрыт",
    ),
)
SCRIPT_EXAMPLE = """text = read("src/pkg/a.py")
print("\\n".join(grep(r"def load", "src/", context=2)))
edit("src/pkg/a.py", "return 1", "return 2")
r = run_tests(["tests/test_a.py"])
print(r)
if r.ok:
    step_done("a.py возвращает 2")
else:
    print(r.output[-2000:])"""
SCRIPT_FINAL_EXAMPLE = """r = run("ruff check src/pkg/a.py tests/test_a.py")
if r.ok:
    finish("a.py возвращает 2, тесты и ruff зелёные")
else:
    print(r)"""

PROLOGUE = r'''import builtins
import fnmatch
import io
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import traceback

_START = time.monotonic()
_IGNORED = frozenset(_CONFIG["ignored"])
_ROOT = os.getcwd()
for _path in (os.path.join(_ROOT, "src"), _ROOT):
    if os.path.isdir(_path):
        sys.path.insert(0, _path)

_real_popen_init = subprocess.Popen.__init__
_real_open = builtins.open
_BACKUPS = {}
_STATE = {"green": None}


def _forbidden(*args, **kwargs):
    raise PermissionError(_CONFIG["forbidden"])


subprocess.Popen.__init__ = _forbidden
for _name in dir(os):
    if _name in ("system", "popen", "fork", "forkpty") or _name.startswith(
        ("exec", "spawn", "posix_spawn")
    ):
        setattr(os, _name, _forbidden)


class EditError(Exception):
    pass


class Result:
    def __init__(self, code, output):
        self.code = code
        self.output = output

    @property
    def ok(self):
        return self.code == 0

    def __str__(self):
        return f"[код {self.code}]\n{self.output}".rstrip()

    __repr__ = __str__


class _Signal(BaseException):
    def __init__(self, tool, args):
        super().__init__(tool)
        self.tool = tool
        self.payload = args


def _signal(tool, key, value):
    if not isinstance(value, str):
        raise TypeError(f"{tool}: аргумент {key} должен быть строкой")
    raise _Signal(tool, {key: value})


def step_done(text):
    _signal("step_done", "title", text)


def finish(summary):
    _signal("finish", "summary", summary)


def ask_owner(question):
    _signal("ask_owner", "question", question)


def _relative(path):
    path = os.fspath(path)
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    return os.path.relpath(os.path.join(_ROOT, path), _ROOT).replace(os.sep, "/")


def _matches(relative, patterns):
    return any(
        fnmatch.fnmatch(relative, pattern)
        or (pattern.endswith("/") and relative.startswith(pattern))
        for pattern in patterns
    )


def _violation(relative):
    if relative.startswith("../") or os.path.isabs(relative):
        return f"путь {relative} недопустим: нужен относительный путь внутри проекта"
    if _matches(relative, _CONFIG["closed"]):
        return f"путь {relative} закрыт для правок правилами проекта"
    prefixes = _CONFIG["write_prefixes"]
    if prefixes and not any(relative.startswith(prefix) for prefix in prefixes):
        return f"путь {relative} вне путей роли: {', '.join(prefixes)}"
    return None


def _backup(path):
    relative = _relative(path)
    if relative in _BACKUPS or relative.startswith("../"):
        return
    try:
        with _real_open(relative, "rb") as handle:
            _BACKUPS[relative] = handle.read()
    except OSError:
        _BACKUPS[relative] = None


def _open(file, mode="r", *args, **kwargs):
    if isinstance(file, (str, bytes, os.PathLike)) and any(
        flag in str(kwargs.get("mode", mode)) for flag in "wax+"
    ):
        _backup(file)
    return _real_open(file, mode, *args, **kwargs)


builtins.open = _open
io.open = _open


def _walk():
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in _IGNORED]
        for name in files:
            yield os.path.relpath(os.path.join(root, name), ".").replace(os.sep, "/")


def _stats():
    found = {}
    for relative in _walk():
        try:
            info = os.stat(relative)
        except OSError:
            continue
        found[relative] = (info.st_size, info.st_mtime_ns)
    return found


_BASE = _stats()


def ls(prefix=""):
    return sorted(relative for relative in _walk() if relative.startswith(prefix))


def _bytes(path):
    try:
        with _real_open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        raise EditError(f"файл {path} не найден") from None


def _text(path):
    try:
        return _bytes(path).decode()
    except UnicodeDecodeError:
        raise EditError(f"файл {path} не текстовый") from None


def read(path, start=1, end=None):
    lines = _text(path).splitlines(keepends=True)
    return "".join(lines[max(1, start) - 1 : end])


def read_numbered(path, start=1, end=None):
    lines = _text(path).splitlines()
    last = len(lines) if end is None else min(end, len(lines))
    return "\n".join(f"{n}\t{lines[n - 1]}" for n in range(max(1, start), last + 1))


def read_many(paths):
    return {path: read(path) for path in paths}


def grep(pattern, prefix="", context=0):
    compiled = re.compile(pattern)
    found = []
    total = 0
    for path in ls(prefix):
        if os.path.getsize(path) > _CONFIG["max_bytes"]:
            continue
        try:
            lines = _text(path).splitlines()
        except (EditError, OSError):
            continue
        hits = []
        for index, line in enumerate(lines):
            if compiled.search(line):
                hits.append(index)
                total += 1
                if total >= _CONFIG["max_matches"]:
                    break
        shown = sorted({
            near
            for hit in hits
            for near in range(max(0, hit - context), min(len(lines), hit + context + 1))
        })
        marks = set(hits)
        for index in shown:
            sep = ":" if index in marks else "-"
            found.append(f"{path}{sep}{index + 1}{sep}{lines[index]}")
        if total >= _CONFIG["max_matches"]:
            found.append(f"... показаны первые {_CONFIG['max_matches']} совпадений")
            break
    return found


def _writable(path):
    if not isinstance(path, str) or not path.strip():
        raise EditError("путь должен быть непустой строкой")
    relative = _relative(path)
    reason = _violation(relative)
    if reason:
        raise EditError(reason)
    return relative


def _store(relative, data):
    if data and not data.endswith(b"\n") and b"\0" not in data:
        crlf = b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
        data += b"\r\n" if crlf else b"\n"
    _backup(relative)
    parent = os.path.dirname(relative)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _real_open(relative, "wb") as handle:
        handle.write(data)


def edit(path, old, new):
    relative = _writable(path)
    if not isinstance(old, str) or not isinstance(new, str):
        raise EditError("old и new должны быть строками")
    text = _text(relative)
    found = text.count(old)
    if not old or found != 1:
        raise EditError(
            f"фрагмент old найден {found} раз в {relative}: нужно ровно один, добавь контекста"
        )
    _store(relative, text.replace(old, new, 1).encode())


def write(path, content):
    relative = _writable(path)
    if not isinstance(content, str):
        raise EditError("content должен быть строкой")
    _store(relative, content.encode())


def _clip(text):
    limit = _CONFIG["output_chars"]
    if len(text) <= limit:
        return text
    return f"[показан конец: {limit} симв. из {len(text)}]\n{text[-limit:]}"


def _execute(argv, limit):
    left = _CONFIG["budget"] - (time.monotonic() - _START)
    timeout = min(limit, left)
    if timeout < 1:
        return Result(-1, "не хватает времени на выполнение скрипта")
    process = subprocess.Popen.__new__(subprocess.Popen)
    try:
        _real_popen_init(
            process,
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        return Result(127, f"{type(error).__name__}: {error}")
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.communicate()
        return Result(-1, f"[таймаут {timeout:g} с]")
    finally:
        _BASE.clear()
        _BASE.update({k: v for k, v in _stats().items() if k not in _BACKUPS})
    return Result(process.returncode, _clip(output.decode(errors="replace")))


def run(command):
    try:
        argv = shlex.split(command)
    except (ValueError, AttributeError) as error:
        return Result(2, f"команда не разобрана: {error}")
    if not any(argv[: len(allowed)] == allowed for allowed in _CONFIG["allowed"]):
        listed = ", ".join(" ".join(allowed) for allowed in _CONFIG["allowed"])
        return Result(126, f"команда не разрешена; разрешены: {listed}")
    return _execute(argv, _CONFIG["command_timeout"])


def run_tests(paths):
    if not _CONFIG["test_argv"]:
        return Result(127, "команда тестов не задана")
    listed = [paths] if isinstance(paths, str) else list(paths)
    tokens = " ".join(listed).split()
    if not tokens:
        return Result(2, "paths обязателен: укажи свои тесты, весь набор проверит finish")
    if any(token.startswith("-") for token in tokens):
        return Result(2, "paths — только пути тестов, без флагов pytest")
    for token in tokens:
        if token.startswith("/") or ".." in token.split("/"):
            return Result(2, f"путь {token!r} недопустим: нужен относительный путь внутри проекта")
    result = _execute([*_CONFIG["test_argv"], *tokens], _CONFIG["test_timeout"])
    _STATE["green"] = result.ok
    return result


def _finalize():
    changed = set()
    for relative, old in _BACKUPS.items():
        try:
            with _real_open(relative, "rb") as handle:
                current = handle.read()
        except OSError:
            current = None
        if current != old:
            changed.add(relative)
    now = _stats()
    for relative in now.keys() | _BASE.keys():
        if relative not in _BACKUPS and now.get(relative) != _BASE.get(relative):
            changed.add(relative)
    notes = []
    for relative in sorted(changed):
        reason = _violation(relative)
        if reason is None:
            continue
        if relative in _BACKUPS:
            if _BACKUPS[relative] is None:
                os.remove(relative)
            else:
                with _real_open(relative, "wb") as handle:
                    handle.write(_BACKUPS[relative])
            notes.append(f"[правка отклонена, файл возвращён как был: {reason}]")
        else:
            notes.append(f"[правка недопустима, откатить не удалось: {reason}]")
    return notes
'''

_EPILOGUE = r'''
_failed = False
_control = None
_namespace = {"__name__": "__main__", **{name: globals()[name] for name in _CONFIG["names"]}}
try:
    exec(compile(_code, _CONFIG["filename"], "exec"), _namespace)
except _Signal as _signal_exit:
    _control = {"tool": _signal_exit.tool, "args": _signal_exit.payload}
except SystemExit as _exit:
    _failed = _exit.code not in (None, 0)
except BaseException as _error:
    _failed = True
    traceback.print_exception(
        type(_error), _error, _error.__traceback__.tb_next, file=sys.stdout
    )
try:
    for _note in _finalize():
        print(_note)
except Exception as _error:
    print(f"[проверка путей записи не удалась: {_error}]")
sys.stdout.flush()
print("\n" + _CONFIG["marker"] + json.dumps(
    {"failed": _failed, "control": _control, "green": _STATE["green"]}, ensure_ascii=False
))
'''


@dataclass(frozen=True)
class ScriptControl:
    tool: str
    args: dict[str, str]


@dataclass(frozen=True)
class ScriptRun:
    output: str
    failed: bool
    control: ScriptControl | None = None
    green: bool | None = None


def api_stubs(query: str = "") -> str:
    needle = query.strip().lower()
    return "\n".join(
        f"{stub}  # {about}"
        for name, stub, about in SCRIPT_API
        if needle in name.lower() or needle in about.lower()
    )


def script_help() -> str:
    return f"{api_stubs()}\nПример:\n{SCRIPT_EXAMPLE}"


def script_argv(code: str, config: dict[str, object], python: str = "python") -> list[str]:
    full = {
        **config,
        "ignored": sorted(IGNORED_DIRS),
        "forbidden": PROCESS_FORBIDDEN,
        "max_bytes": SCRIPT_GREP_MAX_FILE_BYTES,
        "max_matches": SCRIPT_GREP_MAX_MATCHES,
        "names": [name for name, _, _ in SCRIPT_API] + ["Result", "EditError"],
        "filename": SCRIPT_FILENAME,
        "marker": ACTIONS_MARKER,
    }
    source = f"_CONFIG = json.loads({json.dumps(full)!r})\n_code = {code!r}\n"
    return [python, "-I", "-c", f"import json\n{source}{PROLOGUE}\n{_EPILOGUE}"]


def _control(payload: object) -> ScriptControl | None:
    if not isinstance(payload, dict):
        return None
    tool, args = payload.get("tool"), payload.get("args")
    if not isinstance(tool, str) or not isinstance(args, dict):
        return None
    return ScriptControl(tool, args)


def parse_output(text: str) -> ScriptRun:
    lines = text.splitlines()
    marked = [i for i, line in enumerate(lines) if line.startswith(ACTIONS_MARKER)]
    if not marked:
        return ScriptRun(text.rstrip(), True)
    index = marked[-1]
    output = "\n".join(lines[:index] + lines[index + 1 :]).rstrip()
    try:
        payload = json.loads(lines[index][len(ACTIONS_MARKER) :])
    except ValueError:
        return ScriptRun(text.rstrip(), True)
    if not isinstance(payload, dict):
        return ScriptRun(text.rstrip(), True)
    green = payload.get("green")
    return ScriptRun(
        output,
        bool(payload.get("failed", True)),
        _control(payload.get("control")),
        green if isinstance(green, bool) else None,
    )
