"""Реестр команд: меню-фильтрация, callback-действия, клавиатура /status."""

from __future__ import annotations

from sa_home_bot.bot import commands
from sa_home_bot.bot.status_view import build_status_keyboard
from sa_home_bot.proto.messages import ActionParam, ActionSpec
from sa_home_bot.subscriptions.models import Subscription


def test_menu_hides_status_subactions():
    menu_names = {c.name for c in commands.MENU_CONTROL_COMMANDS}
    assert menu_names == {"status", "node", "wake"}  # подкоманды /status скрыты
    for name in ("status_full", "stats", "scan_now", "downtime"):
        assert commands.get(name).menu is False


def test_all_status_actions_are_control_commands():
    for cmd in commands.STATUS_ACTIONS.values():
        assert commands.is_control(cmd.name)


def test_command_for_callback():
    assert commands.command_for_callback("st:full") is commands.STATUS_FULL
    assert commands.command_for_callback("st:unknown") is None
    assert commands.command_for_callback("garbage") is None
    assert commands.command_for_callback(None) is None


def test_parse_action_callback():
    assert commands.parse_action_callback("act:monitor:scan_now") == (
        "monitor",
        "scan_now",
        None,
    )
    assert commands.parse_action_callback("act:node:restart:telegram-bot") == (
        "node",
        "restart",
        "telegram-bot",
    )
    assert commands.parse_action_callback("st:full") is None
    assert commands.parse_action_callback("act:node") is None
    assert commands.parse_action_callback(None) is None


def test_command_for_callback_downtime_pagination():
    # «st:downtime_page:<offset>» — те же права, что и у команды DOWNTIME.
    assert commands.command_for_callback("st:downtime_page:10") is commands.DOWNTIME
    assert commands.command_for_callback("st:downtime_page:0") is commands.DOWNTIME


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


def _monitor_actions() -> list[ActionSpec]:
    return [ActionSpec(id="scan_now", title="🔄 Скан датчиков")]


def test_keyboard_shows_only_allowed_actions():
    kb = build_status_keyboard(_sub("status", "downtime"), _monitor_actions())
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == ["st:downtime"]  # только разрешённое представление


def test_keyboard_full_rights_two_by_two():
    kb = build_status_keyboard(
        _sub("status_full", "stats", "downtime", "scan_now@monitor"),
        _monitor_actions(),
    )
    assert len(kb.inline_keyboard) == 2  # 4 кнопки, по 2 в ряд
    codes = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert codes == {"st:full", "st:stats", "st:downtime", "act:monitor:scan_now"}


def test_keyboard_dynamic_action_takes_title_from_describe():
    kb = build_status_keyboard(_sub("scan_now"), _monitor_actions())  # голое имя — ок
    button = kb.inline_keyboard[0][0]
    assert button.text == "🔄 Скан датчиков"
    assert button.callback_data == "act:monitor:scan_now"


def test_keyboard_parametrized_actions_not_in_status():
    actions = [
        ActionSpec(
            id="set_threshold",
            title="Порог",
            params=(ActionParam(name="value", type="float"),),
        )
    ]
    assert build_status_keyboard(_sub("set_threshold"), actions) is None


def test_keyboard_none_when_no_actions_allowed():
    assert build_status_keyboard(_sub("status"), _monitor_actions()) is None
    assert build_status_keyboard(None, _monitor_actions()) is None
