"""Раздел нод: список нод → карточка ноды → карточка службы."""

from sa_home_bot.bot.node_view import (
    NODE_DOWN_TEXT,
    NODES_HEADER,
    REMOTE_STUB_TEXT,
    build_node_card_keyboard,
    build_nodes_list_keyboard,
    build_remote_node_card_keyboard,
    build_service_card_keyboard,
    render_nodes_list,
    render_remote_node_card,
    render_service_card,
    render_services_block,
)
from sa_home_bot.config import WakeConfig
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

NODE_STATE_WITH_PEERS = {
    **NODE_STATE,
    "peers": [
        {"id": "arch-t480", "endpoint": "tcp://100.110.58.31:8710", "alive": True},
        {"id": "winpc", "endpoint": "tcp://100.64.0.5:8710", "alive": False},
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


# --- Список нод ---------------------------------------------------------------


def test_nodes_list_counts_running_services():
    text = render_nodes_list(NODE_STATE, None)
    assert NODES_HEADER in text
    assert "alfred" in text and "1/2 работают" in text
    assert REMOTE_STUB_TEXT not in text


def test_nodes_list_with_wake_shows_remote_stub():
    text = render_nodes_list(NODE_STATE, WakeConfig(mac="AA:BB:CC:DD:EE:FF"))
    assert REMOTE_STUB_TEXT in text


def test_nodes_list_node_down():
    assert NODE_DOWN_TEXT in render_nodes_list(None, None)


def test_nodes_list_shows_peers_with_lamps():
    text = render_nodes_list(NODE_STATE_WITH_PEERS, None)
    assert "🟢 <b>arch-t480</b>" in text
    assert "🔴 <b>winpc</b> — не в сети" in text


def test_nodes_list_keyboard_card_and_wake():
    kb = build_nodes_list_keyboard(
        _sub("status", "wake"), "alfred", wake=WakeConfig(mac="AA:BB:CC:DD:EE:FF")
    )
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == ["st:nodecard", "st:wake"]


def test_nodes_list_keyboard_includes_peer_cards():
    # «st:nodecard[…]» проверяется правом status (как и локальная карточка) —
    # доступ к /nodes само по себе уже требует права nodes.
    kb = build_nodes_list_keyboard(
        _sub("status"), "alfred", NODE_STATE_WITH_PEERS["peers"]
    )
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == ["st:nodecard", "st:nodecard:arch-t480", "st:nodecard:winpc"]


def test_nodes_list_keyboard_peer_cards_need_status_right():
    kb = build_nodes_list_keyboard(
        _sub("nodes"), "alfred", NODE_STATE_WITH_PEERS["peers"]
    )
    assert kb is None  # ни локальной, ни удалённых карточек, ни wake


def test_nodes_list_keyboard_respects_rights():
    wake = WakeConfig(mac="AA:BB:CC:DD:EE:FF")
    kb = build_nodes_list_keyboard(_sub("wake"), "alfred", wake=wake)
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == ["st:wake"]  # без права status нет карточки
    assert build_nodes_list_keyboard(_sub("stats"), "alfred", wake=WakeConfig()) is None
    assert build_nodes_list_keyboard(None, "alfred", wake=wake) is None


# --- Карточка удалённой ноды --------------------------------------------------


def test_remote_node_card_renders_uptime_and_services():
    state = {**NODE_STATE, "uptime_s": 65.0, "system_uptime_s": 3725.0}
    text = render_remote_node_card(state)
    assert "Нода alfred" in text and "v0.9.0" in text
    assert "Аптайм: система 1ч 2м 5с · нода 1м 5с" in text
    assert "🟢 <b>monitor</b>" in text and "🔴 <b>telegram-bot</b>" in text


def test_remote_node_card_keyboard_needs_nodes_right():
    assert build_remote_node_card_keyboard(_sub("status")) is None
    kb = build_remote_node_card_keyboard(_sub("nodes"))
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == ["st:nodes"]


# --- Карточка ноды ------------------------------------------------------------


def test_services_block_renders_statuses():
    text = render_services_block(NODE_STATE)
    assert "Службы ноды alfred" in text and "v0.9.0" in text
    assert "🟢 <b>monitor</b> — работает, pid 123" in text
    assert "🔴 <b>telegram-bot</b> — остановлена" in text


def test_services_block_empty():
    assert "не назначены" in render_services_block(
        {"node": "x", "version": "1", "services": []}
    )


def test_node_card_keyboard_views_and_service_cards():
    monitor_actions = [ActionSpec(id="scan_now", title="🔄 Скан датчиков")]
    kb = build_node_card_keyboard(
        _sub("status_full", "nodes", "scan_now@monitor"),
        monitor_actions,
        ["monitor", "telegram-bot"],
    )
    codes = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert codes == {
        "st:full",
        "act:monitor:scan_now",
        "st:svc:monitor",
        "st:svc:telegram-bot",
    }


def test_node_card_keyboard_service_cards_need_nodes_right():
    kb = build_node_card_keyboard(_sub("status_full"), [], ["monitor"])
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == ["st:full"]


# --- Карточка службы ----------------------------------------------------------


def test_service_card_text():
    text = render_service_card("alfred", NODE_STATE["services"][0])
    assert "Служба monitor" in text and "нода alfred" in text
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
