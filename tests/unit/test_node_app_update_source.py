"""update_source_for_this_platform(): всегда origin_repo_url(), на любой ОС.

check_update/update теперь объявляются и на win32 — переустановку там берёт
на себя Windows-задача планировщика (NodeService._update() →
node_update.trigger_scheduled_task()), а не pipx_reinstall в процессе, см.
deploy/win-auto-update.ps1."""

from __future__ import annotations

from sa_home_bot.node import app as node_app


def test_update_source_passthrough(monkeypatch):
    monkeypatch.setattr(node_app.node_update, "origin_repo_url", lambda: "https://x/y.git")
    assert node_app.update_source_for_this_platform() == "https://x/y.git"


def test_update_source_none_when_not_git_install(monkeypatch):
    monkeypatch.setattr(node_app.node_update, "origin_repo_url", lambda: None)
    assert node_app.update_source_for_this_platform() is None
