"""Голая команда-скилл приложения (bot/handlers/apps.py::cmd_app_skill):
известная команда исполняется, неизвестная и скрытая (HIDDEN_MENU_APP_IDS)
игнорируются одинаково молча."""

from types import SimpleNamespace

from sa_home_bot.bot.handlers.apps import cmd_app_skill
from sa_home_bot.proto.messages import ActionSpec
from sa_home_bot.subscriptions.models import Subscription


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.chat = SimpleNamespace(id=1)
        self.answers: list[str] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append(text)


class FakeAppsLink:
    display_name = "apps"
    connected = True

    def __init__(self):
        self.calls: list[str] = []

    async def actions(self):
        return [ActionSpec(id="jellyfin", title="🎬 Jellyfin")]

    async def command(self, action, args=None):
        self.calls.append(action)
        return {"id": "jellyfin", "title": "🎬 Jellyfin", "unit": "jellyfin.service",
                "status": "active", "urls": []}


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


async def test_known_skill_runs():
    message = FakeMessage("/jellyfin")
    link = FakeAppsLink()
    await cmd_app_skill(message, apps_link=link, subscription=_sub("jellyfin@apps"))
    assert link.calls == ["jellyfin"]
    assert len(message.answers) == 1


async def test_hidden_app_id_is_ignored_even_with_full_rights():
    """qbittorrent убран из голого вызова (apps_view.HIDDEN_MENU_APP_IDS) —
    доступен только кнопкой внутри /torrents; полный доступ (*) на это не
    влияет, команда должна молчать так же, как незнакомая."""
    message = FakeMessage("/qbittorrent")
    link = FakeAppsLink()
    await cmd_app_skill(message, apps_link=link, subscription=_sub("*"))
    assert link.calls == []
    assert message.answers == []


async def test_unknown_command_is_ignored():
    message = FakeMessage("/does_not_exist")
    link = FakeAppsLink()
    await cmd_app_skill(message, apps_link=link, subscription=_sub("*"))
    assert link.calls == []
    assert message.answers == []
