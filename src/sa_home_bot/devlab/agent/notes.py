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


def notes_block(text: str) -> str:
    if not text:
        return ""
    return f"\n\n{NOTES_OPEN}\n{text}\n{NOTES_CLOSE}"


class AgentNotes:
    def __init__(self, path: Path, limit: int = NOTES_LIMIT_CHARS) -> None:
        self._path = path
        self._limit = limit

    def text(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def add(self, text: str) -> str | None:
        note = text.strip()
        if not note:
            return NOTE_EMPTY
        current = self.text()
        joined = f"{current}\n{note}" if current else note
        if len(joined) > self._limit:
            return NOTES_FULL.format(size=len(current), limit=self._limit)
        atomic_write(self._path, joined)
        return None

    def replace(self, text: str) -> str | None:
        note = text.strip()
        if len(note) > self._limit:
            return NOTES_TOO_LONG.format(size=len(note), limit=self._limit)
        atomic_write(self._path, note)
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
