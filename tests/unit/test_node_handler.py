from sa_home_bot.bot.handlers.node import (
    SELF_RESTART_CB_KEY,
    SELF_RESTART_TEXT,
    SELF_STOP_TEXT,
    on_dynamic_action,
)
from sa_home_bot.bot.node_view import NODE_DOWN_TEXT
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings, TelegramConfig
from sa_home_bot.proto.messages import ActionSpec, ServiceDescription, ServiceInfo
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from tests.unit.support.fakes import FakeCallback, FakeNodeLink

SELF_SHUTDOWN_ACTIONS = frozenset({"restart@node", "stop@node", "restart_node@node"})

CHECK_UPDATE_DESCRIPTION = ServiceDescription(
    info=ServiceInfo(node="alfred", service="node", version="0.24.5"),
    capabilities=(),
    actions=(ActionSpec(id="check_update", title="🔄 Проверить обновление"),),
)


def _dying_link() -> FakeNodeLink:
    return FakeNodeLink(error=ServiceUnavailableError("клиент закрыт"))


def _check_update_link(result: dict) -> FakeNodeLink:
    return FakeNodeLink(
        {"node": "alfred", "version": "0.24.5", "services": [], "peers": []},
        result=result,
        description=CHECK_UPDATE_DESCRIPTION,
        actions=list(CHECK_UPDATE_DESCRIPTION.actions),
    )


async def _dispatch(callback, store, link, allowed=SELF_SHUTDOWN_ACTIONS):
    await on_dynamic_action(
        callback,
        store=store,
        node_link=link,
        apps_link=link,
        notifier=None,
        config=Settings(telegram=TelegramConfig(token="x"), subscriptions=[]),
        pending_vpn_secrets=PendingVpnSecrets(),
        book=SubscriptionBook([]),
        subscription=Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed)),
    )


async def test_self_restart_says_goodbye_not_node_down(store):
    link = _dying_link()
    callback = FakeCallback("act:node:restart:telegram-bot")
    await _dispatch(callback, store, link)

    assert callback.message.answers == [SELF_RESTART_TEXT]
    assert NODE_DOWN_TEXT not in callback.message.answers
    assert callback.message.edits == []
    assert link.commands == [("restart", {"name": "telegram-bot"})]


async def test_self_stop_message_mentions_nodectl(store):
    callback = FakeCallback("act:node:stop:telegram-bot")
    await _dispatch(callback, store, _dying_link())
    assert callback.message.answers == [SELF_STOP_TEXT]
    assert "nodectl" in SELF_STOP_TEXT


async def test_restart_node_of_own_node_is_self_shutdown(store):
    link = _dying_link()
    callback = FakeCallback("act:node:restart_node")
    await _dispatch(callback, store, link)
    assert callback.message.answers == [SELF_RESTART_TEXT]
    assert link.commands == [("restart_node", {})]


async def test_replayed_callback_after_restart_is_ignored(store):
    link = _dying_link()
    first = FakeCallback("act:node:restart:telegram-bot", callback_id="same-id")
    await _dispatch(first, store, link)
    assert await store.get_state(SELF_RESTART_CB_KEY) == "same-id"

    replayed = FakeCallback("act:node:restart:telegram-bot", callback_id="same-id")
    await _dispatch(replayed, store, link)
    assert replayed.message.answers == []
    assert len(link.commands) == 1


async def test_peer_telegram_bot_restart_goes_normal_path(store):
    callback = FakeCallback("act:node:restart:telegram-bot:arch-t480")
    await _dispatch(callback, store, _dying_link())
    assert callback.message.answers != [SELF_RESTART_TEXT]


async def test_check_update_sends_message_when_update_available(store):
    link = _check_update_link(
        {"repo": "x", "running": "0.24.4", "installed": "0.24.4", "latest": "0.24.5"}
    )
    callback = FakeCallback("act:node:check_update")
    await _dispatch(callback, store, link, allowed={"check_update@node"})

    assert len(callback.message.answers) == 1
    assert "0.24.5" in callback.message.answers[0]
    assert callback.message.edits == []


async def test_check_update_sends_message_when_up_to_date(store):
    link = _check_update_link(
        {"repo": "x", "running": "0.24.5", "installed": "0.24.5", "latest": "0.24.5"}
    )
    callback = FakeCallback("act:node:check_update")
    await _dispatch(callback, store, link, allowed={"check_update@node"})

    assert len(callback.message.answers) == 1
    assert "Обновлений нет" in callback.message.answers[0]
