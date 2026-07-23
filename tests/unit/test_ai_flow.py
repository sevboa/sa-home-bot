"""Presence/wake-сценарий /ai (bot/ai_flow.py): «шаги», молчаливый wake через
рой, Агнольд/Альбегт. Персонаж и текстовки — из обсуждения с пользователем
2026-07-23 (см. докстринг модуля ai_flow.py)."""

from __future__ import annotations

from datetime import UTC, datetime
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
    from_user = None

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


class FakeUser:
    def __init__(self, first_name, last_name=None, username=None, id=1):  # noqa: A002
        self.id = id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}" if self.last_name else self.first_name


class NoteMessage:
    """Мини-заглушка Message только для _build_context_note — не тянет весь
    presence/wake-сценарий FakeMessage/FakeNodeLink."""

    def __init__(self, chat_id, chat_type, from_user):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.from_user = from_user


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
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Добгый день, сэ"
    assert message.answers == []  # никаких «шагов»/Агнольда — узел жив, модель не спит
    assert link.command_calls == [
        (
            "chat",
            {"messages": [{"role": "user", "content": "привет"}], "chat_id": 1},
            "winpc",
        )
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
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
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
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
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
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Сейчас подойду"
    # Второе «шаги» — про поднятие контейнера (отдельная неопределённость
    # от самого wake); успех не добавляет отдельного сообщения персонажа.
    assert message.answers == [ai_flow.STEPS_TEXT, ai_flow.ARNOLD_WAKING, ai_flow.STEPS_TEXT]
    assert link.wol_sent == [{"mac": WINPC_WAKE["mac"]}]  # разбудили молча


async def test_unavailable_and_no_wake_data_gives_up_immediately(store, monkeypatch):
    # get_state_routes пуст — presence-проверка сама уже "недоступна",
    # полноценный _ask() с этим же исходом не запускается вовсе (живая
    # находка 2026-07-23: не тратим до request_timeout_s на заведомо
    # обречённую попытку, см. _PRESENCE_CHECK_TIMEOUT_S).
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_TIMEOUT_S", 0.05)
    message = FakeMessage()
    link = FakeNodeLink()

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw is None
    assert message.answers == [ai_flow.STEPS_TEXT, ai_flow.ALBERT_UNAVAILABLE]
    assert link.wol_sent == []  # нечем будить — нет кэша MAC
    assert link.command_calls == []  # chat вообще не пытались звать


async def test_unavailable_wake_sent_but_still_unreachable_after_30s(store, monkeypatch):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_TIMEOUT_S", 0.05)
    message = FakeMessage()
    link = FakeNodeLink(get_state_routes={})  # winpc:llm так и не отвечает

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
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
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw is None
    assert message.answers == [
        ai_flow.STEPS_TEXT,
        ai_flow.ARNOLD_WAKING,
        ai_flow.STEPS_TEXT,
        ai_flow.ALBERT_UNAVAILABLE,
    ]


async def test_internal_error_on_first_try_answers_user_and_notifies_admin(store):
    # Не «недоступна» (нода жива, Ollama сама упала) — раньше улетало
    # необработанным исключением, теперь: сообщение юзеру + диагностика админу.
    # get_state должен успешно ответить (узел доступен) — иначе presence-
    # проверка сама сочтёт это недоступностью и chat не будет вызван вовсе.
    message = FakeMessage()
    notifier = FakeNotifier()
    link = FakeNodeLink(
        chat_results=[ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева")],
        get_state_routes={"winpc:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
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
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), notifier,
    )

    assert raw is None
    # Провал именно после успешного wake (контейнер не поднялся) — это
    # шаги уже Альбегта, не безликое извинение Альфреда: Агнольд успешно
    # разбудил машину, а дальше не задалось у того, кто пошёл за Альфредом.
    assert message.answers == [
        ai_flow.STEPS_TEXT,
        ai_flow.ARNOLD_WAKING,
        ai_flow.STEPS_TEXT,
        ai_flow.ALBERT_ASLEEP,
    ]
    assert len(notifier.sent) == 1


# --- display_name / _build_context_note (2026-07-24: "кто пишет", "кто
# начал", "кто ещё обращался" — контекст для промпта LLM) ---


def test_display_name_with_username():
    assert ai_flow.display_name(FakeUser("Иван", username="ivan")) == "Иван (@ivan)"


def test_display_name_without_username():
    assert ai_flow.display_name(FakeUser("Иван", "Иванов")) == "Иван Иванов"


def test_display_name_none_for_missing_user():
    assert ai_flow.display_name(None) is None


async def test_context_note_none_without_sender(store):
    message = NoteMessage(1, "private", None)
    assert await ai_flow._build_context_note(message, store, dialogue_id=1) is None


async def test_context_note_private_chat_only_mentions_sender(store):
    message = NoteMessage(1, "private", FakeUser("Иван", username="ivan"))
    note = await ai_flow._build_context_note(message, store, dialogue_id=1)
    assert note is not None
    assert "Иван (@ivan)" in note
    assert "начал" not in note
    assert "также обращались" not in note


async def test_context_note_group_includes_starter_and_other_participants(store):
    ivan = FakeUser("Иван", username="ivan", id=10)
    maria = FakeUser("Мария", id=20)
    petr = FakeUser("Пётр", id=30)
    now = datetime.now(tz=UTC)

    # Иван начал этот тред (dialogue_id=500).
    await store.record_ai_turn(
        1, 500, 500, "user", "привет", now, user_id=10, user_name=ai_flow.display_name(ivan)
    )
    await store.record_ai_turn(1, 501, 500, "assistant", "Здравствуйте", now)
    # Мария обращалась к Альфреду в этом же чате, но в другом треде.
    await store.record_ai_turn(
        1, 600, 600, "user", "как дела", now, user_id=20, user_name=ai_flow.display_name(maria)
    )

    message = NoteMessage(1, "group", petr)
    note = await ai_flow._build_context_note(message, store, dialogue_id=500)

    assert "Пётр" in note  # сейчас пишет
    assert "Иван (@ivan)" in note  # начал тред
    assert "Мария" in note  # ещё обращалась (другой тред, тот же чат)


async def test_context_note_skips_starter_line_when_same_as_sender(store):
    ivan = FakeUser("Иван", username="ivan", id=10)
    now = datetime.now(tz=UTC)
    await store.record_ai_turn(
        1, 500, 500, "user", "привет", now, user_id=10, user_name=ai_flow.display_name(ivan)
    )

    message = NoteMessage(1, "group", ivan)
    note = await ai_flow._build_context_note(message, store, dialogue_id=500)

    assert note.count("Иван (@ivan)") == 1  # не повторяем "начал" для того же человека
    assert "начал" not in note
