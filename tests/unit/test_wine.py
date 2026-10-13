"""Этап 59.3: вино — особенная вещь подвала и общий механизм «Применение»
(consumable, 49.4): «Распить» → вещь consumed, эффект tipsy на 3 ч, текст
эффекта в заметке Альфреду, снимок с бокалом."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest_asyncio

from sa_home_bot.bot.interactives import engine, items, places
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from tests.unit.test_cellar import CANARY, GUEST, Clock, Dice, _dir, _make, _run, _to_hatch, _turn


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


async def _wine(svc, store, user=CANARY) -> dict:
    item = await svc._create_found_item(user, items.WINE, "test")
    assert item is not None
    return item


# --- вещь появляется на стадии 3 ---


async def test_wine_appears_at_stage_three_with_fallback_look(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_hatch(svc, store, link)
    wine = (await store.items_of(CANARY, "wine"))[0]
    assert wine["status"] == "owned"
    assert len(wine["traits"]) == 2 and wine["note"] in items.WINE.fallback_notes
    # Бутылка одна: повторный вход ничего не добавляет.
    assert await svc._create_found_item(CANARY, items.WINE, "again") is None
    assert len(await store.items_of(CANARY, "wine")) == 1


async def test_director_describes_the_bottle_on_the_next_call(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_hatch(svc, store, link)
    # _to_hatch уже сделал вызов Ведущего со стадии 3 (без found_item в ответе):
    # запасное описание остаётся, просьба снята.
    run = await _run(store)
    assert "found_item" not in run.world
    # Новая бутылка (после сброса): Ведущий отвечает приметами.
    await store.consume_item((await store.items_of(CANARY, "wine"))[0]["id"])
    item = await _wine(svc, store)
    run.world["found_item"] = {"id": item["id"], "name": "Бутылка вина", "ask": "Опиши."}
    await svc._state.save_run(run)
    link.director_replies = [
        _dir(
            stage=4,
            found_item={
                "traits": ["этикетка выцвела", "год — 1887", "пыль"],
                "note": "«Мортем», 1887.",
            },
        )
    ]
    await _turn(svc, "А что за вино?")
    assert "found_item" in link.director_inputs[-1]
    fresh = await store.item_by_id(item["id"])
    assert fresh["traits"] == ["этикетка выцвела", "год — 1887", "пыль"]
    assert fresh["note"] == "«Мортем», 1887"
    assert "found_item" not in (await _run(store)).world


async def test_inventory_and_card_speak_in_wine_words(store):
    svc, _, _ = _make(store)
    item = await _wine(svc, store)
    text, markup = await svc.inventory(CANARY)
    assert "Бутылка вина" in text and "в подвале, у входа" in text
    assert markup is not None
    caption = engine.item_caption(items.WINE, item, False)
    assert "На этикетке" in caption and "радиостанц" not in caption
    lines = await svc.manor_lines(CANARY, [item])
    assert "action=drink" in lines


async def test_wine_is_invisible_to_non_canary(store):
    svc, _, _ = _make(store)
    await store.add_item(
        type="wine", owner_user_id=GUEST, traits=[], image_id=None, origin="x", now=svc._now()
    )
    text, markup = await svc.inventory(GUEST)
    assert "вина" not in text
    assert await svc.tool_item_action(GUEST, GUEST, item="вино", action="drink") != ""
    assert await svc._effects_note(CANARY) is None


# --- распить ---


async def test_drink_consumes_the_bottle_and_sets_tipsy_for_three_hours(store):
    clock = Clock()
    svc, _, link = _make(store, clock=clock)
    item = await _wine(svc, store)
    answer, markup, caption = await svc.handle_item_click(CANARY, CANARY, item["id"], "d")
    assert answer == "Распито."
    assert (await store.item_by_id(item["id"]))["status"] == "consumed"
    assert await store.items_of(CANARY, "wine") == []
    assert "распита" in caption and not markup.inline_keyboard
    raw = json.loads(await store.get_state(f"user_effect:tipsy:{CANARY}"))
    assert raw["expires_at"] == (clock.now + timedelta(hours=3)).isoformat()
    # Альфред: реплика после глотка и снимок с бокалом (селфи).
    assert svc.spoken and "распили" in svc.spoken[0][0]
    await engine.asyncio.gather(*list(svc._photo_tasks))
    shot = link.generated[-1]
    assert "glass of dark red wine" in shot["description"]
    assert "size" not in shot  # обычный размер: фотоаппарата нет
    events = [e["kind"] for e in await store.item_events(item["id"])]
    assert events == ["created", "consumed"]


async def test_drink_only_once(store):
    svc, _, _ = _make(store)
    item = await _wine(svc, store)
    await svc.handle_item_click(CANARY, CANARY, item["id"], "d")
    first = await store.get_state(f"user_effect:tipsy:{CANARY}")
    spoken = len(svc.spoken)
    answer, markup, _ = await svc.handle_item_click(CANARY, CANARY, item["id"], "d")
    assert answer == "Бутылка распита."
    assert len(svc.spoken) == spoken
    assert await store.get_state(f"user_effect:tipsy:{CANARY}") == first
    # Из формы тула — то же самое, и в описи бутылки больше нет.
    assert await svc.tool_item_action(CANARY, CANARY, item="вино", action="drink") != ""
    assert svc._queued.get(CANARY) is None or not svc._queued[CANARY].items


async def test_item_action_tool_asks_with_a_form(store):
    svc, notifier, _ = _make(store)
    item = await _wine(svc, store)
    answer = await svc.tool_item_action(CANARY, CANARY, item="бутылка вина", action="drink")
    assert "Распить бутылку вина?" in answer
    await svc.flush_forms(CANARY)
    assert notifier.texts(CANARY)[-1] == items.WINE_FORM_TEXT
    markup = notifier.sent[-1][2]
    assert markup.inline_keyboard[0][0].text == "Распить"
    assert markup.inline_keyboard[0][0].callback_data == f"it:{item['id']}:fd"


# --- эффект в заметке Альфреду ---


def _no_where(note: str | None) -> str | None:
    """Заметка без «Где ты» (59.7): она есть у канарейки каждый ход."""
    assert note is not None and places.MOVE_RULE in note
    return note.split(places.MOVE_RULE)[-1].strip() or None


async def test_tipsy_text_goes_to_alfred_note_until_it_expires(store):
    clock = Clock()
    svc, _, _ = _make(store, clock=clock)
    item = await _wine(svc, store)
    plan = await svc.before_turn(CANARY, CANARY, "Привет", is_private=True)
    assert _no_where(plan.note) is None
    await svc.handle_item_click(CANARY, CANARY, item["id"], "d")
    plan = await svc.before_turn(CANARY, CANARY, "Как самочувствие?", is_private=True)
    assert items.TIPSY_TEXT in _no_where(plan.note)
    clock.now += timedelta(hours=2, minutes=59)
    plan = await svc.before_turn(CANARY, CANARY, "И ещё?", is_private=True)
    assert items.TIPSY_TEXT in _no_where(plan.note)
    clock.now += timedelta(minutes=2)
    plan = await svc.before_turn(CANARY, CANARY, "Ну?", is_private=True)
    assert _no_where(plan.note) == items.TIPSY_ENDED  # один раз: хмель прошёл
    assert await store.get_state(f"user_effect:tipsy:{CANARY}") is None
    plan = await svc.before_turn(CANARY, CANARY, "Ну?", is_private=True)
    assert _no_where(plan.note) is None


async def test_tipsy_effect_is_ignored_for_non_canary(store):
    svc, _, _ = _make(store)
    await svc._state.set_timed_effect("tipsy", GUEST, svc._now() + timedelta(hours=1))
    plan = await svc.before_turn(GUEST, GUEST, "Привет", is_private=True)
    assert plan.note is None
