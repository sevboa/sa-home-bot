"""Presence/wake-сценарий /ai (bot/ai_flow.py): «шаги», молчаливый wake через
рой, Агнольд/Альбегт. Персонаж и текстовки — из обсуждения с пользователем
2026-07-23 (см. докстринг модуля ai_flow.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio

from sa_home_bot.bot import ai_flow, wake_state
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ERR_INTERNAL, ERR_UNAVAILABLE, ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

WINPC_WAKE = {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.0.50", "broadcast": "192.168.0.255"}
ALFRED_WAKE = {"mac": "7c:83:34:b4:59:ac", "ip": "192.168.0.100", "broadcast": "192.168.0.255"}

OWN_STATE = {
    "node": "alfred",
    "version": "0.27.0",
    "services": [],
    "wake": ALFRED_WAKE,
    "peers": [{"id": "winpc", "endpoint": "tcp://y:8710", "alive": False}],
}


class FakeMessage:
    chat = SimpleNamespace(id=1)

    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_direct(self, chat_id, text, reply_to_message_id=None):
        self.sent.append((chat_id, text))
        return 1


def _admin_book() -> SubscriptionBook:
    return SubscriptionBook(
        [Subscription(chat_id=999, name="admin", allowed_commands=frozenset({"*"}))]
    )


class FakeNodeLink:
    display_name = "нода"

    def __init__(self, own=None, chat_results=(), get_state_routes=None, wol_sent=None):
        self._own = own or OWN_STATE
        # chat_results — список результатов/исключений, по одному на каждый
        # вызов command("chat", ...) (по порядку) — эмулирует "недоступна,
        # затем доступна после wake".
        self._chat_results = list(chat_results)
        self._get_state_routes = get_state_routes or {}
        self.wol_sent = wol_sent if wol_sent is not None else []
        self.command_calls: list[tuple[str, dict, str | None]] = []
        self.get_state_calls: list[str] = []

    async def get_state(self, dst=None):
        key = f"{dst.node}:{dst.service}" if dst is not None else "own"
        self.get_state_calls.append(key)
        if key == "own":
            return self._own
        if key in self._get_state_routes:
            result = self._get_state_routes[key]
            if isinstance(result, Exception):
                raise result
            return result
        raise ServiceUnavailableError("нет связи")

    async def command(self, action, args=None, dst=None, timeout=None):
        self.command_calls.append((action, args, dst.node if dst else None))
        if action == "send_wol":
            self.wol_sent.append(args)
            return {"sent": True}
        assert action == "chat"
        result = self._chat_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _settings() -> Settings:
    return Settings(llm=LlmConfig(request_timeout_s=5.0))


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


async def test_fast_path_no_narrative_when_node_already_up(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": "Добгый день, сэ"}],
        get_state_routes={"winpc:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}],
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Добгый день, сэ"
    assert message.answers == []  # никаких «шагов»/Агнольда — узел жив, модель не спит
    assert link.command_calls == [
        ("chat", {"messages": [{"role": "user", "content": "привет"}]}, "winpc")
    ]


async def test_asleep_model_shows_steps_but_no_wake(store):
    # Узел доступен, модель просто спит (idle-таймер llm/service.py) — не
    # сценарий wake (магик-пакет тут ни при чём), но пользователь должен
    # увидеть «шаги», а не молча ждать до request_timeout_s.
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": "Секунду, сэг"}],
        get_state_routes={"winpc:llm": {"asleep": True}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}],
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Секунду, сэг"
    assert message.answers == [ai_flow.STEPS_TEXT]
    assert link.wol_sent == []  # узел был доступен — будить не нужно


async def test_asleep_warmup_fails_answers_as_albert_not_generic_error(store):
    # Прогрев не уложился (Ollama не поднялась) — раз мы уже знали, что
    # модель спит, это подаётся как «Альфред, кажется, уснул» (Альбегт),
    # а не безликое «Прошу прощения, не вышло» от самого Альфреда.
    message = FakeMessage()
    notifier = FakeNotifier()
    link = FakeNodeLink(
        chat_results=[ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева")],
        get_state_routes={"winpc:llm": {"asleep": True}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}],
        _admin_book(), notifier,
    )

    assert raw is None
    assert message.answers == [ai_flow.STEPS_TEXT, ai_flow.ALBERT_ASLEEP]
    assert link.wol_sent == []  # узел был доступен — будить не нужно
    assert len(notifier.sent) == 1  # админ всё равно узнаёт о сбое


async def test_unavailable_then_woken_within_30s(store, monkeypatch):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            ProtoError(ERR_UNAVAILABLE, "нода недоступна"),
            {"response": "Сейчас подойду"},
        ],
        get_state_routes={"winpc:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}],
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Сейчас подойду"
    assert message.answers == [ai_flow.STEPS_TEXT, ai_flow.ARNOLD_WAKING]
    assert link.wol_sent == [{"mac": WINPC_WAKE["mac"]}]  # разбудили молча


async def test_unavailable_and_no_wake_data_gives_up_immediately(store, monkeypatch):
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_TIMEOUT_S", 0.05)
    message = FakeMessage()
    link = FakeNodeLink(chat_results=[ProtoError(ERR_UNAVAILABLE, "нода недоступна")])

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}],
        _admin_book(), FakeNotifier(),
    )

    assert raw is None
    assert message.answers == [ai_flow.STEPS_TEXT, ai_flow.ALBERT_UNAVAILABLE]
    assert link.wol_sent == []  # нечем будить — нет кэша MAC


async def test_unavailable_wake_sent_but_still_unreachable_after_30s(store, monkeypatch):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_TIMEOUT_S", 0.05)
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[ProtoError(ERR_UNAVAILABLE, "нода недоступна")],
        get_state_routes={},  # winpc:llm так и не отвечает
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}],
        _admin_book(), FakeNotifier(),
    )

    assert raw is None
    assert message.answers == [ai_flow.STEPS_TEXT, ai_flow.ALBERT_UNAVAILABLE]
    assert link.wol_sent == [{"mac": WINPC_WAKE["mac"]}]  # будили, но не помогло


async def test_woken_but_retry_call_still_fails(store, monkeypatch):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            ProtoError(ERR_UNAVAILABLE, "нода недоступна"),
            ProtoError(ERR_UNAVAILABLE, "опять недоступна"),
        ],
        get_state_routes={"winpc:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}],
        _admin_book(), FakeNotifier(),
    )

    assert raw is None
    assert message.answers == [
        ai_flow.STEPS_TEXT,
        ai_flow.ARNOLD_WAKING,
        ai_flow.ALBERT_UNAVAILABLE,
    ]


async def test_internal_error_on_first_try_answers_user_and_notifies_admin(store):
    # Не «недоступна» (нода жива, Ollama сама упала) — раньше улетало
    # необработанным исключением, теперь: сообщение юзеру + диагностика админу.
    message = FakeMessage()
    notifier = FakeNotifier()
    link = FakeNodeLink(
        chat_results=[ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева")]
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}],
        _admin_book(), notifier,
    )

    assert raw is None
    # Без «шагов» — это не сценарий недоступности узла. Текст пользователю —
    # намеренно общий (не палим инфраструктуру/LLM), подробности — только
    # админу.
    assert message.answers == [ai_flow._GENERIC_ERROR_TEXT]
    assert "Ollama" not in message.answers[0]
    assert link.wol_sent == []  # это не сценарий недоступности — wake не трогаем
    assert len(notifier.sent) == 1
    admin_chat_id, admin_text = notifier.sent[0]
    assert admin_chat_id == 999
    assert "internal" in admin_text
    assert "Ollama не поднялась после прогрева" in admin_text


async def test_internal_error_after_wake_answers_user_and_notifies_admin(store, monkeypatch):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    message = FakeMessage()
    notifier = FakeNotifier()
    link = FakeNodeLink(
        chat_results=[
            ProtoError(ERR_UNAVAILABLE, "нода недоступна"),
            ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева"),
        ],
        get_state_routes={"winpc:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}],
        _admin_book(), notifier,
    )

    assert raw is None
    assert message.answers == [
        ai_flow.STEPS_TEXT,
        ai_flow.ARNOLD_WAKING,
        ai_flow._GENERIC_ERROR_TEXT,
    ]
    assert len(notifier.sent) == 1
