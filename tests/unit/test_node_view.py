"""Раздел /node: рендер состояния и динамическая клавиатура из describe ноды."""

from sa_home_bot.bot.node_view import build_node_keyboard, render_node_state
from sa_home_bot.proto.messages import ActionParam, ActionSpec
from sa_home_bot.subscriptions.models import Subscription

NODE_STATE = {
    "node": "alfred",
    "version": "0.9.0",
    "services": [
        {
            "name": "monitor",
            "status": "running",
            "pid": 123,
            "restarts": 0,
            "started_at": "2026-07-07T06:14:50+00:00",
        },
        {"name": "telegram-bot", "status": "stopped", "pid": None, "restarts": 2},
    ],
}


def _node_actions() -> list[ActionSpec]:
    name_param = ActionParam(
        name="name", choices=("monitor", "telegram-bot"), title="Служба"
    )
    return [
        ActionSpec(id="start", title="▶️ Запустить", params=(name_param,)),
        ActionSpec(id="stop", title="⏹ Остановить", params=(name_param,)),
        ActionSpec(id="restart", title="🔄 Перезапустить", params=(name_param,)),
    ]


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


def test_render_node_state():
    text = render_node_state(NODE_STATE)
    assert "Нода alfred" in text and "v0.9.0" in text
    assert "✅ <b>monitor</b> — работает, pid 123" in text
    assert "⏹ <b>telegram-bot</b> — остановлена" in text


def test_render_node_state_empty():
    assert "не назначены" in render_node_state({"node": "x", "version": "1", "services": []})


def test_keyboard_action_per_choice_with_full_rights():
    kb = build_node_keyboard(
        _sub("start@node", "stop@node", "restart@node"), _node_actions()
    )
    buttons = [b for row in kb.inline_keyboard for b in row]
    # 3 действия × 2 службы = 6 кнопок, callback несёт службу-значение.
    assert len(buttons) == 6
    callbacks = {b.callback_data for b in buttons}
    assert "act:node:restart:telegram-bot" in callbacks
    assert "act:node:stop:monitor" in callbacks
    texts = {b.text for b in buttons}
    assert "🔄 Перезапустить · monitor" in texts


def test_keyboard_filters_by_action_permission():
    kb = build_node_keyboard(_sub("restart@node"), _node_actions())
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callbacks == ["act:node:restart:monitor", "act:node:restart:telegram-bot"]


def test_keyboard_none_without_rights():
    assert build_node_keyboard(_sub("status"), _node_actions()) is None
    assert build_node_keyboard(None, _node_actions()) is None


def test_action_without_params_is_single_button():
    actions = [ActionSpec(id="reload", title="Перечитать конфиг")]
    kb = build_node_keyboard(_sub("reload@node"), actions)
    button = kb.inline_keyboard[0][0]
    assert button.callback_data == "act:node:reload"
