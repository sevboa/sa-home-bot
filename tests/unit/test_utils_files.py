from __future__ import annotations

import os
import stat

import pytest
from pydantic import BaseModel

from sa_home_bot.utils.files import atomic_write, load_model, save_model, write_private


class Sample(BaseModel):
    name: str = "default"
    count: int = 0


def test_atomic_write_creates_parents_and_leaves_no_temp_files(tmp_path):
    target = tmp_path / "a" / "b.bin"
    atomic_write(target, b"data")
    atomic_write(target, "text")
    assert target.read_text() == "text"
    assert [p.name for p in target.parent.iterdir()] == ["b.bin"]


def test_atomic_write_failure_keeps_old_content_and_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / "f"
    atomic_write(target, b"old")

    def boom(*_):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["f"]


def test_model_roundtrip_and_default_when_missing(tmp_path):
    path = tmp_path / "state.json"
    assert load_model(Sample, path) == Sample()
    save_model(Sample(name="x", count=3), path)
    assert load_model(Sample, path) == Sample(name="x", count=3)


def test_write_private_is_0600_bytes_and_text(tmp_path):
    binary = tmp_path / "bin"
    text = tmp_path / "sub" / "txt"
    write_private(binary, b"\x00\x01")
    write_private(text, "привет", make_parents=True, encoding="utf-8")
    assert binary.read_bytes() == b"\x00\x01"
    assert text.read_text(encoding="utf-8") == "привет"
    assert stat.S_IMODE(binary.stat().st_mode) == 0o600
    assert stat.S_IMODE(text.stat().st_mode) == 0o600


def test_write_private_truncates_and_enforces_mode_on_existing_file(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"long old content")
    path.chmod(0o644)
    write_private(path, b"new", enforce_mode=True)
    assert path.read_bytes() == b"new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_private_exclusive_refuses_existing_and_removes_on_failure(tmp_path):
    path = tmp_path / "key"
    write_private(path, "k", exclusive=True)
    with pytest.raises(FileExistsError):
        write_private(path, "other", exclusive=True)
    assert path.read_text() == "k"

    broken = tmp_path / "broken"
    with pytest.raises(UnicodeEncodeError):
        write_private(broken, "ключ", exclusive=True, encoding="ascii")
    assert not broken.exists()
