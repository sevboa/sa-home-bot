from __future__ import annotations

from datetime import UTC, datetime

from sa_home_bot.bot import tools
from sa_home_bot.config import (
    GuestSubscriptionConfig,
    SubscriptionConfig,
)
from sa_home_bot.subscriptions.book import SubscriptionBook
from tests.unit.tools_support import (
    FakeNodeLink,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)

# --- note_person (Этап 54.2, с Этапа 58 — в person_claims) ---

SPEAKER = 501
FRIEND = 502
STRANGER = 503


def _person_ctx(store, link, *, user_id=SPEAKER):
    ctx = _ctx(store, node_link=link)
    ctx.user_id = user_id
    return ctx


def _claims(rows):
    return [(r["subject_id"], r["field"], r["value"], r["by_id"], r["strength"]) for r in rows]


async def test_note_person_about_self_uses_speaker_id(store):
    link = FakeNodeLink()
    ctx = _person_ctx(store, link)
    result = await tools.tool_note_person(ctx, {"gender": "мужчина", "alias": ["Лили"]})
    assert "со слов самого человека" in result
    assert _claims(await store.person_claims()) == [
        (SPEAKER, "gender", "m", SPEAKER, "self"),
        (SPEAKER, "alias", "Лили", SPEAKER, "self"),
    ]
    assert link.calls == []  # граф больше не трогаем
    assert ctx.people.gender(SPEAKER) == "m"  # снимок хода обновлён


async def test_note_person_records_birth_date_city_and_timezone(store):
    result = await tools.tool_note_person(
        _person_ctx(store, FakeNodeLink()),
        {"birth_date": "1990-04-29", "city": "Казань", "timezone": "Europe/Moscow"},
    )
    assert result.startswith("записал")
    assert {(f, v) for _s, f, v, _b, _st in _claims(await store.person_claims())} == {
        ("birth_date", "1990-04-29"),
        ("city", "Казань"),
        ("timezone", "Europe/Moscow"),
    }


async def test_note_person_rejects_bad_timezone_and_date(store):
    ctx = _person_ctx(store, FakeNodeLink())
    assert (await tools.tool_note_person(ctx, {"timezone": "Москва"})).startswith("ошибка")
    assert (await tools.tool_note_person(ctx, {"birth_date": "29 апреля"})).startswith("ошибка")
    assert await store.person_claims() == []


async def test_note_person_about_acquaintance_is_weak(store):
    now = datetime.now(tz=UTC)
    await store.add_confirmed_relationship(FRIEND, SPEAKER, "acquaintance", now, now)
    result = await tools.tool_note_person(
        _person_ctx(store, FakeNodeLink()), {"person_id": FRIEND, "name": "Милана"}
    )
    assert "знакомого" in result
    assert _claims(await store.person_claims()) == [
        (FRIEND, "name", "Милана", SPEAKER, "acquaintance")
    ]


async def test_note_person_refuses_strangers(store):
    link = FakeNodeLink()
    result = await tools.tool_note_person(
        _person_ctx(store, link), {"person_id": STRANGER, "gender": "f"}
    )
    assert result.startswith("не записал")
    assert f"request_acquaintance(recipient_id={STRANGER})" in result
    assert link.calls == []



def _stranger_book():
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=SPEAKER, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name="Александр (@ksytal_as)", chat_id=STRANGER, allowed_commands=["chat@llm"]
            )
        ],
    )


async def test_note_person_names_whose_id_it_is_when_name_differs(store):
    """Живая находка 2026-10-09: «Я знаком с Андреем» — модель взяла из
    фактов памяти id Александра; отказ должен сказать, чей он, и отправить в
    find_person, а не открывать знакомство с Александром."""
    link = FakeNodeLink()
    ctx = _person_ctx(store, link)
    ctx.book = _stranger_book()
    result = await tools.tool_note_person(ctx, {"person_id": STRANGER, "name": "Андрей"})
    assert result.startswith("не записал")
    assert "Александр (@ksytal_as)" in result and "а не «Андрей»" in result
    assert "find_person" in result
    assert "request_acquaintance" not in result
    assert link.calls == []


async def test_note_person_stranger_with_matching_name_points_to_acquaintance(store):
    link = FakeNodeLink()
    ctx = _person_ctx(store, link)
    ctx.book = _stranger_book()
    result = await tools.tool_note_person(ctx, {"person_id": STRANGER, "name": "Александра"})
    assert "Александр (@ksytal_as)" in result
    assert f"request_acquaintance(recipient_id={STRANGER})" in result

async def test_note_person_rejects_bad_gender_before_calling_service(store):
    link = FakeNodeLink()
    result = await tools.tool_note_person(_person_ctx(store, link), {"gender": "x"})
    assert result.startswith("ошибка")
    assert link.calls == []


async def test_note_person_needs_known_speaker(store):
    result = await tools.tool_note_person(
        _person_ctx(store, FakeNodeLink(), user_id=None), {"gender": "m"}
    )
    assert result.startswith("недоступно")
