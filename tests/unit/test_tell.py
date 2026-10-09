"""Тул tell: Альфред передаёт человеку личное сообщение (этап 28)."""

from __future__ import annotations

import pytest

from sa_home_bot.bot import recipients
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.config import (
    GuestSubscriptionConfig,
    PersonConfig,
    Settings,
    SubscriptionConfig,
)
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol as task_protocol

ANDREY_CHAT = 555
GROUP_CHAT = -100500


def _book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="me", chat_id=1, allowed_commands=["*"]),
            SubscriptionConfig(name="family_room", chat_id=GROUP_CHAT, allowed_commands=[]),
        ],
        [
            GuestSubscriptionConfig(
                name="Андрей Иванов (@andrey)",
                chat_id=ANDREY_CHAT,
                allowed_commands=["chat@llm"],
                invited_user="Андрей Иванов (@andrey)",
            )
        ],
    )


def _people() -> list[PersonConfig]:
    return [
        PersonConfig(
            telegram_username="andrey",
            telegram_id=ANDREY_CHAT,
            full_name="Андрей Иванов",
            gender="m",
        ),
        # Известен конфигу, но подписки нет — писать ему нельзя.
        PersonConfig(
            telegram_username="stranger",
            telegram_id=4242,
            full_name="Пётр Незнакомый",
            gender="m",
        ),
    ]


class FakeNotifier:
    def __init__(self, message_id: int | None = 777) -> None:
        self.sent: list[tuple[int, str]] = []
        self._message_id = message_id

    async def send_direct(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))
        return self._message_id


class FakeStore:
    # По умолчанию владелец (chat 1) знаком с Андреем: механика доставки
    # (лимиты, запись хода, мост tasks) проверяется на законной паре; права —
    # в разделе «этап 36/46» ниже, на _permission_book.
    def __init__(self, acquainted: tuple[tuple[int, int], ...] = ((1, ANDREY_CHAT),)) -> None:
        self.turns: list[tuple] = []
        # Подтверждённые знакомства (Этап 46) — пары chat_id.
        self._acquainted = acquainted

    async def relationships_for(self, chat_id, status="confirmed"):
        return [
            {"guest_a": a, "guest_b": b, "relation": "acquaintance"}
            for a, b in self._acquainted
            if chat_id in (a, b)
        ]

    async def record_ai_turn(self, chat_id, message_id, dialogue_id, role, content, at, **kw):
        self.turns.append((chat_id, message_id, dialogue_id, role, content))


class FakeEmit:
    """Мост службы tasks (ctx.emit) — вместо настоящего notifier собирает
    события EVENT_DELIVER_MESSAGE, которые в проде уходят боту (см.
    bot/node_events.py::_handle_deliver_message)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


def _ctx(**over):
    defaults = dict(
        chat_id=1,
        dialogue_id=10,
        trigger_message_id=10,
        settings=Settings(people=_people()),
        book=_book(),
        notifier=FakeNotifier(),
        store=FakeStore(),
        author="Алексей (@sevboa)",
    )
    defaults.update(over)
    # В реальном потоке (bot/ai_flow.py) ctx.subscription — это подписка
    # САМОГО звонящего чата, не получателя; здесь по умолчанию берём её из
    # book по chat_id, как и наяву, если тест не передал subscription сам.
    defaults.setdefault(
        "subscription",
        defaults["book"].for_chat(defaults["chat_id"]) if defaults.get("book") else None,
    )
    return ai_tools.ToolContext(**defaults)


# --- резолвер получателей ------------------------------------------------


@pytest.mark.parametrize("query", ["Андрей", "андрей", "@andrey", "andrey", "Андрей Иванов"])
def test_recipient_found_by_name_and_username(query):
    found = recipients.find_recipients(query, _book(), _people())
    assert [r.chat_id for r in found] == [ANDREY_CHAT]


def test_person_without_subscription_is_not_a_recipient():
    """Знать человека и иметь право ему написать — разные вещи: право даёт
    приглашение, а не запись в [[people]]."""
    assert recipients.find_recipients("Пётр", _book(), _people()) == []


def test_group_chat_is_never_a_recipient():
    """«Передать лично» в группу — это не лично."""
    assert recipients.find_recipients("family_room", _book(), _people()) == []


def test_unknown_name_finds_nobody():
    assert recipients.find_recipients("Никодим", _book(), _people()) == []


def test_partial_word_does_not_match_by_middle():
    # «дрей» — это середина слова, такому совпадению доверять нельзя.
    assert recipients.find_recipients("дрей", _book(), _people()) == []


def test_ambiguity_returns_all_candidates():
    book = SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="Андрей Иванов", chat_id=11, allowed_commands=["chat@llm"]),
            SubscriptionConfig(name="Андрей Петров", chat_id=12, allowed_commands=["chat@llm"]),
        ]
    )
    found = recipients.find_recipients("Андрей", book, [])
    assert sorted(r.chat_id for r in found) == [11, 12]


# --- сам тул -------------------------------------------------------------


async def test_tell_delivers_message_and_records_turn():
    notifier = FakeNotifier()
    store = FakeStore()
    ctx = _ctx(notifier=notifier, store=store)

    result = await ai_tools.tool_tell(ctx, {"recipient_id": ANDREY_CHAT, "text": "Ужин в семь"})

    assert "передано" in result
    chat_id, text = notifier.sent[0]
    assert chat_id == ANDREY_CHAT
    assert "Ужин в семь" in text
    assert "Алексей" in text  # видно, по чьей просьбе
    # Доставленное — ход диалога получателя: он сможет ответить реплаем.
    assert store.turns == [(ANDREY_CHAT, 777, 777, "assistant", "Ужин в семь")]


async def test_tell_refuses_unknown_recipient():
    notifier = FakeNotifier()
    result = await ai_tools.tool_tell(
        _ctx(notifier=notifier), {"recipient_id": 9999, "text": "привет"}
    )
    assert "не получилось" in result
    assert notifier.sent == []


async def test_find_person_asks_to_disambiguate():
    book = SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="Андрей Иванов", chat_id=11, allowed_commands=["chat@llm"]),
            SubscriptionConfig(name="Андрей Петров", chat_id=12, allowed_commands=["chat@llm"]),
        ]
    )
    ctx = _ctx(book=book, settings=Settings(), store=FakeStore(((1, 11), (1, 12))))
    result = await ai_tools.tool_find_person(ctx, {"description": "Андрею"})
    assert "переспроси" in result
    assert "id 11" in result and "id 12" in result


async def test_tell_does_not_write_into_the_same_chat():
    notifier = FakeNotifier()
    result = await ai_tools.tool_tell(
        _ctx(chat_id=ANDREY_CHAT, notifier=notifier),
        {"recipient_id": ANDREY_CHAT, "text": "привет"},
    )
    assert "не нужно" in result
    assert notifier.sent == []


async def test_tell_reports_failed_delivery():
    notifier = FakeNotifier(message_id=None)  # бот заблокирован получателем
    store = FakeStore()
    result = await ai_tools.tool_tell(
        _ctx(notifier=notifier, store=store), {"recipient_id": ANDREY_CHAT, "text": "привет"}
    )
    assert "не дошло" in result
    assert store.turns == []


async def test_tell_unavailable_without_notifier():
    """У службы tasks нет ни книги подписок, ни отправителя — тул не
    отказывает, а честно не умеет (то же, что dismiss без dismissal)."""
    ctx = ai_tools.ToolContext(chat_id=1, dialogue_id=1, trigger_message_id=1, settings=Settings())
    result = await ai_tools.tool_tell(ctx, {"recipient_id": ANDREY_CHAT, "text": "привет"})
    assert result.startswith("недоступно")


async def test_tell_delivers_via_emit_bridge_when_no_notifier(monkeypatch):
    """Служба tasks (self-scheduled remind, живая находка 2026-08-06): своей
    книги подписок и моста хватает на резолвинг + доставку, даже без
    настоящего notifier — тул просит бота отправить через
    EVENT_DELIVER_MESSAGE, а не честно отказывает.

    Свои лимитеры (см. test_tell_stops_at_the_hourly_limit) — общие
    ``_tell_limiter``/``_tell_broadcast_limiter`` за счёт множества других
    тестов этого файла, шлющих с chat_id=1, к этому месту почти наверняка
    исчерпаны, а формулировка отказа по получателю сама содержит слово
    "получил" и могла бы ложно пройти наивную проверку."""
    from sa_home_bot.bot import invites

    monkeypatch.setattr(
        ai_tools, "_tell_limiter", invites.AttemptLimiter(ai_tools.TELL_MAX_PER_RECIPIENT_PER_HOUR)
    )
    monkeypatch.setattr(
        ai_tools,
        "_tell_broadcast_limiter",
        invites.AttemptLimiter(ai_tools.TELL_MAX_TOTAL_PER_HOUR),
    )
    emit = FakeEmit()
    ctx = _ctx(notifier=None, store=None, emit=emit)

    result = await ai_tools.tool_tell(ctx, {"recipient_id": ANDREY_CHAT, "text": "Ужин в семь"})

    assert len(emit.events) == 1
    event_type, data = emit.events[0]
    assert event_type == task_protocol.EVENT_DELIVER_MESSAGE
    assert "передано" in result
    assert data["chat_id"] == ANDREY_CHAT
    assert "Ужин в семь" in data["html"]
    assert data["plain"] == "Ужин в семь"
    # Store в tasks нет — знакомство проверит бот перед отправкой.
    assert data["require_acquaintance"] == [1, ANDREY_CHAT]


async def test_tell_still_unavailable_without_book_even_with_emit():
    ctx = ai_tools.ToolContext(
        chat_id=1, dialogue_id=1, trigger_message_id=1, settings=Settings(), emit=FakeEmit()
    )
    result = await ai_tools.tool_tell(ctx, {"recipient_id": ANDREY_CHAT, "text": "привет"})
    assert result.startswith("недоступно")


async def test_tell_needs_both_arguments():
    ctx = _ctx()
    assert "find_person" in await ai_tools.tool_tell(ctx, {"recipient": "Андрей", "text": "x"})
    assert "ошибка" in await ai_tools.tool_tell(ctx, {"recipient_id": ANDREY_CHAT, "text": " "})


async def test_tell_stops_at_the_hourly_limit(monkeypatch):
    from sa_home_bot.bot import invites

    monkeypatch.setattr(ai_tools, "_tell_limiter", invites.AttemptLimiter(2))
    monkeypatch.setattr(
        ai_tools,
        "_tell_broadcast_limiter",
        invites.AttemptLimiter(ai_tools.TELL_MAX_TOTAL_PER_HOUR),
    )
    notifier = FakeNotifier()
    ctx = _ctx(notifier=notifier)
    for _ in range(2):
        assert "передано" in await ai_tools.tool_tell(
            ctx, {"recipient_id": ANDREY_CHAT, "text": "привет"}
        )
    result = await ai_tools.tool_tell(ctx, {"recipient_id": ANDREY_CHAT, "text": "привет"})
    assert "не сейчас" in result
    assert "Андрей Иванов" in result  # отказ по КОНКРЕТНОМУ получателю, не общий
    assert len(notifier.sent) == 2


async def test_tell_broadcast_to_many_recipients_not_blocked_by_per_recipient_limit(monkeypatch):
    # Живой инцидент 2026-08-06: рассылка ОДНОГО уведомления 10 РАЗНЫМ
    # гостям исчерпывала прежний единый лимит на автора (10/час) целиком —
    # хотя каждый получил ровно одно сообщение, что не спам. Теперь потолок
    # на получателя (2 в этом тесте) не мешает рассылке разным людям.
    from sa_home_bot.bot import invites

    monkeypatch.setattr(ai_tools, "_tell_limiter", invites.AttemptLimiter(2))
    monkeypatch.setattr(
        ai_tools,
        "_tell_broadcast_limiter",
        invites.AttemptLimiter(ai_tools.TELL_MAX_TOTAL_PER_HOUR),
    )
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=1, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name=f"Гость{i}",
                chat_id=1000 + i,
                allowed_commands=["chat@llm"],
                invited_user=f"Гость{i}",
            )
            for i in range(5)
        ],
    )
    notifier = FakeNotifier()
    store = FakeStore(acquainted=tuple((1, 1000 + i) for i in range(5)))
    ctx = _ctx(book=book, notifier=notifier, store=store, settings=Settings())
    for i in range(5):
        result = await ai_tools.tool_tell(ctx, {"recipient_id": 1000 + i, "text": "привет"})
        assert "передано" in result
    assert len(notifier.sent) == 5


async def test_tell_broadcast_total_limit_stops_mass_send(monkeypatch):
    # Общий потолок рассылки — страховка от по-настоящему массовой рассылки
    # (например, если гостей окажется больше сотни), даже если по получателю
    # лимит не тронут.
    from sa_home_bot.bot import invites

    monkeypatch.setattr(
        ai_tools, "_tell_limiter", invites.AttemptLimiter(ai_tools.TELL_MAX_PER_RECIPIENT_PER_HOUR)
    )
    monkeypatch.setattr(ai_tools, "_tell_broadcast_limiter", invites.AttemptLimiter(2))
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=1, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name=f"Гость{i}",
                chat_id=1000 + i,
                allowed_commands=["chat@llm"],
                invited_user=f"Гость{i}",
            )
            for i in range(3)
        ],
    )
    notifier = FakeNotifier()
    store = FakeStore(acquainted=tuple((1, 1000 + i) for i in range(3)))
    ctx = _ctx(book=book, notifier=notifier, store=store, settings=Settings())
    for i in range(2):
        assert "передано" in await ai_tools.tool_tell(
            ctx, {"recipient_id": 1000 + i, "text": "привет"}
        )
    result = await ai_tools.tool_tell(ctx, {"recipient_id": 1002, "text": "привет"})
    assert "не сейчас" in result
    assert "общий лимит" in result
    assert len(notifier.sent) == 2


# --- права ---------------------------------------------------------------


def test_tell_is_declared_only_with_its_right():
    admin = _book().for_chat(1)
    guest = _book().for_chat(ANDREY_CHAT)
    assert "tell" in ai_tools.tools_for(admin).handlers
    # Гость по умолчанию (chat@llm) писать другим не может.
    assert "tell" not in ai_tools.tools_for(guest).handlers
    assert "tell" not in ai_tools.tools_for(None).handlers


def test_tell_right_can_be_granted_explicitly():
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="x", chat_id=9, allowed_commands=["chat@llm", "tell@llm"])]
    )
    assert "tell" in ai_tools.tools_for(book.for_chat(9)).handlers


def test_rendered_message_escapes_html():
    rendered = ai_tools.render_tell("<b>жирный</b>", "Алексей & Co")
    assert "&lt;b&gt;" in rendered
    assert "Алексей &amp; Co" in rendered


# --- этап 36: владелец всегда доступен; этап 46: знакомые — друг другу ----

OWNER_CHAT = 1
PLAIN_GUEST_CHAT = 600  # tell@llm — может писать только владельцу
FAMILY_A_CHAT = 601  # tell@llm, знаком с Б (в тестах, где передан store)
FAMILY_B_CHAT = 602  # tell@llm
PRIVILEGED_GUEST_CHAT = 603  # tell@llm + упразднённое tell_guests@llm


def _permission_book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="owner", chat_id=OWNER_CHAT, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name="Гость Плоский",
                chat_id=PLAIN_GUEST_CHAT,
                allowed_commands=["chat@llm", "tell@llm"],
                invited_user="Гость Плоский",
            ),
            GuestSubscriptionConfig(
                name="Семья А",
                chat_id=FAMILY_A_CHAT,
                allowed_commands=["chat@llm", "tell@llm"],
                invited_user="Семья А",
            ),
            GuestSubscriptionConfig(
                name="Семья Б",
                chat_id=FAMILY_B_CHAT,
                allowed_commands=["chat@llm", "tell@llm"],
                invited_user="Семья Б",
            ),
            GuestSubscriptionConfig(
                name="Привилегированный",
                chat_id=PRIVILEGED_GUEST_CHAT,
                allowed_commands=["chat@llm", "tell@llm", "tell_guests@llm"],
                invited_user="Привилегированный",
            ),
        ],
    )


async def test_tell_guest_always_reaches_owner_without_tell_guests_right():
    book = _permission_book()
    notifier = FakeNotifier()
    ctx = _ctx(chat_id=PLAIN_GUEST_CHAT, book=book, notifier=notifier, settings=Settings())
    result = await ai_tools.tool_tell(
        ctx, {"recipient_id": OWNER_CHAT, "to_owner_role": True, "text": "привет"}
    )
    assert "передано" in result
    assert notifier.sent[0][0] == OWNER_CHAT


async def test_tell_guest_cannot_reach_another_guest_without_right_or_acquaintance():
    book = _permission_book()
    notifier = FakeNotifier()
    ctx = _ctx(chat_id=PLAIN_GUEST_CHAT, book=book, notifier=notifier, settings=Settings())
    result = await ai_tools.tool_tell(ctx, {"recipient_id": FAMILY_A_CHAT, "text": "привет"})
    assert "не умею" in result
    assert "не знакомы" in result
    assert f"request_acquaintance(recipient_id={FAMILY_A_CHAT})" in result
    assert notifier.sent == []


@pytest.mark.parametrize(
    ("sender", "target"),
    [(FAMILY_A_CHAT, FAMILY_B_CHAT), (FAMILY_B_CHAT, FAMILY_A_CHAT)],
)
async def test_tell_acquaintances_reach_each_other_both_ways(sender, target):
    """Этап 46: подтверждённое знакомство открывает tell в обе стороны без
    tell_guests@llm, независимо от того, кто предлагал (guest_a/guest_b)."""
    book = _permission_book()
    notifier = FakeNotifier()
    store = FakeStore(acquainted=((FAMILY_A_CHAT, FAMILY_B_CHAT),))
    ctx = _ctx(chat_id=sender, book=book, notifier=notifier, store=store, settings=Settings())
    result = await ai_tools.tool_tell(ctx, {"recipient_id": target, "text": "привет"})
    assert "передано" in result
    assert notifier.sent[0][0] == target


async def test_tell_acquaintance_opens_only_that_pair():
    """Знакомство — точечное: знакомый с Б не может писать третьему гостю."""
    book = _permission_book()
    notifier = FakeNotifier()
    store = FakeStore(acquainted=((FAMILY_A_CHAT, FAMILY_B_CHAT),))
    ctx = _ctx(
        chat_id=FAMILY_A_CHAT, book=book, notifier=notifier, store=store, settings=Settings()
    )
    result = await ai_tools.tool_tell(ctx, {"recipient_id": PLAIN_GUEST_CHAT, "text": "привет"})
    assert "не умею" in result
    assert notifier.sent == []


async def test_tell_without_store_refuses_non_acquainted_guest():
    """В службе tasks Store нет — знакомство не проверить, честный отказ."""
    book = _permission_book()
    notifier = FakeNotifier()
    ctx = _ctx(chat_id=FAMILY_A_CHAT, book=book, notifier=notifier, store=None, settings=Settings())
    result = await ai_tools.tool_tell(ctx, {"recipient_id": FAMILY_B_CHAT, "text": "привет"})
    assert "не умею" in result


async def test_retired_tell_guests_right_no_longer_opens_strangers():
    """Этап 46 (решение 2026-09-27): обходов знакомства больше нет —
    tell_guests@llm упразднено и снимается с гостя при загрузке пакета."""
    book = _permission_book()
    assert "tell_guests@llm" not in book.for_chat(PRIVILEGED_GUEST_CHAT).allowed_commands
    notifier = FakeNotifier()
    ctx = _ctx(
        chat_id=PRIVILEGED_GUEST_CHAT,
        book=book,
        notifier=notifier,
        store=FakeStore(acquainted=()),
        settings=Settings(),
    )
    result = await ai_tools.tool_tell(ctx, {"recipient_id": PLAIN_GUEST_CHAT, "text": "привет"})
    assert "не знакомы" in result
    assert notifier.sent == []


async def test_owner_needs_acquaintance_too_and_is_pointed_to_notify_guest():
    """Владелец лично (tell) — такой же гость: незнакомому не передаёт, но
    получает подсказку про официальный notify_guest."""
    book = _permission_book()
    notifier = FakeNotifier()
    ctx = _ctx(
        chat_id=OWNER_CHAT,
        book=book,
        notifier=notifier,
        store=FakeStore(acquainted=()),
        settings=Settings(),
    )
    result = await ai_tools.tool_tell(ctx, {"recipient_id": PLAIN_GUEST_CHAT, "text": "привет"})
    assert "не знакомы" in result and "notify_guest" in result
    assert notifier.sent == []

    ctx = _ctx(
        chat_id=OWNER_CHAT,
        book=book,
        notifier=notifier,
        store=FakeStore(acquainted=((PLAIN_GUEST_CHAT, OWNER_CHAT),)),
        settings=Settings(),
    )
    assert "передано" in await ai_tools.tool_tell(
        ctx, {"recipient_id": PLAIN_GUEST_CHAT, "text": "привет"}
    )


async def test_any_guest_reaches_owner_without_acquaintance():
    book = _permission_book()
    notifier = FakeNotifier()
    ctx = _ctx(
        chat_id=PRIVILEGED_GUEST_CHAT,
        book=book,
        notifier=notifier,
        store=FakeStore(acquainted=()),
        settings=Settings(),
    )
    result = await ai_tools.tool_tell(
        ctx, {"recipient_id": OWNER_CHAT, "to_owner_role": True, "text": "привет"}
    )
    assert "передано" in result


# --- обращение к владельцу по роли, а не по личному имени -----------------
#
# _book() (в отличие от _permission_book()) называет владельческую подписку
# технически "me" — как в прод-конфиге (instances/telegram-bot.alfred.toml),
# а не "owner" — чтобы не подловить случайное совпадение имени со словом
# роли (см. AUTHORIZATION.md, находка "владелец без гостевого имени").


async def test_tell_reaches_owner_by_role_word_not_name():
    notifier = FakeNotifier()
    ctx = _ctx(chat_id=ANDREY_CHAT, book=_book(), notifier=notifier, settings=Settings())
    result = await ai_tools.tool_tell(
        ctx, {"recipient_id": OWNER_CHAT, "to_owner_role": True, "text": "привет"}
    )
    assert "передано" in result
    chat_id, text = notifier.sent[0]
    assert chat_id == 1  # chat_id владельца в _book()
    assert "к вам как к владельцу" in text


async def test_tell_by_personal_name_has_no_owner_role_marker():
    notifier = FakeNotifier()
    people = [
        PersonConfig(
            telegram_username="asevbo",
            telegram_id=1,
            full_name="Алексей Александрович Севбо",
            gender="m",
        )
    ]
    ctx = _ctx(
        chat_id=ANDREY_CHAT,
        book=_book(),
        notifier=notifier,
        settings=Settings(people=people),
    )
    result = await ai_tools.tool_tell(ctx, {"recipient_id": OWNER_CHAT, "text": "привет"})
    assert "передано" in result
    chat_id, text = notifier.sent[0]
    assert chat_id == 1
    assert "к вам как к владельцу" not in text


# --- notify_persona/notify_guest: официальное уведомление владельца гостю --


async def test_notify_tools_are_declared_only_for_owner():
    owner = _book().for_chat(1)
    guest = _book().for_chat(ANDREY_CHAT)
    assert "notify_persona" in ai_tools.tools_for(owner).handlers
    assert "notify_guest" in ai_tools.tools_for(owner).handlers
    assert "notify_persona" not in ai_tools.tools_for(guest).handlers
    assert "notify_guest" not in ai_tools.tools_for(guest).handlers
    assert "notify_persona" not in ai_tools.tools_for(None).handlers
    assert "notify_guest" not in ai_tools.tools_for(None).handlers


async def test_notify_persona_returns_one_of_the_known_personas(monkeypatch):
    picked = ai_tools._NOTIFY_PERSONAS[-1]
    monkeypatch.setattr(ai_tools.random, "choice", lambda seq: picked)
    ctx = _ctx(chat_id=1, book=_book(), settings=Settings())
    result = await ai_tools.tool_notify_persona(ctx, {})
    assert picked["title"] in result
    assert picked["style_prompt"] in result


async def test_notify_guest_delivers_without_on_behalf_of_marker():
    notifier = FakeNotifier()
    ctx = _ctx(chat_id=1, book=_book(), notifier=notifier, settings=Settings())
    result = await ai_tools.tool_notify_guest(
        ctx, {"recipient_id": ANDREY_CHAT, "persona": "Хозяин", "text": "новое правило дома"}
    )
    assert "передано" in result
    chat_id, text = notifier.sent[0]
    assert chat_id == ANDREY_CHAT
    assert "по просьбе" not in text
    assert "новое правило дома" in text
    assert "Хозяин" in text


async def test_notify_guest_uses_exactly_the_given_persona():
    for persona in ai_tools._NOTIFY_PERSONAS:
        notifier = FakeNotifier()
        ctx = _ctx(chat_id=1, book=_book(), notifier=notifier, settings=Settings())
        await ai_tools.tool_notify_guest(
            ctx, {"recipient_id": ANDREY_CHAT, "persona": persona["title"], "text": "х"}
        )
        chat_id, text = notifier.sent[0]
        assert persona["title"] in text
        assert persona["emoji"] in text


async def test_notify_guest_tolerates_emoji_stuck_to_persona_name():
    # Живой баг 2026-08-05: модель скопировала persona вместе с эмодзи из
    # ответа notify_persona ("Админ 🤓" вместо "Админ") — точное совпадение
    # по словарю такое не находило и отказывало, хотя notify_persona был
    # вызван как положено.
    notifier = FakeNotifier()
    ctx = _ctx(chat_id=1, book=_book(), notifier=notifier, settings=Settings())
    result = await ai_tools.tool_notify_guest(
        ctx, {"recipient_id": ANDREY_CHAT, "persona": "Админ 🤓", "text": "х"}
    )
    assert "передано" in result
    chat_id, text = notifier.sent[0]
    assert "Админ" in text


async def test_notify_guest_can_target_the_owner_themselves():
    # В отличие от tell, у notify_guest есть смысл слать себе (self-напоминание
    # в стиле персонажа, например через remind) — "тот же чат" не отказ.
    notifier = FakeNotifier()
    ctx = _ctx(chat_id=1, book=_book(), notifier=notifier, settings=Settings())
    result = await ai_tools.tool_notify_guest(
        ctx, {"recipient_id": 1, "persona": "Граф", "text": "пора кушать"}
    )
    assert "передано" in result
    chat_id, text = notifier.sent[0]
    assert chat_id == 1
    assert "Граф" in text


async def test_notify_guest_refuses_unknown_persona():
    notifier = FakeNotifier()
    ctx = _ctx(chat_id=1, book=_book(), notifier=notifier, settings=Settings())
    result = await ai_tools.tool_notify_guest(
        ctx, {"recipient_id": ANDREY_CHAT, "persona": "Дворецкий", "text": "х"}
    )
    assert "ошибка" in result
    assert "notify_persona" in result
    assert notifier.sent == []


async def test_notify_guest_requires_persona():
    notifier = FakeNotifier()
    ctx = _ctx(chat_id=1, book=_book(), notifier=notifier, settings=Settings())
    result = await ai_tools.tool_notify_guest(ctx, {"recipient_id": ANDREY_CHAT, "text": "х"})
    assert "ошибка" in result
    assert notifier.sent == []


# --- notify_guest из self-scheduled remind (служба tasks) — живой баг
# 2026-08-06: утренний remind звал notify_persona (работал — своих
# зависимостей нет), а notify_guest падал в "недоступно: сейчас я не могу
# никому написать", хотя владелец просил себя же уведомить в стиле
# персонажа. Мост (ctx.emit) чинит именно этот сценарий.


async def test_notify_guest_delivers_via_emit_bridge_when_no_notifier(monkeypatch):
    # Те же общие лимитеры, что и у теста tell чуть выше — сбрасываем по
    # той же причине (см. его докстринг).
    from sa_home_bot.bot import invites

    monkeypatch.setattr(
        ai_tools, "_tell_limiter", invites.AttemptLimiter(ai_tools.TELL_MAX_PER_RECIPIENT_PER_HOUR)
    )
    monkeypatch.setattr(
        ai_tools,
        "_tell_broadcast_limiter",
        invites.AttemptLimiter(ai_tools.TELL_MAX_TOTAL_PER_HOUR),
    )
    emit = FakeEmit()
    ctx = _ctx(chat_id=1, book=_book(), notifier=None, store=None, emit=emit, settings=Settings())

    result = await ai_tools.tool_notify_guest(
        ctx, {"recipient_id": 1, "persona": "Админ", "text": "Протокол «Утро» активирован"}
    )

    assert len(emit.events) == 1
    event_type, data = emit.events[0]
    assert event_type == task_protocol.EVENT_DELIVER_MESSAGE
    assert data["chat_id"] == 1  # self-напоминание владельцу
    assert "Админ" in data["html"]
    assert "Протокол «Утро» активирован" in data["plain"]
    assert "передано" in result


async def test_notify_guest_still_unavailable_without_notifier_or_emit():
    ctx = ai_tools.ToolContext(
        chat_id=1, dialogue_id=1, trigger_message_id=1, settings=Settings(), book=_book()
    )
    result = await ai_tools.tool_notify_guest(
        ctx, {"recipient_id": 1, "persona": "Админ", "text": "х"}
    )
    assert result.startswith("недоступно")


# --- решение 2026-09-27: владелец по роли — всем, по имени — знакомым ------


def _owner_people() -> list[PersonConfig]:
    return [
        PersonConfig(
            telegram_username="asevbo",
            telegram_id=OWNER_CHAT,
            full_name="Алексей Александрович Севбо",
            gender="m",
        )
    ]


async def test_owner_by_personal_name_needs_acquaintance():
    """«Передай Алексею» — владелец как обычный человек: без знакомства нельзя."""
    notifier = FakeNotifier()
    ctx = _ctx(
        chat_id=PLAIN_GUEST_CHAT,
        book=_permission_book(),
        notifier=notifier,
        store=FakeStore(acquainted=()),
        settings=Settings(people=_owner_people()),
    )
    result = await ai_tools.tool_tell(ctx, {"recipient_id": OWNER_CHAT, "text": "привет"})
    assert "не знакомы" in result and "владельцу" in result
    assert notifier.sent == []

    # А по роли — тому же человеку, тем же гостем, без знакомства.
    result = await ai_tools.tool_tell(
        ctx, {"recipient_id": OWNER_CHAT, "to_owner_role": True, "text": "привет"}
    )
    assert "передано" in result
    assert notifier.sent[0][0] == OWNER_CHAT


async def test_owner_by_name_via_tasks_bridge_requires_acquaintance():
    emit = FakeEmit()
    ctx = _ctx(
        chat_id=PLAIN_GUEST_CHAT,
        book=_permission_book(),
        notifier=None,
        store=None,
        settings=Settings(people=_owner_people()),
        emit=emit,
    )
    await ai_tools.tool_tell(ctx, {"recipient_id": OWNER_CHAT, "text": "привет"})
    await ai_tools.tool_tell(
        ctx, {"recipient_id": OWNER_CHAT, "to_owner_role": True, "text": "привет"}
    )
    payloads = [p for _, p in emit.events]
    assert payloads[0]["require_acquaintance"] == [PLAIN_GUEST_CHAT, OWNER_CHAT]
    assert "require_acquaintance" not in payloads[1]


def _twins_book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="owner", chat_id=OWNER_CHAT, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name=name,
                chat_id=chat_id,
                allowed_commands=["chat@llm", "tell@llm"],
                invited_user=name,
            )
            for name, chat_id in (
                ("Гость Плоский", PLAIN_GUEST_CHAT),
                ("Милана Юрьевна", FAMILY_A_CHAT),
                ("Милана", FAMILY_B_CHAT),
            )
        ],
    )


# --- Этап 54.4: find_person — id по описанию, только среди допустимых -----


def _find_ctx(chat_id, acquainted, **over):
    return _ctx(
        chat_id=chat_id,
        book=_twins_book(),
        store=FakeStore(acquainted=acquainted),
        settings=Settings(),
        **over,
    )


async def test_find_person_picks_the_only_acquaintance_among_namesakes():
    ctx = _find_ctx(PLAIN_GUEST_CHAT, ((PLAIN_GUEST_CHAT, FAMILY_B_CHAT),))
    result = await ai_tools.tool_find_person(ctx, {"description": "Милане"})
    assert result.startswith("Нашёл:")
    assert f"id {FAMILY_B_CHAT}" in result
    assert f"id {FAMILY_A_CHAT}" not in result


async def test_find_person_lists_several_acquaintances_and_asks():
    ctx = _find_ctx(
        PLAIN_GUEST_CHAT,
        ((PLAIN_GUEST_CHAT, FAMILY_A_CHAT), (PLAIN_GUEST_CHAT, FAMILY_B_CHAT)),
    )
    result = await ai_tools.tool_find_person(ctx, {"description": "Милана"})
    assert "переспроси" in result
    assert f"id {FAMILY_A_CHAT}" in result and f"id {FAMILY_B_CHAT}" in result


async def test_find_person_does_not_reveal_strangers_to_a_guest():
    ctx = _find_ctx(PLAIN_GUEST_CHAT, ())
    result = await ai_tools.tool_find_person(ctx, {"description": "Милана"})
    assert "никого не нашёл" in result
    assert str(FAMILY_A_CHAT) not in result and str(FAMILY_B_CHAT) not in result


async def test_find_person_owner_role_for_a_guest():
    ctx = _find_ctx(PLAIN_GUEST_CHAT, ())
    result = await ai_tools.tool_find_person(ctx, {"description": "хозяину"})
    assert f"id {OWNER_CHAT}" in result and "to_owner_role=true" in result


async def test_find_person_for_acquaintance_searches_all_guests_but_lists_nobody():
    ctx = _find_ctx(PLAIN_GUEST_CHAT, ())
    found = await ai_tools.tool_find_person(
        ctx, {"description": "Милана Юрьевна", "purpose": "acquaintance"}
    )
    assert f"id {FAMILY_A_CHAT}" in found
    missing = await ai_tools.tool_find_person(
        ctx, {"description": "Никодим", "purpose": "acquaintance"}
    )
    assert "никого" in missing and str(FAMILY_B_CHAT) not in missing


async def test_find_person_owner_sees_all_guests():
    ctx = _find_ctx(OWNER_CHAT, ())
    result = await ai_tools.tool_find_person(ctx, {"description": "Гостю Плоскому"})
    assert f"id {PLAIN_GUEST_CHAT}" in result


async def test_find_person_nick_wins_over_surrounding_words():
    """Живая находка 2026-10-09: «Александр Сергеевич Севбо @ksytal_as»
    целиком не совпадал ни с одной меткой — @ник ищется отдельно."""
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="owner", chat_id=OWNER_CHAT, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name="Александр (@ksytal_as)",
                chat_id=FAMILY_A_CHAT,
                allowed_commands=["chat@llm"],
                invited_user="Александр (@ksytal_as)",
            )
        ],
    )
    ctx = _ctx(chat_id=OWNER_CHAT, book=book, store=FakeStore(), settings=Settings())
    result = await ai_tools.tool_find_person(
        ctx, {"description": "Александр Сергеевич Севбо @ksytal_as"}
    )
    assert result.startswith("Нашёл:") and f"id {FAMILY_A_CHAT}" in result


async def test_find_person_by_words_and_cyrillic_nick():
    """Живая находка 2026-10-09: «Андрей который кейн» — ни целиком, ни по
    основам; по отдельным словам и транслиту «кейн» → @kein находится."""
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="owner", chat_id=OWNER_CHAT, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name=name, chat_id=chat_id, allowed_commands=["chat@llm"], invited_user=name
            )
            for name, chat_id in (
                ("Андрей Александрович Севбо (@kein)", FAMILY_A_CHAT),
                ("Андрей Петров (@andy)", FAMILY_B_CHAT),
            )
        ],
    )
    ctx = _ctx(chat_id=OWNER_CHAT, book=book, store=FakeStore(), settings=Settings())
    result = await ai_tools.tool_find_person(
        ctx, {"description": "Андрей который кейн", "purpose": "acquaintance"}
    )
    assert result.startswith("Нашёл:") and f"id {FAMILY_A_CHAT}" in result
    both = await ai_tools.tool_find_person(ctx, {"description": "Андрей который мой брат"})
    assert "переспроси" in both
    assert f"id {FAMILY_A_CHAT}" in both and f"id {FAMILY_B_CHAT}" in both


async def test_find_person_for_acquaintance_points_to_request_acquaintance():
    ctx = _find_ctx(OWNER_CHAT, ())
    result = await ai_tools.tool_find_person(
        ctx, {"description": "Гость Плоский", "purpose": "acquaintance"}
    )
    assert f"request_acquaintance(recipient_id={PLAIN_GUEST_CHAT})" in result


class _CardsLink:
    """graph_memory с одной карточкой: прозвище «Мила» у FAMILY_B_CHAT."""

    async def command(self, action, args=None, dst=None, *, timeout=None):
        assert action == "person_cards"
        return {
            "cards": {
                str(FAMILY_B_CHAT): {
                    "gender": {"value": "f", "by_id": FAMILY_B_CHAT, "strength": "self"},
                    "name": None,
                    "aliases": [{"value": "Мила", "by_id": FAMILY_B_CHAT, "strength": "self"}],
                }
            }
        }


async def test_find_person_matches_alias_from_card_and_tells_gender():
    ctx = _find_ctx(
        PLAIN_GUEST_CHAT,
        ((PLAIN_GUEST_CHAT, FAMILY_A_CHAT), (PLAIN_GUEST_CHAT, FAMILY_B_CHAT)),
        node_link=_CardsLink(),
    )
    result = await ai_tools.tool_find_person(ctx, {"description": "Миле"})
    assert result.startswith("Нашёл:")
    assert f"id {FAMILY_B_CHAT} — женщина — совпало: прозвище «Мила»" in result


async def test_tell_result_is_inflected_by_card_gender():
    notifier = FakeNotifier()
    ctx = _find_ctx(
        PLAIN_GUEST_CHAT,
        ((PLAIN_GUEST_CHAT, FAMILY_B_CHAT),),
        node_link=_CardsLink(),
        notifier=notifier,
    )
    result = await ai_tools.tool_tell(ctx, {"recipient_id": FAMILY_B_CHAT, "text": "привет"})
    assert result == "передано: Милана получила сообщение"


async def test_tell_owner_role_flag_requires_the_owner_id():
    ctx = _find_ctx(PLAIN_GUEST_CHAT, ((PLAIN_GUEST_CHAT, FAMILY_B_CHAT),))
    result = await ai_tools.tool_tell(
        ctx, {"recipient_id": FAMILY_B_CHAT, "to_owner_role": True, "text": "привет"}
    )
    assert result.startswith("ошибка: to_owner_role")


# --- живой баг 2026-09-28: «передай маме» → модель угадала не того -------


async def test_refusal_lists_acquaintances_with_ids_and_id_resolves():
    """Отказ/«не знаю» подсказывает знакомых с id; повтор по id — доходит."""
    notifier = FakeNotifier()
    ctx = _ctx(
        chat_id=OWNER_CHAT,
        book=_twins_book(),
        notifier=notifier,
        store=FakeStore(acquainted=((FAMILY_B_CHAT, OWNER_CHAT),)),
        settings=Settings(),
    )
    refused = await ai_tools.tool_tell(ctx, {"recipient_id": PLAIN_GUEST_CHAT, "text": "привет"})
    assert "не знакомы" in refused
    assert f"Милана (id {FAMILY_B_CHAT})" in refused

    unknown = await ai_tools.tool_find_person(ctx, {"description": "маме"})
    assert "никого не нашёл" in unknown and f"id {FAMILY_B_CHAT}" in unknown
    assert notifier.sent == []

    result = await ai_tools.tool_tell(ctx, {"recipient_id": FAMILY_B_CHAT, "text": "привет"})
    assert "передано" in result
    assert notifier.sent[0][0] == FAMILY_B_CHAT


async def test_id_of_non_acquaintance_still_refused():
    notifier = FakeNotifier()
    ctx = _ctx(
        chat_id=OWNER_CHAT,
        book=_twins_book(),
        notifier=notifier,
        store=FakeStore(acquainted=()),
        settings=Settings(),
    )
    result = await ai_tools.tool_tell(ctx, {"recipient_id": FAMILY_A_CHAT, "text": "привет"})
    assert "не знакомы" in result and "знакомых у собеседника нет" in result
    assert notifier.sent == []


async def test_guests_list_shows_config_name_and_nick():
    """Живая находка 2026-10-09: брат владельца был в списке как «Kein»
    без имени и ника из [[people]]."""
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=1, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name="Kein", chat_id=ANDREY_CHAT, allowed_commands=["chat@llm"]
            )
        ],
    )
    ctx = _ctx(book=book)
    result = await ai_tools.tool_guests_list(ctx, {})
    line = next(row for row in result.splitlines() if str(ANDREY_CHAT) in row)
    assert "Kein" in line and "Андрей Иванов" in line and "@andrey" in line
