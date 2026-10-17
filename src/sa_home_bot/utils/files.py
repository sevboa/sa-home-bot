from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)

PRIVATE_FILE_MODE = 0o600


def atomic_write(path: str | Path, data: bytes | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def load_model(model_class: type[ModelT], path: str | Path) -> ModelT:
    path = Path(path)
    if not path.exists():
        return model_class()
    return model_class.model_validate_json(path.read_bytes())


def save_model(model: BaseModel, path: str | Path) -> None:
    atomic_write(path, model.model_dump_json(indent=2))


def write_private(
    path: str | Path,
    data: bytes | str,
    *,
    exclusive: bool = False,
    enforce_mode: bool = False,
    make_parents: bool = False,
    encoding: str | None = None,
) -> None:
    path = Path(path)
    if make_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        if isinstance(data, bytes):
            handle = os.fdopen(fd, "wb")
        else:
            handle = os.fdopen(fd, "w", encoding=encoding)
        with handle:
            handle.write(data)
    except BaseException:
        if exclusive:
            with contextlib.suppress(OSError):
                path.unlink()
        raise
    if enforce_mode:
        os.chmod(path, PRIVATE_FILE_MODE)
