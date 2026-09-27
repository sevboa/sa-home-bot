"""Интерактивы (Этап 47, bot/interactives): согласие, запрет в переписке,
рамки темпа поверх Ведущего, финал с гарантированной формой, глобальная
завершённость на гостя и переключатель передатчика после неё.

Ведущий и служба llm — подделка FakeNodeLink (ответ Ведущего задаётся
тестом), Store — настоящий на временной SQLite."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest_asyncio

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.interactives import engine, radio
from sa_home_bot.bot.interactives.base import (
    STATUS_ACTIVE,
    STATUS_DECLINED,
    STATUS_DONE,
    STATUS_IDLE,
    STATUS_OFFERED,
    STATUS_PAUSED,
    InteractiveStore,
    Run,
)
from sa_home_bot.bot.interactives.director import DirectorDecision, parse_decision
from sa_home_bot.bot.interactives.engine import Interactives, apply_decision
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.tasks import protocol as task_protocol

GUEST = 501
OTHER = 502
GROUP = -1001
PINNED = 503
COMPLAINT = "Альфред, ты картавишь!"


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self._next = 1000

    async def send_direct(
        self, chat_id, text, reply_to_message_id=None, reply_markup=None, message_thread_id=None
    ):
        self._next += 1
        self.sent.append((chat_id, text, reply_markup))
        return self._next

    def texts(self, chat_id: int) -> list[str]:
        return [text for chat, text, _ in self.sent if chat == chat_id]


class FakeNodeLink:
    """Ведущий (chat role=director), set_speech_clear и tasks.create."""

    def __init__(self) -> None:
        self.director_replies: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail_llm = False

    async def command(self, action, args, dst=None, timeout=None):
        self.calls.append((action, args))
        if action == task_protocol.ACTION_CREATE:
            return {"task_id": 1}
        if self.fail_llm:
            raise ServiceUnavailableError("mycraft спит")
        if action == "chat":
            assert args["role"] == "director"
            reply = self.director_replies.pop(0) if self.director_replies else "не json"
            return {"response": reply}
        if action == "set_speech_clear":
            return {"changed": True}
        raise AssertionError(action)

    def actions(self) -> list[str]:
        return [a for a, _ in self.calls]


def _director(**fields) -> str:
    data = {"active": True, "stage": 0, "effect": None, "directive": None, "finale": False}
    data.update(fields)
    return json.dumps(data, ensure_ascii=False)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _make(store, *, clock=None):
    notifier = FakeNotifier()
    link = FakeNodeLink()
    settings = Settings(llm=LlmConfig(model="m", speech_therapy_pinned_chat_ids=[PINNED]))
    svc = Interactives(
        store,
        notifier,
        settings,
        lambda: link,
        now=clock or Clock(),
        choose=lambda options: options[0],
    )
    return svc, notifier, link


async def _turn(svc, text, reply="Ответ Альфреда", *, chat=GUEST, user=GUEST, private=True):
    plan = await svc.before_turn(chat, user, text, is_private=private)
    await svc.after_turn(plan, reply, dialogue_id=77)
    await svc.flush_forms(chat, plan, dialogue_id=77)
    return plan


async def _run(store, chat=GUEST) -> Run:
    run = await InteractiveStore(store).load_run(chat, radio.SCENARIO_ID)
    assert run is not None
    return run


async def _play(svc, store):
    await _turn(svc, COMPLAINT)
    assert await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_PLAY) == (
        "Хорошо.",
        radio.RADIO.offer_text + engine.OFFER_YES_SUFFIX,
        True,
    )


# --- согласие ---


async def test_offer_form_does_not_reveal_a_game(store):
    svc, notifier, _ = _make(store)
    await _turn(svc, COMPLAINT)
    _, text, markup = notifier.sent[-1]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["Да", "Нет"]
    for word in ("сценк", "игр", "Ведущ"):
        assert word not in text.lower()


async def test_complaint_offers_scene_but_does_not_start_it(store):
    svc, notifier, link = _make(store)
    plan = await _turn(svc, COMPLAINT)
    assert plan.offered and not plan.scene and plan.note is None
    assert notifier.texts(GUEST) == [radio.RADIO.offer_text]
    assert (await _run(store)).status == STATUS_OFFERED
    assert "chat" not in link.actions()  # Ведущий до согласия не зовётся


async def test_no_trigger_no_offer(store):
    svc, notifier, _ = _make(store)
    plan = await _turn(svc, "Какая завтра погода?")
    assert plan.scenario is None and notifier.sent == []


async def test_scenes_only_in_private_chats_and_not_pinned(store):
    svc, notifier, _ = _make(store)
    await _turn(svc, COMPLAINT, chat=GROUP, private=False)
    await _turn(svc, COMPLAINT, chat=PINNED, user=PINNED)
    assert notifier.sent == []


async def test_play_starts_scene_with_note_for_alfred(store):
    svc, _, _ = _make(store)
    await _play(svc, store)
    plan = await svc.before_turn(GUEST, GUEST, "Слышу «пгивет»", is_private=True)
    assert plan.scene
    assert radio.RADIO.scene_frame in plan.note
    assert radio.RADIO.ladder[0] in plan.note
    assert f"Гость: {COMPLAINT}" in plan.note  # журнал держит начало сцены


async def test_yes_makes_alfred_react_with_conversation_context(store):
    svc, _, link = _make(store)
    await _turn(svc, COMPLAINT, reply="Я говогю безупгечно.")
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_PLAY)
    created = [args for action, args in link.calls if action == task_protocol.ACTION_CREATE]
    assert len(created) == 1
    directive = created[0]["args"]["messages"][0]["content"]
    assert f"Гость: {COMPLAINT}" in directive
    assert "что-то не так" in directive


async def test_yes_reply_goes_to_the_forms_thread_and_dialogue(store):
    """Живой баг 2026-09-28: реакция на «Да» уходила в общий топик лички."""
    svc, notifier, link = _make(store)
    await _turn(svc, COMPLAINT)
    form_id = notifier._next  # форма согласия записана в диалог 77
    await svc.handle_click(
        GUEST, GUEST, "radio", engine.BTN_PLAY, message_id=form_id, message_thread_id=42
    )
    meta = next(a for act, a in link.calls if act == task_protocol.ACTION_CREATE)["meta"]
    assert meta["message_thread_id"] == 42
    assert meta["trigger_message_id"] == form_id
    assert meta["dialogue_id"] == 77


async def test_no_keeps_alfred_silent(store):
    svc, _, link = _make(store)
    await _turn(svc, COMPLAINT)
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_NEVER)
    assert task_protocol.ACTION_CREATE not in link.actions()


async def test_scene_alternates_acting_and_asking_for_advice(store):
    svc, _, link = _make(store)
    await _play(svc, store)
    link.director_replies = [_director(), _director()]
    first = await _turn(svc, "Слышу «пгивет»")
    second = await _turn(svc, "Всё ещё «г»")
    assert radio.RADIO.nudges[0] in first.note
    assert radio.RADIO.nudges[1] in second.note


def test_transmitter_is_not_blamed_early():
    """Альфред не кидается сразу на передатчик (пользователь 2026-09-28):
    первые стадии и «действие» через ход о передатчике не говорят."""
    r = radio.RADIO
    for text in (r.ladder[0], r.ladder[1]):
        assert "кроме передатчика" in text or "вне подозрений" in text
    assert "передатчик" not in r.nudges[0]
    assert "не вини передатчик" in radio.AFTER_AGREE_DIRECTIVE
    assert "в полном порядке" in radio.TOOL_NOT_YET
    assert "вне подозрений" in r.scene_frame
    assert "стадиях 0 и 1 передатчик вне подозрений" in r.director_prompt


def test_alfred_is_never_told_to_mention_buttons():
    """Альфред не ломает четвёртую стену: «нажмите Заменить» — только в форме,
    не в его речи (пользователь 2026-09-28)."""
    to_alfred = [
        radio.TOOL_SWAP_FORM,
        radio.TOOL_RETURN_FORM,
        radio.TOOL_REINSTALL_FORM,
        radio.RADIO.finale_directive,
    ]
    for text in to_alfred:
        assert "«Заменить»" not in text and "кнопкой" not in text
        assert "предложи нажать" not in text
    assert "не указывай собеседнику, что ему нажать" in radio.RADIO.scene_frame


def test_closing_line_after_swap_does_not_announce_the_change():
    """Что речь стала чистой, гость заметит сам (пользователь 2026-09-28)."""
    text = radio.AFTER_SWAP_DIRECTIVE
    assert "Не говори" in text
    assert "как прекрасно теперь слышно" not in text


async def test_later_gives_cooldown_then_offers_again(store):
    clock = Clock()
    svc, notifier, _ = _make(store, clock=clock)
    await _turn(svc, COMPLAINT)
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_LATER)
    assert (await _run(store)).status == STATUS_DECLINED
    await _turn(svc, COMPLAINT)
    assert len(notifier.texts(GUEST)) == 1  # в кулдауне — молчим
    clock.now += timedelta(hours=25)
    await _turn(svc, COMPLAINT)
    assert len(notifier.texts(GUEST)) == 2


async def test_never_opts_chat_out_of_all_scenes(store):
    svc, notifier, _ = _make(store)
    await _turn(svc, COMPLAINT)
    answer, text, _ = await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_NEVER)
    assert text == radio.RADIO.offer_text + engine.OFFER_NO_SUFFIX
    assert await svc.is_opted_out(GUEST)
    await _turn(svc, COMPLAINT)
    assert await ai_tools.tool_swap_radio(_ctx(svc), {}) == radio.TOOL_OPTED_OUT
    assert len(notifier.texts(GUEST)) == 1
    await svc.set_opted_out(GUEST, False)  # /interactives on
    await _turn(svc, COMPLAINT)
    assert (await _run(store)).status == STATUS_DECLINED  # кулдаун «не сейчас» всё ещё в силе


async def test_offer_expires_after_ttl(store):
    clock = Clock()
    svc, _, _ = _make(store, clock=clock)
    await _turn(svc, COMPLAINT)
    clock.now += timedelta(hours=2)
    assert await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_PLAY) == (
        "Вопрос уже неактуален.",
        radio.RADIO.offer_text + engine.OFFER_EXPIRED_SUFFIX,
        True,
    )
    assert (await _run(store)).status == STATUS_DECLINED


async def test_offer_buttons_only_for_that_guest_and_only_once(store):
    svc, _, _ = _make(store)
    await _turn(svc, COMPLAINT)
    assert (await svc.handle_click(GUEST, OTHER, "radio", engine.BTN_PLAY))[0] == (
        "Эта форма не для вас."
    )
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_PLAY)
    assert (await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_LATER))[0] == "Уже решено."


async def test_exit_button_pauses_and_next_complaint_asks_again(store):
    svc, notifier, _ = _make(store)
    await _play(svc, store)
    answer, text, drop = await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_EXIT)
    assert (answer, text, drop) == (engine.EXIT_ALERT, None, True)
    assert (await _run(store)).status == STATUS_PAUSED
    plan = await _turn(svc, COMPLAINT)
    assert plan.offered


# --- ход сцены и Ведущий ---


async def test_director_is_invisible_and_alfred_retells_the_effect(store):
    # Ведущий гостю не виден: никаких отдельных сообщений — событие уходит
    # Альфреду скрытой подсказкой на следующий ход, и рассказывает он сам.
    svc, notifier, link = _make(store)
    await _play(svc, store)
    sent_before = len(notifier.sent)
    link.director_replies = [_director(effect="Из динамика пахнет болотом.", directive="Понюхай")]
    await _turn(svc, "Проверь антенну", "Проверяю антенну")
    assert len(notifier.sent) == sent_before
    run = await _run(store)
    assert run.transcript[-3:] == [
        "Гость: Проверь антенну",
        "Альфред: Проверяю антенну",
        "Событие: Из динамика пахнет болотом.",
    ]
    plan = await svc.before_turn(GUEST, GUEST, "ну?", is_private=True)
    assert "Подсказка на этот ход: Понюхай" in plan.note
    assert "произошло вот что: Из динамика пахнет болотом." in plan.note
    # Пересказал — на следующем ходу событие уже не подсказывается.
    link.director_replies = [_director()]
    await svc.after_turn(plan, "Фу, болотом тянет!", dialogue_id=77)
    plan = await svc.before_turn(GUEST, GUEST, "и?", is_private=True)
    assert "произошло вот что" not in plan.note


async def test_nothing_the_guest_sees_has_emoji_or_game_words(store):
    svc, notifier, link = _make(store)
    await _reach_finale(svc, store, link)
    await _turn(svc, "что там?")
    texts = [text for _, text, _ in notifier.sent]
    labels = [
        b.text
        for _, _, markup in notifier.sent
        if markup
        for row in markup.inline_keyboard
        for b in row
    ]
    for shown in texts + labels:
        assert shown.isascii() or all(ord(ch) < 0x2000 for ch in shown), shown
        for word in ("сценк", "игр", "ведущ"):
            assert word not in shown.lower(), shown


async def test_director_topic_change_makes_scene_idle_and_complaint_resumes_silently(store):
    svc, notifier, link = _make(store)
    await _play(svc, store)
    link.director_replies = [_director(active=False)]
    await _turn(svc, "А какая погода?")
    assert (await _run(store)).status == STATUS_IDLE
    sent_before = len(notifier.sent)
    plan = await _turn(svc, "Опять ты картавишь")
    assert plan.scene and not plan.offered
    assert len(notifier.sent) == sent_before  # без повторного согласия


def _fresh_run() -> Run:
    return Run(scenario="radio", chat_id=GUEST, user_id=GUEST, status=STATUS_ACTIVE)


def test_stage_grows_at_most_one_per_turn():
    run = _fresh_run()
    apply_decision(radio.RADIO, run, _decision(stage=3))
    assert run.stage == 1


def test_soft_cap_advances_stage_even_without_director():
    run = _fresh_run()
    for _ in range(radio.RADIO.stage_soft_cap):
        apply_decision(radio.RADIO, run, None)
    assert run.stage == 1 and run.turns_on_stage == 0


def test_finale_not_before_last_stage_and_min_turns():
    run = _fresh_run()
    apply_decision(radio.RADIO, run, _decision(stage=0, finale=True, finale_fault="слизь"))
    assert not run.finale
    run.stage = radio.RADIO.last_stage
    run.turns_total = 1
    apply_decision(radio.RADIO, run, _decision(stage=3, finale=True, finale_fault="слизь"))
    assert not run.finale  # ходов мало
    apply_decision(radio.RADIO, run, _decision(stage=3, finale=True, finale_fault="слизь"))
    assert not run.finale
    effect = apply_decision(radio.RADIO, run, _decision(stage=3, finale=True, finale_fault="слизь"))
    assert run.finale and run.finale_fault == "слизь"
    assert "слизь" in effect


def test_finale_forced_by_soft_cap_uses_fallback_fault():
    run = _fresh_run()
    run.stage = radio.RADIO.last_stage
    run.turns_total = 10
    run.turns_on_stage = radio.RADIO.stage_soft_cap - 1
    apply_decision(radio.RADIO, run, _decision(stage=3), choose=lambda o: o[1])
    assert run.finale and run.finale_fault == radio.RADIO.fallback_faults[1]


def _decision(**fields) -> DirectorDecision:
    base = {
        "active": True,
        "stage": 0,
        "finale": False,
        "effect": None,
        "directive": None,
        "finale_fault": None,
        "note": None,
    }
    base.update(fields)
    return DirectorDecision(**base)


def test_parse_decision_tolerates_wrapping_and_garbage():
    assert parse_decision("```json\n" + _director(stage=2) + "\n```", 0).stage == 2
    assert parse_decision("это не json", 0) is None
    assert parse_decision('{"stage": "два"}', 1).stage == 1


async def test_broken_director_does_not_break_turn(store):
    svc, notifier, link = _make(store)
    await _play(svc, store)
    link.fail_llm = True
    await _turn(svc, "Проверь антенну")
    run = await _run(store)
    assert run.status == STATUS_ACTIVE and run.turns_total == 1


# --- финал, смена передатчика, глобальность ---


async def _reach_finale(svc, store, link):
    await _play(svc, store)
    stages = [1, 2, 3, 3]
    link.director_replies = [_director(stage=s, effect=f"эффект {s}") for s in stages[:-1]]
    link.director_replies.append(
        _director(stage=3, finale=True, finale_fault="жижа слизня в катушке", effect="Жижа!")
    )
    for i in range(len(stages)):
        await _turn(svc, f"идея {i}")
    run = await _run(store)
    assert run.finale and run.finale_fault == "жижа слизня в катушке"


async def test_finale_directive_and_guaranteed_swap_form(store):
    svc, notifier, link = _make(store)
    await _reach_finale(svc, store, link)
    plan = await svc.before_turn(GUEST, GUEST, "и что теперь?", is_private=True)
    assert "жижа слизня в катушке" in plan.note and "swap_radio" in plan.note
    assert plan.force_swap_form
    await svc.after_turn(plan, "Надо менять передатчик!", dialogue_id=77)
    await svc.flush_forms(GUEST, plan, dialogue_id=77)
    assert notifier.texts(GUEST)[-1] == radio.SWAP_FORM_TEXT
    # Второй раз форма сама не лезет — только по тулу.
    plan = await _turn(svc, "не хочу")
    assert notifier.texts(GUEST).count(radio.SWAP_FORM_TEXT) == 1


async def test_swap_accept_completes_globally_and_clears_speech(store):
    svc, notifier, link = _make(store)
    await _reach_finale(svc, store, link)
    assert await ai_tools.tool_swap_radio(_ctx(svc), {}) == radio.TOOL_SWAP_FORM
    await svc.flush_forms(GUEST)
    answer, text, _ = await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP)
    assert text == radio.SWAP_ACCEPTED_TEXT
    assert (await _run(store)).status == STATUS_DONE
    assert ("set_speech_clear", {"user_id": GUEST, "clear": True}) in link.calls
    assert task_protocol.ACTION_CREATE in link.actions()  # первая чистая реплика
    # Эффект — на гостя в любом чате, в т.ч. общем.
    plan = await svc.before_turn(GROUP, GUEST, "привет", is_private=False)
    assert plan.speech_clear is True
    other = await svc.before_turn(GROUP, OTHER, "привет", is_private=False)
    assert other.speech_clear is False
    # Повторное нажатие — уже решено.
    assert (await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP))[0] == "Уже решено."


async def test_swap_works_when_llm_asleep_mirror_heals_later(store):
    svc, _, link = _make(store)
    await _reach_finale(svc, store, link)
    link.fail_llm = True
    _, text, _ = await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_SWAP)
    assert text == radio.SWAP_ACCEPTED_TEXT
    assert await svc.speech_clear(GUEST) is True


async def test_keep_old_radio_leaves_finale_open(store):
    svc, _, link = _make(store)
    await _reach_finale(svc, store, link)
    assert (await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_KEEP))[1] == (
        radio.SWAP_KEPT_TEXT
    )
    run = await _run(store)
    assert run.status == STATUS_ACTIVE and run.finale
    assert await svc.speech_clear(GUEST) is False


async def test_return_old_radio_only_after_completion(store):
    svc, notifier, link = _make(store)
    # До завершения «вернуть» нет: тул лишь предлагает сцену.
    assert await ai_tools.tool_swap_radio(_ctx(svc), {}) == radio.TOOL_OFFER
    assert (await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_RETURN_OLD))[0] == (
        "Эта форма не для вас."
    )
    await svc.handle_click(GUEST, GUEST, "radio", engine.BTN_LATER)
    await InteractiveStore(store).mark_completed("radio", GUEST)
    await InteractiveStore(store).set_effect("speech_clear", GUEST, "1")
    # После — переключатель, в любом чате, без сцены.
    ctx = _ctx(svc, chat=GROUP, private=False)
    assert await ai_tools.tool_swap_radio(ctx, {}) == radio.TOOL_RETURN_FORM
    await svc.flush_forms(GROUP)
    assert notifier.texts(GROUP) == [radio.RETURN_FORM_TEXT]
    _, text, _ = await svc.handle_click(GROUP, GUEST, "radio", engine.BTN_RETURN_OLD)
    assert text == radio.RETURN_ACCEPTED_TEXT
    assert await svc.speech_clear(GUEST) is False
    assert await ai_tools.tool_swap_radio(ctx, {}) == radio.TOOL_REINSTALL_FORM
    _, text, _ = await svc.handle_click(GROUP, GUEST, "radio", engine.BTN_INSTALL_NEW)
    assert text == radio.REINSTALL_ACCEPTED_TEXT
    assert await svc.speech_clear(GUEST) is True
    assert (await svc.handle_click(GROUP, GUEST, "radio", engine.BTN_INSTALL_NEW))[0] == "Уже так."


async def test_progress_is_per_chat(store):
    svc, _, _ = _make(store)
    await _play(svc, store)
    plan = await svc.before_turn(GROUP, GUEST, COMPLAINT, is_private=False)
    assert not plan.scene
    assert await InteractiveStore(store).load_run(GROUP, "radio") is None


async def test_tool_without_service_refuses_honestly():
    ctx = ai_tools.ToolContext(
        chat_id=GUEST, dialogue_id=None, trigger_message_id=None, settings=Settings()
    )
    assert await ai_tools.tool_swap_radio(ctx, {}) == radio.TOOL_UNAVAILABLE


def _ctx(svc, *, chat=GUEST, user=GUEST, private=True) -> ai_tools.ToolContext:
    return ai_tools.ToolContext(
        chat_id=chat,
        dialogue_id=None,
        trigger_message_id=None,
        settings=Settings(),
        interactives=svc,
        user_id=user,
        is_private=private,
    )
