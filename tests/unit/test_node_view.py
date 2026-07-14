"""Раздел нод: единая карточка ноды → карточка службы (сводка роя — в
test_swarm_view.py)."""

from sa_home_bot.bot.node_view import (
    build_node_card_keyboard,
    build_service_card_keyboard,
    render_node_card_header,
    render_service_card,
    render_services_block,
)
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


def _power_action(action_id: str) -> ActionSpec:
    return ActionSpec(id=action_id, title=f"⏻ {action_id}")


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


# --- Единая карточка ноды (своя и пир — один рендер и одна клавиатура) --------


def test_node_card_header_renders_name_version_uptime():
    state = {**NODE_STATE, "uptime_s": 65.0, "system_uptime_s": 3725.0}
    text = render_node_card_header(state)
    assert "Нода alfred" in text and "v0.9.0" in text
    assert "Аптайм: система 1ч 2м 5с · нода 1м 5с" in text


def test_node_card_header_without_uptime_fields():
    text = render_node_card_header(NODE_STATE)
    assert "Нода alfred" in text
    assert "Аптайм" not in text


def test_services_block_renders_statuses_with_links():
    text = render_services_block(NODE_STATE)
    # Имя службы — ссылка на её карточку; дефисы нормализованы.
    assert "🟢 /svc_alfred_monitor — работает, pid 123" in text
    assert "🔴 /svc_alfred_telegram_bot — остановлена" in text


def test_services_block_empty():
    assert "не назначены" in render_services_block(
        {"node": "x", "version": "1", "services": []}
    )


def test_node_card_keyboard_actions_only_no_service_cards():
    # Навигация к службам — ссылками в тексте, кнопок ⚙️ больше нет.
    monitor_actions = [ActionSpec(id="scan_now", title="🔄 Скан датчиков")]
    kb = build_node_card_keyboard(
        _sub("status_full", "nodes", "scan_now@monitor"),
        monitor_actions,
        ["monitor", "telegram-bot"],
    )
    codes = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert codes == {"st:full", "act:monitor:scan_now"}


def test_node_card_keyboard_peer_carries_node_id_everywhere():
    # Тот же состав кнопок, что у своей ноды, — каждая несёт node_id пира
    # (ARCHITECTURE §11 п. 1: рой равноправен).
    monitor_actions = [ActionSpec(id="scan_now", title="🔄 Скан датчиков")]
    assign = ActionSpec(
        id="assign",
        title="➕ Назначить",
        params=(ActionParam(name="name", choices=("monitor", "apps")),),
    )
    kb = build_node_card_keyboard(
        _sub("status_full", "nodes", "scan_now@monitor", "poweroff@node", "assign@node"),
        monitor_actions,
        ["monitor"],
        [_power_action("poweroff"), assign],
        node_id="arch-t480",
    )
    codes = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert codes == {
        "st:full:arch-t480",
        "act:monitor:scan_now::arch-t480",
        "act:node:poweroff::arch-t480",
        "act:node:assign:apps:arch-t480",  # «Назначить» доступно и пиру
    }


def test_node_card_keyboard_includes_power_buttons():
    kb = build_node_card_keyboard(
        _sub("poweroff@node", "suspend@node"),
        [],
        [],
        [_power_action("poweroff"), _power_action("suspend"), _power_action("reboot")],
    )
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == ["act:node:poweroff", "act:node:suspend"]  # reboot без права — нет кнопки


# --- Карточка службы ----------------------------------------------------------


def test_service_card_text():
    text = render_service_card("alfred", NODE_STATE["services"][0])
    assert "Служба monitor" in text
    assert "нода /node_alfred" in text  # обратный переход — ссылкой
    assert "🟢 работает, pid 123" in text
    assert "Рестартов после падений: 0" in text


def test_service_card_keyboard_actions_for_this_service():
    kb = build_service_card_keyboard(
        _sub("start@node", "stop@node", "restart@node"), _node_actions(), "monitor"
    )
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == [
        "act:node:start:monitor",
        "act:node:stop:monitor",
        "act:node:restart:monitor",
    ]


def test_service_card_keyboard_carries_peer_node_id():
    kb = build_service_card_keyboard(
        _sub("restart@node"), _node_actions(), "monitor", "arch-t480"
    )
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == ["act:node:restart:monitor:arch-t480"]


def test_service_card_keyboard_filters_by_right_and_choices():
    kb = build_service_card_keyboard(_sub("restart@node"), _node_actions(), "monitor")
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == ["act:node:restart:monitor"]
    # Служба вне choices действия — кнопок нет.
    assert (
        build_service_card_keyboard(_sub("restart@node"), _node_actions(), "apps")
        is None
    )
    assert build_service_card_keyboard(None, _node_actions(), "monitor") is None
