from __future__ import annotations

from types import SimpleNamespace

from sa_home_bot.bot import ai_flow, ai_turns, group_context
from sa_home_bot.bot.handlers import ai as ai_handler
from sa_home_bot.config import Settings
from sa_home_bot.llm.prompt import ROUTE_OK
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.utils.clock import utc_now
from tests.unit.test_ai_flow import FakeMessage as FlowMessage
from tests.unit.test_ai_flow import FakeNodeLink
from tests.unit.test_ai_handler import FakeMessage, FakeNotifier


def _book(*, recorded=True, rights=("chat@llm", "chat_recap@llm")):
    return SubscriptionBook(
        [
            Subscription(chat_id=999, name="admin", allowed_commands=frozenset({"*"})),
            Subscription(chat_id=-100, name="g", allowed_commands=frozenset(rights)),
        ],
        (-100,) if recorded else (),
    )


def _message(message_id=1, text="/alfred", chat_type="supergroup"):
    message = FakeMessage(-100, text=text, chat_type=chat_type)
    message.message_id = message_id
    return message


def _link():
    return FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )


async def _say(store, content="про кота", message_id=1, from_alfred=False):
    await store.record_group_message(
        -100,
        message_id,
        created_at=utc_now(),
        user_id=1,
        user_name="Анна",
        content=content,
        reply_to=None,
        from_alfred=from_alfred,
        keep=1000,
    )


class TestBuildOpinionNote:
    async def test_no_messages(self, store):
        note = await group_context.build_opinion_note(store, -100, 100)
        assert note is None

    async def test_only_alfred_messages(self, store):
        await _say(store, "привет", message_id=1, from_alfred=True)
        await _say(store, "как дела", message_id=2, from_alfred=True)
        note = await group_context.build_opinion_note(store, -100, 3)
        assert note is None

    async def test_only_trigger_message(self, store):
        await _say(store, "триггер", message_id=5, from_alfred=False)
        note = await group_context.build_opinion_note(store, -100, 5)
        assert note is None

    async def test_human_message_before_trigger(self, store):
        await _say(store, "человек говорит", message_id=1, from_alfred=False)
        await _say(store, "альфред отвечает", message_id=2, from_alfred=True)
        note = await group_context.build_opinion_note(store, -100, 3)
        assert note is not None
        assert "человек говорит" in note
        assert "chat_recap" not in note

    async def test_twenty_messages_all_included(self, store):
        for i in range(20):
            await _say(store, f"сообщение {i}", message_id=i + 1, from_alfred=(i % 2 == 0))
        note = await group_context.build_opinion_note(store, -100, 21)
        assert note is not None
        for i in range(20):
            assert f"сообщение {i}" in note


class TestGroupOpinionNote:
    async def test_not_recorded(self, store):
        book = _book(recorded=False)
        message = _message()
        note = await ai_turns.group_opinion_note(store, book, message)
        assert note is None

    async def test_build_note_failure(self, store, monkeypatch):
        await _say(store, "человек", message_id=1, from_alfred=False)
        book = _book(recorded=True)
        message = _message(message_id=2)

        async def failing_build_note(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(group_context, "build_opinion_note", failing_build_note)
        note = await ai_turns.group_opinion_note(store, book, message)
        assert note is None


class TestHandlers:
    async def test_cmd_ai_empty_with_human_message(self, store, monkeypatch):
        await _say(store, "человек говорит", message_id=1, from_alfred=False)
        book = _book(recorded=True)
        message = _message(message_id=2, text="/alfred")
        seen = {}

        async def fake_request(*args, **kwargs):
            seen["history"] = args[4]
            seen["kwargs"] = kwargs
            return "ответ"

        monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
        await ai_handler.cmd_ai(
            message,
            node_link=None,
            store=store,
            config=Settings(),
            book=book,
            notifier=FakeNotifier(),
            active_ai_chats=ai_flow.ActiveAiChats(),
            tool_calls=ai_handler.ToolCalls(),
        )

        assert seen["history"] == [{"role": "user", "content": ai_turns.GROUP_OPINION_PROMPT}]
        assert "opinion_note" in seen["kwargs"]
        assert "человек говорит" in seen["kwargs"]["opinion_note"]

    async def test_on_group_mention_empty_with_human_message(self, store, monkeypatch):
        await _say(store, "человек говорит", message_id=1, from_alfred=False)
        book = _book(recorded=True)
        message = _message(message_id=2, text="@bot")
        seen = {}

        async def fake_request(
            message, node_link, store_, config, history, dialogue_id, book, notifier,
            dismissal=None, tool_calls=None, speech_remark=None, rich_session=None, **extras
        ):
            seen["history"] = history
            seen["kwargs"] = extras
            return "ответ"

        monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
        await ai_handler.on_group_mention(
            message,
            mention_prompt="",
            node_link=None,
            store=store,
            config=Settings(),
            book=book,
            notifier=FakeNotifier(),
            active_ai_chats=ai_flow.ActiveAiChats(),
            tool_calls=ai_handler.ToolCalls(),
            subscription=book.for_chat(-100),
        )

        assert seen["history"] == [{"role": "user", "content": ai_turns.GROUP_OPINION_PROMPT}]
        assert "opinion_note" in seen["kwargs"]
        assert "человек говорит" in seen["kwargs"]["opinion_note"]

    async def test_cmd_ai_empty_no_messages(self, store, monkeypatch):
        book = _book(recorded=True)
        message = _message(message_id=1, text="/alfred")
        seen = {}

        async def fake_request(*args, **kwargs):
            seen["history"] = args[4]
            seen["kwargs"] = kwargs
            return "ответ"

        monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
        await ai_handler.cmd_ai(
            message,
            node_link=None,
            store=store,
            config=Settings(),
            book=book,
            notifier=FakeNotifier(),
            active_ai_chats=ai_flow.ActiveAiChats(),
            tool_calls=ai_handler.ToolCalls(),
        )

        assert seen["history"] == [{"role": "user", "content": ai_turns.OPENING_PROMPT}]
        assert "opinion_note" not in seen["kwargs"]

    async def test_cmd_ai_empty_not_recorded(self, store, monkeypatch):
        book = _book(recorded=False)
        message = _message(message_id=1, text="/alfred")
        seen = {}

        async def fake_request(*args, **kwargs):
            seen["history"] = args[4]
            seen["kwargs"] = kwargs
            return "ответ"

        monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
        await ai_handler.cmd_ai(
            message,
            node_link=None,
            store=store,
            config=Settings(),
            book=book,
            notifier=FakeNotifier(),
            active_ai_chats=ai_flow.ActiveAiChats(),
            tool_calls=ai_handler.ToolCalls(),
        )

        assert seen["history"] == [{"role": "user", "content": ai_turns.OPENING_PROMPT}]
        assert "opinion_note" not in seen["kwargs"]

    async def test_greeting_portrait_not_called_for_opinion(self, store, monkeypatch):
        await _say(store, "человек говорит", message_id=1, from_alfred=False)
        book = _book(recorded=True)
        message = _message(message_id=2, text="/alfred")
        portrait_called = []

        def fake_portrait(msg, interactives):
            portrait_called.append(True)

        monkeypatch.setattr(ai_handler, "_greeting_portrait", fake_portrait)

        async def fake_request(*args, **kwargs):
            return "ответ"

        monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
        await ai_handler.cmd_ai(
            message,
            node_link=None,
            store=store,
            config=Settings(),
            book=book,
            notifier=FakeNotifier(),
            active_ai_chats=ai_flow.ActiveAiChats(),
            tool_calls=ai_handler.ToolCalls(),
        )

        assert not portrait_called

    async def test_greeting_portrait_called_for_greeting(self, store, monkeypatch):
        book = _book(recorded=True)
        message = _message(message_id=1, text="/alfred")
        portrait_called = []

        def fake_portrait(msg, interactives):
            portrait_called.append(True)

        monkeypatch.setattr(ai_handler, "_greeting_portrait", fake_portrait)

        async def fake_request(*args, **kwargs):
            return "ответ"

        monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
        await ai_handler.cmd_ai(
            message,
            node_link=None,
            store=store,
            config=Settings(),
            book=book,
            notifier=FakeNotifier(),
            active_ai_chats=ai_flow.ActiveAiChats(),
            tool_calls=ai_handler.ToolCalls(),
        )

        assert portrait_called

    async def test_no_directive_in_db(self, store, monkeypatch):
        await _say(store, "человек говорит", message_id=1, from_alfred=False)
        book = _book(recorded=True)
        message = _message(message_id=2, text="/alfred")

        async def fake_request(*args, **kwargs):
            return "ответ"

        monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
        await ai_handler.cmd_ai(
            message,
            node_link=None,
            store=store,
            config=Settings(),
            book=book,
            notifier=FakeNotifier(),
            active_ai_chats=ai_flow.ActiveAiChats(),
            tool_calls=ai_handler.ToolCalls(),
        )

        rows = await store.ai_turns_for_dialogue(-100, message.message_id)
        for row in rows:
            assert ai_turns.GROUP_OPINION_PROMPT not in row.get("content", "")

    async def test_away_intercepts(self, store, monkeypatch):
        await _say(store, "человек говорит", message_id=1, from_alfred=False)
        book = _book(recorded=True)
        message = _message(message_id=2, text="/alfred")
        request_called = []
        opinion_called = []

        async def fake_request(*args, **kwargs):
            request_called.append(True)
            return "ответ"

        async def fake_opinion_note(*args, **kwargs):
            opinion_called.append(True)
            return "заметка"

        monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
        monkeypatch.setattr(ai_turns, "group_opinion_note", fake_opinion_note)

        async def intercept(*a, **kw):
            return True

        away = SimpleNamespace(intercept=intercept)

        await ai_handler.cmd_ai(
            message,
            node_link=None,
            store=store,
            config=Settings(),
            book=book,
            notifier=FakeNotifier(),
            active_ai_chats=ai_flow.ActiveAiChats(),
            tool_calls=ai_handler.ToolCalls(),
            away=away,
        )

        assert not request_called
        assert not opinion_called


class TestFlow:
    async def test_opinion_note_passed_to_llm(self, store, monkeypatch):
        await _say(store, "человек говорит", message_id=1, from_alfred=False)
        book = _book(recorded=True)
        message = FlowMessage()
        message.chat = SimpleNamespace(id=-100, type="supergroup")
        link = _link()

        await ai_flow.request_alfred(
            message,
            link,
            store,
            Settings(),
            [{"role": "user", "content": "тест"}],
            7,
            book,
            SimpleNamespace(send=None),
            opinion_note="ЗАМЕТКА МНЕНИЯ",
        )

        last_call_messages = link.command_calls[-1][1]["messages"]
        contents = "\n".join(str(m.get("content", "")) for m in last_call_messages)
        assert "ЗАМЕТКА МНЕНИЯ" in contents

    async def test_tools_declarations_empty(self, store, monkeypatch):
        await _say(store, "человек говорит", message_id=1, from_alfred=False)
        book = _book(recorded=True)
        message = FlowMessage()
        message.chat = SimpleNamespace(id=-100, type="supergroup")
        link = _link()

        await ai_flow.request_alfred(
            message,
            link,
            store,
            Settings(),
            [{"role": "user", "content": "тест"}],
            7,
            book,
            SimpleNamespace(send=None),
            opinion_note="ЗАМЕТКА МНЕНИЯ",
        )

        for call in link.command_calls:
            args = call[1]
            tools = args.get("tools")
            if tools is not None:
                assert tools == []
