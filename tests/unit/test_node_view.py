"""Раздел нод: единая карточка ноды → карточка службы (сводка роя — в
test_swarm_view.py)."""

from sa_home_bot.bot.node_view import (
    SERVICES_PAGE_SIZE,
    build_node_card_keyboard,
    build_service_card_keyboard,
    build_services_list_view,
    render_node_card_header,
    render_node_card_summary,
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


def test_node_card_summary_is_just_a_count():
    assert render_node_card_summary(NODE_STATE) == "Служб: 2"


# --- Список служб (отдельный постраничный экран, право `nodes`) -------------


def test_services_list_view_shows_status_lines_and_buttons():
    text, kb = build_services_list_view(NODE_STATE, node_id=None, offset=0)
    assert "Службы «alfred»" in text
    assert "monitor" in text and "🟢" in text
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "st:sw_svc:-:monitor" in codes
    assert "st:sw_svc:-:telegram-bot" in codes
    assert "st:sw_node:-" in codes  # ⬅️ Назад


def test_services_list_view_peer_carries_node_id():
    text, kb = build_services_list_view(NODE_STATE, node_id="arch-t480", offset=0)
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "st:sw_svc:arch-t480:monitor" in codes
    assert "st:sw_node:arch-t480" in codes


def test_services_list_view_paginates():
    many_services = {
        "node": "alfred",
        "services": [
            {"name": f"svc{i}", "status": "running"} for i in range(SERVICES_PAGE_SIZE + 2)
        ],
    }
    text, kb = build_services_list_view(many_services, node_id=None, offset=0)
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"st:sw_svcs:-:{SERVICES_PAGE_SIZE}" in codes


def test_services_list_view_empty():
    text, _ = build_services_list_view({"node": "alfred", "services": []}, None, 0)
    assert "не назначены" in text


def _assign_action(*choices: str) -> ActionSpec:
    return ActionSpec(
        id="assign",
        title="➕ Назначить",
        params=(ActionParam(name="name", choices=choices),),
    )


def test_services_list_view_shows_assign_buttons_for_unassigned_names():
    # Кнопка переехала сюда с карточки ноды (у кого есть право `nodes`) —
    # по одной на каждое ещё не назначенное имя, уже назначенные не дублируются.
    text, kb = build_services_list_view(
        NODE_STATE,
        node_id=None,
        offset=0,
        subscription=_sub("nodes", "assign@node"),
        node_actions=[_assign_action("monitor", "telegram-bot", "apps")],
    )
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "act:node:assign:apps" in codes
    assert "act:node:assign:monitor" not in codes  # уже назначена
    assert "act:node:assign:telegram-bot" not in codes  # уже назначена


def test_services_list_view_assign_buttons_carry_peer_node_id():
    text, kb = build_services_list_view(
        NODE_STATE,
        node_id="arch-t480",
        offset=0,
        subscription=_sub("nodes", "assign@node"),
        node_actions=[_assign_action("monitor", "telegram-bot", "apps")],
    )
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "act:node:assign:apps:arch-t480" in codes


def test_services_list_view_no_assign_buttons_without_right():
    text, kb = build_services_list_view(
        NODE_STATE,
        node_id=None,
        offset=0,
        subscription=_sub("nodes"),  # без assign@node
        node_actions=[_assign_action("monitor", "telegram-bot", "apps")],
    )
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert not any(c.startswith("act:node:assign") for c in codes)


def test_node_card_keyboard_actions_only_no_service_cards():
    # Навигация к службам — отдельный постраничный экран (право `nodes`),
    # на карточке — счётчик и кнопка «⚙️ Службы», не полный список.
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
        "st:sw_node:-",  # 🔄 Обновить
        "st:sw_svcs:-:0",  # ⚙️ Службы (2)
        "st:sw_nodes:0",  # ⬅️ Назад
    }


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
        # «Назначить» тут нет — с правом `nodes` кнопка теперь на экране
        # «Службы» (build_services_list_view), см. тест ниже.
        "st:sw_node:arch-t480",  # 🔄 Обновить
        "st:sw_svcs:arch-t480:0",  # ⚙️ Службы (1)
        "st:sw_nodes:0",  # ⬅️ Назад
    }


def test_node_card_keyboard_assign_stays_on_card_without_nodes_right():
    # Без права `nodes` экрана «Службы» не существует — «➕ Назначить»
    # остаётся единственным способом добавить службу, поэтому держится
    # прямо на карточке (единственный экран, доступный такой подписке).
    assign = ActionSpec(
        id="assign",
        title="➕ Назначить",
        params=(ActionParam(name="name", choices=("monitor", "apps")),),
    )
    kb = build_node_card_keyboard(
        _sub("poweroff@node", "assign@node"),
        [],
        ["monitor"],
        [_power_action("poweroff"), assign],
        node_id="arch-t480",
    )
    codes = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert codes == {
        "act:node:poweroff::arch-t480",
        "act:node:assign:apps:arch-t480",
        "st:sw_node:arch-t480",  # 🔄 Обновить
        # Без «nodes» — ни «Службы», ни «Назад» (тех экранов не существует).
    }


def test_node_card_keyboard_includes_power_buttons():
    kb = build_node_card_keyboard(
        _sub("poweroff@node", "suspend@node"),
        [],
        [],
        [_power_action("poweroff"), _power_action("suspend"), _power_action("reboot")],
    )
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    # reboot без права — нет кнопки; без права `nodes` — только «Обновить»,
    # без «Службы»/«Назад» (тех экранов у этой подписки и так нет).
    assert codes == ["act:node:poweroff", "act:node:suspend", "st:sw_node:-"]


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
        "st:sw_svc:-:monitor",  # 🔄 Обновить
        "st:sw_svcs:-:0",  # ⬅️ Назад
    ]


def test_service_card_keyboard_carries_peer_node_id():
    kb = build_service_card_keyboard(
        _sub("restart@node"), _node_actions(), "monitor", "arch-t480"
    )
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == [
        "act:node:restart:monitor:arch-t480",
        "st:sw_svc:arch-t480:monitor",
        "st:sw_svcs:arch-t480:0",
    ]


def test_service_card_keyboard_filters_by_right_and_choices():
    kb = build_service_card_keyboard(_sub("restart@node"), _node_actions(), "monitor")
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert codes == ["act:node:restart:monitor", "st:sw_svc:-:monitor", "st:sw_svcs:-:0"]
    # Служба вне choices действия — кнопок действий нет, но «Обновить»/«Назад»
    # остаются (право на сам переход сюда уже проверено выше по стеку).
    no_action_kb = build_service_card_keyboard(_sub("restart@node"), _node_actions(), "apps")
    assert [b.callback_data for row in no_action_kb.inline_keyboard for b in row] == [
        "st:sw_svc:-:apps",
        "st:sw_svcs:-:0",
    ]
    assert build_service_card_keyboard(None, _node_actions(), "monitor") is None
