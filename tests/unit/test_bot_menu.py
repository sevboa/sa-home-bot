"""Меню/help не должны показывать параметризованные apps-действия голыми.

Живой баг 2026-07-18: describe службы apps отдаёт вперемешку действия per-app
(id приложения, без параметров — карточка) и общие управляющие start/stop/
restart (обязательный параметр "какое приложение" — кнопки НА карточке).
build_menu_commands/build_help добавляли и вторые тоже как голые команды
/start, /stop, /restart — без приложения они бессмысленны (или падают
"нет такого приложения: ''").
"""

from __future__ import annotations

from sa_home_bot.bot.handlers.basic import build_help
from sa_home_bot.bot.setup import build_menu_commands
from sa_home_bot.proto.messages import ActionParam, ActionSpec
from sa_home_bot.subscriptions.models import Subscription

_NAME_PARAM = ActionParam(name="name", type="string", required=True, choices=("qbittorrent",))

_APP_ACTIONS = [
    ActionSpec(id="qbittorrent", title="🧲 qBittorrent"),
    ActionSpec(id="start", title="▶️ Запустить", params=(_NAME_PARAM,)),
    ActionSpec(id="stop", title="⏹ Остановить", params=(_NAME_PARAM,)),
    ActionSpec(id="restart", title="🔄 Перезапустить", params=(_NAME_PARAM,)),
]


def _sub() -> Subscription:
    return Subscription(
        chat_id=1,
        name="me",
        allowed_commands=frozenset(
            {"qbittorrent@apps", "start@apps", "stop@apps", "restart@apps"}
        ),
    )


def test_menu_commands_skip_parameterized_app_actions():
    menu = build_menu_commands(_sub(), _APP_ACTIONS)
    names = {c.command for c in menu}
    assert "qbittorrent" in names
    assert names.isdisjoint({"start", "stop", "restart"})


def test_help_skips_parameterized_app_actions():
    text = build_help(_sub(), _APP_ACTIONS)
    assert "/qbittorrent" in text
    for bare in ("/start —", "/stop —", "/restart —"):
        assert bare not in text
