from __future__ import annotations

import re
import time
from pathlib import Path

from sa_home_bot.utils.files import atomic_write

NOTES_LIMIT_CHARS = 6000
NOTES_OPEN = "=== Твои заметки (не перечитывай то, что здесь записано) ==="
NOTES_CLOSE = "=== конец заметок ==="
NOTES_BLOCK_PATTERN = re.compile(r"\n\n=== Твои заметки[^\n]*\n.*\n=== конец заметок ===", re.S)
NOTES_UNAVAILABLE = "заметки недоступны"
NOTE_EMPTY = "заметка пуста: ничего не записано"
NOTES_FULL = (
    "заметки полны ({size} из {limit} симв.), заметка не записана: сожми заметки через "
    "notes_set(текст), оставив главное, и повтори note()"
)
NOTES_TOO_LONG = "текст заметок {size} симв., лимит {limit}: сократи и повтори notes_set()"
NOTES_SUFFIX = ".md"
TODO_LIMIT_CHARS = 2000
TODO_HEADING = "Todo:"
TODO_MARKER_PATTERN = re.compile(r"^=== todo ===\n", re.M)
TODO_MARKER = "=== todo ===\n"
TODO_TOO_LONG = "todo {size} симв., лимит {limit}: сократи список"
TODO_MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
TODO_DONE = frozenset({"done", "completed", "complete", "finished", "x"})
TODO_ACTIVE = frozenset({"in_progress", "doing", "active", "started", "wip"})


def _todo_mark(status: str) -> str:
    key = status.strip().lower()
    if key in TODO_DONE:
        return TODO_MARKS["done"]
    return TODO_MARKS["in_progress"] if key in TODO_ACTIVE else TODO_MARKS["pending"]


def notes_block(text: str) -> str:
    if not text:
        return ""
    return f"\n\n{NOTES_OPEN}\n{text}\n{NOTES_CLOSE}"


class AgentNotes:
    def __init__(self, path: Path, limit: int = NOTES_LIMIT_CHARS) -> None:
        self._path = path
        self._limit = limit

    def _load(self) -> tuple[str, str]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return "", ""
        found = TODO_MARKER_PATTERN.search(raw)
        if found is None:
            return raw.strip(), ""
        return raw[: found.start()].strip(), raw[found.end() :].strip()

    def _save(self, notes: str, todo: str) -> None:
        parts = [notes, TODO_MARKER + todo if todo else ""]
        atomic_write(self._path, "\n\n".join(part for part in parts if part))

    def text(self) -> str:
        return self._load()[0]

    def todo_text(self) -> str:
        return self._load()[1]

    def block_text(self) -> str:
        notes, todo = self._load()
        listed = f"{TODO_HEADING}\n{todo}" if todo else ""
        return "\n\n".join(part for part in (notes, listed) if part)

    def set_todo(self, entries: list[tuple[str, str]]) -> str | None:
        lines = [f"{_todo_mark(status)} {text.strip()}" for text, status in entries if text.strip()]
        todo = "\n".join(lines)
        if len(todo) > TODO_LIMIT_CHARS:
            return TODO_TOO_LONG.format(size=len(todo), limit=TODO_LIMIT_CHARS)
        self._save(self._load()[0], todo)
        return None

    def add(self, text: str) -> str | None:
        note = text.strip()
        if not note:
            return NOTE_EMPTY
        current, todo = self._load()
        joined = f"{current}\n{note}" if current else note
        if len(joined) > self._limit:
            return NOTES_FULL.format(size=len(current), limit=self._limit)
        self._save(joined, todo)
        return None

    def replace(self, text: str) -> str | None:
        note = text.strip()
        if len(note) > self._limit:
            return NOTES_TOO_LONG.format(size=len(note), limit=self._limit)
        self._save(note, self._load()[1])
        return None

    def discard(self) -> None:
        self._path.unlink(missing_ok=True)


def prune_notes(directory: Path, max_age_s: float, now: float | None = None) -> int:
    if not directory.is_dir():
        return 0
    moment = time.time() if now is None else now
    removed = 0
    for entry in directory.glob(f"*{NOTES_SUFFIX}"):
        try:
            if moment - entry.stat().st_mtime > max_age_s:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed
