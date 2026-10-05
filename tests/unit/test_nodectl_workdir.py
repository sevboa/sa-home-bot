"""Локальные команды бэкапа nodectl работают из рабочего каталога ноды."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sa_home_bot import nodectl


def test_backup_release_writes_marker_into_node_workdir(tmp_path, monkeypatch):
    workdir = tmp_path / "node"
    (workdir / "data").mkdir(parents=True)
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    config = elsewhere / "config.toml"
    config.write_text('[node]\nid = "jeeves"\n')
    monkeypatch.setattr(nodectl, "NODE_WORKDIR", str(workdir))
    monkeypatch.chdir(elsewhere)

    args = argparse.Namespace(config="config.toml")
    assert nodectl._run_backup_release(args) == 0

    assert (workdir / "data" / "backup-publish.ok").is_file()
    assert not (elsewhere / "data").exists()
    assert Path(args.config) == config
    assert Path(os.getcwd()) == workdir
