"""Персистентное состояние ноды (assignments, пиры) — не TOML, без БД.

Ноду можно донастраивать в рантайме (assign/unassign службы — этап 17; join
к рою — этап 18) без правки конфига и без рестарта процесса. Новое состояние
переживает рестарт через этот файл. TOML (`node.assignments`,
`[[swarm.nodes]]`) остаётся стартовым источником, не единственным — см.
`node/app.py`.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from sa_home_bot.config import SwarmNodeConfig


class NodeState(BaseModel):
    assignments: list[str] = Field(default_factory=list)
    peers: list[SwarmNodeConfig] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> NodeState:
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.model_validate_json(p.read_bytes())

    def save(self, path: str | Path) -> None:
        """Атомарная запись: temp-файл в том же каталоге + `os.replace`."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(self.model_dump_json(indent=2))
            os.replace(tmp_name, p)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
