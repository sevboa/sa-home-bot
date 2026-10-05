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


def test_backup_cli_without_config_uses_default(tmp_path, monkeypatch):
    import asyncio

    from sa_home_bot import backup_cli

    config = tmp_path / "config.toml"
    config.write_text('[node]\nid = "alfred"\nsocket = "/nonexistent/node.sock"\n')
    monkeypatch.setattr(nodectl, "_default_config", lambda: str(config))
    seen = {}

    async def fake_open_ask(settings, config_path, holder, node):
        seen["socket"] = settings.node.socket
        seen["config"] = config_path
        raise ConnectionError("стоп")

    monkeypatch.setattr(backup_cli, "_open_ask", fake_open_ask)
    args = argparse.Namespace(config=None, backup_command="list", node="jeeves", holder=None)
    try:
        asyncio.run(backup_cli._run_restore(args))
    except ConnectionError:
        pass
    assert seen == {"socket": "/nonexistent/node.sock", "config": str(config)}
