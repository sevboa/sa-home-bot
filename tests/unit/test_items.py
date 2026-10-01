"""Этап 49.3: сюжетные предметы — «Проклятое радио».

Облик копится по ходу сцены (черты от Ведущего или по лестнице), портрет
рисует служба llm (item_portrait), в кадры кабинета радио вставляется
пикселями (generate_image + paste), в финале гость получает предмет с
кнопкой, которая ставит/убирает старый передатчик (= картавость)."""

from __future__ import annotations

import json

import pytest_asyncio

from sa_home_bot.bot.interactives import engine, items, radio
from sa_home_bot.bot.interactives.base import STATUS_ACTIVE, InteractiveStore, Run
from sa_home_bot.bot.interactives.director import (
    DirectorDecision,
    build_director_input,
    parse_decision,
)
from sa_home_bot.bot.interactives.engine import grow_item, photo_description
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from tests.unit.test_cabinet_photo import GUEST, FakeLink, _drain, _make

RADIO = items.RADIO


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


class ItemLink(FakeLink):
    """FakeLink + зеркало речи и задачи tasks (реплики Альфреда после кнопок)."""

    async def command(self, action, args, dst=None, timeout=None):
        if action in ("chat", "generate_image", "item_portrait"):
            return await super().command(action, args, dst=dst, timeout=timeout)
        self.calls.append((action, args))
        return {}


def _decision(**kw) -> DirectorDecision:
    base = dict(
        active=True,
        stage=0,
        finale=False,
        effect=None,
        directive=None,
        finale_fault=None,
        note=None,
    )
    base.update(kw)
    return DirectorDecision(**base)


async def _active_run(store, **kw) -> Run:
    run = Run(scenario="radio", chat_id=GUEST, user_id=GUEST, status=STATUS_ACTIVE, **kw)
    await InteractiveStore(store).save_run(run)
    return run


# --- вид и черты ---


def test_portrait_prompt_draws_only_tested_traits_with_weight():
    kind = items.ItemKind(
        type="radio",
        name="Р",
        archetype="ARCH",
        checks=(),
        ladder=("a", "b"),
        traits={"a": items.Trait("пыль", "dusty", drawn=True), "b": items.Trait("глаз", "eye")},
        paste_hint="",
        focus_re=RADIO.focus_re,
        trait_weight=1.3,
    )
    assert kind.portrait_prompt(["b", "a", "zzz"]) == "ARCH, (dusty)1.3"
    assert kind.drawn_key(["b", "a"]) == ("a",)
    assert kind.restyle(["a"]) is None  # без проклятия
    assert kind.traits_ru(["a", "b"]) == ["пыль", "глаз"]
    assert kind.next_trait(["a"]) == "b" and kind.next_trait(["a", "b"]) is None


def test_curse_grows_with_traits_and_changes_portrait_version():
    assert RADIO.restyle([]) is None
    one = RADIO.restyle(["пыль"])
    assert one["model"] == "revanim" and one["loras"] == [["rottech", 0.8]]
    assert one["strength"] == 0.25 and "dusty with cobwebs" in one["prompt"]
    assert "(" not in one["prompt"]  # SD1.5 без compel — веса не нужны
    assert RADIO.restyle(["пыль", "дым", "копоть"])["strength"] == 0.35
    # Нерисуемые черты меняют портрет через силу проклятия.
    assert RADIO.drawn_key(["пыль"]) != RADIO.drawn_key(["пыль", "дым", "копоть"])


def test_cut_key_matches_llm_key_rule():
    from sa_home_bot.llm.item_paste import KEY_RE

    assert KEY_RE.match(items.new_cut_key(RADIO, 123456789))
    assert KEY_RE.match(engine.NEW_RADIO_CUT_KEY)


def test_grow_item_takes_director_trait_or_ladder_on_key_moment():
    run = Run(scenario="radio", chat_id=1, user_id=1)
    assert grow_item(RADIO, run, _decision(item_trait="дым"), key_moment=False) == "дым"
    # Повтор — не черта; без ключевого момента лестница молчит.
    assert grow_item(RADIO, run, _decision(item_trait="дым"), key_moment=False) is None
    assert grow_item(RADIO, run, None, key_moment=False) is None
    assert grow_item(RADIO, run, None, key_moment=True) == RADIO.ladder[0]
    assert run.item_traits == ["дым", RADIO.ladder[0]]


def test_parse_decision_keeps_only_known_trait_keys():
    keys = tuple(RADIO.traits)
    assert parse_decision('{"item_trait": "«Дым»"}', 0, keys).item_trait == "дым"
    assert parse_decision('{"item_trait": "щупальца"}', 0, keys).item_trait is None
    assert parse_decision('{"item_trait": "дым"}', 0).item_trait is None


def test_director_sees_look_and_free_traits():
    run = Run(scenario="radio", chat_id=1, user_id=1, item_traits=["пыль"])
    text = build_director_input(radio.RADIO, run, finale_allowed=False)
    assert "Как выглядит передатчик: покрыт пылью и паутиной." in text
    assert "«трещина»" in text and "«пыль» —" not in text
    assert '"item_trait"' in text


def test_scene_note_tells_alfred_how_the_radio_looks():
    run = Run(scenario="radio", chat_id=1, user_id=1, item_traits=["пыль", "глаз"])
    note = engine.build_scene_note(radio.RADIO, run)
    assert "Как сейчас выглядит передатчик: покрыт пылью и паутиной; на шкале" in note


# --- описание кадра с вставкой ---


def test_photo_description_hides_radio_words_when_item_is_pasted():
    from sa_home_bot.bot.interactives.cabinet import CANON_EN, Cabinet
    from sa_home_bot.bot.interactives.transylvania import Outside

    cab = Cabinet(user_id=1, features=["чучело совы на шкафу"], scene=["передатчик дымит"])
    outside = Outside(phase="night", weather=None, local_time="21:00")
    desc, _ = photo_description(
        cab, outside, "", "из передатчика пошёл дым", item=RADIO, item_place="desk"
    )
    assert "передатчик" not in desc and "сов" in desc and CANON_EN in desc
    desc, ctx = photo_description(
        cab, outside, "дымящийся передатчик", None, item=RADIO, item_place="closeup"
    )
    assert desc == engine.ITEM_CLOSEUP_RU and "передатчик" not in ctx


# --- хранилище ---


async def test_store_items_roundtrip(store):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 1, tzinfo=UTC)
    item_id = await store.add_item(
        type="radio",
        owner_user_id=5,
        traits=["пыль"],
        image_id=None,
        origin="t",
        now=now,
        note="бабкин волос",
    )
    item = await store.item_by_id(item_id)
    assert item["traits"] == ["пыль"] and item["status"] == "owned"
    assert item["note"] == "бабкин волос"
    assert [i["id"] for i in await store.items_of(5, "radio")] == [item_id]
    assert await store.items_of(6) == []
    await store.set_item_image(item_id, 42, ["пыль", "дым"])
    assert (await store.item_by_id(item_id))["image_id"] == 42
    await store.add_item_event(item_id, "removed", user_id=5, chat_id=5, data=None, now=now)
    assert [e["kind"] for e in await store.item_events(item_id)] == ["created", "removed"]


# --- сцена: кадры с радио ---


async def test_scene_frame_draws_portrait_then_pastes_radio_on_desk(store):
    link = ItemLink()
    svc, notifier = _make(store, link)
    await _active_run(store, item_seed=77, item_traits=["пыль"])
    assert await svc._start_photo(
        GUEST,
        GUEST,
        focus="",
        caption="В кабинете",
        happening=None,
        outside=await svc._transylvania.outside(svc._now()),
        message_thread_id=None,
        trigger_message_id=None,
    )
    await _drain(svc)
    (portrait,) = link.portraits()
    assert portrait["seed"] == 77 and portrait["prompt"].startswith(items.RADIO_ARCHETYPE)
    (gen,) = link.generated()
    assert gen["paste"]["key"] == portrait["key"] and gen["paste"]["place"] == "desk"
    run = await InteractiveStore(store).load_run(GUEST, "radio")
    assert run.item_key == portrait["key"] and run.item_drawn == list(RADIO.drawn_key(["пыль"]))
    # Тот же облик — портрет не перерисовывается.
    await svc._start_photo(
        GUEST,
        GUEST,
        focus="передатчик крупно",
        caption="x",
        happening=None,
        outside=await svc._transylvania.outside(svc._now()),
        message_thread_id=None,
        trigger_message_id=None,
    )
    await _drain(svc)
    assert len(link.portraits()) == 1
    assert link.generated()[-1]["paste"]["place"] == "closeup"
    assert link.generated()[-1]["description"] == engine.ITEM_CLOSEUP_RU


async def test_other_closeups_have_no_radio(store):
    link = ItemLink()
    svc, _ = _make(store, link)
    await _active_run(store, item_seed=77)
    await svc._start_photo(
        GUEST,
        GUEST,
        focus="портрет над камином",
        caption="x",
        happening=None,
        outside=await svc._transylvania.outside(svc._now()),
        message_thread_id=None,
        trigger_message_id=None,
    )
    await _drain(svc)
    assert link.portraits() == [] and "paste" not in link.generated()[0]


# --- финал, карточка, кнопка ---


async def _finale(store, svc, **kw):
    run = await _active_run(store, finale=True, finale_fault="в катушке жижа слизня", **kw)
    return run


async def test_swap_grants_cursed_radio_card_with_toggle(store):
    link = ItemLink()
    svc, notifier = _make(store, link)
    await _finale(store, svc, item_seed=9, item_traits=["пыль", "глаз"], item_key="radio-601-a")
    answer, _, _ = await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP)
    assert answer == "Готово."
    await _drain(svc)
    (owned,) = await store.items_of(GUEST, "radio")
    assert owned["traits"] == ["пыль", "глаз"] and owned["note"] == "в катушке жижа слизня"
    image = await store.image_by_id(owned["image_id"])
    assert image["purpose"] == "item"
    assert json.loads(image["params"])["cut_key"] == "radio-601-a"
    (_, _, caption) = notifier.photos[-1]
    assert "Проклятое радио" in caption and "жижа слизня" in caption and "чулане" in caption
    # Повторный финал (другой чат) второй предмет не выдаёт.
    await svc._grant_item(
        GUEST, GUEST, await InteractiveStore(store).load_run(GUEST, "radio"), None
    )
    assert len(await store.items_of(GUEST)) == 1


async def test_item_button_toggles_speech_and_label(store):
    link = ItemLink()
    svc, _ = _make(store, link)
    await _finale(store, svc, item_seed=9)
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP)
    await _drain(svc)
    (owned,) = await store.items_of(GUEST)
    assert await svc.speech_clear(GUEST) is True  # старое убрано — речь чистая
    answer, markup, caption = await svc.handle_item_click(GUEST, GUEST, owned["id"], "p")
    assert answer == "Поставлено." and await svc.speech_clear(GUEST) is False
    assert markup.inline_keyboard[0][0].text == radio.ITEM_REMOVE_LABEL
    assert radio.ITEM_CARD_INSTALLED in caption
    # Старая карточка с «Поставить» после смены формами — уже так, без
    # переключения, надпись обновляется.
    answer, stale, _ = await svc.handle_item_click(GUEST, GUEST, owned["id"], "p")
    assert answer == "Уже так."
    assert stale.inline_keyboard[0][0].text == radio.ITEM_REMOVE_LABEL
    answer, markup, _ = await svc.handle_item_click(GUEST, GUEST, owned["id"], "r")
    assert answer == "Убрано." and await svc.speech_clear(GUEST) is True
    assert markup.inline_keyboard[0][0].text == radio.ITEM_PUT_LABEL
    assert [e["kind"] for e in await store.item_events(owned["id"])] == [
        "created",
        "installed",
        "removed",
    ]
    # Чужая кнопка ничего не меняет.
    assert (await svc.handle_item_click(GUEST, 999, owned["id"], "p"))[0] == radio.ITEM_NOT_YOURS
    assert engine.parse_item_callback(markup.inline_keyboard[0][0].callback_data) == (
        owned["id"],
        "p",
    )


async def test_cabinet_general_view_shows_the_radio_that_stands(store):
    link = ItemLink()
    svc, _ = _make(store, link)
    await _finale(store, svc, item_seed=9, item_key="radio-601-a")
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP)
    await _drain(svc)
    outside = await svc._transylvania.outside(svc._now())
    # Старое убрано — на столе новый передатчик, общий для всех.
    await svc._start_photo(
        GUEST,
        GUEST,
        focus="",
        caption="Кабинет",
        happening=None,
        outside=outside,
        message_thread_id=None,
        trigger_message_id=None,
    )
    await _drain(svc)
    assert link.generated()[-1]["paste"]["key"] == engine.NEW_RADIO_CUT_KEY
    assert link.portraits()[-1]["key"] == engine.NEW_RADIO_CUT_KEY
    # Поставили старое — на столе оно.
    (owned,) = await store.items_of(GUEST)
    await svc.handle_item_click(GUEST, GUEST, owned["id"], "p")
    await svc._start_photo(
        GUEST,
        GUEST,
        focus="",
        caption="Кабинет",
        happening=None,
        outside=outside,
        message_thread_id=None,
        trigger_message_id=None,
    )
    await _drain(svc)
    assert link.generated()[-1]["paste"]["key"] == "radio-601-a"


async def test_general_view_by_words_puts_radio_on_desk_and_alfred_knows_it(store):
    """Живой прогон 2026-10-01: «Вид кабинета», «общий план кабинета» шли
    крупным планом без радио, а Альфред ждал «выпотрошенный корпус»."""
    link = ItemLink()
    svc, _ = _make(store, link)
    await _finale(store, svc, item_seed=9, item_traits=["пыль"], item_key="radio-601-a")
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP)
    await _drain(svc)
    (owned,) = await store.items_of(GUEST)
    await svc.handle_item_click(GUEST, GUEST, owned["id"], "p")
    text = await svc.tool_take_photo(
        GUEST,
        GUEST,
        {"caption": "Вид кабинета после инцидента", "focus": "общий план кабинета"},
    )
    await _drain(svc)
    gen = link.generated()[-1]
    assert gen["mode"] != "scene" and gen["paste"]["place"] == "desk"
    assert "старый, проклятый: целый" in text and "покрыт пылью" in text
    # Крупно про передатчик: упор пересъёмки без слов о нём — его вставят.
    miss = ["пустой корпус передатчика"]
    await svc._save_last_photo(GUEST, 1, "стол", miss, miss)
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "пустой корпус передатчика"})
    await _drain(svc)
    gen = link.generated()[-1]
    assert gen["paste"]["place"] == "closeup" and "emphasize" not in gen
    await svc.handle_item_click(GUEST, GUEST, owned["id"], "r")
    text = await svc.tool_take_photo(GUEST, GUEST, {"caption": "Кабинет"})
    assert radio.RADIO_STATE_NEW in text


async def test_photo_of_stored_cursed_radio_sends_its_card(store):
    """Живой прогон 2026-10-01: «покажи проклятый радиоприёмник», когда он в
    чулане, снимал кабинет с новым радио — и Альфред пенял на плёнку."""
    link = ItemLink()
    svc, notifier = _make(store, link)
    await _finale(store, svc, item_seed=9, item_key="radio-601-a")
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP)
    await _drain(svc)
    cards = len(notifier.photos)
    shots = len(link.generated())
    text = await svc.tool_take_photo(
        GUEST, GUEST, {"caption": "Тот самый", "focus": "проклятый передатчик в чулане"}
    )
    assert text == radio.TOOL_PHOTO_STORED_ITEM
    assert len(notifier.photos) == cards + 1 and len(link.generated()) == shots
    # Новый на столе снимается как обычно.
    text = await svc.tool_take_photo(GUEST, GUEST, {"focus": "новый передатчик крупно"})
    await _drain(svc)
    assert text != radio.TOOL_PHOTO_STORED_ITEM and len(link.generated()) == shots + 1


async def test_guest_who_finished_before_items_gets_radio_lazily(store):
    link = ItemLink()
    svc, notifier = _make(store, link)
    await InteractiveStore(store).mark_completed("radio", GUEST)
    text = await svc.tool_show_items(GUEST, GUEST)
    assert text == radio.TOOL_ITEMS_DRAWING.format(items="Проклятое радио")
    await _drain(svc)
    (owned,) = await store.items_of(GUEST)
    assert owned["origin"] == "radio_scene_before_items" and owned["traits"] == []
    assert len(link.portraits()) == 1 and notifier.photos
    # Портрет уже есть — карточка сразу.
    text = await svc.tool_show_items(GUEST, GUEST)
    assert text == radio.TOOL_ITEMS_SENT.format(items="Проклятое радио")
    assert len(link.portraits()) == 1


async def test_show_items_without_items(store):
    svc, _ = _make(store, ItemLink())
    assert await svc.tool_show_items(GUEST, GUEST) == radio.TOOL_ITEMS_NONE


async def test_lazy_radio_is_drawn_instead_of_reshowing_old_general_view(store):
    from sa_home_bot.bot.interactives import cabinet as cabinet_mod

    link = ItemLink()
    svc, notifier = _make(store, link)
    outside = await svc._transylvania.outside(svc._now())
    # Старый общий вид без радио (до Этапа 49.3).
    await svc.tool_take_photo(GUEST, GUEST, {})
    await _drain(svc)
    assert "paste" not in link.generated()[-1]
    # Гость прошёл сцену раньше — предмета нет, портрета нет: снимок не
    # повторяется, а рисуется заново с радио.
    await InteractiveStore(store).mark_completed("radio", GUEST)
    await InteractiveStore(store).set_effect("speech_clear", GUEST, "0")
    await svc.tool_take_photo(GUEST, GUEST, {})
    await _drain(svc)
    assert len(link.generated()) == 2 and link.generated()[-1]["paste"]["place"] == "desk"
    cab = await cabinet_mod.load(store, GUEST)
    image_id = (await store.items_of(GUEST))[0]["image_id"]
    assert any(key.endswith(f":{image_id}") for key in cab.photos)
    # Теперь облик тот же — повтор без генерации.
    await svc.tool_take_photo(GUEST, GUEST, {})
    await _drain(svc)
    assert len(link.generated()) == 2 and outside
