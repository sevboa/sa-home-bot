"""Реестр команд: меню-фильтрация, callback-действия, клавиатура /status."""

from __future__ import annotations

from sa_home_bot.bot import commands
from sa_home_bot.bot.status_view import build_status_keyboard
from sa_home_bot.subscriptions.models import Subscription


def test_menu_hides_status_subactions():
    menu_names = {c.name for c in commands.MENU_CONTROL_COMMANDS}
    assert menu_names == {"status", "wake"}  # подкоманды /status скрыты
    for name in ("status_full", "stats", "scan_now", "downtime"):
        assert commands.get(name).menu is False


def test_all_status_actions_are_control_commands():
    for cmd in commands.STATUS_ACTIONS.values():
        assert commands.is_control(cmd.name)


def test_command_for_callback():
    assert commands.command_for_callback("st:full") is commands.STATUS_FULL
    assert commands.command_for_callback("st:scan") is commands.SCAN_NOW
    assert commands.command_for_callback("st:unknown") is None
    assert commands.command_for_callback("garbage") is None
    assert commands.command_for_callback(None) is None


def test_command_for_callback_downtime_pagination():
    # «st:downtime_page:<offset>» — те же права, что и у команды DOWNTIME.
    assert commands.command_for_callback("st:downtime_page:10") is commands.DOWNTIME
    assert commands.command_for_callback("st:downtime_page:0") is commands.DOWNTIME


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


def test_keyboard_shows_only_allowed_actions():
    kb = build_status_keyboard(_sub("status", "downtime"))
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == ["st:downtime"]  # только разрешённое действие


def test_keyboard_full_rights_two_by_two():
    kb = build_status_keyboard(_sub("status_full", "stats", "downtime", "scan_now"))
    assert len(kb.inline_keyboard) == 2  # 4 кнопки, по 2 в ряд
    codes = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert codes == {"st:full", "st:stats", "st:downtime", "st:scan"}


def test_keyboard_none_when_no_actions_allowed():
    assert build_status_keyboard(_sub("status")) is None
    assert build_status_keyboard(None) is None
