"""Скилы-приложения: карточка, динамическое меню, /help, управление юнитом."""

from sa_home_bot import __version__
from sa_home_bot.bot.apps_view import render_app_card, run_app_skill
from sa_home_bot.bot.handlers.basic import build_help
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.bot.setup import build_menu_commands
from sa_home_bot.proto.messages import ERR_NEEDS_PRIVILEGE, ActionParam, ActionSpec, ProtoError
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
    # Право «nodes» открывает /swarm — алиасы делят одно право.
    menu = build_menu_commands(
        _sub("nodes", "qbittorrent@apps", "jellyfin@apps"), _app_actions()
    )
    names = [c.command for c in menu]
    assert names == ["qbittorrent", "jellyfin", "swarm", "help", "ping", "whoami"]


def test_menu_filters_skills_by_rights():
    menu = build_menu_commands(_sub("jellyfin@apps"), _app_actions())
    names = [c.command for c in menu]
    assert names == ["jellyfin", "help", "ping", "whoami"]


def test_help_lists_allowed_skills_and_about():
    text = build_help(_sub("nodes", "qbittorrent@apps"), _app_actions())
    assert "/qbittorrent — 🧲 qBittorrent" in text
    assert "/jellyfin" not in text  # нет права
    assert "/swarm" in text
    assert f"sa-home-bot v{__version__}" in text


def test_help_without_subscription_only_universal():
    text = build_help(None, _app_actions())
    assert "/qbittorrent" not in text and "/swarm" not in text
    assert "/help" in text and f"v{__version__}" in text


# --- run_app_skill: карточка + кнопки управления / ошибка прав ---


def _manage_actions() -> list[ActionSpec]:
    name_param = ActionParam(name="name", choices=("qbittorrent", "jellyfin"))
    return [
        *_app_actions(),
        ActionSpec(id="start", title="▶️ Запустить", params=(name_param,)),
        ActionSpec(id="stop", title="⏹ Остановить", params=(name_param,)),
        ActionSpec(id="restart", title="🔄 Перезапустить", params=(name_param,)),
    ]


class FakeAppsLink:
    display_name = "apps"

    def __init__(self, result=None, fail=False, proto_error: ProtoError | None = None):
        self.connected = not fail
        self._fail = fail
        self._proto_error = proto_error
        self._result = result or {
            "id": "qbittorrent",
            "title": "🧲 qBittorrent",
            "unit": "qbittorrent-nox.service",
            "status": "active",
            "urls": [],
        }
        self.calls: list[tuple[str, dict]] = []

    async def actions(self):
        return _manage_actions()

    async def command(self, action, args=None):
        if self._fail:
            raise ServiceUnavailableError("нет связи")
        if self._proto_error is not None:
            raise self._proto_error
        self.calls.append((action, args or {}))
        return self._result


def _sub_with(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


async def test_run_app_skill_card_only_no_manage_rights():
    link = FakeAppsLink()
    text, keyboard = await run_app_skill(link, _sub_with("qbittorrent@apps"), "qbittorrent")
    assert "🧲 qBittorrent" in text
    assert keyboard is None  # нет start/stop/restart@apps — кнопок нет


async def test_run_app_skill_adds_manage_buttons_when_allowed():
    link = FakeAppsLink()
    text, keyboard = await run_app_skill(
        link, _sub_with("qbittorrent@apps", "start@apps", "stop@apps"), "qbittorrent"
    )
    assert keyboard is not None
    labels = {b.text for row in keyboard.inline_keyboard for b in row}
    assert labels == {"▶️ Запустить", "⏹ Остановить"}  # restart не разрешён


async def test_run_app_skill_manage_action_calls_command_with_name():
    link = FakeAppsLink()
    await run_app_skill(link, _sub_with("start@apps"), "start", "qbittorrent")
    assert link.calls == [("start", {"name": "qbittorrent"})]


async def test_run_app_skill_needs_privilege_shows_fix_hint():
    link = FakeAppsLink(
        proto_error=ProtoError(ERR_NEEDS_PRIVILEGE, "нужны права — выполните nodectl fix")
    )
    text, keyboard = await run_app_skill(link, _sub_with("start@apps"), "start", "qbittorrent")
    assert "nodectl fix" in text
    assert keyboard is None


async def test_run_app_skill_unavailable_when_disconnected():
    link = FakeAppsLink(fail=True)
    text, keyboard = await run_app_skill(link, _sub_with("start@apps"), "start", "qbittorrent")
    assert "недоступна" in text
    assert keyboard is None
