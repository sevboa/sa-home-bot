"""Формы подтверждения (Этап 45/46, bot/pending_actions.py) на настоящем
Store: права нажатия, двойное нажатие, оба срока, порядок «речь Альфреда →
форма», поздравление обоим при принятии знакомства,
заглушка «У Альфреда нет слов» при упавшей LLM и при лежащей службе tasks,
досылка после рестарта, ровно одно событие action_* на переход.

Речь Альфреда в проде приходит task_result'ом chat_loop-задачи службы tasks
— здесь его подаём тем же путём, через build_node_event_handler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest_asyncio

from sa_home_bot.bot import pending_actions as pa
from sa_home_bot.bot.node_events import build_node_event_handler
from sa_home_bot.bot.pending_actions import PendingActions
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import GuestSubscriptionConfig, Settings, SubscriptionConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.llm import prompt
from sa_home_bot.proto.messages import Address, make_event
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol as task_protocol

OWNER = 1
ALICE = 301  # инициатор
BOB = 302  # адресат
CAROL = 303


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _book() -> SubscriptionBook:
    guests = [
        GuestSubscriptionConfig(
            name=name, chat_id=chat_id, allowed_commands=["chat@llm"], invited_user=name
        )
        for name, chat_id in (("Алиса", ALICE), ("Боб", BOB), ("Кэрол", CAROL))
    ]
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="owner", chat_id=OWNER, allowed_commands=["*"])], guests
    )


class FakeNotifier:
    def __init__(self) -> None:
        self.log: list[tuple[str, int, str, object]] = []
        self._next = 100

    async def send_direct(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                          message_thread_id=None):
        self._next += 1
        self.log.append(("send", chat_id, text, reply_markup))
        return self._next

    async def edit_text(self, chat_id, message_id, text, reply_markup=None):
        self.log.append(("edit", chat_id, text, message_id))
        return True

    def sent_to(self, chat_id: int) -> list[str]:
        return [text for kind, chat, text, _ in self.log if kind == "send" and chat == chat_id]


class FakeNodeLink:
    """Служба tasks: create запоминается; ``fail`` — служба лежит."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.created: list[dict] = []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        if self.fail:
            raise ServiceUnavailableError("tasks недоступна")
        self.created.append(args or {})
        return {"task_id": len(self.created)}

    def speeches(self) -> list[dict]:
        return [c for c in self.created if c.get("action") == task_protocol.ACTION_CHAT_LOOP]


class Harness:
    def __init__(self, store, *, fail_tasks=False, speech_fallback_s=3600.0) -> None:
        self.store = store
        self.notifier = FakeNotifier()
        self.node_link = FakeNodeLink(fail=fail_tasks)
        self.pa = PendingActions(
            store,
            self.notifier,
            Settings(),
            lambda: self.node_link,
            speech_fallback_s=speech_fallback_s,
        )
        self.events: list[tuple[str, dict]] = []
        for name in (pa.EVENT_ACTION_CREATED, pa.EVENT_ACTION_SUBMITTED, pa.EVENT_ACTION_DECIDED):
            self.pa.bus.subscribe(name, self._recorder(name))
        self.handler = build_node_event_handler(
            _book(), self.notifier, store, pending_actions=self.pa
        )

    def _recorder(self, name):
        async def _record(data):
            self.events.append((name, data))

        return _record

    async def draft(self, initiator=ALICE, addressee=BOB) -> int:
        row = await self.pa.create_relationship_draft(
            initiator, addressee, "acquaintance", "Алиса", "Боб"
        )
        await self.pa.flush_drafts(initiator, dialogue_id=7)
        return row["id"]

    async def task_result(self, meta: dict, *, ok=True, response="Речь Альфреда"):
        data = {"task_id": 55, "meta": meta, "ok": ok}
        data["result" if ok else "error"] = {"response": response} if ok else "LLM упала"
        await self.handler(
            make_event(task_protocol.EVENT_TASK_RESULT, data, src=Address(node="alfred"))
        )

    async def speak(self, index: int = -1, **kwargs):
        await self.task_result(self.node_link.speeches()[index]["meta"], **kwargs)


# --- счастливый путь: порядок сообщений, запись связи, события ---


async def test_full_accept_flow_keeps_speech_before_form(store):
    h = Harness(store)
    action_id = await h.draft()

    # Форма инициатору — отдельным сообщением, с кнопками, в ai_turns треда.
    [draft_form] = h.notifier.sent_to(ALICE)
    assert "Кому: Боб" in draft_form and "Предложение знакомства" in draft_form
    assert "передавать ваши сообщения" in draft_form
    row = await store.get_pending_action(action_id)
    turn = await store.ai_turn(ALICE, row["draft_message_id"])
    assert turn["dialogue_id"] == 7

    assert await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE) == "Отправлено."
    # Адресату пока ничего: сначала речь Альфреда (chat_loop в tasks).
    assert h.notifier.sent_to(BOB) == []
    [speech_task] = h.node_link.speeches()
    assert speech_task["meta"]["chat_id"] == BOB
    assert speech_task["meta"]["pending_action_stage"] == pa.STAGE_OFFER
    directive = speech_task["args"]["messages"][0]["content"]
    assert "только кнопкой" in directive.lower() or "ТОЛЬКО кнопкой" in directive

    await h.speak()
    speech, offer = h.notifier.sent_to(BOB)
    assert "Речь Альфреда" in speech
    assert "От: Алиса" in offer

    assert await h.pa.handle_click(action_id, pa.BUTTON_ACCEPT, BOB) == "Принято."
    [rel] = await store.relationships_for(ALICE)
    assert rel["relation"] == "acquaintance" and rel["guest_b"] == BOB
    # Форма адресата переписана под итог, кнопок нет.
    edits = [e for e in h.notifier.log if e[0] == "edit" and e[1] == BOB]
    assert "✅ Принято" in edits[-1][2]
    assert "После согласия" not in edits[-1][2]

    # Этап 46: поздравление ОБОИМ — инициатору итог, адресату welcome, у
    # каждого сначала речь, потом детерминированное оповещение.
    outcome, welcome = h.node_link.speeches()[-2:]
    assert outcome["meta"]["pending_action_stage"] == pa.STAGE_OUTCOME
    assert outcome["meta"]["chat_id"] == ALICE
    assert welcome["meta"]["pending_action_stage"] == pa.STAGE_WELCOME
    assert welcome["meta"]["chat_id"] == BOB
    for speech in (outcome, welcome):
        assert "передавать сообщения" in speech["args"]["messages"][0]["content"]

    await h.speak(-2, response="Поздравляю!")
    assert h.notifier.sent_to(ALICE)[-2] == "<b>Альфред:</b> Поздравляю!"
    notice = h.notifier.sent_to(ALICE)[-1]
    assert "Боб — знакомство подтверждено" in notice and "передавать" in notice

    await h.speak(-1, response="С новым знакомством!")
    assert h.notifier.sent_to(BOB)[-2] == "<b>Альфред:</b> С новым знакомством!"
    welcome_notice = h.notifier.sent_to(BOB)[-1]
    assert "Алиса — знакомство подтверждено" in welcome_notice
    assert await store.pending_actions_needing_delivery() == []

    assert [name for name, _ in h.events] == [
        pa.EVENT_ACTION_CREATED,
        pa.EVENT_ACTION_SUBMITTED,
        pa.EVENT_ACTION_DECIDED,
    ]
    assert h.events[-1][1]["verdict"] == pa.VERDICT_ACCEPTED
    assert h.events[-1][1]["decided_by"] == BOB


async def test_reject_notifies_initiator(store):
    h = Harness(store)
    action_id = await h.draft()
    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)
    await h.speak()
    assert await h.pa.handle_click(action_id, pa.BUTTON_REJECT, BOB) == "Отклонено."
    await h.speak()
    assert "отклонено" in h.notifier.sent_to(ALICE)[-1]
    assert await store.relationships_for(ALICE) == []
    # Отказ — адресату поздравлять не с чем.
    assert all(
        s["meta"]["pending_action_stage"] != pa.STAGE_WELCOME for s in h.node_link.speeches()
    )


# --- права и идемпотентность ---


async def test_only_the_right_person_can_press(store):
    h = Harness(store)
    action_id = await h.draft()
    assert await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, BOB) == "Эта форма не для вас."
    assert await h.pa.handle_click(action_id, pa.BUTTON_ACCEPT, BOB) == "Уже решено."
    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)
    # Инициатор не может принять за адресата.
    assert await h.pa.handle_click(action_id, pa.BUTTON_ACCEPT, ALICE) == "Эта форма не для вас."
    assert await h.pa.handle_click(action_id, pa.BUTTON_ACCEPT, CAROL) == "Эта форма не для вас."
    assert (await store.get_pending_action(action_id))["status"] == "pending"


async def test_double_click_decides_once(store):
    h = Harness(store)
    action_id = await h.draft()
    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)
    assert await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE) == "Уже решено."
    await h.pa.handle_click(action_id, pa.BUTTON_REJECT, BOB)
    assert await h.pa.handle_click(action_id, pa.BUTTON_ACCEPT, BOB) == "Уже решено."
    assert await store.relationships_for(ALICE) == []
    submitted = [e for e in h.events if e[0] == pa.EVENT_ACTION_SUBMITTED]
    decided = [e for e in h.events if e[0] == pa.EVENT_ACTION_DECIDED]
    assert len(submitted) == 1 and len(decided) == 1


async def test_cancel_by_initiator_has_no_outcome_notice(store):
    h = Harness(store)
    action_id = await h.draft()
    assert await h.pa.handle_click(action_id, pa.BUTTON_CANCEL, ALICE) == "Отменено."
    assert (await store.get_pending_action(action_id))["status"] == "cancelled"
    assert h.node_link.speeches() == []  # сам нажал — оповещать нечем
    assert await store.pending_actions_needing_delivery() == []


async def test_unknown_form_and_garbage_callback(store):
    h = Harness(store)
    assert await h.pa.handle_click(999, pa.BUTTON_ACCEPT, BOB) == "Форма не найдена."
    assert pa.parse_callback("pa:12:a") == (12, "a")
    assert pa.parse_callback("pa:x:a") is None
    assert pa.parse_callback("st:12:a") is None
    assert len(f"pa:{2**40}:{pa.BUTTON_ACCEPT}".encode()) <= 64


# --- повторные проверки на кнопках ---


async def test_accept_rechecks_existing_acquaintance(store):
    h = Harness(store)
    action_id = await h.draft()
    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)
    await h.speak()
    # Пока Боб думал, знакомство уже подтвердилось (гонка двух форм).
    now = datetime.now(tz=UTC)
    await store.add_confirmed_relationship(BOB, ALICE, "acquaintance", now, now)

    answer = await h.pa.handle_click(action_id, pa.BUTTON_ACCEPT, BOB)

    assert "Не получилось" in answer
    row = await store.get_pending_action(action_id)
    assert row["status"] == "cancelled" and "уже знакомы" in row["reason"]
    assert len(await store.relationships_for(BOB)) == 1
    # Отменил не инициатор — ему итог положен.
    await h.speak()
    assert "не состоялось" in h.notifier.sent_to(ALICE)[-1]


async def test_submit_cancelled_by_counter_offer(store):
    """Встречные черновики: Боб отправил первым — «Отправить» Алисы не
    создаёт второе предложение, а отсылает к форме Боба."""
    h = Harness(store)
    alice_draft = await h.draft()
    bob_draft = await h.draft(initiator=BOB, addressee=ALICE)
    await h.pa.handle_click(bob_draft, pa.BUTTON_SUBMIT, BOB)

    assert "Не получилось" in await h.pa.handle_click(alice_draft, pa.BUTTON_SUBMIT, ALICE)
    row = await store.get_pending_action(alice_draft)
    assert row["status"] == "cancelled" and "уже сам предложил" in row["reason"]
# --- сроки ---


async def _age(store, action_id: int, **delta) -> None:
    past = (datetime.now(tz=UTC) - timedelta(**delta)).isoformat()
    await store.db.conn.execute(
        "UPDATE pending_actions SET expires_at=? WHERE id=?", (past, action_id)
    )
    await store.db.conn.commit()


async def test_ttls_are_one_hour_and_72_hours(store):
    h = Harness(store)
    action_id = await h.draft()
    row = await store.get_pending_action(action_id)
    ttl = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])
    assert ttl == timedelta(hours=1)
    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)
    row = await store.get_pending_action(action_id)
    ttl = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["submitted_at"])
    assert ttl == timedelta(hours=72)
    # Будильники поставлены в tasks на оба срока.
    timers = [c for c in h.node_link.created if c.get("action") == task_protocol.ACTION_TIMER]
    assert [t["meta"]["pending_action_id"] for t in timers] == [action_id, action_id]


async def test_draft_expiry_via_tasks_timer_notifies_initiator(store):
    h = Harness(store)
    action_id = await h.draft()
    await _age(store, action_id, seconds=1)

    await h.task_result(
        {"kind": task_protocol.TASK_KIND_PENDING_ACTION_EXPIRE, "pending_action_id": action_id},
        response="",
    )

    row = await store.get_pending_action(action_id)
    assert row["status"] == "expired"
    assert "⌛ Срок истёк" in [e for e in h.notifier.log if e[0] == "edit"][-1][2]
    await h.speak()
    assert "не была отправлена" in h.notifier.sent_to(ALICE)[-1]


async def test_stale_draft_timer_after_submit_is_noop(store):
    # Будильник черновика (1 ч) сработает и после «Отправить» — срок уже
    # продлён до 72 ч, это no-op.
    h = Harness(store)
    action_id = await h.draft()
    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)
    await h.pa.expire(action_id)
    assert (await store.get_pending_action(action_id))["status"] == "pending"


async def test_pending_expiry_and_expiry_after_decision(store):
    h = Harness(store)
    expiring = await h.draft()
    await h.pa.handle_click(expiring, pa.BUTTON_SUBMIT, ALICE)
    await h.speak()
    await _age(store, expiring, seconds=1)
    # Нажатие после срока — не решение, а экспирация.
    assert await h.pa.handle_click(expiring, pa.BUTTON_ACCEPT, BOB) == "Срок формы истёк."
    assert (await store.get_pending_action(expiring))["status"] == "expired"
    await h.speak()
    assert "осталось без ответа" in h.notifier.sent_to(ALICE)[-1]

    decided = await h.draft(addressee=CAROL)
    await h.pa.handle_click(decided, pa.BUTTON_CANCEL, ALICE)
    await _age(store, decided, seconds=1)
    events_before = len(h.events)
    await h.pa.expire(decided)
    assert (await store.get_pending_action(decided))["status"] == "cancelled"
    assert len(h.events) == events_before


# --- LLM/служба tasks недоступны: форма уходит всё равно ---


async def test_llm_failure_sends_placeholder_then_form(store):
    h = Harness(store)
    action_id = await h.draft()
    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)

    await h.speak(ok=False)

    placeholder, offer = h.notifier.sent_to(BOB)
    assert placeholder == pa.NO_WORDS_TEXT
    assert "От: Алиса" in offer
    # Кнопки на форме, а не на заглушке.
    markups = [m for kind, chat, _, m in h.notifier.log if kind == "send" and chat == BOB]
    assert markups[0] is None and markups[1] is not None
    # Заглушка и форма — один тред в ai_turns.
    row = await store.get_pending_action(action_id)
    offer_turn = await store.ai_turn(BOB, row["offer_message_id"])
    assert offer_turn["dialogue_id"] != row["offer_message_id"]


async def test_tasks_down_sends_placeholder_and_form_immediately(store):
    h = Harness(store)
    action_id = await h.draft()
    h.node_link.fail = True

    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)

    placeholder, offer = h.notifier.sent_to(BOB)
    assert placeholder == pa.NO_WORDS_TEXT and "От: Алиса" in offer
    await h.pa.handle_click(action_id, pa.BUTTON_REJECT, BOB)
    assert h.notifier.sent_to(ALICE)[-2] == pa.NO_WORDS_TEXT
    assert "отклонено" in h.notifier.sent_to(ALICE)[-1]
    await h.pa.aclose()


async def test_tasks_down_accept_still_congratulates_both(store):
    h = Harness(store, fail_tasks=True)
    action_id = await h.draft()
    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)
    await h.pa.handle_click(action_id, pa.BUTTON_ACCEPT, BOB)
    assert h.notifier.sent_to(ALICE)[-2] == pa.NO_WORDS_TEXT
    assert "знакомство подтверждено" in h.notifier.sent_to(ALICE)[-1]
    assert h.notifier.sent_to(BOB)[-2] == pa.NO_WORDS_TEXT
    assert "знакомство подтверждено" in h.notifier.sent_to(BOB)[-1]
    await h.pa.aclose()


async def test_speech_fallback_timer_then_late_speech_dropped(store):
    h = Harness(store, speech_fallback_s=0.01)
    action_id = await h.draft()
    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)

    await asyncio.sleep(0.1)
    placeholder, offer = h.notifier.sent_to(BOB)
    assert placeholder == pa.NO_WORDS_TEXT

    # Речь опоздала — после формы её не шлём, форма не дублируется.
    await h.speak()
    assert h.notifier.sent_to(BOB) == [placeholder, offer]
    await h.pa.aclose()


# --- рестарт бота ---


async def test_recover_delivers_missing_forms(store):
    h = Harness(store)
    row = await h.pa.create_relationship_draft(ALICE, BOB, "acquaintance", "Алиса", "Боб")
    submitted = await h.draft(addressee=CAROL)
    await h.pa.handle_click(submitted, pa.BUTTON_SUBMIT, ALICE)
    overdue = await h.draft(addressee=OWNER)
    await _age(store, overdue, seconds=1)
    # Принято, итог инициатору ушёл, а поздравление адресату — нет.
    accepted = await h.draft(initiator=CAROL, addressee=OWNER)
    await h.pa.handle_click(accepted, pa.BUTTON_SUBMIT, CAROL)
    await h.speak()
    await h.pa.handle_click(accepted, pa.BUTTON_ACCEPT, OWNER)
    await h.speak(-2)

    # Новый процесс: речь адресату так и не пришла, форма черновика не ушла.
    h2 = Harness(store, speech_fallback_s=0.01)
    await h2.pa.recover()
    await asyncio.sleep(0.1)

    assert (await store.get_pending_action(row["id"]))["draft_message_id"] is not None
    assert h2.notifier.sent_to(CAROL)[0] == pa.NO_WORDS_TEXT
    assert (await store.get_pending_action(overdue))["status"] == "expired"
    assert (await store.get_pending_action(accepted))["welcome_message_id"] is not None
    assert "знакомство подтверждено" in h2.notifier.sent_to(OWNER)[-1]
    await h2.pa.aclose()


async def test_all_stage_directives_wrapped_as_system_directive(store):
    """Регрессия 2026-09-26: все три речи (offer/outcome/welcome) обязаны
    идти через wrap_system_directive — иначе Альфред открывает чужую весть
    как поручение ТЕКУЩЕГО собеседника («Принято, исполню ваше поручение»),
    хотя тот ещё ничего не говорил. Проверяем role/content-маркер на
    фактическом сообщении, отправленном в schedule_agent_dialogue, для
    каждой из трёх стадий."""
    h = Harness(store)
    action_id = await h.draft()
    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)
    await h.speak()  # offer -> адресату
    assert await h.pa.handle_click(action_id, pa.BUTTON_ACCEPT, BOB) == "Принято."
    await h.speak(-2)  # outcome -> инициатору
    await h.speak(-1)  # welcome -> адресату

    speeches = h.node_link.speeches()
    stages = {s["meta"]["pending_action_stage"]: s for s in speeches}
    assert set(stages) == {pa.STAGE_OFFER, pa.STAGE_OUTCOME, pa.STAGE_WELCOME}
    for stage, task in stages.items():
        message = task["args"]["messages"][0]
        assert message["role"] == prompt._DIRECTIVE_ROLE, stage
        assert message["content"].startswith(prompt._DIRECTIVE_MARKER), stage


async def test_open_forms_note_tells_model_to_point_at_buttons(store):
    h = Harness(store)
    action_id = await h.draft()
    await h.pa.handle_click(action_id, pa.BUTTON_SUBMIT, ALICE)
    rows = await store.open_pending_actions(chat_id=BOB)
    note = pa.open_forms_note(rows, BOB)
    assert "Принять" in note and "ТОЛЬКО кнопками" in note
    assert "ждём ответа адресата" in pa.open_forms_note(
        await store.open_pending_actions(chat_id=ALICE), ALICE
    )
    assert pa.open_forms_note([], CAROL) is None
