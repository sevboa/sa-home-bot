"""Presence/wake-сценарий /ai (bot/ai_flow.py): «шаги», молчаливый wake через
рой, Агнольд/Альбегт. Персонаж и текстовки — из обсуждения с пользователем
2026-07-23 (см. докстринг модуля ai_flow.py)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest_asyncio

from sa_home_bot import llm_chat
from sa_home_bot.bot import ai_flow, wake_state
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import LlmConfig, PersonConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ERR_INTERNAL, ERR_UNAVAILABLE, ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

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


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_direct(self, chat_id, text, reply_to_message_id=None, reply_markup=None):
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

    def __init__(self, chat_id, chat_type, from_user, reply_to_message=None, quote=None):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.from_user = from_user
        self.reply_to_message = reply_to_message
        self.quote = quote


class FakeRepliedMessage:
    """Заглушка reply_to_message — сообщение, на которое отвечают."""

    def __init__(self, message_id, from_user=None, text=None, caption=None):
        self.message_id = message_id
        self.from_user = from_user
        self.text = text
        self.caption = caption


class FakeQuote:
    def __init__(self, text):
        self.text = text


def _admin_book() -> SubscriptionBook:
    """Админский чат для уведомлений (999) + сам собеседник (1, чат FakeMessage).

    У собеседника права перечислены группами, но БЕЗ голого "*" — иначе
    notify_admins считал бы админом и его тоже. По этой подписке собирается
    комплект тулов для /ai (bot/tools.py::tools_for).
    """
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
        self, own=None, chat_results=(), get_state_routes=None, wol_sent=None, memory_facts=()
    ):
        self.memory_facts = list(memory_facts)
        self.recall_calls: list[dict] = []
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
        if action == "recall":
            # Память спрашивается перед КАЖДЫМ запросом (bot/ai_flow.py::
            # recall_facts) — в command_calls её не пишем, иначе каждый тест
            # про раунды chat считал бы её лишним вызовом. По умолчанию
            # память пуста: заметка остаётся ровно такой, как была раньше.
            self.recall_calls.append(args)
            return {"facts": list(self.memory_facts), "count": len(self.memory_facts)}
        self.command_calls.append((action, args, dst.node if dst else None))
        if action == "send_wol":
            self.wol_sent.append(args)
            return {"sent": True}
        if action == "search":
            # Служба net (тул web_search) — отвечаем канонично, не ходя в сеть.
            return {
                "query": args.get("query", ""),
                "results": [{"title": "T", "url": "u"}],
                "count": 1,
            }
        assert action == "chat"
        result = self._chat_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeRichSession:
    """Двойник bot/rich_stream.py::RichStreamSession — только то, что видит
    ai_flow.py (push_status/on_partial), без реального draft/Bot API."""

    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.partials: list[tuple[str, bool]] = []

    async def push_status(self, text: str) -> None:
        self.statuses.append(text)

    async def on_partial(self, text: str, done: bool) -> None:
        self.partials.append((text, done))


def _settings() -> Settings:
    return Settings(llm=LlmConfig(request_timeout_s=5.0))


def _single_call_settings() -> Settings:
    return Settings(llm=LlmConfig(request_timeout_s=5.0, mode="single_call"))


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
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "Добгый день, сэ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Добгый день, сэ"
    assert message.answers == []  # никаких «шагов»/Агнольда — узел жив, модель не спит
    # Два вызова — лёгкий router-проход (без персонажа, решает think/тулы,
    # ЯВНО think=false ради скорости), затем персонажный проход с явным
    # think=false — роутер не настоял на думании.
    assert len(link.command_calls) == 2
    action, router_args, node = link.command_calls[0]
    assert (action, node) == ("chat", "mycraft")
    assert router_args["chat_id"] == 1
    assert router_args["think"] is False
    # Комплект собран под права собеседника, а не глобальный список.
    assert (
        router_args["tools"]
        == ai_flow.ai_tools.tools_for(_admin_book().for_chat(1)).declarations
    )
    assert router_args["role"] == "router"
    persona_args = link.command_calls[1][1]
    assert "role" not in persona_args
    assert persona_args["think"] is False
    # Router и персонажный проход видят ОДНУ и ту же историю — никакой
    # отдельной триаж-инструкции больше не вставляется в сообщения.
    assert router_args["messages"] == persona_args["messages"]
    assert router_args["messages"][-1] == {"role": "user", "content": "привет"}
    assert len(router_args["messages"]) == 2
    assert router_args["messages"][0]["role"] == "system"
    assert "Точное время сейчас" in router_args["messages"][0]["content"]


async def test_single_call_think_false_is_sent_explicitly(store):
    # Живая находка 2026-08-10: «не слать флаг think» ≠ «модель не думает» —
    # gemma-4 размышляла по умолчанию над каждым приветствием (сотни
    # невидимых токенов, 23 с в проде). Настройка single_call_think должна
    # доезжать до запроса ЯВНЫМ False, а не теряться по дороге.
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": "Добгый день, сэг"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )
    settings = Settings(
        llm=LlmConfig(request_timeout_s=5.0, mode="single_call", single_call_think=False)
    )

    await ai_flow.request_alfred(
        message, link, store, settings,
        [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    _, args, _ = link.command_calls[0]
    assert args["think"] is False


async def test_single_call_mode_skips_router(store):
    # LlmConfig.mode="single_call" — ни router-прохода, ни поля think в
    # запросе (single_call_think не задан — прежнее поведение), один
    # персонажный вызов сразу.
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": "Добгый день, сэ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _single_call_settings(),
        [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Добгый день, сэ"
    assert message.answers == []  # ни «шагов», ни THINKING_TEXT — решать нечего
    assert len(link.command_calls) == 1
    action, args, node = link.command_calls[0]
    assert (action, node) == ("chat", "mycraft")
    assert "think" not in args
    assert "role" not in args


async def test_speech_remark_is_captured_not_sent_inline(store):
    # Живой баг 2026-08-03: ремарка «Логопеда» раньше дописывалась llm/
    # service.py прямо в текст ответа — уезжала одним сообщением с ним И
    # ломано отформатированной (bot/handlers/ai.py экранирует ВЕСЬ текст
    # персонажа как plain text). Теперь служба llm отдаёт её отдельным
    # полем speech_remark, а request_alfred только складывает её в box —
    # отправка отдельным сообщением ПОСЛЕ ответа остаётся за вызывающим
    # (bot/handlers/ai.py::_do_ask_and_reply), не за этой функцией.
    message = FakeMessage()
    remark = "🗣 <i>Логопед:</i> не «сэг», а «сэр»!"
    link = FakeNodeLink(
        chat_results=[{"response": "Добгый день, сэг", "speech_remark": remark}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )
    box = ai_flow.SpeechRemarkBox()

    raw = await ai_flow.request_alfred(
        message, link, store, _single_call_settings(),
        [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(), speech_remark=box,
    )

    assert raw == "Добгый день, сэг"  # ремарка НЕ подмешана в текст ответа
    assert box.text == remark
    assert message.answers == []  # request_alfred сама ничего не шлёт


async def test_tool_call_round_trip_reaches_final_response(store):
    # Тул вызывается в router-проходе (round 1: просьба вызвать calc, round
    # 2: после результата — решение "OK"), персонажный проход (3й вызов)
    # видит уже готовый результат тула в истории и формулирует финальный
    # ответ (§7.1 плана).
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [{"function": {"name": "calc", "arguments": {"expression": "2 + 2"}}}]},
            {"response": ai_flow.ROUTE_OK},
            {"response": "Отвечу: 4"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "сколько 2+2"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Отвечу: 4"
    assert len(link.command_calls) == 3
    router_round2_messages = link.command_calls[1][1]["messages"]
    assert router_round2_messages[-2]["role"] == "assistant"
    assert router_round2_messages[-1] == {"role": "tool", "content": "4", "name": "calc"}
    # Персонажный проход видит тот же обмен с тулом, что и router.
    assert link.command_calls[2][1]["messages"] == router_round2_messages
    assert "role" not in link.command_calls[2][1]


async def test_tool_call_is_recorded_in_store(store):
    # Живая находка 2026-07-24: "шиза" с часовыми поясами была
    # недиагностируема — ai_turns хранит только финальный текст ответа, не
    # видно, звала ли модель тул вообще. Теперь каждый вызов тула пишется
    # через колбэк в Store.record_tool_call (schema.sql::ai_tool_calls).
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [{"function": {"name": "calc", "arguments": {"expression": "2 + 2"}}}]},
            {"response": ai_flow.ROUTE_OK},
            {"response": "Отвечу: 4"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "сколько 2+2"}], 1,
        _admin_book(), FakeNotifier(),
    )

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
            {"response": ai_flow.ROUTE_OK},
            {"response": "ладно"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "..."}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "ладно"
    tool_msg = link.command_calls[1][1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "неизвестный инструмент" in tool_msg["content"]


async def test_tool_call_round_limit_squeezes_answer_without_tools(store):
    """Лимит раундов исчерпан — не сбой, а последний запрос БЕЗ инструментов.

    Живая находка 2026-07-27: модель делала три поиска подряд и упиралась в
    лимит, после чего пользователь получал ALBERT_HICCUP, прождав минуты, —
    хотя результаты тулов уже лежали в messages и отвечать было чем.
    """
    tool_calls = [{"function": {"name": "calc", "arguments": {"expression": "1+1"}}}]
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            # router-проход: упирается в лимит...
            *[{"tool_calls": tool_calls}] * llm_chat.MAX_TOOL_ROUNDS,
            # ...и дожимает решение уже без тулов
            {"response": ai_flow.ROUTE_OK},
            # персонажный проход
            {"response": "Двá, сэг"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "..."}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Двá, сэг"
    assert ai_flow.ALBERT_HICCUP not in message.answers
    # Дожимающий запрос — ровно тот, что идёт сразу после исчерпания лимита;
    # у него пустой tools, иначе модели снова было бы что вызвать.
    squeeze_args = link.command_calls[llm_chat.MAX_TOOL_ROUNDS][1]
    assert squeeze_args["tools"] == []


async def test_empty_answer_after_round_limit_is_still_a_hiccup(store):
    """Пустой ответ, когда звать уже нечего, — настоящий сбой генерации."""
    tool_calls = [{"function": {"name": "calc", "arguments": {"expression": "1+1"}}}]
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            *[{"tool_calls": tool_calls}] * llm_chat.MAX_TOOL_ROUNDS,
            {"response": ""},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "..."}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw is None
    assert message.answers == [ai_flow.ALBERT_HICCUP]


# --- вариативное рассуждение (THINK_MARKER/THINKING_TEXT): быстрый проход
# (think=false) с триаж-инструкцией; думающий (think=true) — только если
# модель сама попросила подумать ---


def test_think_marker_survives_llm_service_transforms(tmp_path):
    # Живой баг на проде 2026-07-24: старый маркер с кириллической "Р"
    # утекал в чат мангленным ("[[ТГЕБУЕТСЯ_ГАЗМЫШЛЕНИЕ]]") — llm/service.py
    # прогоняет ЛЮБОЙ ответ модели (в т.ч. этот служебный маркер) через
    # SpeechTherapist/strip_math_notation до того, как отдать боту. Маркер
    # обязан пережить обе трансформации байт-в-байт, иначе проверка
    # "THINK_MARKER not in fast_answer" в _ask() молча перестаёт работать.
    # rand=0.0 — наихудший случай (искажение/визит логопеда сработали бы
    # всегда) — показывает, что маркер защищён структурно (латиница вне
    # regex [А-Яа-яЁё]+), не везением рандома.
    from sa_home_bot.config import LlmConfig
    from sa_home_bot.llm.prompt import strip_math_notation
    from sa_home_bot.llm.speech_therapy import SpeechTherapist

    cfg = LlmConfig(speech_therapy_state_path=str(tmp_path / "speech-therapy.json"))
    therapist = SpeechTherapist(cfg, rand=lambda: 0.0)
    transformed, _, _ = therapist.process(strip_math_notation(ai_flow.THINK_MARKER), chat_id=None)
    assert transformed == ai_flow.THINK_MARKER


async def test_fast_path_no_thinking_when_marker_absent(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "Добгый день, сэ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Добгый день, сэ"
    assert message.answers == []  # THINKING_TEXT не показывался — думать не понадобилось
    assert len(link.command_calls) == 2
    assert link.command_calls[0][1]["think"] is False
    assert link.command_calls[1][1]["think"] is False


async def test_escalates_to_thinking_when_marker_returned(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"response": f"кое-что... {ai_flow.THINK_MARKER}"},
            {"response": "Точный ответ после раздумий"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "сложный вопрос"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Точный ответ после раздумий"
    assert message.answers == [ai_flow.THINKING_TEXT]
    assert len(link.command_calls) == 2
    router_args = link.command_calls[0][1]
    persona_args = link.command_calls[1][1]
    assert router_args["role"] == "router"
    assert router_args["think"] is False
    assert "role" not in persona_args
    assert persona_args["think"] is True
    # Персонажный проход видит ту же историю, что и router (никакой
    # отдельной триаж-инструкции/маркера в сообщениях не остаётся: её
    # дописывает служба llm у себя, см. llm/service.py — так у обоих
    # проходов общий префикс и общий KV-кэш).
    assert persona_args["messages"] == router_args["messages"]
    assert persona_args["messages"][-1] == {"role": "user", "content": "сложный вопрос"}


async def test_escalates_to_thinking_uses_rich_status_not_plain_message(store):
    # rich_session передан (приватный чат + response_mode="rich") — THINKING_
    # TEXT идёт курсивной строкой в тот же черновик, а не отдельным
    # сообщением (см. ai_flow.py::THINKING_TEXT_PLAIN).
    message = FakeMessage()
    rich_session = FakeRichSession()
    link = FakeNodeLink(
        chat_results=[
            {"response": f"кое-что... {ai_flow.THINK_MARKER}"},
            {"response": "Точный ответ после раздумий"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "сложный вопрос"}], 1,
        _admin_book(), FakeNotifier(),
        rich_session=rich_session,
    )

    assert raw == "Точный ответ после раздумий"
    assert message.answers == []
    assert rich_session.statuses == [ai_flow.THINKING_TEXT_PLAIN]


async def test_implicit_think_style_omits_flag_instead_of_true(store):
    # think_style="implicit" (LlmConfig): моделям, которые на явный
    # think=true отвечают 400, «думать» выражается ОТСУТСТВИЕМ флага — они
    # уходят в размышление сами. Роутер при этом по-прежнему получает
    # явный False: его задача — скорость, и его такие модели принимают.
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"response": f"кое-что... {ai_flow.THINK_MARKER}"},
            {"response": "Точный ответ после раздумий"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )
    settings = Settings(llm=LlmConfig(request_timeout_s=5.0, think_style="implicit"))

    await ai_flow.request_alfred(
        message, link, store, settings, [{"role": "user", "content": "сложный вопрос"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert link.command_calls[0][1]["think"] is False  # роутер — явный False
    assert "think" not in link.command_calls[1][1]  # персонаж — флага нет вовсе


async def test_asleep_model_shows_steps_but_no_wake(store):
    # Узел доступен, модель просто спит (idle-таймер llm/service.py) — не
    # сценарий wake (магик-пакет тут ни при чём), но пользователь должен
    # увидеть «шаги», а не молча ждать до request_timeout_s.
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "Секунду, сэг"}],
        get_state_routes={"mycraft:llm": {"asleep": True}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Секунду, сэг"
    assert message.answers == [ai_flow.STEPS_TEXT]
    assert link.wol_sent == []  # узел был доступен — будить не нужно


async def test_asleep_model_shows_steps_via_rich_status_not_plain_message(store):
    # Тот же сценарий, что test_asleep_model_shows_steps_but_no_wake, но с
    # rich_session — STEPS_TEXT идёт в черновик курсивом (push_status), а не
    # отдельным message.answer (см. ai_flow.py::_announce_steps).
    message = FakeMessage()
    rich_session = FakeRichSession()
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "Секунду, сэг"}],
        get_state_routes={"mycraft:llm": {"asleep": True}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
        rich_session=rich_session,
    )

    assert raw == "Секунду, сэг"
    assert message.answers == []
    assert rich_session.statuses == [ai_flow.STEPS_TEXT_PLAIN]


async def test_asleep_warmup_fails_answers_as_albert_not_generic_error(store):
    # Прогрев не уложился (Ollama не поднялась) — раз мы уже знали, что
    # модель спит, это подаётся как «Альфред, кажется, уснул» (Альбегт),
    # а не безликое «Прошу прощения, не вышло» от самого Альфреда.
    message = FakeMessage()
    notifier = FakeNotifier()
    link = FakeNodeLink(
        chat_results=[ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева")],
        get_state_routes={"mycraft:llm": {"asleep": True}},
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
    await wake_state.remember(store, "mycraft", MYCRAFT_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            ProtoError(ERR_UNAVAILABLE, "нода недоступна"),
            {"response": ai_flow.ROUTE_OK},
            {"response": "Сейчас подойду"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Сейчас подойду"
    # Второе «шаги» — про поднятие контейнера (отдельная неопределённость
    # от самого wake); успех не добавляет отдельного сообщения персонажа.
    assert message.answers == [ai_flow.STEPS_TEXT, ai_flow.ARNOLD_WAKING, ai_flow.STEPS_TEXT]
    assert link.wol_sent == [{"mac": MYCRAFT_WAKE["mac"]}]  # разбудили молча


async def test_unavailable_then_woken_within_30s_via_rich_status(store, monkeypatch):
    # Тот же сценарий, что test_unavailable_then_woken_within_30s, но с
    # rich_session: «шаги» (до и после wake) идут эфемерными статусами в
    # черновик (push_status), а реплика Агнольда — своя персистентная
    # message.answer, как и в фолбэк-пути (живая находка 2026-08-10: Агнольд
    # отдельный персонаж, его реплика должна остаться в чате, не перекрываясь
    # ни «шагами», ни финальным ответом Альфреда).
    await wake_state.remember(store, "mycraft", MYCRAFT_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    message = FakeMessage()
    rich_session = FakeRichSession()
    link = FakeNodeLink(
        chat_results=[
            ProtoError(ERR_UNAVAILABLE, "нода недоступна"),
            {"response": ai_flow.ROUTE_OK},
            {"response": "Сейчас подойду"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
        rich_session=rich_session,
    )

    assert raw == "Сейчас подойду"
    assert message.answers == [ai_flow.ARNOLD_WAKING]
    assert rich_session.statuses == [ai_flow.STEPS_TEXT_PLAIN, ai_flow.STEPS_TEXT_PLAIN]
    assert link.wol_sent == [{"mac": MYCRAFT_WAKE["mac"]}]  # разбудили молча


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
    await wake_state.remember(store, "mycraft", MYCRAFT_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_TIMEOUT_S", 0.05)
    message = FakeMessage()
    link = FakeNodeLink(get_state_routes={})  # mycraft:llm так и не отвечает

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw is None
    assert message.answers == [ai_flow.STEPS_TEXT, ai_flow.ALBERT_UNAVAILABLE]
    assert link.wol_sent == [{"mac": MYCRAFT_WAKE["mac"]}]  # будили, но не помогло


async def test_woken_but_retry_call_still_fails(store, monkeypatch):
    await wake_state.remember(store, "mycraft", MYCRAFT_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            ProtoError(ERR_UNAVAILABLE, "нода недоступна"),
            ProtoError(ERR_UNAVAILABLE, "опять недоступна"),
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
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
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), notifier,
    )

    assert raw is None
    # Без «шагов» — это не сценарий недоступности узла. Текст пользователю —
    # в характере персонажа (Альбегт просит повторить), без утечки техники;
    # подробности — только админу.
    assert message.answers == [ai_flow.ALBERT_HICCUP]
    assert "Ollama" not in message.answers[0]
    assert link.wol_sent == []  # это не сценарий недоступности — wake не трогаем
    assert len(notifier.sent) == 1
    admin_chat_id, admin_text = notifier.sent[0]
    assert admin_chat_id == 999
    assert "internal" in admin_text
    assert "Ollama не поднялась после прогрева" in admin_text


async def test_internal_error_after_wake_answers_user_and_notifies_admin(store, monkeypatch):
    await wake_state.remember(store, "mycraft", MYCRAFT_WAKE)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    message = FakeMessage()
    notifier = FakeNotifier()
    link = FakeNodeLink(
        chat_results=[
            ProtoError(ERR_UNAVAILABLE, "нода недоступна"),
            ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева"),
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
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


# --- settings.people: сопоставление собеседника, точный возраст, местное
# время (2026-07-25) — ФИО/даты рождения намеренно НЕ в тестах/репозитории,
# только выдуманные значения ---


def _person(**overrides) -> PersonConfig:
    defaults = dict(
        telegram_username="ivan",
        telegram_id=0,
        full_name="Иван Иванович Иванов",
        gender="m",
        city="Алматы",
        timezone="Asia/Almaty",
        birth_date="",
    )
    defaults.update(overrides)
    return PersonConfig(**defaults)


def test_find_known_person_matches_by_username_case_insensitive():
    settings = Settings(people=[_person(telegram_username="Ivan")])
    user = FakeUser("Иван", username="ivan")
    assert ai_flow._find_known_person(settings, user) is not None


def test_find_known_person_matches_by_telegram_id_when_username_unset():
    settings = Settings(people=[_person(telegram_username="", telegram_id=42424242)])
    user = FakeUser("Без ника", username=None, id=42424242)
    assert ai_flow._find_known_person(settings, user) is not None


def test_find_known_person_returns_none_for_unknown_user():
    settings = Settings(people=[_person()])
    user = FakeUser("Пётр", username="petr", id=999)
    assert ai_flow._find_known_person(settings, user) is None


def test_find_known_person_returns_none_without_user():
    settings = Settings(people=[_person()])
    assert ai_flow._find_known_person(settings, None) is None


def test_person_age_computed_from_birth_date():
    person = _person(birth_date="1990-06-15")
    assert ai_flow._person_age(person, date(2026, 6, 14)) == 35  # день рождения ещё не наступил
    assert ai_flow._person_age(person, date(2026, 6, 15)) == 36  # наступил ровно сегодня
    assert ai_flow._person_age(person, date(2026, 6, 16)) == 36


def test_person_age_none_when_birth_date_unknown():
    person = _person(birth_date="")
    assert ai_flow._person_age(person, date(2026, 6, 15)) is None


def test_known_person_note_includes_local_time_and_exact_age():
    person = _person(
        gender="f", city="Москва", timezone="Europe/Moscow", birth_date="1990-06-15"
    )
    note = ai_flow._known_person_note(person)
    assert "Иван Иванович Иванов" in note  # ФИО из карточки, как задали
    assert "Москва" in note
    assert "часовой пояс" in note
    assert "лет" in note  # возраст посчитан, не None


def test_known_person_note_skips_age_line_when_birth_date_unknown():
    person = _person(birth_date="")
    note = ai_flow._known_person_note(person)
    assert "Точный возраст" not in note


async def test_context_note_time_only_without_sender(store):
    # Без отправителя (from_user=None) заметка больше не пустая — время
    # (§8.1 плана) вставляется безусловно, только сведений о собеседнике нет.
    message = NoteMessage(1, "private", None)
    note = await ai_flow._build_context_note(message, store, dialogue_id=1)
    assert "Точное время сейчас" in note
    assert "говорит" not in note


async def test_context_note_includes_alfred_own_time_always(store):
    # Живая находка 2026-07-25: личное время Альфреда (ALFRED_TIMEZONE) —
    # безусловно, не привязано к тому, узнан ли отправитель.
    message = NoteMessage(1, "private", None)
    note = await ai_flow._build_context_note(message, store, dialogue_id=1)
    assert "ТВОЁ (Альфреда) личное время" in note


async def test_context_note_warns_against_fabricating_after_tool_failure(store):
    # Живая находка 2026-08-10: get_weather дважды честно вернул отказ
    # («не удалось определить координаты»), а модель всё равно ответила
    # конкретной температурой — выдумала. Инструкция безусловная (не
    # привязана к конкретному тулу), как и предупреждение про интернет.
    message = NoteMessage(1, "private", None)
    note = await ai_flow._build_context_note(message, store, dialogue_id=1)
    assert "не повод придумать" in note


async def test_context_note_private_chat_only_mentions_sender(store):
    message = NoteMessage(1, "private", FakeUser("Иван", username="ivan"))
    note = await ai_flow._build_context_note(message, store, dialogue_id=1)
    assert note is not None
    assert "Иван (@ivan)" in note
    assert "начал" not in note
    assert "также обращались" not in note


async def test_context_note_includes_known_person_note_when_matched(store):
    settings = Settings(people=[_person(telegram_username="ivan")])
    message = NoteMessage(1, "private", FakeUser("Иван", username="ivan"))
    note = await ai_flow._build_context_note(message, store, dialogue_id=1, settings=settings)
    assert "Ты знаешь этого собеседника" in note


async def test_context_note_skips_known_person_note_without_settings(store):
    # Обратная совместимость: settings — необязательный параметр, старые
    # вызовы (без него) не должны падать и не подмешивают карточку.
    message = NoteMessage(1, "private", FakeUser("Иван", username="ivan"))
    note = await ai_flow._build_context_note(message, store, dialogue_id=1)
    assert "Ты знаешь этого собеседника" not in note


async def test_context_note_skips_known_person_note_for_unknown_sender(store):
    settings = Settings(people=[_person(telegram_username="ivan")])
    message = NoteMessage(1, "private", FakeUser("Пётр", username="petr", id=999))
    note = await ai_flow._build_context_note(message, store, dialogue_id=1, settings=settings)
    assert "Ты знаешь этого собеседника" not in note


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
    assert "чужой тред" in note  # инструкция вести себя сдержаннее в тред другого


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
    assert "чужой тред" not in note  # свой же тред — подсказка про сдержанность не нужна


# --- реплай-контекст: содержимое сообщения, на которое отвечают, и
# конкретно выделенная (quote) цитата (2026-07-24) ---


async def test_reply_context_includes_foreign_message_text(store):
    # Реплай на сообщение, не принадлежащее ни одному ходу Альфреда —
    # модель иначе его вообще не увидела бы.
    vasya = FakeUser("Вася", id=99)
    foreign = FakeRepliedMessage(777, from_user=vasya, text="Когда следующий матч?")
    ivan = FakeUser("Иван", username="ivan", id=10)

    message = NoteMessage(1, "group", ivan, reply_to_message=foreign)
    note = await ai_flow._build_context_note(message, store, dialogue_id=500)

    assert "Вася" in note
    assert "Когда следующий матч?" in note


async def test_reply_context_skips_text_already_in_current_dialogue_history(store):
    # Реплай на сообщение Альфреда из ТЕКУЩЕГО треда — текст уже есть в
    # истории, отдельно дублировать не нужно.
    ivan = FakeUser("Иван", username="ivan", id=10)
    now = datetime.now(tz=UTC)
    await store.record_ai_turn(1, 500, 500, "user", "привет", now, user_id=10, user_name="Иван")
    await store.record_ai_turn(1, 501, 500, "assistant", "Слушаю, сэр", now)

    reply_to = FakeRepliedMessage(501, text="Слушаю, сэр")
    message = NoteMessage(1, "group", ivan, reply_to_message=reply_to)
    note = await ai_flow._build_context_note(message, store, dialogue_id=500)

    assert "Сообщение, на которое сейчас отвечают" not in note


async def test_reply_context_includes_quote_even_for_own_thread(store):
    # Quote (выделенный при ответе фрагмент) — информация, которой нет в
    # истории диалога, поэтому добавляется даже для реплая в свой же тред.
    ivan = FakeUser("Иван", username="ivan", id=10)
    now = datetime.now(tz=UTC)
    await store.record_ai_turn(1, 500, 500, "user", "привет", now, user_id=10, user_name="Иван")
    await store.record_ai_turn(
        1, 501, 500, "assistant", "Я умею много всего, сэр", now
    )

    reply_to = FakeRepliedMessage(501, text="Я умею много всего, сэр")
    message = NoteMessage(
        1, "group", ivan, reply_to_message=reply_to, quote=FakeQuote("много всего")
    )
    note = await ai_flow._build_context_note(message, store, dialogue_id=500)

    assert "много всего" in note
    assert "Сообщение, на которое сейчас отвечают" not in note  # текст уже в истории


async def test_reply_context_truncates_long_foreign_message(store):
    ivan = FakeUser("Иван", username="ivan", id=10)
    long_text = "Ы" * (ai_flow._REPLY_QUOTE_MAX_CHARS + 200)
    foreign = FakeRepliedMessage(777, from_user=None, text=long_text)

    message = NoteMessage(1, "group", ivan, reply_to_message=foreign)
    note = await ai_flow._build_context_note(message, store, dialogue_id=500)

    assert "…" in note
    assert long_text not in note  # обрезано, а не вставлено целиком


async def test_no_reply_context_without_reply_to_message(store):
    ivan = FakeUser("Иван", username="ivan", id=10)
    message = NoteMessage(1, "private", ivan)
    note = await ai_flow._build_context_note(message, store, dialogue_id=500)

    assert "отвечают" not in note


async def test_reply_context_quote_is_self_contained_for_own_thread(store):
    # Живая находка 2026-07-24: старая формулировка ("в нём выделено...")
    # ссылалась на строку про "чужое сообщение", которая для своего же
    # треда как раз пропускается — "нём" повисало без антецедента, и
    # модель на практике путала процитированное слово с другим. Новая
    # формулировка должна называть источник цитаты сама, без внешних ссылок.
    ivan = FakeUser("Иван", username="ivan", id=10)
    now = datetime.now(tz=UTC)
    await store.record_ai_turn(1, 500, 500, "user", "привет", now, user_id=10, user_name="Иван")
    await store.record_ai_turn(1, 501, 500, "assistant", "не имею привычки", now)

    reply_to = FakeRepliedMessage(501, text="не имею привычки")
    message = NoteMessage(
        1, "group", ivan, reply_to_message=reply_to, quote=FakeQuote("привычки")
    )
    note = await ai_flow._build_context_note(message, store, dialogue_id=500)

    assert "твоего же предыдущего сообщения" in note.lower() or "твоего же" in note
    assert "привычки»" in note


async def test_context_note_inserted_right_before_current_turn(store):
    # Живая находка 2026-07-24: заметка вставлялась ПЕРЕД всей историей —
    # на длинном треде это слишком далеко от текущего хода, модель хуже
    # использовала её (проверено вживую: верная цитата в заметке, но не тот
    # ответ). Теперь заметка должна идти прямо перед последним (текущим)
    # сообщением истории, а не в самом начале.
    message = FakeMessage()
    message.from_user = FakeUser("Иван", username="ivan", id=10)
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )
    history = [
        {"role": "user", "content": "первый вопрос"},
        {"role": "assistant", "content": "первый ответ"},
        {"role": "user", "content": "текущий вопрос"},
    ]

    await ai_flow.request_alfred(
        message, link, store, _settings(), history, 500, _admin_book(), FakeNotifier()
    )

    sent_messages = link.command_calls[0][1]["messages"]
    # Последнее сообщение — текущий ход, перед ним — заметка о контексте
    # (никакой отдельной триаж-инструкции больше не вставляется).
    assert sent_messages[-1] == {"role": "user", "content": "текущий вопрос"}
    assert sent_messages[-2]["role"] == "system"
    assert sent_messages[:-2] == history[:-1]


# --- ActiveAiChats (живая находка 2026-07-24: редеплой бота посреди
# долгого думающего ответа обрывал /ai голой сетевой ошибкой; второй заход —
# хранит саму asyncio.Task (не просто chat_id), чтобы bot/app.py::_shutdown
# мог не только уведомить, но и по-настоящему отменить задачу до того, как
# она сама упадёт на разорванном node_link) ---


def test_active_ai_chats_starts_empty():
    assert ai_flow.ActiveAiChats().snapshot() == {}


def test_active_ai_chats_register_and_snapshot():
    chats = ai_flow.ActiveAiChats()
    task42, task7 = object(), object()
    chats.register(42, task42)
    chats.register(7, task7)
    assert chats.snapshot() == {42: task42, 7: task7}


def test_active_ai_chats_register_overwrites_same_chat():
    chats = ai_flow.ActiveAiChats()
    old_task, new_task = object(), object()
    chats.register(42, old_task)
    chats.register(42, new_task)
    assert chats.snapshot() == {42: new_task}


def test_active_ai_chats_unregister_removes():
    chats = ai_flow.ActiveAiChats()
    task = object()
    chats.register(42, task)
    chats.unregister(42, task)
    assert chats.snapshot() == {}


def test_active_ai_chats_unregister_missing_is_noop():
    chats = ai_flow.ActiveAiChats()
    chats.unregister(42, object())  # не бросает, даже если не был зарегистрирован
    assert chats.snapshot() == {}


def test_active_ai_chats_unregister_only_matching_task():
    # Если chat_id успел перерегистрироваться на новую задачу (например,
    # новый /ai подряд), unregister по СТАРОЙ задаче не должен снять новую.
    chats = ai_flow.ActiveAiChats()
    old_task, new_task = object(), object()
    chats.register(42, old_task)
    chats.register(42, new_task)
    chats.unregister(42, old_task)
    assert chats.snapshot() == {42: new_task}


def test_active_ai_chats_ignores_none():
    chats = ai_flow.ActiveAiChats()
    chats.register(None, object())
    chats.unregister(None, object())
    assert chats.snapshot() == {}


# --- «Альфред увлечённо сёрфит»: пауза на поиск не должна читаться как
# зависший бот (живой замер 2026-07-27: три минуты от вопроса до ответа) ---


def _search_call(query: str = "новости"):
    return {"function": {"name": "web_search", "arguments": {"query": query}}}


async def test_web_search_announces_surfing_once_per_request(store):
    """Вставка шлётся ОДИН раз, хотя поисков за запрос бывает несколько и
    колбэк общий для router- и персонажного прохода."""
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [_search_call("первый")]},
            {"response": ai_flow.ROUTE_OK},
            {"tool_calls": [_search_call("второй")]},
            {"response": "Вот что нашёл, сэг"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "что нового?"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert raw == "Вот что нашёл, сэг"
    assert message.answers.count(ai_flow.SURFING_TEXT) == 1
    # Оба поиска реально случились — вставка не подменяет работу тула.
    calls = await store.tool_calls_for_dialogue(message.chat.id, 1)
    assert [c["tool_name"] for c in calls] == ["web_search", "web_search"]


async def test_other_tools_do_not_announce_surfing(store):
    """calc/погода отрабатывают за миллисекунды — про сёрфинг там врать нечего."""
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [{"function": {"name": "calc", "arguments": {"expression": "2+2"}}}]},
            {"response": ai_flow.ROUTE_OK},
            {"response": "Четыге"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "2+2"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert ai_flow.SURFING_TEXT not in message.answers


# --- курсивная "бегущая строка" на каждый тул в rich-пути (2026-08-10):
# в отличие от SURFING_TEXT выше (фолбэк, один раз за запрос, только
# web_search), это для ВСЕХ тулов, без гейтинга, до выполнения тула ---


async def test_tool_status_pushed_for_every_call_when_rich_session_present(store):
    """rich-путь: статус пушится в черновик на КАЖДЫЙ вызов тула, не
    отдельным сообщением (RichStreamSession._last_sent сам дедупит
    одинаковые подряд — см. ai_flow.py::_announce_tool_start)."""
    message = FakeMessage()
    rich_session = FakeRichSession()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [_search_call("первый")]},
            {"response": ai_flow.ROUTE_OK},
            {"tool_calls": [_search_call("второй")]},
            {"response": "Вот что нашёл, сэг"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "что нового?"}], 1,
        _admin_book(), FakeNotifier(),
        rich_session=rich_session,
    )

    assert raw == "Вот что нашёл, сэг"
    assert ai_flow.SURFING_TEXT not in message.answers  # фолбэк-путь не сработал
    assert rich_session.statuses == [ai_flow.TOOL_STATUS_TEXT["web_search"]] * 2


async def test_tool_status_uses_per_tool_phrase(store):
    message = FakeMessage()
    rich_session = FakeRichSession()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [{"function": {"name": "calc", "arguments": {"expression": "2+2"}}}]},
            {"response": ai_flow.ROUTE_OK},
            {"response": "Четыге"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "2+2"}], 1,
        _admin_book(), FakeNotifier(),
        rich_session=rich_session,
    )

    assert rich_session.statuses == [ai_flow.TOOL_STATUS_TEXT["calc"]]


def test_tool_status_default_fallback_for_unmapped_tool():
    assert (
        ai_flow.TOOL_STATUS_TEXT.get("future_tool", ai_flow.TOOL_STATUS_DEFAULT)
        == ai_flow.TOOL_STATUS_DEFAULT
    )


def test_tool_status_covers_every_registered_tool():
    # Защита от будущего тула, который забудут дописать в TOOL_STATUS_TEXT —
    # не упадёт (TOOL_STATUS_DEFAULT), но лучше поймать на тесте, чем в проде.
    admin = Subscription(chat_id=1, name="admin", allowed_commands=frozenset({"*"}))
    toolkit = ai_tools.tools_for(admin)
    missing = set(toolkit.handlers) - set(ai_flow.TOOL_STATUS_TEXT)
    assert missing == set()


# --- нет права на поиск => прямое указание не выдумывать (2026-07-27) ---


def _note_msg(messages) -> str:
    """Служебная заметка — единственное system-сообщение в запросе."""
    return next(m["content"] for m in messages if m["role"] == "system")


async def test_without_search_right_model_is_told_not_to_invent(store):
    """Живая находка: без права search@net модель не видит тул вовсе и,
    не зная, что поиск бывает, отвечала про свежие события из памяти —
    выдумывая даты и факты. Молчаливое отсутствие тула её не останавливает,
    нужно сказать прямо."""
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "Не знаю, сэг"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )
    book = SubscriptionBook(
        [Subscription(chat_id=1, name="группа", allowed_commands=frozenset({"chat@llm"}))]
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(),
        [{"role": "user", "content": "что вчера запустил SpaceX?"}], 1,
        book, FakeNotifier(),
    )

    note = _note_msg(link.command_calls[0][1]["messages"])
    assert "Выхода в интернет у тебя сейчас нет" in note
    assert "НЕ придумывай" in note


async def test_with_search_right_no_such_warning(store):
    """У кого поиск есть — предупреждение только сбивало бы с толку."""
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "Извольте"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "новости?"}], 1,
        _admin_book(), FakeNotifier(),
    )

    note = _note_msg(link.command_calls[0][1]["messages"])
    assert "Выхода в интернет у тебя сейчас нет" not in note


async def test_chat_without_subscription_also_warned(store):
    """Fail-closed: подписки нет — тулов нет, значит и интернета нет."""
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "..."}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "?"}], 1,
        SubscriptionBook([]), FakeNotifier(),
    )

    assert "Выхода в интернет" in _note_msg(link.command_calls[0][1]["messages"])


# --- роспуск: «ты свободен» (тул dismiss + perform_dismissal) ---


class FakeDismissLink:
    """Двойник ServiceLink для perform_dismissal: фиксирует, кому и что
    отправили, и умеет падать на конкретном действии."""

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
    """Тул только назначает уход: пока идёт запрос, гасить нельзя — ответом
    занята ровно та модель, которую предстоит выгрузить."""
    message = FakeMessage()
    box = ai_flow.ai_tools.DismissalBox()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [{"function": {"name": "dismiss", "arguments": {"mode": "off"}}}]},
            {"response": ai_flow.ROUTE_OK},
            {"response": "Всего доброго, сэг"},
        ],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    raw = await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "спасибо, свободен"}], 1,
        _admin_book(), FakeNotifier(), box,
    )

    assert raw == "Всего доброго, сэг"
    assert box.mode == "off"
    # Ни одной power-команды за время запроса — только сами chat-раунды.
    assert {call[0] for call in link.command_calls} == {"chat"}


async def test_perform_dismissal_off_unloads_model_then_powers_machine_off(store):
    message = FakeMessage()
    link = FakeDismissLink()

    await ai_flow.perform_dismissal(
        message, link, ai_flow.ai_tools.DISMISS_OFF, _admin_book(), FakeNotifier()
    )

    assert link.calls == [
        # quiet=True — прощание уже сказано, llm_idle_sleep поверх него был бы
        # прямым противоречием («не дождался обращения»).
        ("sleep", {"quiet": True}, "mycraft", "llm"),
        ("poweroff", {}, "mycraft", "node"),
    ]
    assert message.answers == [ai_flow.DISMISS_TEXTS[ai_flow.ai_tools.DISMISS_OFF]]


async def test_perform_dismissal_sleep_suspends_instead_of_powering_off(store):
    message = FakeMessage()
    link = FakeDismissLink()

    await ai_flow.perform_dismissal(
        message, link, ai_flow.ai_tools.DISMISS_SLEEP, _admin_book(), FakeNotifier()
    )

    assert [c[0] for c in link.calls] == ["sleep", "suspend"]
    assert message.answers == [ai_flow.DISMISS_TEXTS[ai_flow.ai_tools.DISMISS_SLEEP]]


async def test_perform_dismissal_model_only_leaves_the_machine_alone(store):
    message = FakeMessage()
    link = FakeDismissLink()

    await ai_flow.perform_dismissal(
        message, link, ai_flow.ai_tools.DISMISS_MODEL, _admin_book(), FakeNotifier()
    )

    assert [c[0] for c in link.calls] == ["sleep"]
    assert message.answers == [ai_flow.DISMISS_TEXTS[ai_flow.ai_tools.DISMISS_MODEL]]


async def test_perform_dismissal_powers_off_even_if_model_did_not_unload(store):
    """Невыгруженная модель погаснет вместе с машиной — не повод отменять
    выключение, которое человек уже попросил и получил на него прощание."""
    message = FakeMessage()
    link = FakeDismissLink(fail_on="sleep")

    await ai_flow.perform_dismissal(
        message, link, ai_flow.ai_tools.DISMISS_OFF, _admin_book(), FakeNotifier()
    )

    assert [c[0] for c in link.calls] == ["sleep", "poweroff"]
    assert message.answers == [ai_flow.DISMISS_TEXTS[ai_flow.ai_tools.DISMISS_OFF]]


async def test_perform_dismissal_reports_when_machine_stayed_up(store):
    message = FakeMessage()
    notifier = FakeNotifier()
    link = FakeDismissLink(fail_on="poweroff")

    await ai_flow.perform_dismissal(
        message, link, ai_flow.ai_tools.DISMISS_OFF, _admin_book(), notifier
    )

    assert message.answers == [ai_flow.DISMISS_FAILED_TEXT]
    assert notifier.sent and "poweroff" in notifier.sent[0][1]


async def test_perform_dismissal_model_only_failure_is_reported(store):
    """Для mode=model выгрузка модели — единственное, что было обещано."""
    message = FakeMessage()
    notifier = FakeNotifier()
    link = FakeDismissLink(fail_on="sleep")

    await ai_flow.perform_dismissal(
        message, link, ai_flow.ai_tools.DISMISS_MODEL, _admin_book(), notifier
    )

    assert message.answers == [ai_flow.DISMISS_FAILED_TEXT]
    assert notifier.sent and "llm.sleep" in notifier.sent[0][1]


# --- долгая память: факты подмешиваются сами, до ответа ---


async def test_memory_facts_land_in_the_context_note(store):
    """На тул надежда плохая — модель зовёт его далеко не всегда, поэтому
    подходящее подмешивается в заметку само."""
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "Как скажете"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
        memory_facts=[
            {"text": "Качаем в /mnt/data/pr", "sensitive": False},
            {"text": "Код от домофона 1234", "sensitive": True},
        ],
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "скачай кино"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert link.recall_calls[0]["chat_id"] == 1
    note = _note_msg(link.command_calls[0][1]["messages"])
    assert "Качаем в /mnt/data/pr" in note
    # Личное уходит с оговоркой: видимость уже ограничена запросом к службе,
    # а это — просьба не произносить вслух без повода.
    assert "Код от домофона 1234 [личное: знай, но вслух не пересказывай]" in note


async def test_silent_memory_leaves_the_note_as_it_was(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "…"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert "Из того, что ты помнишь" not in _note_msg(link.command_calls[0][1]["messages"])


async def test_broken_memory_does_not_break_the_conversation(store):
    """Память необязательна: её сбой не должен ронять разговор."""
    facts = await ai_flow.recall_facts(
        FakeNodeLink(chat_results=[]), 1, "привет"
    )
    assert facts == []


async def test_recall_facts_forwards_guest_family_flag():
    link = FakeNodeLink(chat_results=[])
    await ai_flow.recall_facts(link, 1, "текст", guest_family=True)
    assert link.recall_calls[0]["guest_family"] is True

    await ai_flow.recall_facts(link, 1, "текст")
    assert link.recall_calls[1]["guest_family"] is False


async def test_ask_passes_guest_family_from_subscription(store):
    """Флаг «семья» гостя (Subscription.family, /guests) должен доехать до
    memory тем же путём, что и chat_id — бот его проставляет, не модель."""
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )
    book = SubscriptionBook(
        [
            Subscription(
                chat_id=1, name="speaker", allowed_commands=frozenset({"chat@llm"}), family=True
            ),
        ]
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        book, FakeNotifier(),
    )

    assert link.recall_calls[0]["guest_family"] is True


# --- typing keep-alive: индикатор "печатает" должен гореть только пока
# модель реально готовит ответ (решение пользователя 2026-08-01) — не с
# момента приёма сообщения человеком, и не во время presence-проверки/
# прогрева контейнера (тем этапам хватает своих текстовых "шагов") ---


async def test_typing_fires_for_the_chat_around_the_actual_ask(store):
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "Добгый день, сэ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert message.bot.typing_chats == [message.chat.id]
    assert message.bot.typing_threads == [None]  # вне топика — без message_thread_id


async def test_typing_uses_the_topic_thread_id(store):
    message = FakeMessage()
    message.message_thread_id = 777
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert message.bot.typing_threads == [777]


async def test_typing_does_not_fire_while_node_unreachable_and_no_wake_data(store, monkeypatch):
    # get_state_routes пуст — presence-проверка сама уже "недоступна", до
    # настоящей генерации дело не доходит (см. test_unavailable_and_no_wake_
    # data_gives_up_immediately) — значит и typing загораться не должен.
    monkeypatch.setattr(ai_flow, "WAKE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ai_flow, "WAKE_POLL_TIMEOUT_S", 0.05)
    message = FakeMessage()
    link = FakeNodeLink()

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert message.bot.typing_chats == []


async def test_typing_does_not_fire_during_asleep_warmup_before_the_retry(store):
    # Модель спит (см. test_asleep_model_shows_steps_but_no_wake) — "шаги"
    # уже сказаны текстом, до typing на этом этапе доходить не должно; он
    # загорится только внутри самого _ask(), который ниже успешно отвечает.
    message = FakeMessage()
    link = FakeNodeLink(
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "Секунду, сэг"}],
        get_state_routes={"mycraft:llm": {"asleep": True}},
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    assert message.bot.typing_chats == [message.chat.id]  # ровно вокруг _ask(), не раньше


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
        chat_results=[{"response": ai_flow.ROUTE_OK}, {"response": "ответ"}],
        get_state_routes={"mycraft:llm": {"asleep": False}},
    )

    await ai_flow.request_alfred(
        message, link, store, _settings(), [{"role": "user", "content": "привет"}], 1,
        _admin_book(), FakeNotifier(),
    )

    # Два раунда "chat" по 0.05с каждый на интервале в 0.01с — keep-alive
    # обязан повториться хотя бы раз, не только зажечься единожды.
    assert len(message.bot.typing_chats) > 1
    assert set(message.bot.typing_chats) == {message.chat.id}
