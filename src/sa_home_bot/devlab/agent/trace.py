from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sa_home_bot.devlab import protocol

TraceSink = Callable[[dict[str, Any]], None]


def _identity(text: str) -> str:
    return text


class StepTrace:
    def __init__(
        self,
        path: Path,
        max_bytes: int = protocol.STEPS_MAX_BYTES,
        scrub: Callable[[str], str] = _identity,
    ) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._scrub = scrub

    def record(self, entry: dict[str, Any]) -> None:
        line = self._scrub(json.dumps(entry, ensure_ascii=False))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if self._path.stat().st_size > self._max_bytes:
            data = self.tail(self._max_bytes // 2)
            if data is not None:
                self._path.write_bytes(data)

    def tail(self, limit: int) -> bytes | None:
        if not self._path.is_file():
            return None
        size = self._path.stat().st_size
        with self._path.open("rb") as handle:
            handle.seek(max(0, size - limit))
            data = handle.read()
        if size > limit:
            newline = data.find(b"\n")
            data = data[newline + 1 :] if newline >= 0 else b""
        return data
