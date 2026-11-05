from __future__ import annotations

from types import SimpleNamespace

import pytest

from sa_home_bot.bot import ai_flow, ai_reply, group_context
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.config import Settings
from sa_home_bot.llm.prompt import ROUTE_OK
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.utils.clock import utc_now
from tests.unit.test_ai_flow import FakeMessage, FakeNodeLink
from tests.unit.test_ai_handler import FakeMessage as HandlerMessage
from tests.unit.test_ai_handler import FakeNotifier


def _book(*, recorded=True, rights=("chat_recap@llm",)):
    return SubscriptionBook(
        [
            Subscription(chat_id=999, name="admin", allowed_commands=frozenset({"*"})),
            Subscription(chat_id=-100, name="g", allowed_commands=frozenset(rights)),
        ],
        (-100,) if recorded else (),
    )


def _message():
    message = FakeMessage()
    message.chat = SimpleNamespace(id=-100, type="supergroup")
    return message


def _link():
    return FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )


async def _turn(store, book, link):
    message = _message()
    history = [{"role": "user", "content": "вопрос"}]
    return await ai_flow.request_alfred(
        message, link, store, Settings(), history, 7, book, SimpleNamespace(send=None)
    )


def _all_text(link, index=-1):
    return "\n".join(str(m.get("content")) for m in link.command_calls[index][1]["messages"])


async def _say(store, content="про кота"):
    await store.record_group_message(
        -100,
        1,
        created_at=utc_now(),
        user_id=1,
        user_name="Анна",
        content=content,
        reply_to=None,
        from_alfred=False,
        keep=1000,
    )


class TestGroupNote:
    async def test_with_right_and_recorded(self, store):
        await _say(store)
        book = _book(recorded=True, rights=("chat_recap@llm",))
        link = _link()
        await _turn(store, book, link)
        assert "про кота" in _all_text(link)
        assert len(link.command_calls) == 2
        assert all(c[0] == "chat" for c in link.command_calls)

    async def test_without_recorded(self, store):
        await _say(store)
        book = _book(recorded=False, rights=("chat_recap@llm",))
        link = _link()
        await _turn(store, book, link)
        assert "про кота" not in _all_text(link)

    async def test_without_right(self, store):
        await _say(store)
        book = _book(recorded=True, rights=("chat@llm",))
        link = _link()
        await _turn(store, book, link)
        assert "про кота" not in _all_text(link)

    async def test_build_note_failure(self, store, monkeypatch):
        await _say(store)
        book = _book(recorded=True, rights=("chat_recap@llm",))
        link = _link()

        async def failing_build_note(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(group_context, "build_note", failing_build_note)
        result = await _turn(store, book, link)
        assert result == "ответ"


class TestReadsGroup:
    @pytest.mark.parametrize(
        "subscription,expected",
        [
            (None, False),
            (
                Subscription(
                    chat_id=-100,
                    name="g",
                    allowed_commands=frozenset({"chat_recap@llm"}),
                    group_log=True,
                ),
                True,
            ),
            (
                Subscription(
                    chat_id=-100,
                    name="g",
                    allowed_commands=frozenset({"chat_recap@llm"}),
                    group_log=False,
                ),
                False,
            ),
            (
                Subscription(
                    chat_id=-100,
                    name="g",
                    allowed_commands=frozenset({"chat@llm"}),
                    group_log=True,
                ),
                False,
            ),
        ],
    )
    def test_reads_group(self, subscription, expected):
        assert group_context.reads_group(subscription) == expected


class TestAfterDelivery:
    async def test_with_right_schedules_summary_no_episode(self, store, monkeypatch):
        book = _book(recorded=True, rights=("chat_recap@llm",))
        message = HandlerMessage(-100, text="вопрос", chat_type="supergroup")
        await store.record_ai_turn(-100, message.message_id, 7, "user", "вопрос", utc_now())

        episode_called = []
        dismissal_called = []
        schedule_called = []

        async def fake_episode(node_link, chat_id, user_text, assistant_text):
            episode_called.append((chat_id, user_text, assistant_text))

        async def fake_dismissal(message, node_link, mode, book, notifier):
            dismissal_called.append(mode)

        def fake_schedule(node_link, store, config, chat_id, active_chats):
            schedule_called.append(chat_id)
            return True

        monkeypatch.setattr(ai_flow, "piggyback_dialogue_episode", fake_episode)
        monkeypatch.setattr(ai_flow, "perform_dismissal", fake_dismissal)
        monkeypatch.setattr(group_context.DAY_SUMMARY, "schedule", fake_schedule)

        services = SimpleNamespace(
            store=store,
            config=Settings(),
            node_link=None,
            book=book,
            notifier=FakeNotifier(),
            interactives=None,
            active_ai_chats=ai_flow.ActiveAiChats(),
        )
        box = ai_tools.DismissalBox()
        await ai_reply._after_delivery(
            message, services, 7, [], ai_flow.SpeechRemarkBox(), box, None, "ответ"
        )

        assert not episode_called
        assert not dismissal_called
        assert schedule_called == [-100]

    async def test_without_right_episode_no_schedule(self, store, monkeypatch):
        book = _book(recorded=True, rights=("chat@llm",))
        message = HandlerMessage(-100, text="вопрос", chat_type="supergroup")
        await store.record_ai_turn(-100, message.message_id, 7, "user", "вопрос", utc_now())

        episode_called = []
        schedule_called = []

        async def fake_episode(node_link, chat_id, user_text, assistant_text):
            episode_called.append((chat_id, user_text, assistant_text))

        def fake_schedule(node_link, store, config, chat_id, active_chats):
            schedule_called.append(chat_id)
            return True

        monkeypatch.setattr(ai_flow, "piggyback_dialogue_episode", fake_episode)
        monkeypatch.setattr(group_context.DAY_SUMMARY, "schedule", fake_schedule)

        services = SimpleNamespace(
            store=store,
            config=Settings(),
            node_link=None,
            book=book,
            notifier=FakeNotifier(),
            interactives=None,
            active_ai_chats=ai_flow.ActiveAiChats(),
        )
        box = ai_tools.DismissalBox()
        await ai_reply._after_delivery(
            message, services, 7, [], ai_flow.SpeechRemarkBox(), box, None, "ответ"
        )

        assert episode_called
        assert not schedule_called

    async def test_with_right_and_dismissal_off_no_schedule(self, store, monkeypatch):
        book = _book(recorded=True, rights=("chat_recap@llm",))
        message = HandlerMessage(-100, text="вопрос", chat_type="supergroup")
        await store.record_ai_turn(-100, message.message_id, 7, "user", "вопрос", utc_now())

        schedule_called = []
        dismissal_called = []

        def fake_schedule(node_link, store, config, chat_id, active_chats):
            schedule_called.append(chat_id)
            return True

        async def fake_dismissal(message, node_link, mode, book, notifier):
            dismissal_called.append(mode)

        monkeypatch.setattr(group_context.DAY_SUMMARY, "schedule", fake_schedule)
        monkeypatch.setattr(ai_flow, "perform_dismissal", fake_dismissal)

        services = SimpleNamespace(
            store=store,
            config=Settings(),
            node_link=None,
            book=book,
            notifier=FakeNotifier(),
            interactives=None,
            active_ai_chats=ai_flow.ActiveAiChats(),
        )
        box = ai_tools.DismissalBox(mode="off")
        await ai_reply._after_delivery(
            message, services, 7, [], ai_flow.SpeechRemarkBox(), box, None, "ответ"
        )

        assert not schedule_called
        assert dismissal_called == ["off"]
