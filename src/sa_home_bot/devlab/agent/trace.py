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

    def _scrubbed(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._scrub(value)
        if isinstance(value, dict):
            return {key: self._scrubbed(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._scrubbed(item) for item in value]
        return value

    def record(self, entry: dict[str, Any]) -> None:
        line = json.dumps(self._scrubbed(entry), ensure_ascii=False)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if self._path.stat().st_size > self._max_bytes:
            half = self._max_bytes // 2
            data = self.tail(half)
            if not data:
                data = self._shortened(line, half)
            self._path.write_bytes(data)

    def _shortened(self, line: str, limit: int) -> bytes:
        entry = json.loads(line)
        calls = entry.pop("calls", None)
        if calls is not None:
            entry["calls_dropped"] = len(calls)
        for key, value in entry.items():
            if isinstance(value, str):
                entry[key] = value[: max(limit // 8, 1)]
        return (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")[-limit:]

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
