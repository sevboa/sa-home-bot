"""Этап 49.3: сюжетные предметы — «Проклятая радиостанция».

Облик копится по ходу сцены (черты от Ведущего или по лестнице), портрет
рисует служба llm (item_portrait), в кадры кабинета радио вставляется
пикселями (generate_image + paste), в финале гость получает предмет с
кнопкой, которая ставит/убирает старый передатчик (= картавость)."""

from __future__ import annotations

import json

import pytest_asyncio

from sa_home_bot.bot.interactives import cabinet, engine, items, radio
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
    assert "Как выглядит радиостанция: покрыта пылью и паутиной." in text
    assert "«трещина»" in text and "«пыль» —" not in text
    assert '"item_trait"' in text


def test_scene_note_tells_alfred_how_the_radio_looks():
    run = Run(scenario="radio", chat_id=1, user_id=1, item_traits=["пыль", "глаз"])
    note = engine.build_scene_note(radio.RADIO, run)
    assert "Как сейчас выглядит радиостанция: покрыта пылью и паутиной; на шкале" in note


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
    assert "Проклятая радиостанция" in caption and "жижа слизня" in caption and "чулане" in caption
    # Повторный финал (другой чат) второй предмет не выдаёт.
    await svc._grant_item(
        GUEST, GUEST, await InteractiveStore(store).load_run(GUEST, "radio"), None
    )
    assert len(await store.items_of(GUEST)) == 1


async def test_item_card_actions_menu_toggles_speech(store):
    """Под карточкой — только «Действия»; перечень действий заменяет
    карточку, действие или «Назад» возвращают её (пользователь 2026-10-02)."""
    link = ItemLink()
    svc, _ = _make(store, link)
    await _finale(store, svc, item_seed=9)
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP)
    await _drain(svc)
    (owned,) = await store.items_of(GUEST)
    assert await svc.speech_clear(GUEST) is True  # старое убрано — речь чистая
    card = engine.item_keyboard(RADIO, owned["id"])
    assert [b.text for row in card.inline_keyboard for b in row] == [radio.ITEM_ACTIONS_LABEL]
    _, menu, caption = await svc.handle_item_click(
        GUEST, GUEST, owned["id"], engine.ITEM_BTN_ACTIONS, message_id=50
    )
    assert [row[0].text for row in menu.inline_keyboard] == [
        radio.ITEM_PUT_LABEL,
        radio.ITEM_BACK_LABEL,
    ]
    assert radio.ITEM_PLACE_STORED in caption
    _, back, caption = await svc.handle_item_click(
        GUEST, GUEST, owned["id"], engine.ITEM_BTN_BACK, message_id=50
    )
    assert back == card and radio.ITEM_CARD_STORED in caption
    answer, markup, caption = await svc.handle_item_click(
        GUEST, GUEST, owned["id"], engine.ITEM_BTN_PUT, message_id=50
    )
    assert answer == "Поставлено." and await svc.speech_clear(GUEST) is False
    assert markup == card and radio.ITEM_CARD_INSTALLED in caption
    _, menu, _ = await svc.handle_item_click(GUEST, GUEST, owned["id"], engine.ITEM_BTN_ACTIONS)
    assert menu.inline_keyboard[0][0].text == radio.ITEM_REMOVE_LABEL
    # Старая кнопка «Поставить» после смены формами — уже так, без переключения.
    answer, _, _ = await svc.handle_item_click(GUEST, GUEST, owned["id"], "p")
    assert answer == "Уже так."
    answer, _, _ = await svc.handle_item_click(GUEST, GUEST, owned["id"], "r")
    assert answer == "Убрано." and await svc.speech_clear(GUEST) is True
    assert [e["kind"] for e in await store.item_events(owned["id"])] == [
        "created",
        "installed",
        "removed",
    ]
    # Чужая кнопка ничего не меняет.
    assert (await svc.handle_item_click(GUEST, 999, owned["id"], "p"))[0] == radio.ITEM_NOT_YOURS
    assert engine.parse_item_callback(menu.inline_keyboard[0][0].callback_data) == (
        owned["id"],
        "r",
    )


class EditNotifier:
    """Notifier с правкой: что сняли и что свернули."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.markups: list[tuple[int, object]] = []
        self.captions: list[tuple[int, str, object, bool]] = []
        self.next_id = 700

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def send_direct(self, chat_id, text, **kw):
        await self._inner.send_direct(chat_id, text, **kw)
        self.next_id += 1
        return self.next_id

    async def edit_markup(self, chat_id, message_id, reply_markup):
        self.markups.append((message_id, reply_markup))
        return True

    async def edit_caption(self, chat_id, message_id, caption, *, reply_markup=None, photo=True):
        self.captions.append((message_id, caption, reply_markup, photo))
        return True


async def test_next_guest_message_dismisses_forms_and_folds_menu(store):
    """Пользователь 2026-10-02: форма «заменить/оставить», присланная снова,
    оставляла живыми кнопки прежней. Следующее сообщение гостя гасит все
    формы, а раскрытый перечень действий сворачивает в карточку."""
    link = ItemLink()
    svc, inner = _make(store, link)
    notifier = EditNotifier(inner)
    svc._notifier = notifier
    await InteractiveStore(store).mark_completed("radio", GUEST)
    assert await svc.tool_swap_radio(GUEST, GUEST, is_private=True) == radio.TOOL_REINSTALL_FORM
    await svc.flush_forms(GUEST)
    form_id = notifier.next_id
    (owned,) = await store.items_of(GUEST)
    await svc.handle_item_click(
        GUEST, GUEST, owned["id"], engine.ITEM_BTN_ACTIONS, message_id=555, photo=True
    )
    # Чужое сообщение в том же чате ничего не трогает.
    assert await svc.dismiss_buttons(GUEST, 999) == 0
    await svc.before_turn(GUEST, GUEST, "а что ещё есть в поместье?", is_private=True)
    assert notifier.markups == [(form_id, None)]
    ((menu_id, caption, markup, photo),) = notifier.captions
    assert menu_id == 555 and photo and radio.ITEM_CARD_INSTALLED in caption
    assert markup == engine.item_keyboard(RADIO, owned["id"])
    # Погашено один раз: следующее сообщение уже ничего не правит.
    assert await svc.dismiss_buttons(GUEST, GUEST) == 0
    # Нажатая форма из живых уходит сама.
    await svc.tool_swap_radio(GUEST, GUEST, is_private=True)
    await svc.flush_forms(GUEST)
    await svc.handle_click(
        GUEST, GUEST, "radio", engine.BTN_TOGGLE_KEEP, message_id=notifier.next_id
    )
    assert await svc.dismiss_buttons(GUEST, GUEST) == 0


async def test_ignored_offer_can_be_asked_again(store):
    link = ItemLink()
    svc, inner = _make(store, link)
    notifier = EditNotifier(inner)
    svc._notifier = notifier
    plan = await svc.before_turn(GUEST, GUEST, "радиостанция хрипит", is_private=True)
    assert plan.offered
    await svc.flush_forms(GUEST, plan)
    plan = await svc.before_turn(GUEST, GUEST, "ладно, неважно", is_private=True)
    assert notifier.markups == [(notifier.next_id, None)] and not plan.offered
    # Гость снова о радиостанции — согласие спрашивается снова, без кулдауна.
    plan = await svc.before_turn(GUEST, GUEST, "радиостанция опять хрипит", is_private=True)
    assert plan.offered


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
    # Какой передатчик на столе, Альфред узнаёт к рассказу о готовом снимке.
    assert text == cabinet.TOOL_PHOTO_STARTED
    directive = link.lines()[-1]
    assert "старая, проклятая: целая" in directive and "покрыта пылью" in directive
    # Крупно про передатчик: упор пересъёмки без слов о нём — его вставят.
    miss = ["пустой корпус передатчика"]
    await svc._save_last_photo(GUEST, 1, "стол", miss, miss)
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "пустой корпус передатчика"})
    await _drain(svc)
    gen = link.generated()[-1]
    assert gen["paste"]["place"] == "closeup" and "emphasize" not in gen
    await svc.handle_item_click(GUEST, GUEST, owned["id"], "r")
    assert await svc.tool_take_photo(GUEST, GUEST, {"caption": "Кабинет"}) == (
        cabinet.TOOL_PHOTO_STARTED
    )
    await _drain(svc)
    assert radio.RADIO_STATE_NEW in link.lines()[-1]


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
    text = await svc.tool_manor_items(GUEST, GUEST, show="радио")
    assert text == radio.TOOL_ITEMS_DRAWING.format(items="Проклятая радиостанция")
    await _drain(svc)
    (owned,) = await store.items_of(GUEST)
    assert owned["origin"] == "radio_scene_before_items" and owned["traits"] == []
    assert len(link.portraits()) == 1 and notifier.photos
    # Портрет уже есть — карточка сразу.
    text = await svc.tool_manor_items(GUEST, GUEST, show="старый передатчик")
    assert text == radio.TOOL_ITEMS_SENT.format(items="Проклятая радиостанция")
    assert len(link.portraits()) == 1


async def test_manor_items_tells_alfred_not_the_chat(store):
    """Пользователь 2026-10-02: опись — Альфреду, он отвечает своими словами;
    в чат ничего не уходит. В описи — где вещь и что с ней можно сделать."""
    link = ItemLink()
    svc, notifier = _make(store, link)
    await _finale(store, svc, item_seed=9, item_traits=["пыль"], item_key="radio-601-a")
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP)
    await _drain(svc)
    sent, photos = len(notifier.sent), len(notifier.photos)
    text = await svc.tool_manor_items(GUEST, GUEST)
    assert len(notifier.sent) == sent and len(notifier.photos) == photos
    assert "Проклятая радиостанция — в чулане" in text and "покрыта пылью" in text
    assert radio.RADIO_ACTION_PUT in text
    (owned,) = await store.items_of(GUEST)
    await svc.handle_item_click(GUEST, GUEST, owned["id"], engine.ITEM_BTN_PUT)
    text = await svc.tool_manor_items(GUEST, GUEST)
    assert "на столе в кабинете" in text and radio.RADIO_ACTION_REMOVE in text
    # Незнакомую вещь показать нельзя — Альфред получает опись.
    text = await svc.tool_manor_items(GUEST, GUEST, show="меч")
    assert text.startswith("Такой вещи в поместье нет") and len(notifier.photos) == photos


async def test_inventory_lists_items_with_place_and_card_buttons(store):
    """Этап 49.3.5: опись /items — где каждая вещь, кнопка приносит карточку."""
    link = ItemLink()
    svc, notifier = _make(store, link)
    await _finale(store, svc, item_seed=9, item_traits=["пыль"], item_key="radio-601-a")
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP)
    await _drain(svc)
    photos = len(notifier.photos)
    (owned,) = await store.items_of(GUEST)
    text, markup = await svc.inventory(GUEST)
    assert "📻 <b>Проклятая радиостанция</b> — в чулане." in text and "покрыта пылью" in text
    button = markup.inline_keyboard[0][0]
    assert button.text == "📻 Проклятая радиостанция"
    item_id, code = engine.parse_item_callback(button.callback_data)
    answer, new_markup, _ = await svc.handle_item_click(GUEST, GUEST, item_id, code)
    assert answer == radio.ITEM_CARD_SHOWN and new_markup is None
    assert len(notifier.photos) == photos + 1
    # Поставили — в описи «на столе».
    await svc.handle_item_click(GUEST, GUEST, owned["id"], engine.ITEM_BTN_PUT)
    text, _ = await svc.inventory(GUEST)
    assert "на столе в кабинете" in text


async def test_inventory_empty(store):
    svc, _ = _make(store, ItemLink())
    text, markup = await svc.inventory(GUEST)
    assert text == items.INVENTORY_EMPTY and markup is None


async def test_manor_items_without_items(store):
    svc, _ = _make(store, ItemLink())
    assert await svc.tool_manor_items(GUEST, GUEST) == radio.TOOL_ITEMS_NONE


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
    # Теперь облик тот же — в другой чат повтор без генерации (в этот же —
    # снимали бы заново: дубль в чате не шлём).
    await svc.tool_take_photo(-100, GUEST, {})
    await _drain(svc)
    assert len(link.generated()) == 2 and outside


# --- действия объявляет вид (Этап 49.3.7) ---


def test_kind_declares_actions_by_place():
    """Карточка, перечень и опись строятся из ItemKind.actions, а не из типа."""
    assert [a.name for a in RADIO.available(items.PLACE_DESK)] == ["remove"]
    assert [a.name for a in RADIO.available(items.PLACE_STOREROOM)] == ["put"]
    assert RADIO.action("p") is RADIO.action("put") and RADIO.action("x") is None
    assert RADIO.transferable is False
    plain = items.ItemKind(
        type="cup", name="Чашка", archetype="", checks=(), traits={}, ladder=(),
        paste_hint="", focus_re=items.re.compile("чашк"),
    )  # fmt: skip
    assert engine.item_keyboard(plain, 1) is None
    menu = engine.item_actions_keyboard(RADIO, 7, items.PLACE_DESK, engine.ITEM_FROM_INVENTORY)
    assert [row[0].callback_data for row in menu.inline_keyboard] == ["it:7:ir", "it:7:ib"]


async def _swapped(store):
    link = ItemLink()
    svc, inner = _make(store, link)
    notifier = EditNotifier(inner)
    svc._notifier = notifier
    await _finale(store, svc, item_seed=9, item_traits=["пыль"], item_key="radio-601-a")
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP)
    await _drain(svc)
    (owned,) = await store.items_of(GUEST)
    return svc, notifier, owned


async def test_inventory_actions_return_to_inventory(store):
    """Особенные вещи — действия всегда доступны прямо из /items."""
    svc, notifier, owned = await _swapped(store)
    _, markup = await svc.inventory(GUEST)
    _, actions_btn = markup.inline_keyboard[0]
    assert actions_btn.text == radio.ITEM_ACTIONS_LABEL
    _, code = engine.parse_item_callback(actions_btn.callback_data)
    _, menu, caption = await svc.handle_item_click(GUEST, GUEST, owned["id"], code, message_id=80)
    assert radio.ITEM_PLACE_STORED in caption
    assert [row[0].text for row in menu.inline_keyboard] == [
        radio.ITEM_PUT_LABEL,
        radio.ITEM_BACK_LABEL,
    ]
    _, put = engine.parse_item_callback(menu.inline_keyboard[0][0].callback_data)
    answer, back, text = await svc.handle_item_click(
        GUEST, GUEST, owned["id"], put, message_id=80
    )
    assert answer == "Поставлено." and await svc.speech_clear(GUEST) is False
    assert text.startswith(items.INVENTORY_TITLE) and "на столе в кабинете" in text
    assert back.inline_keyboard[0][1].text == radio.ITEM_ACTIONS_LABEL
    # Перечень из описи, брошенный открытым, сворачивается обратно в опись.
    await svc.handle_item_click(GUEST, GUEST, owned["id"], code, message_id=81)
    await svc.dismiss_buttons(GUEST, GUEST)
    ((menu_id, text, _, photo),) = notifier.captions
    assert menu_id == 81 and not photo and text.startswith(items.INVENTORY_TITLE)
    # «Назад» из описи — опись.
    _, _, text = await svc.handle_item_click(GUEST, GUEST, owned["id"], "ib")
    assert text.startswith(items.INVENTORY_TITLE)


async def test_item_action_tool_sends_form_and_button_applies(store):
    svc, notifier, owned = await _swapped(store)
    text = await svc.tool_manor_items(GUEST, GUEST)
    assert "(action=put)" in text
    assert await svc.tool_item_action(GUEST, GUEST, item="радиостанция", action="remove") == (
        radio.TOOL_ACTION_ALREADY.format(where="в чулане")
    )
    assert (await svc.tool_item_action(GUEST, GUEST, item="радио", action="съесть")).startswith(
        "Так с этой вещью нельзя"
    )
    text = await svc.tool_item_action(GUEST, GUEST, item="радиостанция", action="put")
    assert text == radio.TOOL_ACTION_FORM.format(form=radio.RETURN_FORM_TEXT)
    await svc.flush_forms(GUEST)
    form_id = notifier.next_id
    assert notifier._inner.sent[-1] == (GUEST, radio.RETURN_FORM_TEXT)
    # Форма — живые кнопки: следующее сообщение гостя её гасит.
    await svc.dismiss_buttons(GUEST, GUEST)
    assert notifier.markups == [(form_id, None)] and await svc.speech_clear(GUEST) is True
    # Новая форма, на этот раз согласие.
    await svc.tool_item_action(GUEST, GUEST, item="радиостанция", action="put")
    await svc.flush_forms(GUEST)
    answer, markup, done = await svc.handle_item_click(
        GUEST, GUEST, owned["id"], "fp", message_id=notifier.next_id
    )
    assert answer == "Поставлено." and markup.inline_keyboard == []
    assert done == radio.ITEM_FORM_DONE.format(form=radio.RETURN_FORM_TEXT, confirm="Вернуть")
    assert await svc.speech_clear(GUEST) is False
    assert await svc.dismiss_buttons(GUEST, GUEST) == 0  # нажатая форма ушла из живых
    # «Оставить как есть» ничего не меняет.
    await svc.tool_item_action(GUEST, GUEST, item="радиостанция", action="remove")
    await svc.flush_forms(GUEST)
    answer, _, kept = await svc.handle_item_click(GUEST, GUEST, owned["id"], "fkr")
    assert kept == radio.ITEM_FORM_KEPT.format(form=radio.REINSTALL_FORM_TEXT)
    assert await svc.speech_clear(GUEST) is False
