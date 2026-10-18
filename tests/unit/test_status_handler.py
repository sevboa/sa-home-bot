import pytest

from sa_home_bot.bot.handlers.status import on_status_action
from sa_home_bot.bot.status_view import MONITOR_DOWN_TEXT
from sa_home_bot.config import Settings, TelegramConfig
from sa_home_bot.subscriptions.models import Subscription
from tests.unit.support.fakes import FakeCallback
from tests.unit.swarm_support import DeadNodeLink


async def _dispatch(data: str, link=None) -> FakeCallback:
    callback = FakeCallback(data)
    await on_status_action(
        callback,
        node_link=link or DeadNodeLink(),
        config=Settings(telegram=TelegramConfig(token="x"), subscriptions=[]),
        subscription=Subscription(chat_id=1, name="me", allowed_commands=frozenset({"*"})),
    )
    return callback


@pytest.mark.parametrize("data", ["st:full", "st:full:arch-t480", "st:stats", "st:downtime"])
async def test_monitor_views_answer_with_new_message(data):
    callback = await _dispatch(data)
    assert callback.message.answers == [MONITOR_DOWN_TEXT]
    assert callback.message.edits == []
    assert len(callback.answered) == 1


async def test_downtime_page_button_edits_same_message():
    callback = await _dispatch("st:downtime_page:20:arch-t480")
    assert callback.message.edits == [MONITOR_DOWN_TEXT]
    assert callback.message.answers == []


async def test_node_card_of_dead_node_says_it_is_down():
    callback = await _dispatch("st:nodecard:arch-t480")
    assert "arch-t480" in callback.message.answers[0]


async def test_unknown_callback_is_just_acknowledged():
    callback = await _dispatch("st:nonsense")
    assert callback.message.answers == []
    assert len(callback.answered) == 1
