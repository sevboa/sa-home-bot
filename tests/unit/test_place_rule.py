"""Одно место Альфреда (Этап 59.7): «Где ты» каждый ход канарейки, снимок и
обыск только того места, где он стоит, комнаты без окна — без окна, света дня
и погоды. Гость вне списка ничего этого не видит."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from sa_home_bot.bot.interactives import cabinet, photo_text, places, underground
from sa_home_bot.bot.interactives.cabinet import Cabinet
from sa_home_bot.bot.interactives.places import Room
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from tests.unit.test_cellar import CANARY, GUEST, Dice, _active, _make


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


async def _put(svc, place, user=CANARY):
    await places.set_at(svc._store, user, places.new_at(place, svc._now()))


async def _drawn(svc):
    await asyncio.gather(*list(svc._photo_tasks))


async def _open_ways(svc):
    await svc._state.set_effect(places.EFFECT_CELLAR_UNLOCKED, CANARY, "1")
    await svc._state.set_effect(places.EFFECT_CATACOMBS_OPEN, CANARY, "1")


# --- словарь мест ---


@pytest.mark.parametrize(
    ("text", "here", "named"),
    [
        ("тёмный пролёт лестницы вниз", "cabinet", "лестница к двери подвала"),
        ("старые ящики и сундуки в подвале", "cabinet", "подвал"),
        ("Кабинет Альфреда", "cellar", "кабинет"),
        ("Мой кабинет", "cellar_door", "кабинет"),
        ("дверь подвала с замком", "cabinet", "лестница к двери подвала"),
        ("общий вид подвала", "cabinet", "подвал"),
        ("каменный коридор", "cabinet", "катакомбы"),
        ("вход в катакомбы", "cellar", "катакомбы"),
        ("старая проявочная", "cabinet", "старая проявочная"),
        ("винный погреб", "cabinet", "винный погреб"),
        ("лаз в стене", "cabinet", "лаз в катакомбы"),
        # то место, где стоишь, — не чужое
        ("бочки в подвале", "cellar", None),
        ("письменный стол в кабинете", "cabinet", None),
        ("дверь подвала", "cellar_door", None),
        ("сыроватый коридор", "catacombs", None),
        ("красный фонарь в проявочной", "darkroom", None),
        ("лаз за бочкой", "cellar", None),
        ("лаз в стене", "catacombs", None),
        # «глаз» — не лаз, «дверь» без подвала — не лестница
        ("глаз совы на стене", "cabinet", None),
        ("дверь кабинета", "cabinet", None),
        ("общий вид", "cellar", None),
        ("сними, что вокруг", "catacombs", None),
    ],
)
def test_foreign_place_by_stems(text, here, named):
    assert places.foreign_place(text, here) == named


def test_wine_cellar_is_not_the_cellar():
    assert places.foreign_place("винный погреб", "wine_cellar") is None
    assert places.foreign_place("бочки в винном погребе", "cellar") == "винный погреб"


def test_general_view_cabinet_noun_belongs_to_the_cabinet_only():
    study = Cabinet(user_id=1)
    cellar = Room(user_id=1, canon=places.CELLAR_CANON)
    assert photo_text.is_general_view("Мой кабинет", study)
    assert not photo_text.is_general_view("Мой кабинет", cellar)
    assert not photo_text.is_general_view("Кабинет Альфреда", cellar)
    assert photo_text.is_general_view("общий вид", cellar)
    assert photo_text.is_general_view("комнату", cellar)


# --- комнаты без окна ---


def test_windowless_state_key_has_no_weather():
    cellar = Room(user_id=1, canon=places.CELLAR_CANON)
    assert cellar.state_key("ночь:дождь") == cellar.state_key("день:ясно")
    assert "дождь" not in cellar.state_key("ночь:дождь")
    study = Cabinet(user_id=1)
    assert study.state_key("ночь:дождь") == study.state_key("ночь:дождь")
    assert study.state_key("ночь:дождь") != study.state_key("день:ясно")


def test_photo_negative_adds_window_only_without_one():
    cellar = Room(user_id=1, canon=places.CELLAR_CANON)
    assert photo_text.photo_negative(Cabinet(user_id=1)) == cabinet.PHOTO_NEGATIVE_EN
    negative = photo_text.photo_negative(cellar)
    assert negative.startswith(cabinet.PHOTO_NEGATIVE_EN)
    assert "window" in negative and "daylight" in negative


@pytest.mark.parametrize(
    "canon",
    [
        places.CELLAR_DOOR_CANON,
        places.CELLAR_CANON,
        underground.CATACOMBS_CANON,
        *underground.SPECIAL_CANONS.values(),
    ],
)
def test_every_windowless_place_is_lit_by_a_lantern_only(canon):
    room = Room(user_id=1, canon=canon)
    assert not room.windowed and canon.light_en == places.UNDERGROUND_LIGHT_EN
    assert room.light_en(object()) == places.UNDERGROUND_LIGHT_EN


# --- «Где ты» каждый ход ---


async def test_canary_gets_where_note_every_turn_in_the_study(store):
    svc, _, _ = _make(store)
    plan = await svc.before_turn(CANARY, CANARY, "Привет", is_private=True)
    assert plan.note.startswith("Где ты сейчас: в кабинете.")
    assert places.WINDOW_YES in plan.note and places.MOVE_RULE in plan.note
    assert "go(" not in plan.note  # подвал ещё не открыт — идти некуда


async def test_study_note_lists_open_ways(store):
    svc, _, _ = _make(store)
    await svc._state.set_effect(places.EFFECT_CELLAR_UNLOCKED, CANARY, "1")
    plan = await svc.before_turn(CANARY, CANARY, "Привет", is_private=True)
    assert "в подвал — cellar" in plan.note and "катакомбы" not in plan.note
    await svc._state.set_effect(places.EFFECT_CATACOMBS_OPEN, CANARY, "1")
    plan = await svc.before_turn(CANARY, CANARY, "Привет", is_private=True)
    assert "в катакомбы — catacombs" in plan.note


async def test_note_in_the_cellar_says_no_window_and_the_way_out(store):
    svc, _, _ = _make(store)
    await _put(svc, places.PLACE_CELLAR)
    plan = await svc.before_turn(CANARY, CANARY, "Ну что?", is_private=True)
    assert plan.note.startswith("Где ты сейчас: в подвале.")
    assert "Подвал замка" in plan.note
    assert places.WINDOW_NO in plan.note and places.WINDOW_YES not in plan.note
    assert "go(cabinet)" in plan.note and places.MOVE_RULE in plan.note


async def test_scene_note_gets_only_the_rule_not_a_second_place(store):
    svc, _, _ = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    plan = await svc.before_turn(CANARY, CANARY, "Ну что там?", is_private=True)
    assert plan.scene
    assert plan.note.count("Где ты:") == 1 and "Где ты: Узкая каменная лестница" in plan.note
    assert "Где ты сейчас" not in plan.note
    assert places.MOVE_RULE in plan.note and places.WINDOW_NO in plan.note


async def test_non_canary_gets_no_where_note(store):
    svc, _, _ = _make(store)
    plan = await svc.before_turn(GUEST, GUEST, "Привет", is_private=True)
    assert plan.note is None


# --- take_photo ---


async def test_photo_of_another_place_is_refused_without_a_shot(store):
    svc, _, link = _make(store)
    for args in (
        {"focus": "тёмный пролёт лестницы вниз"},
        {"focus": "ящики", "caption": "Подвал"},
        {"focus": "каменный коридор"},
        {"selfie": True, "focus": "в подвале с фонарём"},
    ):
        out = await svc.tool_take_photo(CANARY, CANARY, args)
        assert out.startswith("Снимка не будет: место «")
        assert "ты сейчас в кабинете" in out and "go" in out
    await _drawn(svc)
    assert link.generated == [] and not svc._photo_tasks


async def test_study_view_from_the_cellar_is_refused_not_drawn_as_the_cellar(store):
    svc, _, link = _make(store)
    await _put(svc, places.PLACE_CELLAR_DOOR)
    for focus in ("Мой кабинет", "Кабинет Альфреда", "кабинет"):
        out = await svc.tool_take_photo(CANARY, CANARY, {"focus": focus})
        assert out == places.TOOL_PHOTO_ELSEWHERE.format(name="кабинет", here="у двери подвала")
    await _drawn(svc)
    assert link.generated == []


async def test_window_view_without_a_window_is_refused(store):
    svc, _, link = _make(store)
    await _put(svc, places.PLACE_CELLAR)
    for focus in ("вид из окна", "что за окном", "окно и вид за ним"):
        out = await svc.tool_take_photo(CANARY, CANARY, {"focus": focus})
        assert out == places.TOOL_PHOTO_NO_WINDOW.format(here="в подвале")
    await _drawn(svc)
    assert link.generated == []


async def test_general_view_here_is_drawn_with_a_lantern_and_no_window(store):
    svc, _, link = _make(store)
    await _put(svc, places.PLACE_CELLAR)
    for args in ({}, {"focus": "общий вид"}, {"focus": "что вокруг", "caption": "Подвал"}):
        assert await svc.tool_take_photo(CANARY, CANARY, args) == cabinet.TOOL_PHOTO_STARTED
        await _drawn(svc)
    request = link.generated[0]
    assert request["light"] == places.UNDERGROUND_LIGHT_EN
    assert "window" in request["negative_extra"] and "daylight" in request["negative_extra"]
    assert "cellar" in request["description"] and "study" not in request["description"]


async def test_study_keeps_its_window_and_negative(store):
    svc, _, link = _make(store)
    assert await svc.tool_take_photo(CANARY, CANARY, {"focus": "вид из окна"}) == (
        cabinet.TOOL_PHOTO_STARTED
    )
    await _drawn(svc)
    assert link.generated[0]["negative_extra"] == cabinet.PHOTO_NEGATIVE_EN
    assert link.generated[0]["light"] != places.UNDERGROUND_LIGHT_EN


async def test_catacombs_photo_names_only_catacombs(store):
    svc, _, link = _make(store)
    await _open_ways(svc)
    await svc.tool_go(CANARY, CANARY, "catacombs")
    out = await svc.tool_take_photo(CANARY, CANARY, {"focus": "кабинет с камином"})
    assert out.startswith("Снимка не будет: место «кабинет»")
    assert "ты сейчас в катакомбах" in out
    out = await svc.tool_take_photo(CANARY, CANARY, {"focus": "вид из окна"})
    assert out == places.TOOL_PHOTO_NO_WINDOW.format(here="в катакомбах")


async def test_non_canary_photo_is_not_checked(store):
    """Гость вне списка: Альфред всегда в кабинете, правило места не действует."""
    svc, _, link = _make(store)
    out = await svc.tool_take_photo(GUEST, GUEST, {"focus": "подвал"})
    assert out == cabinet.TOOL_PHOTO_STARTED
    await _drawn(svc)
    assert link.generated[0]["negative_extra"] == cabinet.PHOTO_NEGATIVE_EN


# --- search ---


async def test_search_elsewhere_is_refused_and_not_marked(store):
    svc, _, _ = _make(store)
    out = await svc.tool_search(CANARY, CANARY, "старые ящики и сундуки в подвале")
    assert out == places.TOOL_SEARCH_ELSEWHERE.format(name="подвал", here="в кабинете")
    assert await store.get_state(places.searched_key("cabinet", CANARY)) is None
    assert await store.get_state(places.searched_key("cellar", CANARY)) is None
    # Здесь — обычный обыск.
    assert "Ты обыскал «ящик стола»" in await svc.tool_search(CANARY, CANARY, "ящик стола")


async def test_search_in_the_cellar_refuses_the_study_and_the_corridor(store):
    svc, _, _ = _make(store)
    await _put(svc, places.PLACE_CELLAR)
    out = await svc.tool_search(CANARY, CANARY, "ящик в кабинете")
    assert out == places.TOOL_SEARCH_ELSEWHERE.format(name="кабинет", here="в подвале")
    out = await svc.tool_search(CANARY, CANARY, "каменный коридор")
    assert "место «катакомбы»" in out
    assert await store.get_state(places.searched_key("cellar", CANARY)) is None
    assert "Ты обыскал «бочка у стены»" in await svc.tool_search(CANARY, CANARY, "бочка у стены")


async def test_search_in_the_corridor_refuses_the_cellar(store):
    svc, _, _ = _make(store)
    await _open_ways(svc)
    await svc.tool_go(CANARY, CANARY, "catacombs")
    out = await svc.tool_search(CANARY, CANARY, "ящик в подвале")
    assert out == places.TOOL_SEARCH_ELSEWHERE.format(name="подвал", here="в катакомбах")
