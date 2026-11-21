from __future__ import annotations

import json
from dataclasses import dataclass
from string import Template

from sa_home_bot.devlab.agent.sandbox import IGNORED_DIRS

ACTIONS_MARKER = "DEVLAB_ACTIONS "
ACTION_EDIT = "edit"
ACTION_WRITE = "write"
ACTION_RUN_TESTS = "run_tests"
SCRIPT_GREP_MAX_MATCHES = 200
SCRIPT_GREP_MAX_FILE_BYTES = 200_000
SCRIPT_FILENAME = "<script>"

_PROLOGUE_TEMPLATE = Template(
    '''import json
import os
import re
import sys
import traceback

_IGNORED = frozenset($ignored)
_ACTIONS = []


def ls(prefix=""):
    found = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in _IGNORED]
        for name in files:
            relative = os.path.relpath(os.path.join(root, name), ".").replace(os.sep, "/")
            if relative.startswith(prefix):
                found.append(relative)
    return sorted(found)


def _lines(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read().splitlines()


def read(path, start=1, end=None):
    lines = _lines(path)
    last = len(lines) if end is None else min(end, len(lines))
    return "\\n".join(f"{n}\\t{lines[n - 1]}" for n in range(max(1, start), last + 1))


def read_many(paths):
    return {path: read(path) for path in paths}


def grep(pattern, prefix="", context=0):
    compiled = re.compile(pattern)
    found = []
    total = 0
    for path in ls(prefix):
        if os.path.getsize(path) > $max_bytes:
            continue
        try:
            lines = _lines(path)
        except (UnicodeDecodeError, OSError):
            continue
        hits = []
        for index, line in enumerate(lines):
            if compiled.search(line):
                hits.append(index)
                total += 1
                if total >= $max_matches:
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
        if total >= $max_matches:
            found.append(f"... показаны первые {$max_matches} совпадений")
            break
    return found


def _queue(function, **args):
    for key, value in args.items():
        if not isinstance(value, str):
            raise TypeError(f"{function.__name__}: аргумент {key} должен быть строкой")
    _ACTIONS.append({"tool": function.__name__, "args": args})


def $edit(path, old, new):
    _queue($edit, path=path, old=old, new=new)


def $write(path, content):
    _queue($write, path=path, content=content)


def $run_tests(paths):
    listed = [paths] if isinstance(paths, str) else list(paths)
    _queue($run_tests, paths=" ".join(listed))
'''
)

PROLOGUE: str = _PROLOGUE_TEMPLATE.substitute(
    ignored=repr(sorted(IGNORED_DIRS)),
    max_bytes=SCRIPT_GREP_MAX_FILE_BYTES,
    max_matches=SCRIPT_GREP_MAX_MATCHES,
    edit=ACTION_EDIT,
    write=ACTION_WRITE,
    run_tests=ACTION_RUN_TESTS,
)

_EPILOGUE = f'''
_failed = False
_namespace = {{
    "__name__": "__main__",
    **{{name: globals()[name] for name in (
        "ls", "read", "read_many", "grep", {ACTION_EDIT!r}, {ACTION_WRITE!r}, {ACTION_RUN_TESTS!r}
    )}},
}}
try:
    exec(compile(_code, {SCRIPT_FILENAME!r}, "exec"), _namespace)
except SystemExit as _exit:
    _failed = _exit.code not in (None, 0)
except BaseException:
    _failed = True
    traceback.print_exc(file=sys.stdout)
sys.stdout.flush()
print("\\n" + {ACTIONS_MARKER!r} + json.dumps(
    {{"failed": _failed, "actions": _ACTIONS}}, ensure_ascii=False
))
'''


@dataclass(frozen=True)
class ScriptAction:
    tool: str
    args: dict[str, str]


@dataclass(frozen=True)
class ScriptRun:
    output: str
    actions: tuple[ScriptAction, ...]
    failed: bool


def script_argv(code: str, python: str = "python") -> list[str]:
    source = f"{PROLOGUE}\n_code = {code!r}\n{_EPILOGUE}"
    return [python, "-I", "-c", source]


def _actions(payload: object) -> tuple[ScriptAction, ...] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("actions"), list):
        return None
    actions = []
    for entry in payload["actions"]:
        if not isinstance(entry, dict):
            return None
        tool, args = entry.get("tool"), entry.get("args")
        if not isinstance(tool, str) or not isinstance(args, dict):
            return None
        actions.append(ScriptAction(tool, args))
    return tuple(actions)


def parse_output(text: str) -> ScriptRun:
    lines = text.splitlines()
    marked = [i for i, line in enumerate(lines) if line.startswith(ACTIONS_MARKER)]
    if not marked:
        return ScriptRun(text.rstrip(), (), True)
    index = marked[-1]
    output = "\n".join(lines[:index] + lines[index + 1 :]).rstrip()
    try:
        payload = json.loads(lines[index][len(ACTIONS_MARKER) :])
    except ValueError:
        return ScriptRun(text.rstrip(), (), True)
    actions = _actions(payload)
    if actions is None:
        return ScriptRun(text.rstrip(), (), True)
    return ScriptRun(output, actions, bool(payload.get("failed", True)))
