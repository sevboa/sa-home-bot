"""Обработчики новой иерархии /swarm (bot/handlers/swarm_panel.py): панель →
умения/ноды → карточки, всё редрайвом одного сообщения (edit_text), по
образцу test_self_restart.py / test_check_update_handler.py."""

import pytest_asyncio

from sa_home_bot.bot import commands
from sa_home_bot.bot.handlers.swarm_panel import cmd_swarm, on_swarm_screen
from sa_home_bot.config import Settings, TelegramConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ActionSpec, ServiceDescription, ServiceInfo
from sa_home_bot.subscriptions.models import Subscription


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.edit_keyboards: list = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append(text)

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append(text)
        self.edit_keyboards.append(reply_markup)


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.id = "cb-1"
        self.message = FakeMessage()
        self.answered: list = []

    async def answer(self, *args, **kwargs):
        self.answered.append(args)


OWN_STATE = {
    "node": "alfred",
    "version": "0.70.0",
    "services": [
        {"name": "monitor", "status": "running", "pid": 1, "restarts": 0},
        {"name": "telegram-bot", "status": "running", "pid": 2, "restarts": 0},
    ],
    "peers": [{"id": "mycraft", "endpoint": "tcp://x:8710", "alive": True, "kind": "server"}],
}

PEER_STATE = {
    "node": "mycraft",
    "version": "0.70.0",
    "services": [{"name": "llm", "status": "running", "pid": 3, "restarts": 0}],
}


class FakeNodeLink:
    """Маршрутизация по dst, как FakeNodeLink в test_swarm_view.py — плюс
    describe/command, нужные карточкам и check_updates_summary. Адрес с
    node=None (own-монитор в node_view.py) нормализуется в реальный id own
    ноды — так его видит и wake_core.collect_reports (тот всегда шлёт
    настоящий id, даже для своей ноды)."""

    display_name = "нода"
    connected = True

    def __init__(self, own=None, routes=None, check_update=None):
        self._own = own or OWN_STATE
        self._routes = routes or {}
        self._check_update = check_update or {"latest": "0.70.0"}
        self.commands: list[tuple[str, dict]] = []

    def _key(self, dst) -> str:
        if dst is None:
            return "own"
        node = dst.node if dst.node is not None else self._own.get("node", "?")
        return f"{node}:{dst.service}"

    async def get_state(self, dst=None):
        key = self._key(dst)
        if key == "own":
            return self._own
        if key in self._routes:
            return self._routes[key]
        from sa_home_bot.bot.service_link import ServiceUnavailableError

        raise ServiceUnavailableError("нет связи")

    async def describe(self, dst=None):
        return ServiceDescription(
            info=ServiceInfo(node="alfred", service="node", version="0.70.0"),
            capabilities=(),
            actions=(),
        )

    async def actions(self):
        return ()

    async def command(self, action, args=None, dst=None, timeout=None):
        self.commands.append((action, args or {}))
        if action == "check_update":
            return self._check_update
        return {}


class FakeAppsLink:
    display_name = "apps"
    connected = True

    def __init__(self, app_actions=()):
        self._actions = tuple(app_actions)
        self.commands: list[tuple[str, dict]] = []

    async def actions(self):
        return self._actions

    async def describe(self, dst=None):
        return ServiceDescription(
            info=ServiceInfo(node="alfred", service="apps", version="0.70.0"),
            capabilities=(),
            actions=self._actions,
        )

    async def command(self, action, args=None, dst=None, timeout=None):
        self.commands.append((action, args or {}))
        title = next((a.title for a in self._actions if a.id == action), action)
        return {
            "id": action,
            "title": title,
            "status": "active",
            "unit": f"{action}.service",
            "urls": [],
        }


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _settings() -> Settings:
    return Settings(telegram=TelegramConfig(token="x"), subscriptions=[])


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


def _codes(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row] if kb else []


async def _screen(callback, store, node_link=None, apps_link=None, subscription=None):
    await on_swarm_screen(
        callback,
        node_link=node_link or FakeNodeLink(),
        apps_link=apps_link or FakeAppsLink(),
        store=store,
        config=_settings(),
        subscription=subscription if subscription is not None else _sub("nodes"),
    )


# --- /swarm ------------------------------------------------------------


async def test_cmd_swarm_renders_panel_in_one_message(store):
    message = FakeMessage()
    await cmd_swarm(
        message,
        node_link=FakeNodeLink(),
        apps_link=FakeAppsLink(),
        store=store,
        config=_settings(),
        subscription=_sub("nodes"),
    )
    assert len(message.answers) == 1
    assert "2 нод, в сети 1" in message.answers[0]  # own отвечает, mycraft — нет маршрута


async def test_cmd_swarm_reports_node_down(store):
    class DeadLink(FakeNodeLink):
        async def get_state(self, dst=None):
            from sa_home_bot.bot.service_link import ServiceUnavailableError

            raise ServiceUnavailableError("нет связи")

    message = FakeMessage()
    await cmd_swarm(
        message,
        node_link=DeadLink(),
        apps_link=FakeAppsLink(),
        store=store,
        config=_settings(),
        subscription=_sub("nodes"),
    )
    from sa_home_bot.bot.node_view import NODE_DOWN_TEXT

    assert message.answers == [NODE_DOWN_TEXT]


# --- sw_panel / sw_nodes -------------------------------------------------


async def test_sw_panel_redraws_in_place(store):
    callback = FakeCallback(f"st:{commands.SWARM_PANEL_CODE}")
    await _screen(callback, store)
    assert len(callback.message.edits) == 1
    assert callback.message.answers == []


async def test_sw_nodes_lists_own_with_dash_and_peer_with_real_id(store):
    callback = FakeCallback(f"st:{commands.SWARM_NODES_CODE}:0")
    await _screen(callback, store)
    codes = _codes(callback.message.edit_keyboards[0])
    assert f"st:{commands.SWARM_NODE_CODE}:-" in codes
    assert f"st:{commands.SWARM_NODE_CODE}:mycraft" in codes


# --- sw_node -------------------------------------------------------------


async def test_sw_node_own_card(store):
    node_link = FakeNodeLink(routes={"alfred:monitor": {"health": [], "disks": []}})
    callback = FakeCallback(f"st:{commands.SWARM_NODE_CODE}:-")
    await _screen(callback, store, node_link=node_link)
    assert "Нода alfred" in callback.message.edits[0]


async def test_sw_node_peer_card(store):
    node_link = FakeNodeLink(
        routes={
            "mycraft:node": PEER_STATE,
            "mycraft:monitor": {"health": [], "disks": []},
        }
    )
    callback = FakeCallback(f"st:{commands.SWARM_NODE_CODE}:mycraft")
    await _screen(callback, store, node_link=node_link)
    assert "Нода mycraft" in callback.message.edits[0]


# --- sw_svcs / sw_svc ------------------------------------------------------


async def test_sw_svcs_then_svc_own_node(store):
    node_link = FakeNodeLink()
    callback = FakeCallback(f"st:{commands.SWARM_SVCS_CODE}:-:0")
    await _screen(callback, store, node_link=node_link)
    codes = _codes(callback.message.edit_keyboards[0])
    assert f"st:{commands.SWARM_SVC_CODE}:-:monitor" in codes

    callback2 = FakeCallback(f"st:{commands.SWARM_SVC_CODE}:-:monitor")
    await _screen(callback2, store, node_link=node_link)
    assert "Служба monitor" in callback2.message.edits[0]
    back_codes = _codes(callback2.message.edit_keyboards[0])
    assert f"st:{commands.SWARM_SVCS_CODE}:-:0" in back_codes


# --- sw_skills / sw_skill --------------------------------------------------


async def test_sw_skills_then_skill_card(store):
    app_actions = (ActionSpec(id="jellyfin", title="🎬 Jellyfin"),)
    apps_link = FakeAppsLink(app_actions)
    sub = _sub("nodes", "jellyfin@apps")

    callback = FakeCallback(f"st:{commands.SWARM_SKILLS_CODE}:0")
    await _screen(callback, store, apps_link=apps_link, subscription=sub)
    codes = _codes(callback.message.edit_keyboards[0])
    assert f"st:{commands.SWARM_SKILL_CODE}:jellyfin" in codes

    callback2 = FakeCallback(f"st:{commands.SWARM_SKILL_CODE}:jellyfin")
    await _screen(callback2, store, apps_link=apps_link, subscription=sub)
    assert "Jellyfin" in callback2.message.edits[0]
    back_codes = _codes(callback2.message.edit_keyboards[0])
    assert f"st:{commands.SWARM_SKILLS_CODE}:0" in back_codes


async def test_sw_skill_denied_without_app_right(store):
    app_actions = (ActionSpec(id="jellyfin", title="🎬 Jellyfin"),)
    apps_link = FakeAppsLink(app_actions)
    callback = FakeCallback(f"st:{commands.SWARM_SKILL_CODE}:jellyfin")
    await _screen(callback, store, apps_link=apps_link, subscription=_sub("nodes"))
    assert callback.message.edits == []
    assert callback.answered[0][0] == "⛔️ Недоступно"


# --- sw_update ------------------------------------------------------------


async def test_sw_update_answers_toast_then_edits_with_result(store):
    node_link = FakeNodeLink(check_update={"latest": "0.70.0"})
    callback = FakeCallback(f"st:{commands.SWARM_UPDATE_CODE}")
    await _screen(callback, store, node_link=node_link)
    assert callback.answered[0][0] == "Проверяю обновления…"
    assert "0.70.0" in callback.message.edits[0]
