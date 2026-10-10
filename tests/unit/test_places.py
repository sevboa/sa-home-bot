"""Места Альфреда (Этап 59.1): комнаты, alfred_at, обыск, временные места,
возврат в кабинет — и то, что для гостя вне канарейки ничего не меняется."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest_asyncio

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.interactives import cabinet, engine, places
from sa_home_bot.bot.interactives.cabinet import Cabinet
from sa_home_bot.bot.interactives.engine import Interactives, photo_description
from sa_home_bot.bot.interactives.places import Room
from sa_home_bot.bot.interactives.transylvania import Outside, Transylvania
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store

CANARY = 901
GUEST = 902
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


async def _clear() -> int:
    return 0


class _Link:
    async def command(self, action, args, dst=None, timeout=None):
        raise AssertionError(action)


def _make(store, *, canary=(CANARY,), now=NOW, rng=lambda: 0.99):
    return Interactives(
        store,
        object(),
        Settings(llm=LlmConfig(model="m", interactives_canary_user_ids=list(canary))),
        lambda: _Link(),
        now=lambda: now,
        choose=lambda options: options[0],
        rng=rng,
        transylvania=Transylvania(fetch=_clear),
    )


# --- положение ---


def test_at_json_roundtrip_and_old_junk():
    at = places.new_at("cellar", NOW, node=[1, 2], heading="north")
    back = places.At.from_json(at.to_json())
    assert back is not None
    assert (back.place, back.node, back.heading) == ("cellar", [1, 2], "north")
    assert json.loads(at.to_json()).keys() >= {"place", "since", "node", "heading"}
    assert places.At.from_json("не json") is None
    assert places.At.from_json('{"x": 1}') is None


def test_idle_threshold_counts_from_the_last_touch():
    at = places.new_at("cellar", NOW)
    assert not places.is_idle(at, NOW + timedelta(hours=1, minutes=59), 2.0)
    assert places.is_idle(at, NOW + timedelta(hours=2), 2.0)
    at.touched = (NOW + timedelta(hours=3)).isoformat()
    assert not places.is_idle(at, NOW + timedelta(hours=4), 2.0)


async def test_non_canary_always_in_the_cabinet_and_alfred_at_is_ignored(store):
    svc = _make(store)
    await places.set_at(store, GUEST, places.new_at(places.PLACE_CELLAR, NOW))
    room = await svc._room(GUEST, GUEST)
    assert type(room) is Cabinet and room.place == "cabinet"
    # «Где ты» — прежний кабинет, без приписок.
    assert await svc._where_ru(room, GUEST, in_scene=False) == cabinet.CANON_RU


async def test_canary_without_alfred_at_is_in_the_cabinet_with_old_data(store):
    """Данные кабинета, записанные до Этапа 59, читаются как были."""
    old = Cabinet(user_id=CANARY, features=["чучело совы"], scene=["туман"], photos={"k": 5})
    await cabinet.save(store, old)
    svc = _make(store)
    room = await svc._room(CANARY, CANARY)
    assert type(room) is Cabinet
    assert (room.features, room.scene, room.photos) == (["чучело совы"], ["туман"], {"k": 5})
    await places.set_at(store, CANARY, places.new_at(places.PLACE_CABINET, NOW))
    assert type(await svc._room(CANARY, CANARY)) is Cabinet


async def test_canary_in_a_room_reads_that_room(store):
    svc = _make(store)
    await places.set_at(store, CANARY, places.new_at(places.PLACE_CELLAR, NOW))
    room = await svc._room(CANARY, CANARY)
    assert isinstance(room, Room) and room.place == "cellar" and not room.windowed
    assert "Подвал замка" in room.describe_ru()
    # Убрали из списка — данные остаются, но Альфред снова в кабинете.
    removed = _make(store, canary=())
    assert type(await removed._room(CANARY, CANARY)) is Cabinet
    assert await store.get_state(places.at_key(CANARY)) is not None


async def test_stale_place_falls_back_to_the_cabinet(store):
    svc = _make(store)
    await places.set_at(store, CANARY, places.new_at("t7", NOW))  # временной комнаты нет
    assert type(await svc._room(CANARY, CANARY)) is Cabinet
    await places.set_at(store, CANARY, places.new_at("catacombs", NOW, node=[0, 0]))
    assert type(await svc._room(CANARY, CANARY)) is Cabinet


# --- хранение комнат ---


async def test_room_storage_format_is_the_cabinet_format(store):
    cab = Cabinet(user_id=CANARY, features=["бочка"], scene=["тень"], photos={"a": 1})
    await cabinet.save(store, cab)
    cabinet_raw = json.loads(await store.get_state(f"location:cabinet:{CANARY}"))
    room = Room(
        user_id=CANARY,
        features=["бочка"],
        scene=["тень"],
        photos={"a": 1},
        canon=places.CELLAR_CANON,
    )
    await places.save_room(store, room)
    room_raw = json.loads(await store.get_state(f"location:cellar:{CANARY}"))
    assert room_raw == cabinet_raw
    back = await places.load_room(store, CANARY, "cellar")
    assert (back.features, back.scene, back.photos) == (["бочка"], ["тень"], {"a": 1})
    assert back.canon is places.CELLAR_CANON


async def test_load_room_without_data_is_an_empty_room_of_that_canon(store):
    room = await places.load_room(store, CANARY, places.PLACE_CELLAR_DOOR)
    assert room.features == [] and room.canon is places.CELLAR_DOOR_CANON
    assert await store.state_keys("location:") == []  # чтение ничего не пишет


def test_room_overrides_do_not_touch_the_cabinet_defaults():
    cab = Cabinet(user_id=1)
    assert (cab.place, cab.canon_ru, cab.canon_en) == (
        "cabinet",
        cabinet.CANON_RU,
        cabinet.CANON_EN,
    )
    assert cab.windowed and cab.caption == "Кабинет" and cab.scene_caption == "В кабинете"
    out = Outside(phase="night", weather=None, local_time="01:00")
    assert cab.light_en(out) == out.en() and cab.light_en(out, closeup=True) == out.en(closeup=True)
    room = Room(user_id=1, canon=places.CELLAR_CANON)
    assert room.light_en(out) == places.UNDERGROUND_LIGHT_EN
    assert room.state_key("k").startswith("cellar:")
    assert room.is_general("весь подвал") and not room.is_general("бочка в углу подвала у стены")


def test_photo_description_uses_the_room_canon_and_light():
    out = Outside(phase="day", weather="clear", local_time="13:00")
    room = Room(user_id=1, canon=places.CELLAR_CANON)
    description, _ = photo_description(room, out, "", None)
    assert "dark stone cellar" in description and places.UNDERGROUND_LIGHT_EN in description
    assert "study" not in description
    # Кабинет — как раньше.
    description, _ = photo_description(Cabinet(user_id=1), out, "", None)
    assert cabinet.CANON_EN in description and out.en() in description


def test_selfie_in_a_room_is_not_in_the_study():
    out = Outside(phase="day", weather="clear", local_time="13:00")
    room = Room(user_id=1, canon=places.CELLAR_CANON)
    text = engine.selfie_description(room, out, "", None)
    assert "study" not in text and "dark stone cellar" in text
    posed = engine.selfie_description(room, out, "спиной к зрителю", None)
    assert "study" not in posed


# --- обыск ---


def test_normalize_where():
    assert places.normalize_where("В ящике стола!") == "ящике стола"
    assert places.normalize_where("  за   дальней бочкой ") == "дальней бочкой"
    assert places.normalize_where("...") == ""


def test_search_marks_the_place_and_repeat_gives_nothing():
    searched: list[str] = []
    first = places.search_room(
        places.CABINET_CANON, "ящик стола", searched, rng=lambda: 0.99, choose=lambda o: o[0]
    )
    assert first.kind == places.SEARCH_KIND_EMPTY and searched == ["ящик стола"]
    again = places.search_room(
        places.CABINET_CANON, "В ящик стола!", searched, rng=lambda: 0.0, choose=lambda o: o[0]
    )
    assert again.kind == places.SEARCH_KIND_REPEAT and again.text == places.SEARCH_REPEAT_TEXT
    assert searched == ["ящик стола"]


def test_search_trinket_by_chance_and_special_find_by_table():
    searched: list[str] = []
    trinket = places.search_room(
        places.CELLAR_CANON, "полка", searched, rng=lambda: 0.1, choose=lambda o: o[0]
    )
    assert trinket.kind == places.SEARCH_KIND_TRINKET
    assert places.CELLAR_CANON.trinkets[0] in trinket.text
    canon = places.RoomCanon(
        id="x",
        name_ru="x",
        in_ru="x",
        canon_ru="x",
        canon_en="x",
        caption="x",
        scene_caption="x",
        stem="x",
        in_en="x",
        specials=(places.Find(re.compile("сейф"), "Внутри лежит письмо."),),
    )
    special = places.search_room(
        canon, "старый сейф", searched, rng=lambda: 0.0, choose=lambda o: o[0]
    )
    assert special.kind == places.SEARCH_KIND_SPECIAL and special.text == "Внутри лежит письмо."


async def test_search_tool_outside_a_scene_answers_at_once_and_remembers(store):
    svc = _make(store)
    first = await svc.tool_search(CANARY, CANARY, "ящик стола")
    assert "Ты обыскал «ящик стола»" in first and places.SEARCH_EMPTY_TEXT in first
    second = await svc.tool_search(CANARY, CANARY, "в ящик стола")
    assert places.SEARCH_REPEAT_TEXT in second
    assert json.loads(await store.get_state(places.searched_key("cabinet", CANARY))) == [
        "ящик стола"
    ]


async def test_search_tool_in_another_room_uses_that_rooms_table_and_key(store):
    svc = _make(store, rng=lambda: 0.1)
    await places.set_at(store, CANARY, places.new_at(places.PLACE_CELLAR, NOW))
    answer = await svc.tool_search(CANARY, CANARY, "бочка")
    assert places.CELLAR_CANON.trinkets[0] in answer
    assert await store.get_state(places.searched_key("cellar", CANARY)) == '["бочка"]'


async def test_search_tool_needs_a_place_to_look_at(store):
    svc = _make(store)
    assert await svc.tool_search(CANARY, CANARY, "  ") == places.TOOL_SEARCH_NO_WHERE
    assert await svc.tool_search(GUEST, GUEST, "ящик") == places.TOOL_SEARCH_UNAVAILABLE
    assert await store.state_keys("") == []


# --- временные места ---


def test_temp_spec_requires_name_and_canon():
    ok = places.temp_spec(
        {"name": "кладовая", "canon_ru": "Тесная кладовая.", "canon_en": "tiny pantry"}
    )
    assert ok is not None and ok["where"] == "в месте «кладовая»"
    assert places.temp_spec({"name": "кладовая"}) is None
    assert places.temp_spec("кладовая") is None


def test_temp_rooms_live_in_the_world_and_reuse_by_name():
    world: dict = {}
    spec = {"name": "кухня", "where": "на кухне", "canon_ru": "Кухня.", "canon_en": "kitchen"}
    tid = places.put_temp_room(world, spec)
    assert places.put_temp_room(world, {**spec, "canon_ru": "Большая кухня."}) == tid
    assert world["rooms"][tid]["canon_ru"] == "Большая кухня."
    room = places.temp_room(world, 5, tid)
    assert room is not None and room.temp and room.place == tid
    room.add(["медный котёл на очаге"])
    places.store_temp_room(world, room)
    assert world["rooms"][tid]["features"] == ["медный котёл на очаге"]
    assert places.is_temp(tid) and not places.is_temp("cellar") and not places.is_temp("cabinet")


def test_temp_rooms_are_capped():
    world: dict = {}
    for i in range(12):
        places.put_temp_room(
            world, {"name": f"комната {i}", "where": "в", "canon_ru": "к", "canon_en": "r"}
        )
    assert len(world["rooms"]) == places.TEMP_ROOMS_KEEP


# --- возврат в кабинет ---


async def test_return_to_the_cabinet_is_said_once_next_turn(store):
    svc = _make(store)
    await places.set_at(store, CANARY, places.new_at(places.PLACE_CELLAR, NOW))
    later = _make(store, now=NOW + timedelta(hours=3))
    note = await later._tend_position(CANARY)
    assert note is not None and "вернулся из подвала к себе в кабинет" in note
    at = await places.get_at(store, CANARY)
    assert at.in_cabinet and at.returned_from is None
    assert await later._tend_position(CANARY) is None
    assert svc.canary_ok(CANARY)


async def test_guest_message_refreshes_the_idle_timer(store):
    await places.set_at(store, CANARY, places.new_at(places.PLACE_CELLAR, NOW))
    for minutes in (90, 170, 260):
        svc = _make(store, now=NOW + timedelta(minutes=minutes))
        assert await svc._tend_position(CANARY) is None
    assert (await places.get_at(store, CANARY)).place == places.PLACE_CELLAR


async def test_before_turn_for_non_canary_never_touches_places(store):
    svc = _make(store)
    await places.set_at(store, GUEST, places.new_at(places.PLACE_CELLAR, NOW))
    later = _make(store, now=NOW + timedelta(days=2))
    before = {k: await store.get_state(k) for k in await store.state_keys("")}
    plan = await later.before_turn(GUEST, GUEST, "Привет", is_private=True)
    assert plan.note is None
    after = {k: await store.get_state(k) for k in await store.state_keys("")}
    assert before == after
    assert svc is not None


# --- тул search: только канарейкам ---


def _ctx(interactives, user_id):
    return ai_tools.ToolContext(
        chat_id=user_id,
        dialogue_id=None,
        trigger_message_id=None,
        settings=Settings(llm=LlmConfig(model="m")),
        interactives=interactives,
        user_id=user_id,
    )


async def test_search_declaration_reaches_only_canaries(store):
    svc = _make(store)
    from sa_home_bot.subscriptions.models import Subscription

    sub = Subscription(name="me", chat_id=1, allowed_commands=frozenset({"*"}))
    kit = ai_tools.tools_for(sub)
    assert "search" in kit.handlers
    for_canary = ai_tools.for_context(kit, _ctx(svc, CANARY))
    assert for_canary is kit
    for_guest = ai_tools.for_context(kit, _ctx(svc, GUEST))
    assert "search" not in for_guest.handlers
    assert all(d["function"]["name"] != "search" for d in for_guest.declarations)
    assert len(for_guest.declarations) == len(kit.declarations) - 1
    assert "search" not in ai_tools.for_context(kit, _ctx(None, CANARY)).handlers
    # Подставной вызов от обычного гостя всё равно отказывает.
    handler = kit.handlers["search"]
    assert await handler(_ctx(svc, GUEST), {"where": "ящик"}) == places.TOOL_SEARCH_UNAVAILABLE
    assert "Ты обыскал" in await handler(_ctx(svc, CANARY), {"where": "ящик"})


# --- сброс ---


async def test_reset_scopes_split_cellar_and_catacombs(store):
    for key in (
        f"location:cabinet:{CANARY}",
        f"location:cellar:{CANARY}",
        f"location:cellar_door:{CANARY}",
        f"location:dark_room:{CANARY}",  # не в реестре: особое место катакомб
        f"location_searched:cellar:{CANARY}",
        f"location_searched:cabinet:{CANARY}",
        f"catacombs:{CANARY}",
        f"alfred_at:{CANARY}",
        f"interactive_done:cellar:{CANARY}",
        f"interactive_done:catacombs:{CANARY}",
        f"interactive_done:radio:{CANARY}",
        f"user_effect:catacombs_open:{CANARY}",
        f"user_effect:cellar_unlocked:{CANARY}",
        f"location:cellar:{GUEST}",
    ):
        await store.set_state(key, "{}")
    removed = await places.reset_progress(store, CANARY, "catacombs")
    assert sorted(removed) == sorted(
        [
            f"location:dark_room:{CANARY}",
            f"catacombs:{CANARY}",
            f"alfred_at:{CANARY}",
            f"interactive_done:catacombs:{CANARY}",
        ]
    )
    removed = await places.reset_progress(store, CANARY, "cellar")
    assert f"interactive_done:cellar:{CANARY}" in removed
    assert f"location:cellar:{CANARY}" in removed and f"location:cabinet:{CANARY}" not in removed
    left = await store.state_keys("")
    assert f"location:cabinet:{CANARY}" in left
    assert f"location_searched:cabinet:{CANARY}" in left
    assert f"interactive_done:radio:{CANARY}" in left
    assert f"location:cellar:{GUEST}" in left
