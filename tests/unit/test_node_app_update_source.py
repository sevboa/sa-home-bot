"""update_source_for_this_platform(): на win32 самообновление по pipx
структурно не работает (venv-DLL/exe держит открытыми ЭТОТ ЖЕ работающий
процесс), поэтому check_update/update там не объявляются — см.
deploy/win-auto-update.ps1 для рабочего пути."""

from __future__ import annotations

from sa_home_bot.node import app as node_app


def test_update_source_none_on_win32(monkeypatch):
    monkeypatch.setattr(node_app.sys, "platform", "win32")
    monkeypatch.setattr(node_app.node_update, "origin_repo_url", lambda: "https://x/y.git")
    assert node_app.update_source_for_this_platform() is None


def test_update_source_passthrough_on_linux(monkeypatch):
    monkeypatch.setattr(node_app.sys, "platform", "linux")
    monkeypatch.setattr(node_app.node_update, "origin_repo_url", lambda: "https://x/y.git")
    assert node_app.update_source_for_this_platform() == "https://x/y.git"


def test_update_source_none_when_not_git_install(monkeypatch):
    monkeypatch.setattr(node_app.sys, "platform", "linux")
    monkeypatch.setattr(node_app.node_update, "origin_repo_url", lambda: None)
    assert node_app.update_source_for_this_platform() is None
