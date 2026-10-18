from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sa_home_bot import llm_chat
from sa_home_bot.bot import ai_flow, wake_state
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.llm.prompt import ROUTE_OK, THINK_MARKER
from sa_home_bot.proto.messages import ERR_INTERNAL, ERR_UNAVAILABLE, ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from tests.unit.support.fakes import FakeNotifier

from .ai_helpers import FakeRichSession, user
from .people_helpers import claim

MYCRAFT_WAKE = {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.0.50", "broadcast": "192.168.0.255"}
ALFRED_WAKE = {"mac": "7c:83:34:b4:59:ac", "ip": "192.168.0.100", "broadcast": "192.168.0.255"}

OWN_STATE = {
    "node": "alfred",
    "version": "0.27.0",
    "services": [],
    "wake": ALFRED_WAKE,
    "peers": [{"id": "mycraft", "endpoint": "tcp://y:8710", "alive": False}],
}


class FakeBot:
    def __init__(self) -> None:
        self.typing_chats: list[int] = []
        self.typing_threads: list[int | None] = []

    async def send_chat_action(self, chat_id, action, message_thread_id=None):
        self.typing_chats.append(chat_id)
        self.typing_threads.append(message_thread_id)


class FakeMessage:
    chat = SimpleNamespace(id=1, type="private")
    message_id = 42
    message_thread_id = None
    from_user = None
    reply_to_message = None
    quote = None
    text = "привет"

    def __init__(self) -> None:
        self.answers: list[str] = []
        self.bot = FakeBot()

    async def answer(self, text, **kwargs):
        self.answers.append(text)


def _notifier():
    return FakeNotifier(send_direct_result=1)


def _admin_book() -> SubscriptionBook:
    return SubscriptionBook(
        [
            Subscription(chat_id=999, name="admin", allowed_commands=frozenset({"*"})),
            Subscription(
                chat_id=1,
                name="speaker",
                allowed_commands=frozenset({"*@node", "*@monitor", "*@torrents", "*@net"}),
            ),
        ]
    )


class FakeNodeLink:
    display_name = "нода"

    def __init__(
        self,
        own=None,
        chat_results=(),
        get_state_routes=None,
        get_state_sequences=None,
        wol_sent=None,
        memory_facts=(),
        graph_facts=(),
        warmup_result=None,
        model_profile=None,
        person_cards=None,
    ):
        self._model_profile = model_profile
        self.person_cards = dict(person_cards or {})
        self.person_cards_calls: list[dict] = []
        self.describe_calls: list[str | None] = []
        self.memory_facts = list(memory_facts)
        self.graph_facts = list(graph_facts)
        self.recall_calls: list[dict] = []
        self.graph_recall_calls: list[dict] = []
        self._own = own or OWN_STATE
        self._chat_results = list(chat_results)
        self._get_state_routes = get_state_routes or {}
        self._get_state_sequences = {k: list(v) for k, v in (get_state_sequences or {}).items()}
        self.wol_sent = wol_sent if wol_sent is not None else []
        self.command_calls: list[tuple[str, dict, str | None]] = []
        self.get_state_calls: list[str] = []
        self._warmup_result = warmup_result

    async def describe(self, dst=None):
        self.describe_calls.append(dst.node if dst is not None else None)
        if self._model_profile is None:
            return None
        return SimpleNamespace(model_profile=self._model_profile)

    async def get_state(self, dst=None):
        key = f"{dst.node}:{dst.service}" if dst is not None else "own"
        self.get_state_calls.append(key)
        if key == "own":
            return self._own
        sequence = self._get_state_sequences.get(key)
        if sequence:
            result = sequence.pop(0)
            if result is None:
                raise ServiceUnavailableError("нет связи")
            if isinstance(result, Exception):
                raise result
            return result
        if key in self._get_state_routes:
            result = self._get_state_routes[key]
            if isinstance(result, Exception):
                raise result
            return result
        raise ServiceUnavailableError("нет связи")

    async def command(self, action, args=None, dst=None, timeout=None):
        if action == "recall":
            self.recall_calls.append(args)
            return {"facts": list(self.memory_facts), "count": len(self.memory_facts)}
        if action == "search" and dst is not None and dst.service == "graph_memory":
            self.graph_recall_calls.append(args)
            return {"facts": list(self.graph_facts), "count": len(self.graph_facts)}
        if action == "person_cards":
            self.person_cards_calls.append(args)
            return {"cards": dict(self.person_cards)}
        self.command_calls.append((action, args, dst.node if dst else None))
        if action == "send_wol":
            self.wol_sent.append(args)
            return {"sent": True}
        if action == "search":
            return {
                "query": args.get("query", ""),
                "results": [{"title": "T", "url": "u"}],
                "count": 1,
            }
        if action == "warmup":
            if isinstance(self._warmup_result, Exception):
                raise self._warmup_result
            return self._warmup_result or {"asleep": False}
        assert action == "chat"
        result = self._chat_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _settings() -> Settings:
    return Settings(llm=LlmConfig(request_timeout_s=5.0))


def _single_call_settings() -> Settings:
    return Settings(llm=LlmConfig(request_timeout_s=5.0, mode="single_call"))


async def _ask(
    message,
    link,
    store,
    text="привет",
    *,
    history=None,
    settings=None,
    dialogue_id=1,
    book=None,
    notifier=None,
    **extras,
):
    return await ai_flow.request_alfred(
        message,
        link,
        store,
        settings or _settings(),
        history or [{"role": "user", "content": text}],
        dialogue_id,
        book or _admin_book(),
        notifier or _notifier(),
        **extras,
    )


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    ai_flow._profile_cache.clear()
    yield
    ai_flow._profile_cache.clear()


async def test_fast_path_no_narrative_when_node_already_up(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "Добгый день, сэ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await _ask(message, link, store)

    assert raw == "Добгый день, сэ"
    assert message.answers == []
    assert len(link.command_calls) == 2
    action, router_args, node = link.command_calls[0]
    assert (action, node) == ("chat", "mycraft")
    assert router_args["chat_id"] == 1
    assert router_args["reason"] == "off"
    assert router_args["tools"] == [
        d
        for d in ai_flow.ai_tools.tools_for(_admin_book().for_chat(1)).declarations
        if d["function"]["name"] not in ai_flow.ai_tools.CANARY_TOOLS
    ]
    assert router_args["role"] == "router"
    persona_args = link.command_calls[1][1]
    assert "role" not in persona_args
    assert persona_args["reason"] == "off"
    assert router_args["messages"] == persona_args["messages"]
    assert router_args["messages"][-1] == {"role": "user", "content": "привет"}
    assert len(router_args["messages"]) == 2
    assert router_args["messages"][0]["role"] == "system"
    assert "Точное время сейчас" in router_args["messages"][0]["content"]


async def test_speech_remark_is_captured_not_sent_inline(store):
    message = FakeMessage()
    remark = "🗣 <i>Логопед:</i> не «сэг», а «сэр»!"
    link = FakeNodeLink(
        chat_results=[{"response": "Добгый день, сэг", "speech_remark": remark}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )
    box = ai_flow.SpeechRemarkBox()

    raw = await ai_flow.request_alfred(
        message,
        link,
        store,
        _single_call_settings(),
        [{"role": "user", "content": "привет"}],
        1,
        _admin_book(),
        _notifier(),
        speech_remark=box,
    )

    assert raw == "Добгый день, сэг"
    assert box.text == remark
    assert message.answers == []


async def test_tool_call_round_trip_reaches_final_response(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [{"function": {"name": "calc", "arguments": {"expression": "2 + 2"}}}]},
            {"response": ROUTE_OK},
            {"response": "Отвечу: 4"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await _ask(message, link, store, "сколько 2+2")

    assert raw == "Отвечу: 4"
    assert len(link.command_calls) == 3
    router_round2_messages = link.command_calls[1][1]["messages"]
    assert router_round2_messages[-2]["role"] == "assistant"
    assert router_round2_messages[-1] == {"role": "tool", "content": "4", "name": "calc"}
    assert link.command_calls[2][1]["messages"] == router_round2_messages
    assert "role" not in link.command_calls[2][1]


async def test_photo_tool_ends_the_turn_without_a_reply(store):
    from sa_home_bot.bot.interactives import cabinet

    class Photos:
        async def tool_take_photo(self, chat_id, user_id, args, **_kw):
            return cabinet.TOOL_PHOTO_STARTED

    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"tool_calls": [{"function": {"name": "take_photo", "arguments": {}}}]}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message,
        link,
        store,
        _settings(),
        [{"role": "user", "content": "скинь фото"}],
        1,
        _admin_book(),
        _notifier(),
        interactives=Photos(),
    )

    assert raw == ai_flow.SILENT_REPLY
    assert len(link.command_calls) == 1


async def test_tool_call_is_recorded_in_store(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [{"function": {"name": "calc", "arguments": {"expression": "2 + 2"}}}]},
            {"response": ROUTE_OK},
            {"response": "Отвечу: 4"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await _ask(message, link, store, "сколько 2+2")

    calls = await store.tool_calls_for_dialogue(message.chat.id, 1)
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "calc"
    assert calls[0]["result"] == "4"
    assert calls[0]["trigger_message_id"] == message.message_id


async def test_unknown_tool_name_reported_back_to_model(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [{"function": {"name": "no_such_tool", "arguments": {}}}]},
            {"response": ROUTE_OK},
            {"response": "ладно"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await _ask(message, link, store, "...")

    assert raw == "ладно"
    tool_msg = link.command_calls[1][1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "неизвестный инструмент" in tool_msg["content"]


async def test_tool_call_round_limit_squeezes_answer_without_tools(store):
    tool_calls = [{"function": {"name": "calc", "arguments": {"expression": "1+1"}}}]
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            *[{"tool_calls": tool_calls}] * llm_chat.MAX_TOOL_ROUNDS,
            {"response": ROUTE_OK},
            {"response": "Двá, сэг"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await _ask(message, link, store, "...")

    assert raw == "Двá, сэг"
    assert ai_flow.ALBERT_HICCUP not in message.answers
    squeeze_args = link.command_calls[llm_chat.MAX_TOOL_ROUNDS][1]
    assert squeeze_args["tools"] == []


async def test_empty_answer_after_round_limit_is_still_a_hiccup(store):
    tool_calls = [{"function": {"name": "calc", "arguments": {"expression": "1+1"}}}]
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            *[{"tool_calls": tool_calls}] * llm_chat.MAX_TOOL_ROUNDS,
            {"response": ""},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await _ask(message, link, store, "...")

    assert raw is None
    assert message.answers == [ai_flow.ALBERT_HICCUP]


def test_think_marker_survives_llm_service_transforms(tmp_path):
    from sa_home_bot.config import LlmConfig
    from sa_home_bot.llm.prompt import strip_math_notation
    from sa_home_bot.llm.speech_therapy import SpeechTherapist

    cfg = LlmConfig(speech_therapy_state_path=str(tmp_path / "speech-therapy.json"))
    therapist = SpeechTherapist(cfg, rand=lambda: 0.0)
    transformed, _, _ = therapist.process(strip_math_notation(THINK_MARKER), chat_id=None)
    assert transformed == THINK_MARKER


async def test_router_level_maps_to_reason(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"response": "THINK:2"},
            {"response": "Ответ"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await _ask(message, link, store, "задачка")

    assert link.command_calls[0][1]["reason"] == "off"
    assert link.command_calls[1][1]["reason"] == "medium"
    assert "think" not in link.command_calls[1][1]
    assert message.answers == [ai_flow.THINKING_TEXT]


async def test_scene_turn_does_not_think(store):
    from types import SimpleNamespace

    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"response": "THINK:2"},
            {"response": "Ответ"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )
    turn = SimpleNamespace(scene=True, note=None, user_id=1, speech_clear=False)

    await _ask(message, link, store, "щёлкаю тумблер", interactive_turn=turn)

    assert link.command_calls[1][1]["reason"] == "off"
    assert message.answers == []


async def test_router_uses_describe_profile_when_mode_not_pinned(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": "Ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
        model_profile={"name": "solo", "router": False, "thinking_hidden": False, "num_ctx": 8192},
    )

    await _ask(message, link, store)

    assert len(link.command_calls) == 1
    assert "role" not in link.command_calls[0][1]


async def test_unavailable_wake_sent_but_still_unreachable_after_30s(store, monkeypatch):
    await wake_state.remember(store, "mycraft", MYCRAFT_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_TIMEOUT_S", 0.05)
    message = FakeMessage()
    link = FakeNodeLink(get_state_routes={})

    raw = await _ask(message, link, store)

    assert raw is None
    assert message.answers == [ai_flow.STEPS_TEXT, ai_flow.ALBERT_UNAVAILABLE]
    assert link.wol_sent == [{"mac": MYCRAFT_WAKE["mac"]}]


async def test_ask_discovers_unavailable_despite_fresh_presence_reports_immediately(
    store, monkeypatch
):
    monkeypatch.setattr(llm_chat, "CHAT_RETRY_DELAYS_S", (0.0, 0.0))
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[ProtoError(ERR_UNAVAILABLE, "нода недоступна")] * 3,
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await _ask(message, link, store)

    assert raw is None
    assert message.answers == [ai_flow.ALBERT_UNAVAILABLE]
    assert link.wol_sent == []


async def test_wake_succeeds_but_warmup_fails_after_wake(store, monkeypatch):
    await wake_state.remember(store, "mycraft", MYCRAFT_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    message = FakeMessage()
    notifier = _notifier()
    link = FakeNodeLink(
        get_state_sequences={"mycraft:llm": [None, {"asleep": True}]},
        warmup_result=ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева"),
    )

    raw = await _ask(message, link, store, notifier=notifier)

    assert raw is None
    assert message.answers == [
        ai_flow.STEPS_TEXT,
        ai_flow.ARNOLD_WAKING,
        ai_flow.STEPS_TEXT,
        ai_flow.ALBERT_ASLEEP,
    ]
    assert link.command_calls[-1] == ("warmup", {}, "mycraft")
    assert len(notifier.sent) == 1


async def test_node_alive_but_service_down_skips_wake_reports_crashed(store):
    message = FakeMessage()
    notifier = _notifier()
    link = FakeNodeLink(get_state_routes={"mycraft:node": {"kind": "workstation"}})

    raw = await _ask(message, link, store, notifier=notifier)

    assert raw is None
    assert message.answers == [ai_flow.ALBERT_UNAVAILABLE]
    assert link.wol_sent == []
    assert link.command_calls == []
    assert len(notifier.sent) == 1
    admin_chat_id, admin_text = notifier.sent[0]
    assert admin_chat_id == 999
    assert "WoL" in admin_text


async def test_context_note_inserted_right_before_current_turn(store):
    message = FakeMessage()
    message.from_user = user("Иван", username="ivan", id=10)
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )
    history = [
        {"role": "user", "content": "первый вопрос"},
        {"role": "assistant", "content": "первый ответ"},
        {"role": "user", "content": "текущий вопрос"},
    ]

    await _ask(message, link, store, history=history, dialogue_id=500)

    sent_messages = link.command_calls[0][1]["messages"]
    assert sent_messages[-1] == {"role": "user", "content": "текущий вопрос"}
    assert sent_messages[-2]["role"] == "system"
    assert sent_messages[:-2] == history[:-1]


async def test_person_cards_reach_the_context_note(store):
    now = datetime.now(tz=UTC)
    await store.add_confirmed_relationship(10, 999, "acquaintance", now, now)
    await store.add_person_claims(
        [
            claim(10, "gender", "m"),
            claim(10, "alias", "Лили", by_id=999),
            claim(10, "timezone", "Europe/Moscow"),
            claim(999, "gender", "f", by_id=10),
        ]
    )
    message = FakeMessage()
    message.from_user = user("Лилиан", username="lilian", id=10)
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await ai_flow.request_alfred(
        message,
        link,
        store,
        _settings(),
        [{"role": "user", "content": "привет"}],
        500,
        _admin_book(),
        _notifier(),
    )

    note = link.command_calls[0][1]["messages"][-2]["content"]
    assert "Собеседник — мужчина: сказал это о себе сам" in note
    assert "«сэр»" in note
    assert "Его также зовут: Лили." in note
    assert "У него сейчас" in note
    assert "admin — id 999 — женщина (так говорит Лилиан (@lilian))" in note
    assert link.person_cards_calls == []


async def test_person_cards_unavailable_leave_note_without_them(store):
    message = FakeMessage()
    message.from_user = user("Лилиан", username="lilian", id=10)
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await ai_flow.request_alfred(
        message,
        link,
        store,
        _settings(),
        [{"role": "user", "content": "привет"}],
        500,
        _admin_book(),
        _notifier(),
    )

    note = link.command_calls[0][1]["messages"][-2]["content"]
    assert "Собеседник —" not in note
    assert "Знакомые собеседника" not in note


def _search_call(query: str = "новости"):
    return {"function": {"name": "web_search", "arguments": {"query": query}}}


async def test_web_search_announces_surfing_once_per_request(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [_search_call("первый")]},
            {"response": ROUTE_OK},
            {"tool_calls": [_search_call("второй")]},
            {"response": "Вот что нашёл, сэг"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await _ask(message, link, store, "что нового?")

    assert raw == "Вот что нашёл, сэг"
    assert message.answers.count(ai_flow.SURFING_TEXT) == 1
    calls = await store.tool_calls_for_dialogue(message.chat.id, 1)
    assert [c["tool_name"] for c in calls] == ["web_search", "web_search"]


async def test_other_tools_do_not_announce_surfing(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [{"function": {"name": "calc", "arguments": {"expression": "2+2"}}}]},
            {"response": ROUTE_OK},
            {"response": "Четыге"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await _ask(message, link, store, "2+2")

    assert ai_flow.SURFING_TEXT not in message.answers


async def test_tool_status_pushed_for_every_call_when_rich_session_present(store):
    message = FakeMessage()
    rich_session = FakeRichSession()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [_search_call("первый")]},
            {"response": ROUTE_OK},
            {"tool_calls": [_search_call("второй")]},
            {"response": "Вот что нашёл, сэг"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await _ask(message, link, store, "что нового?", rich_session=rich_session)

    assert raw == "Вот что нашёл, сэг"
    assert ai_flow.SURFING_TEXT not in message.answers
    assert rich_session.statuses == [ai_flow.TOOL_STATUS_TEXT["web_search"]] * 2


async def test_tool_status_uses_per_tool_phrase(store):
    message = FakeMessage()
    rich_session = FakeRichSession()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [{"function": {"name": "calc", "arguments": {"expression": "2+2"}}}]},
            {"response": ROUTE_OK},
            {"response": "Четыге"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await _ask(message, link, store, "2+2", rich_session=rich_session)

    assert rich_session.statuses == [ai_flow.TOOL_STATUS_TEXT["calc"]]


def test_tool_status_covers_every_registered_tool():
    admin = Subscription(chat_id=1, name="admin", allowed_commands=frozenset({"*"}))
    toolkit = ai_tools.tools_for(admin)
    missing = set(toolkit.handlers) - set(ai_flow.TOOL_STATUS_TEXT)
    assert missing == set()


def _note_msg(messages) -> str:
    return next(m["content"] for m in messages if m["role"] == "system")


class FakeDismissLink:
    display_name = "нода"

    def __init__(self, fail_on=None, exc=None):
        self.calls: list[tuple[str, dict, str, str]] = []
        self._fail_on = fail_on
        self._exc = exc or ServiceUnavailableError("нет связи")

    async def command(self, action, args=None, dst=None, timeout=None):
        self.calls.append((action, args or {}, dst.node, dst.service))
        if action == self._fail_on:
            raise self._exc
        return {"ok": True}


async def test_dismiss_tool_powers_nothing_off_during_the_request(store):
    message = FakeMessage()
    box = ai_flow.ai_tools.DismissalBox()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [{"function": {"name": "dismiss", "arguments": {"mode": "off"}}}]},
            {"response": ROUTE_OK},
            {"response": "Всего доброго, сэг"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await _ask(message, link, store, "спасибо, свободен", dismissal=box)

    assert raw == "Всего доброго, сэг"
    assert box.mode == "off"
    assert {call[0] for call in link.command_calls} == {"chat"}


async def test_perform_dismissal_powers_off_even_if_model_did_not_unload(store):
    message = FakeMessage()
    link = FakeDismissLink(fail_on="sleep")

    await ai_flow.perform_dismissal(
        message, link, ai_flow.ai_tools.DISMISS_OFF, _admin_book(), _notifier()
    )

    assert [c[0] for c in link.calls] == ["sleep", "poweroff"]
    assert message.answers == [ai_flow.DISMISS_TEXTS[ai_flow.ai_tools.DISMISS_OFF]]


async def test_perform_dismissal_reports_when_machine_stayed_up(store):
    message = FakeMessage()
    notifier = _notifier()
    link = FakeDismissLink(fail_on="poweroff")

    await ai_flow.perform_dismissal(
        message, link, ai_flow.ai_tools.DISMISS_OFF, _admin_book(), notifier
    )

    assert message.answers == [ai_flow.DISMISS_FAILED_TEXT]
    assert notifier.sent and "poweroff" in notifier.sent[0][1]


async def test_perform_dismissal_model_only_failure_is_reported(store):
    message = FakeMessage()
    notifier = _notifier()
    link = FakeDismissLink(fail_on="sleep")

    await ai_flow.perform_dismissal(
        message, link, ai_flow.ai_tools.DISMISS_MODEL, _admin_book(), notifier
    )

    assert message.answers == [ai_flow.DISMISS_FAILED_TEXT]
    assert notifier.sent and "llm.sleep" in notifier.sent[0][1]


async def test_memory_facts_land_in_the_context_note(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "Как скажете"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
        memory_facts=[
            {"text": "Качаем в /mnt/data/pr", "sensitive": False},
            {"text": "Код от домофона 1234", "sensitive": True},
        ],
    )

    await _ask(message, link, store, "скачай кино")

    assert link.recall_calls[0]["chat_id"] == 1
    note = _note_msg(link.command_calls[0][1]["messages"])
    assert "Качаем в /mnt/data/pr" in note
    assert "Код от домофона 1234 [личное: знай, но вслух не пересказывай]" in note


async def test_silent_memory_leaves_the_note_as_it_was(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "…"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await _ask(message, link, store)

    assert "Из того, что ты помнишь" not in _note_msg(link.command_calls[0][1]["messages"])


async def test_graph_facts_land_in_the_context_note(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "Как скажете"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
        graph_facts=["Наташа — жена Алексея"],
    )

    await _ask(message, link, store, "кто жена Алексея?")

    assert link.graph_recall_calls[0]["chat_id"] == 1
    note = _note_msg(link.command_calls[0][1]["messages"])
    assert "Наташа — жена Алексея" in note
    assert "Дополнительно — связи" in note


async def test_guest_first_turn_queries_graph_for_identity(store):
    message = FakeMessage()
    message.from_user = user("Гусь", username="goose", id=777)
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "Как скажете"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await _ask(message, link, store)

    assert len(link.graph_recall_calls) == 2
    identity_call = link.graph_recall_calls[1]
    assert identity_call["chat_id"] == 1
    assert "Гусь" in identity_call["query"]


async def test_guest_second_turn_does_not_requery_identity(store):
    message = FakeMessage()
    message.from_user = user("Гусь", username="goose", id=777)
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "Как скажете"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )
    history = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "Здравствуйте"},
    ]

    await _ask(message, link, store, history=history)

    assert len(link.graph_recall_calls) == 1


async def test_known_family_member_does_not_query_guest_identity(store):
    message = FakeMessage()
    message.from_user = user("Иван", username="ivan", id=777)
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "Как скажете"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )
    await store.add_person_claims([claim(777, "name", "Иван Иванов", by_id=1)])
    settings = Settings(llm=LlmConfig(request_timeout_s=5.0))

    await _ask(message, link, store, settings=settings)

    assert len(link.graph_recall_calls) == 1


class _FakeEpisodeLink:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, object]] = []
        self._raises = raises

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}, dst))
        if self._raises is not None:
            raise self._raises
        return {"queued": True, "id": 1}


async def test_piggyback_dialogue_episode_writes_the_turn(store):
    link = _FakeEpisodeLink()

    await ai_flow.piggyback_dialogue_episode(link, 777, "какая погода в Москве?", "Прохладно, сэр.")

    action, args, dst = link.calls[0]
    assert action == "add_episode"
    assert dst.node == "mycraft"
    assert dst.service == "graph_memory"
    assert args["chat_id"] == 777
    assert args["source"] == "dialogue_turn"
    assert "какая погода в Москве?" in args["text"]
    assert "Прохладно, сэр." in args["text"]


async def test_piggyback_dialogue_episode_extracts_links_separately():
    link = _FakeEpisodeLink()

    await ai_flow.piggyback_dialogue_episode(
        link, 777, "глянь https://example.com/article", "Посмотрел, сэр."
    )

    assert len(link.calls) == 2
    sources = [call[1]["source"] for call in link.calls]
    assert sources == ["dialogue_turn", "link"]


async def test_piggyback_dialogue_episode_skips_without_node_link_or_chat():
    link = _FakeEpisodeLink()
    await ai_flow.piggyback_dialogue_episode(None, 777, "привет", "Здравствуйте, сэр.")
    await ai_flow.piggyback_dialogue_episode(link, None, "привет", "Здравствуйте, сэр.")
    assert link.calls == []


async def test_piggyback_dialogue_episode_skips_empty_turns():
    link = _FakeEpisodeLink()
    await ai_flow.piggyback_dialogue_episode(link, 777, "", "Здравствуйте, сэр.")
    await ai_flow.piggyback_dialogue_episode(link, 777, "привет", "")
    assert link.calls == []


async def test_typing_fires_for_the_chat_around_the_actual_ask(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "Добгый день, сэ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await _ask(message, link, store)

    assert message.bot.typing_chats == [message.chat.id]
    assert message.bot.typing_threads == [None]


async def test_typing_uses_the_topic_thread_id(store):
    message = FakeMessage()
    message.message_thread_id = 777
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await _ask(message, link, store)

    assert message.bot.typing_threads == [777]


async def test_typing_does_not_fire_while_node_unreachable_and_no_wake_data(store, monkeypatch):
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_TIMEOUT_S", 0.05)
    message = FakeMessage()
    link = FakeNodeLink()

    await _ask(message, link, store)

    assert message.bot.typing_chats == []


async def test_typing_does_not_fire_during_asleep_warmup_before_the_retry(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "Секунду, сэг"}],
        get_state_routes={"mycraft:llm": {"asleep": True}},
    )

    await _ask(message, link, store)

    assert message.bot.typing_chats == [message.chat.id]


async def test_typing_keepalive_repeats_while_the_model_is_slow(store, monkeypatch):
    from sa_home_bot.bot import notifier as notifier_module

    monkeypatch.setattr(notifier_module, "TYPING_KEEPALIVE_INTERVAL_S", 0.01)
    message = FakeMessage()

    class SlowLink(FakeNodeLink):
        async def command(self, action, args=None, dst=None, timeout=None):
            if action == "chat":
                await asyncio.sleep(0.05)
            return await super().command(action, args, dst, timeout)

    link = SlowLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await _ask(message, link, store)

    assert len(message.bot.typing_chats) > 1
    assert set(message.bot.typing_chats) == {message.chat.id}


PLAIN = "plain"
RICH = "rich"
MODES = [PLAIN, RICH]


def _session(mode):
    return FakeRichSession() if mode == RICH else None


def _shown(message, session):
    if session is None:
        return message.answers, [], []
    return message.answers, session.statuses, session.finalized_statuses


def _expected(mode, *, plain=(), statuses=(), finalized=()):
    if mode == PLAIN:
        return list(plain), [], []
    return [], list(statuses), list(finalized)


def _awake_link(*chat_results, **kwargs):
    return FakeNodeLink(
        chat_results=list(chat_results),
        get_state_routes={"mycraft:llm": {"asleep": False}},
        **kwargs,
    )


@pytest.mark.parametrize(
    ("llm_kwargs", "reason"),
    [({}, "off"), ({"single_call_think": True}, "high")],
    ids=["no_think", "think"],
)
async def test_single_call_mode_skips_router_and_sends_intent(store, llm_kwargs, reason):
    message = FakeMessage()
    link = _awake_link({"response": "Добгый день, сэ"})
    settings = Settings(llm=LlmConfig(request_timeout_s=5.0, mode="single_call", **llm_kwargs))

    raw = await _ask(message, link, store, settings=settings)

    assert raw == "Добгый день, сэ"
    assert message.answers == []
    assert len(link.command_calls) == 1
    action, args, node = link.command_calls[0]
    assert (action, node) == ("chat", "mycraft")
    assert args["reason"] == reason
    assert "think" not in args
    assert "role" not in args


@pytest.mark.parametrize("mode", MODES)
async def test_escalates_to_thinking_when_router_asks(store, mode):
    message, session = FakeMessage(), _session(mode)
    link = _awake_link(
        {"response": f"кое-что... {THINK_MARKER}"}, {"response": "Точный ответ после раздумий"}
    )

    raw = await _ask(message, link, store, "сложный вопрос", rich_session=session)

    assert raw == "Точный ответ после раздумий"
    assert _shown(message, session) == _expected(
        mode, plain=[ai_flow.THINKING_TEXT], statuses=[ai_flow.THINKING_TEXT_PLAIN]
    )
    router_args, persona_args = link.command_calls[0][1], link.command_calls[1][1]
    assert router_args["role"] == "router"
    assert router_args["reason"] == "off"
    assert "role" not in persona_args
    assert persona_args["reason"] == "high"
    assert persona_args["messages"] == router_args["messages"]
    assert persona_args["messages"][-1] == {"role": "user", "content": "сложный вопрос"}


@pytest.mark.parametrize("mode", MODES)
async def test_asleep_model_shows_steps_but_no_wake(store, mode):
    message, session = FakeMessage(), _session(mode)
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "Секунду, сэг"}],
        get_state_routes={"mycraft:llm": {"asleep": True}},
    )

    raw = await _ask(message, link, store, rich_session=session)

    assert raw == "Секунду, сэг"
    assert _shown(message, session) == _expected(
        mode, plain=[ai_flow.STEPS_TEXT], statuses=[ai_flow.STEPS_TEXT_PLAIN]
    )
    assert link.wol_sent == []


@pytest.mark.parametrize("mode", MODES)
async def test_asleep_warmup_fails_answers_as_albert(store, mode):
    message, session, notifier = FakeMessage(), _session(mode), _notifier()
    link = FakeNodeLink(
        get_state_routes={"mycraft:llm": {"asleep": True}},
        warmup_result=ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева"),
    )

    raw = await _ask(message, link, store, notifier=notifier, rich_session=session)

    assert raw is None
    assert _shown(message, session) == _expected(
        mode,
        plain=[ai_flow.STEPS_TEXT, ai_flow.ALBERT_ASLEEP],
        statuses=[ai_flow.STEPS_TEXT_PLAIN],
        finalized=[ai_flow.ALBERT_ASLEEP_MD],
    )
    assert link.wol_sent == []
    assert link.command_calls == [("warmup", {}, "mycraft")]
    assert len(notifier.sent) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_unavailable_then_woken_within_30s(store, monkeypatch, mode):
    await wake_state.remember(store, "mycraft", MYCRAFT_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    message, session = FakeMessage(), _session(mode)
    link = FakeNodeLink(
        chat_results=[{"response": ROUTE_OK}, {"response": "Сейчас подойду"}],
        get_state_sequences={"mycraft:llm": [None, {"asleep": True}]},
    )

    raw = await _ask(message, link, store, rich_session=session)

    assert raw == "Сейчас подойду"
    assert _shown(message, session) == _expected(
        mode,
        plain=[ai_flow.STEPS_TEXT, ai_flow.ARNOLD_WAKING, ai_flow.STEPS_TEXT],
        statuses=[ai_flow.STEPS_TEXT_PLAIN, ai_flow.STEPS_TEXT_PLAIN],
        finalized=[ai_flow.ARNOLD_WAKING_MD],
    )
    assert link.wol_sent == [{"mac": MYCRAFT_WAKE["mac"]}]
    assert ("warmup", {}, "mycraft") in link.command_calls


@pytest.mark.parametrize("mode", MODES)
async def test_unavailable_and_no_wake_data_gives_up_immediately(store, monkeypatch, mode):
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_TIMEOUT_S", 0.05)
    message, session = FakeMessage(), _session(mode)
    link = FakeNodeLink()

    raw = await _ask(message, link, store, rich_session=session)

    assert raw is None
    assert _shown(message, session) == _expected(
        mode,
        plain=[ai_flow.STEPS_TEXT, ai_flow.ALBERT_UNAVAILABLE],
        statuses=[ai_flow.STEPS_TEXT_PLAIN],
        finalized=[ai_flow.ALBERT_UNAVAILABLE_MD],
    )
    assert link.wol_sent == []
    assert link.command_calls == []


@pytest.mark.parametrize("mode", MODES)
async def test_internal_error_answers_user_and_notifies_admin(store, mode):
    message, session, notifier = FakeMessage(), _session(mode), _notifier()
    link = _awake_link(ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева"))

    raw = await _ask(message, link, store, notifier=notifier, rich_session=session)

    assert raw is None
    assert _shown(message, session) == _expected(
        mode, plain=[ai_flow.ALBERT_HICCUP], finalized=[ai_flow.ALBERT_HICCUP_MD]
    )
    assert "Ollama" not in "".join(message.answers)
    assert link.wol_sent == []
    assert len(notifier.sent) == 1
    admin_chat_id, admin_text = notifier.sent[0]
    assert admin_chat_id == 999
    assert "internal" in admin_text
    assert "Ollama не поднялась после прогрева" in admin_text


def test_active_ai_chats_register_snapshot_and_overwrite():
    chats = ai_flow.ActiveAiChats()
    assert chats.snapshot() == {}
    old_task, other_task, new_task = object(), object(), object()
    chats.register(42, old_task)
    chats.register(7, other_task)
    chats.register(42, new_task)
    chats.register(None, object())
    assert chats.snapshot() == {42: new_task, 7: other_task}


def test_active_ai_chats_unregister_is_idempotent_and_ignores_none():
    chats = ai_flow.ActiveAiChats()
    task = object()
    chats.register(42, task)
    chats.unregister(42, task)
    chats.unregister(42, task)
    chats.unregister(None, task)
    assert chats.snapshot() == {}


def test_active_ai_chats_unregister_only_matching_task():
    chats = ai_flow.ActiveAiChats()
    old_task, new_task = object(), object()
    chats.register(42, old_task)
    chats.register(42, new_task)
    chats.unregister(42, old_task)
    assert chats.snapshot() == {42: new_task}


NO_SEARCH_BOOK = SubscriptionBook(
    [Subscription(chat_id=1, name="группа", allowed_commands=frozenset({"chat@llm"}))]
)


@pytest.mark.parametrize(
    ("book", "warned"),
    [(None, False), (NO_SEARCH_BOOK, True), (SubscriptionBook([]), True)],
    ids=["search_right", "no_search_right", "no_subscription"],
)
async def test_model_is_told_not_to_invent_only_without_search(store, book, warned):
    link = _awake_link({"response": ROUTE_OK}, {"response": "Не знаю, сэг"})

    await _ask(FakeMessage(), link, store, "что вчера запустил SpaceX?", book=book)

    note = _note_msg(link.command_calls[0][1]["messages"])
    assert ("Выхода в интернет у тебя сейчас нет" in note) is warned
    assert ("НЕ придумывай" in note) is warned


@pytest.mark.parametrize(
    ("mode", "expected_calls"),
    [
        (ai_tools.DISMISS_OFF, ["sleep", "poweroff"]),
        (ai_tools.DISMISS_SLEEP, ["sleep", "suspend"]),
        (ai_tools.DISMISS_MODEL, ["sleep"]),
    ],
)
async def test_perform_dismissal_unloads_model_then_acts_on_machine(store, mode, expected_calls):
    message, link = FakeMessage(), FakeDismissLink()

    await ai_flow.perform_dismissal(message, link, mode, _admin_book(), _notifier())

    assert [c[0] for c in link.calls] == expected_calls
    assert link.calls[0] == ("sleep", {"quiet": True}, "mycraft", "llm")
    assert message.answers == [ai_flow.DISMISS_TEXTS[mode]]
