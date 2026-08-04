"""swarm_panel: рендер иерархии /swarm — чистые функции из готовых данных
(bot/handlers/swarm_panel.py делает сетевые вызовы, сюда не относится)."""

from datetime import UTC, datetime

from sa_home_bot.bot import commands, swarm_panel
from sa_home_bot.proto.messages import ActionParam, ActionSpec
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.wake_core import NodeReport


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _report(node_id: str, version: str, services: list[str], alive: bool = True) -> NodeReport:
    return NodeReport(
        node_id=node_id,
        alive=alive,
        state={"node": node_id, "version": version, "services": [{"name": s} for s in services]}
        if alive
        else None,
        kind="server",
    )


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


def _skill(app_id: str, title: str) -> ActionSpec:
    return ActionSpec(id=app_id, title=title)


# --- панель -----------------------------------------------------------------


def test_panel_shows_uniform_version():
    reports = [_report("alfred", "0.70.0", ["monitor"]), _report("mycraft", "0.70.0", ["llm"])]
    text, _ = swarm_panel.build_panel_view(reports, [], datetime.now(tz=UTC))
    assert "2 нод, в сети 2" in text
    assert "Версия: v0.70.0" in text
    assert "≤" not in text


def test_panel_shows_divergent_version_with_le():
    reports = [_report("alfred", "0.70.0", ["monitor"]), _report("mycraft", "0.69.0", ["llm"])]
    text, _ = swarm_panel.build_panel_view(reports, [], datetime.now(tz=UTC))
    assert "Версия: ≤v0.70.0" in text


def test_panel_counts_unique_services_across_nodes():
    reports = [
        _report("alfred", "0.70.0", ["monitor", "telegram-bot"]),
        _report("mycraft", "0.70.0", ["monitor", "llm"]),
    ]
    text, _ = swarm_panel.build_panel_view(reports, [], datetime.now(tz=UTC))
    # monitor встречается дважды, но должен посчитаться один раз.
    assert "Служб: 3 уникальных" in text


def test_panel_counts_skills_ignoring_parametrized_actions():
    app_actions = [
        _skill("jellyfin", "🎬 Jellyfin"),
        _skill("qbittorrent", "🧲 qBittorrent"),
        ActionSpec(id="start", title="▶️", params=(ActionParam(name="name"),)),
    ]
    text, _ = swarm_panel.build_panel_view([], app_actions, datetime.now(tz=UTC))
    assert "Умений: 2 уникальных" in text


def test_panel_offline_node_excluded_from_online_count():
    reports = [_report("alfred", "0.70.0", ["monitor"]), _report("winpc", "0", [], alive=False)]
    text, _ = swarm_panel.build_panel_view(reports, [], datetime.now(tz=UTC))
    assert "2 нод, в сети 1" in text


def test_panel_keyboard_has_nav_and_refresh():
    text, kb = swarm_panel.build_panel_view([], [], datetime.now(tz=UTC))
    callbacks = _callbacks(kb)
    assert f"st:{commands.SWARM_SKILLS_CODE}:0" in callbacks
    assert f"st:{commands.SWARM_NODES_CODE}:0" in callbacks
    assert f"st:{commands.SWARM_UPDATE_CODE}" in callbacks
    assert f"st:{commands.SWARM_PANEL_CODE}" in callbacks  # 🔄 Обновить


def test_panel_includes_wake_buttons_passed_in():
    from aiogram.types import InlineKeyboardButton

    wake_btn = InlineKeyboardButton(text="🔌 Разбудить winpc", callback_data="st:wake:winpc")
    _, kb = swarm_panel.build_panel_view([], [], datetime.now(tz=UTC), [wake_btn])
    assert "st:wake:winpc" in _callbacks(kb)


# --- список умений ------------------------------------------------------


def test_skills_list_filters_by_right():
    app_actions = [_skill("jellyfin", "🎬 Jellyfin"), _skill("qbittorrent", "🧲 qBittorrent")]
    text, kb = swarm_panel.build_skills_list_view(app_actions, _sub("jellyfin@apps"), 0)
    callbacks = _callbacks(kb)
    assert f"st:{commands.SWARM_SKILL_CODE}:jellyfin" in callbacks
    assert f"st:{commands.SWARM_SKILL_CODE}:qbittorrent" not in callbacks
    assert "1" in text


def test_skills_list_paginates():
    app_actions = [_skill(f"app{i}", f"App {i}") for i in range(8)]
    sub = _sub(*(f"app{i}@apps" for i in range(8)))
    text, kb = swarm_panel.build_skills_list_view(app_actions, sub, 0)
    callbacks = _callbacks(kb)
    assert f"st:{commands.SWARM_SKILLS_CODE}:{swarm_panel.SKILLS_PAGE_SIZE}" in callbacks


def test_skills_list_empty_when_no_rights():
    text, kb = swarm_panel.build_skills_list_view([_skill("jellyfin", "🎬")], _sub(), 0)
    assert "Нет доступных умений" in text
    assert _callbacks(kb) == [f"st:{commands.SWARM_PANEL_CODE}"]


def test_skill_card_view_appends_back_button():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="▶️", callback_data="act:apps:start:jellyfin")]]
    )
    text, out_kb = swarm_panel.build_skill_card_view("карточка", kb)
    callbacks = _callbacks(out_kb)
    assert "act:apps:start:jellyfin" in callbacks
    assert f"st:{commands.SWARM_SKILLS_CODE}:0" in callbacks
    assert text == "карточка"


def test_skill_card_view_handles_no_keyboard():
    text, out_kb = swarm_panel.build_skill_card_view("карточка", None)
    assert _callbacks(out_kb) == [f"st:{commands.SWARM_SKILLS_CODE}:0"]


# --- список нод -----------------------------------------------------------


def test_nodes_list_own_node_uses_dash_placeholder():
    reports = [_report("alfred", "0.70.0", ["monitor"])]
    _, kb = swarm_panel.build_nodes_list_view(reports, 0, [], own_node_id="alfred")
    callbacks = _callbacks(kb)
    assert f"st:{commands.SWARM_NODE_CODE}:-" in callbacks


def test_nodes_list_peer_carries_real_id():
    reports = [_report("mycraft", "0.70.0", ["llm"])]
    _, kb = swarm_panel.build_nodes_list_view(reports, 0, [], own_node_id="alfred")
    callbacks = _callbacks(kb)
    assert f"st:{commands.SWARM_NODE_CODE}:mycraft" in callbacks


def test_nodes_list_shows_wake_button_only_for_wakeable():
    reports = [
        _report("alfred", "0.70.0", ["monitor"]),
        _report("winpc", "0", [], alive=False),
    ]
    _, kb = swarm_panel.build_nodes_list_view(reports, 0, ["winpc"], own_node_id="alfred")
    callbacks = _callbacks(kb)
    assert "st:wake:winpc" in callbacks
    assert not any(c.startswith("st:wake:alfred") for c in callbacks)


def test_nodes_list_paginates_and_refreshes():
    reports = [_report(f"node{i}", "0.70.0", []) for i in range(7)]
    text, kb = swarm_panel.build_nodes_list_view(reports, 0, [], own_node_id="node0")
    callbacks = _callbacks(kb)
    assert f"st:{commands.SWARM_NODES_CODE}:{swarm_panel.NODES_PAGE_SIZE}" in callbacks
    assert f"st:{commands.SWARM_NODES_CODE}:0" in callbacks  # 🔄 Обновить (та же страница)
    assert "7" in text


# --- проверка обновлений -----------------------------------------------


def test_update_result_view_has_back_button():
    text, kb = swarm_panel.build_update_result_view("Последняя версия: v0.70.0.")
    assert "Последняя версия: v0.70.0." in text
    assert _callbacks(kb) == [f"st:{commands.SWARM_PANEL_CODE}"]
