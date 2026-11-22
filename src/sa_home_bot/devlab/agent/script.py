from __future__ import annotations

import json
from dataclasses import dataclass

from sa_home_bot.devlab.agent.sandbox import IGNORED_DIRS

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
    (
        "ls",
        'def ls(prefix: str = "") -> list[str]: ...',
        "пути файлов проекта, начинающиеся с prefix",
    ),
    (
        "read",
        "def read(path: str, start: int = 1, end: int | None = None) -> str: ...",
        "текст файла, строки start..end (с 1, end включительно); годится для old в edit(); "
        "нет файла — EditError",
    ),
    (
        "read_numbered",
        "def read_numbered(path: str, start: int = 1, end: int | None = None) -> str: ...",
        "то же с номерами «n<TAB>строка», для просмотра; для old в edit() не годится",
    ),
    (
        "read_many",
        "def read_many(paths: list[str]) -> dict[str, str]: ...",
        "словарь путь → текст файла, как read()",
    ),
    (
        "grep",
        'def grep(pattern: str, prefix: str = "", context: int = 0) -> list[str]: ...',
        "регулярное выражение по файлам с префиксом пути prefix (файл или каталог): "
        "совпадения «путь:n:строка», строки контекста «путь-n-строка»",
    ),
    (
        "edit",
        "def edit(path: str, old: str, new: str) -> None: ...",
        "заменить в файле единственное вхождение old на new сразу; 0 или больше 1 вхождения, "
        "закрытый путь — EditError",
    ),
    (
        "write",
        "def write(path: str, content: str) -> None: ...",
        "создать или перезаписать файл сразу; закрытый путь — EditError",
    ),
    (
        "run",
        "def run(command: str) -> Result: ...",
        "разрешённая команда без оболочки (pytest…, ruff…); вернёт Result",
    ),
    (
        "run_tests",
        "def run_tests(paths: list[str]) -> Result: ...",
        "pytest по списку путей тестов (можно путь::имя_теста); вернёт Result",
    ),
    (
        CONTROL_STEP_DONE,
        "def step_done(text: str) -> NoReturn: ...",
        "закрыть пронумерованный шаг задания с итогом text; страж прогонит изменённые тесты; "
        "завершает скрипт",
    ),
    (
        CONTROL_FINISH,
        "def finish(summary: str) -> NoReturn: ...",
        "завершить задание с итогом summary; страж проверит дифф, тесты и правила проекта; "
        "завершает скрипт",
    ),
    (
        CONTROL_ASK_OWNER,
        "def ask_owner(question: str) -> NoReturn: ...",
        "вопрос владельцу; ответ придёт следующим сообщением; завершает скрипт",
    ),
    (
        "Result",
        "class Result: ...",
        "итог run()/run_tests(): code: int, output: str (хвост вывода), ok: bool (code == "
        "0); print(r) печатает «[код N]» и вывод",
    ),
    (
        "EditError",
        "class EditError(Exception): ...",
        "правка или чтение не удались: фрагмент не найден или найден не один раз, файла нет, "
        "путь закрыт",
    ),
)
SCRIPT_READ_EXAMPLE = """# Намерение: прочитать load и его тест в переменные, найти описание в docs
src = read("src/pkg/a.py", 40, 90)
test = read("tests/test_a.py")
print(src)
print(test)
print("\\n".join(grep(r"load", "docs/pkg.md")))"""
SCRIPT_EDIT_EXAMPLE = """# Намерение: тест на 2, правка load, прогон, закрыть шаги 1–2
old_test = next(r for r in test.splitlines(keepends=True) if "load() == 1" in r)
edit("tests/test_a.py", old_test, old_test.replace("== 1", "== 2"))
old = next(r for r in src.splitlines(keepends=True) if "return 1" in r)
edit("src/pkg/a.py", old, old.replace("1", "2"))
r = run_tests(["tests/test_a.py"])
print(r)
if r.ok:
    step_done("1–2: load возвращает 2, тест зелёный")"""
SCRIPT_FINAL_EXAMPLE = """# Намерение: запись в docs, шаг 3, ruff и финал
write("docs/pkg.md", read("docs/pkg.md") + "\\n- 2026-11-22: load возвращает 2.\\n")
step_done("3: docs/pkg.md дополнен")
r = run("ruff check src/pkg/a.py tests/test_a.py")
print(r)
if r.ok:
    finish("load возвращает 2; тесты, ruff и docs готовы")"""

PROLOGUE = r'''import ast
import builtins
import fnmatch
import importlib
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

_WORKER = r'''
def _show(value):
    if isinstance(value, str):
        print(value)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        print("\n".join(value))
    elif isinstance(value, dict) and all(isinstance(item, str) for item in value.values()):
        print("\n".join(f"=== {key} ===\n{item}" for key, item in value.items()))
    else:
        print(repr(value))


_wire_in = os.fdopen(os.dup(0), "rb")
_wire_out = os.fdopen(os.dup(1), "wb")
_null = os.open(os.devnull, os.O_RDWR)
for _fd in (0, 1, 2):
    os.dup2(_null, _fd)
sys.stdin = io.StringIO("")
_KNOWN_MODULES = frozenset(sys.modules)
_namespace = {"__name__": "__main__", **{name: globals()[name] for name in _CONFIG["names"]}}


def _drop_project_modules():
    stale = [
        name
        for name, module in list(sys.modules.items())
        if name not in _KNOWN_MODULES
        and str(getattr(module, "__file__", None) or "").startswith(_ROOT + os.sep)
    ]
    for name in stale:
        del sys.modules[name]
    importlib.invalidate_caches()


def _run_one(code):
    global _START
    _START = time.monotonic()
    _BACKUPS.clear()
    _BASE.clear()
    _BASE.update(_stats())
    _STATE["green"] = None
    _drop_project_modules()
    buffer = io.StringIO()
    failed = False
    control = None
    sys.stdout = buffer
    try:
        try:
            tree = ast.parse(code, _CONFIG["filename"])
            last = None
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last = ast.Expression(tree.body.pop().value)
            exec(compile(tree, _CONFIG["filename"], "exec"), _namespace)
            if last is not None:
                value = eval(compile(last, _CONFIG["filename"], "eval"), _namespace)
                if value is not None:
                    _show(value)
        except _Signal as signal_exit:
            control = {"tool": signal_exit.tool, "args": signal_exit.payload}
        except SystemExit as exit_request:
            failed = exit_request.code not in (None, 0)
        except BaseException as error:
            failed = True
            traceback.print_exception(
                type(error), error, error.__traceback__.tb_next, file=sys.stdout
            )
        try:
            for note in _finalize():
                print(note)
        except Exception as error:
            print(f"[проверка путей записи не удалась: {error}]")
    finally:
        sys.stdout = sys.__stdout__
    return {
        "output": buffer.getvalue(),
        "failed": failed,
        "control": control,
        "green": _STATE["green"],
    }


def _send(payload):
    data = json.dumps(payload).encode()
    _wire_out.write(str(len(data)).encode() + b"\n" + data)
    _wire_out.flush()


while True:
    _head = _wire_in.readline()
    if not _head:
        break
    _request = json.loads(_wire_in.read(int(_head)).decode())
    _send(_run_one(_request["code"]))
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


def worker_argv(config: dict[str, object], python: str = "python") -> list[str]:
    full = {
        **config,
        "ignored": sorted(IGNORED_DIRS),
        "forbidden": PROCESS_FORBIDDEN,
        "max_bytes": SCRIPT_GREP_MAX_FILE_BYTES,
        "max_matches": SCRIPT_GREP_MAX_MATCHES,
        "names": [name for name, _, _ in SCRIPT_API] + ["Result", "EditError"],
        "filename": SCRIPT_FILENAME,
    }
    source = f"_CONFIG = json.loads({json.dumps(full)!r})\n"
    return [python, "-I", "-c", f"import json\n{source}{PROLOGUE}\n{_WORKER}"]


def request_payload(code: str) -> bytes:
    return json.dumps({"code": code}).encode()


def _control(payload: object) -> ScriptControl | None:
    if not isinstance(payload, dict):
        return None
    tool, args = payload.get("tool"), payload.get("args")
    if not isinstance(tool, str) or not isinstance(args, dict):
        return None
    return ScriptControl(tool, args)


def parse_reply(data: bytes) -> ScriptRun:
    text = data.decode(errors="replace")
    try:
        payload = json.loads(data)
    except ValueError:
        return ScriptRun(text.rstrip(), True)
    if not isinstance(payload, dict) or not isinstance(payload.get("output"), str):
        return ScriptRun(text.rstrip(), True)
    green = payload.get("green")
    return ScriptRun(
        payload["output"].rstrip(),
        bool(payload.get("failed", True)),
        _control(payload.get("control")),
        green if isinstance(green, bool) else None,
    )
