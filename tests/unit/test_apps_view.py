"""Скилы-приложения: карточка, динамическое меню и /help."""

from sa_home_bot import __version__
from sa_home_bot.bot.apps_view import render_app_card
from sa_home_bot.bot.handlers.basic import build_help
from sa_home_bot.bot.setup import build_menu_commands
from sa_home_bot.proto.messages import ActionSpec
from sa_home_bot.subscriptions.models import Subscription


def _app_actions() -> list[ActionSpec]:
    return [
        ActionSpec(id="qbittorrent", title="🧲 qBittorrent"),
        ActionSpec(id="jellyfin", title="🎬 Jellyfin"),
    ]


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


def test_app_card_active_with_urls():
    text = render_app_card(
        {
            "id": "qbittorrent",
            "title": "🧲 qBittorrent",
            "unit": "qbittorrent-nox.service",
            "status": "active",
            "urls": ["http://192.168.0.100:8080"],
        }
    )
    assert "🧲 qBittorrent — ✅ работает" in text
    assert "qbittorrent-nox.service" in text
    assert "Веб-морда: http://192.168.0.100:8080" in text


def test_app_card_failed_without_urls():
    text = render_app_card(
        {"id": "x", "title": "X", "unit": "x.service", "status": "failed", "urls": []}
    )
    assert "❌ упал" in text
    assert "Веб-морда" not in text


def test_menu_skills_first_then_universal():
    menu = build_menu_commands(
        _sub("nodes", "qbittorrent@apps", "jellyfin@apps"), _app_actions()
    )
    names = [c.command for c in menu]
    assert names == ["qbittorrent", "jellyfin", "nodes", "help", "ping", "whoami"]


def test_menu_filters_skills_by_rights():
    menu = build_menu_commands(_sub("jellyfin@apps"), _app_actions())
    names = [c.command for c in menu]
    assert names == ["jellyfin", "help", "ping", "whoami"]


def test_help_lists_allowed_skills_and_about():
    text = build_help(_sub("nodes", "qbittorrent@apps"), _app_actions())
    assert "/qbittorrent — 🧲 qBittorrent" in text
    assert "/jellyfin" not in text  # нет права
    assert "/nodes" in text
    assert f"sa-home-bot v{__version__}" in text


def test_help_without_subscription_only_universal():
    text = build_help(None, _app_actions())
    assert "/qbittorrent" not in text and "/nodes" not in text
    assert "/help" in text and f"v{__version__}" in text
