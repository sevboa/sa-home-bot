from __future__ import annotations

import json
import re

import pytest

from sa_home_bot.bot.interactives import items, keyboards, photo_text, places, radio
from sa_home_bot.bot.interactives.base import STATUS_ACTIVE, Run
from tests.unit.interactives_kit import (
    CANARY,
    GUEST,
    Dice,
    cellar_run,
    drain,
    make_service,
    run_turn,
)

LAB = places.RoomCanon(
    id="testlab",
    name_ru="тестовая комната",
    in_ru="в тестовой комнате",
    canon_ru="Тестовая комната со стеллажами.",
    canon_en="test room with shelves",
    caption="Тест",
    scene_caption="В тесте",
    stem="тест",
    in_en="in a test room",
    group=places.GROUP_CATACOMBS,
    specials=(places.DARKROOM_CAMERA,),
    trinkets=("пуговицу",),
)


@pytest.fixture(autouse=True)
def lab(monkeypatch):
    monkeypatch.setitem(places.PLACES, LAB.id, LAB)


async def _camera(svc, user=CANARY) -> dict:
    item = await svc._create_found_item(user, items.CAMERA, "test")
    assert item is not None
    return item


def _search(where, searched, have=frozenset()):
    return places.search_room(
        LAB, where, searched, rng=lambda: 0.99, choose=lambda o: o[0], have_items=have
    )


def test_first_find_ignores_what_alfred_named():
    searched: list[str] = []
    first = _search("пустую полку", searched)
    assert first.kind == places.SEARCH_KIND_SPECIAL and first.item == "camera"
    assert first.text == places.DARKROOM_CAMERA_TEXT and "оставил" in first.text
    second = _search("ящик", searched)
    assert second.kind == places.SEARCH_KIND_EMPTY and second.item is None
    assert _search("в ящике", searched).kind == places.SEARCH_KIND_EMPTY
    assert _search("ящик", searched).kind == places.SEARCH_KIND_REPEAT


def test_find_with_an_owned_item_is_skipped():
    searched: list[str] = []
    result = _search("полку", searched, frozenset({"camera"}))
    assert result.kind == places.SEARCH_KIND_EMPTY and result.item is None


def test_find_by_pattern_and_plain_find_still_work():
    canon = places.RoomCanon(
        **{
            **LAB.__dict__,
            "specials": (places.Find(re.compile("бочк"), "Там лаз."),),
        }
    )
    kw = {"rng": lambda: 0.99, "choose": lambda o: o[0]}
    assert places.search_room(canon, "ящик", [], **kw).kind == places.SEARCH_KIND_EMPTY
    hit = places.search_room(canon, "дальнюю бочку", [], **kw)
    assert hit.kind == places.SEARCH_KIND_SPECIAL and hit.item is None


async def test_search_gives_the_camera_without_a_director(store):
    svc, _, _ = make_service(store)
    await svc._move_to(CANARY, LAB.id)
    answer = await svc.tool_search(CANARY, CANARY, "полка у стены")
    assert "старый фотоаппарат" in answer and "оставил" in answer
    camera = (await store.items_of(CANARY, "camera"))[0]
    assert camera["origin"] == "find:testlab" and camera["note"] in items.CAMERA.fallback_notes
    assert camera["note"] in answer
    again = await svc.tool_search(CANARY, CANARY, "ещё полка")
    assert "фотоаппарат" not in again
    assert len(await store.items_of(CANARY, "camera")) == 1


async def test_search_in_a_scene_hands_the_find_to_the_director(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    run = Run(scenario="cellar", chat_id=CANARY, user_id=CANARY, status=STATUS_ACTIVE, stage=4)
    await svc._state.save_run(run)
    await svc._move_to(CANARY, LAB.id)
    link.director_replies = [
        json.dumps(
            {
                "result": "На полке старый аппарат в кожаном чехле.",
                "stage": 4,
                "found_item": {
                    "traits": ["кожа потёрта", "трещина на видоискателе"],
                    "note": "«Зоннтаг-Лукс», 1941 года.",
                },
            },
            ensure_ascii=False,
        )
    ]
    answer = await svc.tool_search(CANARY, CANARY, "полка")
    assert "На полке старый аппарат в кожаном чехле." in answer
    assert "старый фотоаппарат" in link.director_inputs[-1]
    assert "производитель и модель" in link.director_inputs[-1]
    assert '"found_item"' in link.director_inputs[-1]
    run = await cellar_run(store)
    camera = (await store.items_of(CANARY, "camera"))[0]
    fresh = await store.item_by_id(camera["id"])
    assert fresh["note"] == "«Зоннтаг-Лукс», 1941 года"
    assert fresh["traits"] == ["кожа потёрта", "трещина на видоискателе"]
    assert "found_item" not in run.world


async def test_search_gives_nothing_to_non_canary(store):
    svc, _, _ = make_service(store)
    assert await svc.tool_search(GUEST, GUEST, "полка") == places.TOOL_SEARCH_UNAVAILABLE
    assert await store.items_of(GUEST) == []


async def test_card_and_inventory_show_the_invented_model(store):
    svc, _, _ = make_service(store)
    camera = await _camera(svc)
    await store.update_item(camera["id"], note="Цейс «Икофлекс», 1938")
    camera = await store.item_by_id(camera["id"])
    caption = keyboards.item_caption(items.CAMERA, camera, False)
    assert "Аппарат: Цейс «Икофлекс», 1938." in caption and "в чулане" in caption
    lines = await svc.manor_lines(CANARY, [camera])
    assert "Аппарат: Цейс «Икофлекс», 1938." in lines
    assert "action=use" in lines and "action=back" not in lines
    text, _ = await svc.inventory(CANARY)
    assert "Старый фотоаппарат" in text and "лежит в чулане" in text


async def test_use_form_warns_in_alfreds_voice(store):
    svc, notifier, _ = make_service(store)
    camera = await _camera(svc)
    answer = await svc.tool_item_action(CANARY, CANARY, item="фотоаппарат", action="use")
    assert "Снимать этим аппаратом?" in answer
    await svc.flush_forms(CANARY)
    form = notifier.texts(CANARY)[-1]
    assert "чётче" in form and "видит то, чего нет" in form
    assert notifier.sent_with_markup[-1][2].inline_keyboard[0][0].text == "Снимать им"
    assert (
        notifier.sent_with_markup[-1][2].inline_keyboard[0][0].callback_data
        == f"it:{camera['id']}:fs"
    )
    assert "Так с этой вещью нельзя" in await svc.tool_item_action(
        CANARY, CANARY, item="фотоаппарат", action="back"
    ) or "Уже так" in await svc.tool_item_action(CANARY, CANARY, item="фотоаппарат", action="back")


async def test_use_and_back_toggle_the_effect_and_the_place(store):
    svc, _, _ = make_service(store)
    camera = await _camera(svc)
    assert await svc.item_place(CANARY, camera) == items.PLACE_STOREROOM
    assert await svc.photo_size(CANARY) is None
    answer, _, _ = await svc.handle_item_click(CANARY, CANARY, camera["id"], "s")
    assert answer == "Теперь снимаю им."
    assert await svc.item_place(CANARY, camera) == items.PLACE_DESK
    assert await svc.photo_size(CANARY) == 128
    assert json.loads(await store.get_state(f"user_effect:camera:{CANARY}"))["expires_at"] is None
    assert "предупреди" in svc.spoken[-1][0]
    again, _, _ = await svc.handle_item_click(CANARY, CANARY, camera["id"], "s")
    assert again == "Уже так."
    answer, _, _ = await svc.handle_item_click(CANARY, CANARY, camera["id"], "v")
    assert answer == "Вернул прежний."
    assert await svc.photo_size(CANARY) is None
    assert await store.get_state(f"user_effect:camera:{CANARY}") is None
    assert len(await store.items_of(CANARY, "camera")) == 1


async def test_camera_text_with_model_goes_to_alfred_note(store):
    svc, _, _ = make_service(store)
    camera = await _camera(svc)
    await store.update_item(camera["id"], note="Цейс «Икофлекс»")
    await svc.handle_item_click(CANARY, CANARY, camera["id"], "s")
    plan = await svc.before_turn(CANARY, CANARY, "Привет", is_private=True)
    assert "(Цейс «Икофлекс»)" in plan.note and "иногда видит то, чего нет" in plan.note


async def _snap(svc, link, **args):
    before = len(link.generated)
    await svc.tool_take_photo(CANARY, CANARY, args)
    await svc.wait_photo(CANARY)
    await drain(svc)
    assert len(link.generated) == before + 1
    return link.generated[-1]


async def test_all_alfred_photos_get_size_128_with_the_camera(store):
    svc, _, link = make_service(store)
    camera = await _camera(svc)
    await svc.handle_item_click(CANARY, CANARY, camera["id"], "s")
    assert (await _snap(svc, link, focus="письменный стол"))["size"] == 128
    assert (await _snap(svc, link, selfie=True))["size"] == 128
    assert (await _snap(svc, link))["size"] == 128
    await svc._move_to(CANARY, places.PLACE_CELLAR)
    assert (await _snap(svc, link, focus="бочки"))["size"] == 128
    await svc._mood_selfie_shot(
        CANARY,
        CANARY,
        "Ах!",
        "joy",
        3,
        "",
        message_thread_id=None,
        trigger_message_id=None,
        on_status=None,
    )
    assert link.generated[-1]["size"] == 128
    await svc._greeting_portrait(CANARY, CANARY, None, None)
    assert link.generated[-1]["size"] == 128


async def test_without_the_camera_nothing_changes_for_anyone(store):
    svc, _, link = make_service(store)
    await _camera(svc)
    assert "size" not in await _snap(svc, link, focus="письменный стол")
    assert "size" not in await _snap(svc, link, selfie=True)
    await svc._state.set_timed_effect("camera", GUEST, None)
    assert await svc.photo_size(GUEST) is None
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "стол"})
    await svc.wait_photo(GUEST)
    await drain(svc)
    assert "size" not in link.generated[-1]


async def test_camera_effect_stops_when_guest_leaves_the_canary_list(store):
    svc, _, link = make_service(store)
    camera = await _camera(svc)
    await svc.handle_item_click(CANARY, CANARY, camera["id"], "s")
    assert await svc.photo_size(CANARY) == 128
    gone, _, _ = make_service(store, canary=())
    assert await gone.photo_size(CANARY) is None
    assert await gone._effects_note(CANARY) is not None
    plan = await gone.before_turn(CANARY, CANARY, "Привет", is_private=True)
    assert plan.note is None
    text, _ = await gone.inventory(CANARY)
    assert "фотоаппарат" not in text.lower()


def test_photo_state_key_separates_sizes():
    from sa_home_bot.bot.interactives import cabinet
    from sa_home_bot.bot.interactives.transylvania import Outside

    cab = cabinet.Cabinet(user_id=1)
    out = Outside(phase="day", weather="clear", local_time="13:00")
    plain = photo_text.photo_state_key(cab, out, None)
    assert plain == cab.state_key(out.key)
    assert photo_text.photo_state_key(cab, out, None, 128) == plain + "#px128"


def test_radio_texts_are_unchanged():
    item = {"traits": ["пыль"], "note": "из-под крышки идёт дым", "status": "owned"}
    old = radio.ITEM_CARD_CAPTION.format(
        name=items.RADIO.name,
        traits=radio.ITEM_CARD_TRAITS.format(traits="покрыта пылью и паутиной"),
        fault=radio.ITEM_CARD_FAULT.format(fault="из-под крышки идёт дым"),
        where=radio.ITEM_CARD_INSTALLED,
    )
    assert keyboards.item_caption(items.RADIO, item, True) == old
    assert items.RADIO.where_ru(items.PLACE_DESK) == "на столе в кабинете"
    assert keyboards.item_actions_caption(items.RADIO, items.PLACE_STOREROOM) == (
        radio.ITEM_ACTIONS_CAPTION.format(name=items.RADIO.name, where=radio.ITEM_PLACE_STORED)
    )
    assert items.RADIO.traits_ru(["пыль", "нет такой"]) == ["покрыта пылью и паутиной"]


def test_find_kind_prefers_exact_and_narrow_words():
    assert items.find_kind("корпус фотоаппарата").type == "camera"
    assert items.find_kind("бутылка вина").type == "wine"
    assert items.find_kind("радиостанция").type == "radio"
    assert items.find_kind("корпус", [items.RADIO]).type == "radio"
    assert items.find_kind("фотоаппарат", [items.RADIO]) is None


async def test_non_canary_radio_flow_is_untouched(store):
    svc, _, _ = make_service(store)
    text, markup = await svc.inventory(GUEST)
    assert text.startswith(items.INVENTORY_TITLE) or text == items.INVENTORY_EMPTY
    plan = await svc.before_turn(GUEST, GUEST, "Привет", is_private=True)
    assert plan.note is None
    await run_turn(svc, "Привет", chat=GUEST, user=GUEST)
    assert await svc._state.get_effect("camera", GUEST) is None


async def test_reset_returns_the_camera_to_be_found_again(store):
    svc, _, _ = make_service(store)
    camera = await _camera(svc)
    await svc._state.set_timed_effect("camera", CANARY, None)
    await places.reset_progress(store, CANARY, "catacombs")
    assert await store.items_of(CANARY, items.CAMERA_TYPE) == []
    assert await store.get_state(f"user_effect:camera:{CANARY}") is None
    assert (await store.item_by_id(int(camera["id"])))["status"] == "consumed"
