"""Детерминированные формы подтверждения (Этап 45, IMPLEMENTATION_PLAN.md).

Альфред — помощник, решает человек. LLM формулирует и сопровождает
словами, но не принимает решений и не пишет итог в БД: решение — нажатие
кнопки на ОТДЕЛЬНОМ сообщении-форме, текст которого собирает код по
шаблону. Живая находка 2026-09-26: заявка связи #5 зависла в pending
навсегда — адресат ответил «Не хочу», модель сказала «решение принято», но
reject_relationship не вызвала (директива с id жила только в chat_loop-
задаче, в историю живого /ai не попадала).

Каждое событие = два сообщения: сначала текст Альфреда (LLM, «желательно»),
затем форма/оповещение (код, «гарантированно»). Если LLM не ответила —
вместо её текста заглушка ``NO_WORDS_TEXT``, форма уходит всё равно.

Жизненный цикл записи ``pending_actions`` (db/schema.sql):
draft (у инициатора, 1 ч) → pending (у адресата, 72 ч) → accepted/rejected;
из draft/pending — cancelled/expired. Писатель статуса один — этот модуль
(у бота Store); кнопки, будильник службы tasks и восстановление на старте
лишь просят перехода, сам переход атомарен (Store.transition_pending_action)
— повторное нажатие или гонка с экспирацией просто не находят строку.

События (``ActionBus``): каждый переход публикует ровно одно из
``action_created``/``action_submitted``/``action_decided``. Реакции —
подписчики, а не код внутри обработчика кнопки: «Альфред говорит + форма
адресату» на submitted, «Альфред говорит + оповещение инициатору» на
decided, а при принятии ещё «Альфред поздравляет + оповещение» адресату
(Этап 46: знакомы теперь оба, и обоим надо сказать, что Альфред готов
передавать сообщения). Позже без правки ядра: эпизод в graph_memory, аудит.

Сейчас единственный ``kind`` — знакомство между гостями (``relationship``,
Этап 46: одна связь без типов, relation в payload всегда acquaintance).
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.tasks import protocol as task_protocol

log = logging.getLogger(__name__)

KIND_RELATIONSHIP = "relationship"

DRAFT_TTL = timedelta(hours=1)
OFFER_TTL = timedelta(hours=72)

# Заглушка вместо текста Альфреда, когда LLM не ответила (упала, не
# проснулась, служба tasks лежит) — форма/оповещение уходят всё равно.
NO_WORDS_TEXT = "🎩 <i>У Альфреда нет слов.</i>"
NO_WORDS_PLAIN = "🎩 У Альфреда нет слов."

EVENT_ACTION_CREATED = "action_created"
EVENT_ACTION_SUBMITTED = "action_submitted"
EVENT_ACTION_DECIDED = "action_decided"

VERDICT_ACCEPTED = "accepted"
VERDICT_REJECTED = "rejected"
VERDICT_CANCELLED = "cancelled"
VERDICT_EXPIRED = "expired"

OPEN_STATUSES = ("draft", "pending")

# callback_data «pa:<id>:<кнопка>» — только id и вердикт (лимит 64 байта),
# всё остальное бот берёт из своей БД.
CALLBACK_PREFIX = "pa"
BUTTON_SUBMIT = "s"
BUTTON_CANCEL = "c"
BUTTON_ACCEPT = "a"
BUTTON_REJECT = "r"

# Какая речь Альфреда сопровождает какое сообщение-форму: meta chat_loop-
# задачи несёт pending_action_id + pending_action_stage, bot/node_events.py
# отдаёт результат сюда (on_speech_result).
STAGE_OFFER = "offer"  # адресату: речь → форма «Принять/Отклонить»
STAGE_OUTCOME = "outcome"  # инициатору: речь → итоговое оповещение
STAGE_WELCOME = "welcome"  # адресату после «Принять»: поздравление → оповещение
_STAGE_COLUMN = {
    STAGE_OFFER: "offer_message_id",
    STAGE_OUTCOME: "notice_message_id",
    STAGE_WELCOME: "welcome_message_id",
}

# Сколько ждать речь Альфреда от службы tasks, прежде чем слать форму с
# заглушкой самим: FIRE_GRACE_S службы (tasks/service.py — побудка/прогрев
# спящей LLM-ноды с повторами) + бюджет самого запроса к модели + запас.
_TASKS_FIRE_GRACE_S = 300.0
_SPEECH_FALLBACK_MARGIN_S = 60.0

Subscriber = Callable[[dict[str, Any]], Awaitable[None]]


def parse_callback(data: str | None) -> tuple[int, str] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or not parts[1].isdigit():
        return None
    return int(parts[1]), parts[2]


def _callback(action_id: int, button: str) -> str:
    return f"{CALLBACK_PREFIX}:{action_id}:{button}"


def _fmt_time(dt: datetime | str | None) -> str:
    if dt is None:
        return "?"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.astimezone().strftime("%d.%m %H:%M")


class ActionBus:
    """Шина событий форм внутри процесса бота. Сбой одного подписчика не
    мешает остальным и не откатывает сам переход — он уже записан."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = {}

    def subscribe(self, event_type: str, handler: Subscriber) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        for handler in self._subscribers.get(event_type, []):
            try:
                await handler(data)
            except Exception:  # noqa: BLE001 — реакция не должна ронять переход
                log.exception("pending_actions: подписчик %s упал", event_type)


# --- тексты форм и оповещений (шаблоны кода, не LLM) ---


RELAY_LATER = "После согласия Альфред сможет передавать ваши сообщения друг другу."
RELAY_NOW = "Теперь Альфред может передавать ваши сообщения друг другу."


def _relation_lines(row: dict, *, for_addressee: bool) -> list[str]:
    payload = row["payload"]
    if for_addressee:
        who = f"От: {html.escape(payload.get('initiator_name') or 'гость')}"
    else:
        who = f"Кому: {html.escape(payload.get('addressee_name') or 'гость')}"
    lines = ["🤝 <b>Предложение знакомства</b>", who]
    if row["status"] in OPEN_STATUSES:
        lines.append(RELAY_LATER)
    return lines


def _outcome_line(row: dict) -> str:
    status = row["status"]
    at = _fmt_time(row.get("decided_at"))
    if status == VERDICT_ACCEPTED:
        return f"✅ Принято {at}."
    if status == VERDICT_REJECTED:
        return f"🚫 Отклонено {at}."
    if status == VERDICT_EXPIRED:
        return f"⌛ Срок истёк {at}."
    reason = row.get("reason")
    if reason:
        return f"⚠️ Не состоялось: {html.escape(reason)}"
    return f"✖️ Отменено {at}."


def render_draft_form(row: dict) -> str:
    lines = _relation_lines(row, for_addressee=False)
    status = row["status"]
    if status == "draft":
        lines += [
            f"Форма действует до {_fmt_time(row['expires_at'])}.",
            "Адресат получит предложение, только когда вы нажмёте «Отправить».",
        ]
        return "\n".join(lines)
    if row.get("submitted_at"):
        lines.append(f"📨 Отправлено {_fmt_time(row['submitted_at'])}.")
    if status == "pending":
        lines.append(f"Ответ ждём до {_fmt_time(row['expires_at'])}.")
    else:
        lines.append(_outcome_line(row))
    return "\n".join(lines)


def render_offer_form(row: dict) -> str:
    lines = _relation_lines(row, for_addressee=True)
    if row["status"] == "pending":
        lines += [
            f"Ответить можно до {_fmt_time(row['expires_at'])}.",
            "Решение принимается только кнопкой ниже.",
        ]
    else:
        lines.append(_outcome_line(row))
    return "\n".join(lines)


def render_outcome_notice(row: dict) -> str:
    """Итог инициатору. Имя — именительным лейблом перед тире: падеж на
    нём не держим (full_name в конфиге только в именительном)."""
    name = html.escape(row["payload"].get("addressee_name") or "гость")
    at = _fmt_time(row.get("decided_at"))
    status = row["status"]
    if status == VERDICT_ACCEPTED:
        return f"🤝 {name} — знакомство подтверждено {at}. {RELAY_NOW}"
    if status == VERDICT_REJECTED:
        return f"🔔 {name} — предложение знакомства отклонено {at}."
    if status == VERDICT_EXPIRED and row.get("submitted_at"):
        return f"🔔 {name} — предложение знакомства осталось без ответа, срок истёк {at}."
    if status == VERDICT_EXPIRED:
        return (
            f"🔔 Форма предложения знакомства (адресат — {name}) не была "
            f"отправлена и закрыта {at}."
        )
    reason = html.escape(row.get("reason") or "отменено")
    return f"🔔 {name} — предложение знакомства не состоялось: {reason}."


def render_welcome_notice(row: dict) -> str:
    """Адресату после его «Принять» — симметрично итогу инициатора."""
    name = html.escape(row["payload"].get("initiator_name") or "гость")
    at = _fmt_time(row.get("decided_at"))
    return f"🤝 {name} — знакомство подтверждено {at}. {RELAY_NOW}"


def _plain(text_html: str) -> str:
    """Текст формы для ai_turns — без разметки: модель увидит форму в
    истории треда и не будет делать вид, что её нет."""
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        text_html = text_html.replace(tag, "")
    return html.unescape(text_html)


def _draft_keyboard(action_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📨 Отправить", callback_data=_callback(action_id, BUTTON_SUBMIT)
                ),
                InlineKeyboardButton(
                    text="✖️ Отмена", callback_data=_callback(action_id, BUTTON_CANCEL)
                ),
            ]
        ]
    )


def _offer_keyboard(action_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Принять", callback_data=_callback(action_id, BUTTON_ACCEPT)
                ),
                InlineKeyboardButton(
                    text="🚫 Отклонить", callback_data=_callback(action_id, BUTTON_REJECT)
                ),
            ]
        ]
    )


# --- директивы для речи Альфреда (LLM сопровождает, но не решает) ---


def _offer_directive(row: dict) -> str:
    payload = row["payload"]
    return (
        "Тебе, Альфреду, нужно передать весть — ты сам НЕ участник этого "
        f"знакомства. Гость «{payload.get('initiator_name')}» предложил(а) "
        "подтвердить знакомство С ЧЕЛОВЕКОМ, С КОТОРЫМ ТЫ СЕЙЧАС РАЗГОВАРИВАЕШЬ "
        "(не с тобой); после согласия ты сможешь передавать сообщения между ними. "
        "Это НЕ поручение от твоего собеседника — весть идёт от третьего лица "
        "через тебя, поэтому не начинай с «Принято»/«исполню ваше поручение». "
        "Коротко, своими словами сообщи суть. Сразу после твоего сообщения "
        "собеседник получит отдельную форму с кнопками «Принять» и «Отклонить» — "
        "решение принимается ТОЛЬКО кнопкой, скажи об этом. Сам ничего не "
        "подтверждай и не отклоняй, не проси ответить текстом."
    )


def _outcome_directive(row: dict) -> str:
    name = row["payload"].get("addressee_name")
    status = row["status"]
    if status == VERDICT_ACCEPTED:
        return (
            f"Адресат ({name}) ПРИНЯЛ(А) предложение знакомства твоего "
            "собеседника — это факт, уже записанный системой. Тепло и коротко "
            "поздравь собеседника своими словами и скажи, что теперь ты готов "
            "передавать сообщения этому новому знакомому — достаточно "
            "попросить. Сразу после твоего сообщения придёт официальное "
            "уведомление; деталей сверх сказанного не выдумывай."
        )
    if status == VERDICT_REJECTED:
        what = f"адресат ({name}) ОТКЛОНИЛ(А) предложение знакомства"
    elif status == VERDICT_EXPIRED and row.get("submitted_at"):
        what = f"адресат ({name}) не ответил(а) на предложение знакомства, срок истёк"
    elif status == VERDICT_EXPIRED:
        what = (
            f"форма предложения знакомства (адресат — {name}) так и не была "
            "отправлена кнопкой и закрылась по сроку"
        )
    else:
        what = f"предложение знакомства (адресат — {name}) не состоялось"
    return (
        "Сообщи собеседнику своими словами, коротко и по-доброму, итог ЕГО "
        f"предложения: {what}. Это факт, уже записанный системой, — не "
        "переспрашивай и не меняй его. Сразу после твоего сообщения придёт "
        "официальное уведомление с итогом; деталей сверх сказанного не выдумывай."
    )


def _welcome_directive(row: dict) -> str:
    name = row["payload"].get("initiator_name")
    return (
        f"Твой собеседник только что кнопкой принял(а) предложение знакомства "
        f"от гостя «{name}» — это факт, уже записанный системой. Тепло и "
        "коротко поздравь собеседника с новым знакомством своими словами и "
        "скажи, что теперь ты готов передавать сообщения этому новому "
        "знакомому — достаточно попросить. Не начинай с «Принято»/«исполню "
        "поручение». Сразу после твоего сообщения придёт официальное "
        "уведомление; деталей сверх сказанного не выдумывай."
    )


class PendingActions:
    """Формы подтверждения: создание, переходы, реакции, таймеры.

    ``get_node_link`` — геттер, а не сам ServiceLink (тот же приём, что у
    build_node_event_handler в app.py): связь с нодой создаётся позже."""

    def __init__(
        self,
        store: Store,
        notifier: Any,
        settings: Settings,
        get_node_link: Callable[[], ServiceLink | None],
        *,
        speech_fallback_s: float | None = None,
    ) -> None:
        self._store = store
        self._notifier = notifier
        self._settings = settings
        self._get_node_link = get_node_link
        self._speech_fallback_s = (
            speech_fallback_s
            if speech_fallback_s is not None
            else _TASKS_FIRE_GRACE_S
            + settings.llm.request_timeout_s
            + _SPEECH_FALLBACK_MARGIN_S
        )
        self.bus = ActionBus()
        self.bus.subscribe(EVENT_ACTION_SUBMITTED, self._on_submitted)
        self.bus.subscribe(EVENT_ACTION_DECIDED, self._on_decided)
        self._locks: dict[int, asyncio.Lock] = {}
        self._speech_claimed: set[tuple[int, str]] = set()
        self._timers: set[asyncio.Task] = set()

    # --- создание ---

    async def create_relationship_draft(
        self,
        initiator: int,
        addressee: int,
        relation: str,
        initiator_name: str,
        addressee_name: str,
    ) -> dict:
        now = datetime.now(tz=UTC)
        row = await self._store.create_pending_action(
            KIND_RELATIONSHIP,
            initiator,
            addressee,
            {
                "relation": relation,
                "initiator_name": initiator_name,
                "addressee_name": addressee_name,
            },
            now + DRAFT_TTL,
            now,
        )
        await self.bus.publish(EVENT_ACTION_CREATED, _event(row))
        await self._schedule_expiry(row)
        return row

    async def flush_drafts(
        self, chat_id: int, dialogue_id: int | None, message_thread_id: int | None = None
    ) -> None:
        """Отправить формы черновиков инициатору — ПОСЛЕ ответа Альфреда
        (bot/handlers/ai.py зовёт это последним шагом хода): тул
        request_acquaintance срабатывает посреди генерации, и форма,
        посланная сразу, обогнала бы речь."""
        for row in await self._store.open_pending_actions(KIND_RELATIONSHIP, chat_id):
            if row["status"] == "draft" and row["initiator"] == chat_id:
                await self._deliver_draft(row["id"], dialogue_id, message_thread_id)

    async def _deliver_draft(
        self, action_id: int, dialogue_id: int | None, message_thread_id: int | None = None
    ) -> None:
        async with self._lock(action_id):
            row = await self._store.get_pending_action(action_id)
            if row is None or row["draft_message_id"] is not None or row["status"] != "draft":
                return
            text = render_draft_form(row)
            message_id = await self._notifier.send_direct(
                row["initiator"],
                text,
                reply_markup=_draft_keyboard(action_id),
                message_thread_id=message_thread_id,
            )
            if message_id is None:
                log.warning("pending_actions: форма #%s не ушла инициатору", action_id)
                return
            await self._store.set_pending_action_message(action_id, "draft_message_id", message_id)
            await self._record_turn(row["initiator"], message_id, dialogue_id, text)

    # --- нажатия ---

    async def handle_click(self, action_id: int, button: str, user_id: int) -> str:
        """Нажатие кнопки формы. Возвращает короткий текст для
        callback.answer — итог в чат уходит правкой формы/реакциями."""
        row = await self._store.get_pending_action(action_id)
        if row is None:
            return "Форма не найдена."
        if button in (BUTTON_SUBMIT, BUTTON_CANCEL):
            expected_status, decider = "draft", row["initiator"]
        elif button in (BUTTON_ACCEPT, BUTTON_REJECT):
            expected_status, decider = "pending", row["addressee"]
        else:
            return "Неизвестная кнопка."
        if user_id != decider:
            return "Эта форма не для вас."
        if row["status"] != expected_status:
            return "Уже решено."
        now = datetime.now(tz=UTC)
        if datetime.fromisoformat(row["expires_at"]) <= now:
            await self.expire(action_id)
            return "Срок формы истёк."

        if button == BUTTON_CANCEL:
            await self._decide(row, VERDICT_CANCELLED, user_id, now)
            return "Отменено."
        if button == BUTTON_REJECT:
            await self._decide(row, VERDICT_REJECTED, user_id, now)
            return "Отклонено."

        conflict = await ai_tools.acquaintance_conflict(
            self._store,
            row["initiator"],
            row["addressee"],
            row["payload"].get("addressee_name") or "гость",
            ignore_action_id=action_id,
        )
        if conflict is not None:
            await self._decide(row, VERDICT_CANCELLED, user_id, now, reason=conflict)
            return "Не получилось — подробности в форме."
        if button == BUTTON_SUBMIT:
            return await self._submit(row, now)
        return await self._accept(row, user_id, now)

    async def _submit(self, row: dict, now: datetime) -> str:
        updated = await self._store.transition_pending_action(
            row["id"], ("draft",), "pending", now, expires_at=now + OFFER_TTL
        )
        if updated is None:
            return "Уже решено."
        await self._edit(updated, "draft_message_id", render_draft_form(updated))
        await self.bus.publish(EVENT_ACTION_SUBMITTED, _event(updated))
        await self._schedule_expiry(updated)
        return "Отправлено."

    async def _accept(self, row: dict, user_id: int, now: datetime) -> str:
        updated = await self._store.transition_pending_action(
            row["id"], ("pending",), VERDICT_ACCEPTED, now, decided_by=user_id
        )
        if updated is None:
            return "Уже решено."
        await self._store.add_confirmed_relationship(
            updated["initiator"],
            updated["addressee"],
            ai_tools.RELATION_ACQUAINTANCE,
            datetime.fromisoformat(updated["created_at"]),
            now,
        )
        await self._finish(updated)
        return "Принято."

    async def _decide(
        self,
        row: dict,
        verdict: str,
        decided_by: int | None,
        now: datetime,
        *,
        reason: str | None = None,
    ) -> dict | None:
        updated = await self._store.transition_pending_action(
            row["id"], OPEN_STATUSES, verdict, now, decided_by=decided_by, reason=reason
        )
        if updated is not None:
            await self._finish(updated)
        return updated

    async def _finish(self, row: dict) -> None:
        await self._edit(row, "draft_message_id", render_draft_form(row))
        await self._edit(row, "offer_message_id", render_offer_form(row))
        await self.bus.publish(EVENT_ACTION_DECIDED, _event(row))

    # --- экспирация ---

    async def expire(self, action_id: int) -> None:
        """Идемпотентно: уже решено или срок продлён (draft → pending
        переставил expires_at, а будильник черновика всё равно сработает) —
        no-op."""
        row = await self._store.get_pending_action(action_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return
        now = datetime.now(tz=UTC)
        if datetime.fromisoformat(row["expires_at"]) > now:
            return
        await self._decide(row, VERDICT_EXPIRED, None, now)

    async def _schedule_expiry(self, row: dict) -> None:
        """Будильник в службе tasks (персистентная очередь, переживает
        рестарт бота). Служба недоступна — таймер в памяти процесса; его
        потеря при рестарте закрывается recover() на старте."""
        expires_at = datetime.fromisoformat(row["expires_at"])
        node_link = self._get_node_link()
        if node_link is not None:
            try:
                await node_link.command(
                    task_protocol.ACTION_CREATE,
                    {
                        "due_at": expires_at.isoformat(),
                        "dst_node": task_protocol.NODE_ID,
                        "dst_service": task_protocol.SERVICE_NAME,
                        "action": task_protocol.ACTION_TIMER,
                        "meta": {
                            "kind": task_protocol.TASK_KIND_PENDING_ACTION_EXPIRE,
                            "pending_action_id": row["id"],
                        },
                    },
                    dst=Address(node=task_protocol.NODE_ID, service=task_protocol.SERVICE_NAME),
                )
                return
            except (ServiceUnavailableError, ProtoError) as exc:
                log.warning(
                    "pending_actions: будильник #%s в tasks не поставлен (%s), таймер в памяти",
                    row["id"],
                    exc,
                )
        self._arm_expiry_timer(row["id"], expires_at)

    def _arm_expiry_timer(self, action_id: int, expires_at: datetime) -> None:
        delay = max((expires_at - datetime.now(tz=UTC)).total_seconds(), 0.0) + 1.0
        self._spawn(self._after(delay, self.expire(action_id)), f"pa-expire-{action_id}")

    # --- реакции на события ---

    async def _on_submitted(self, data: dict[str, Any]) -> None:
        row = await self._store.get_pending_action(data["id"])
        if row is not None:
            await self._request_speech(row, STAGE_OFFER, row["addressee"], _offer_directive(row))

    async def _on_decided(self, data: dict[str, Any]) -> None:
        row = await self._store.get_pending_action(data["id"])
        if row is None:
            return
        if _initiator_needs_notice(row):
            await self._request_speech(
                row, STAGE_OUTCOME, row["initiator"], _outcome_directive(row)
            )
        if row["status"] == VERDICT_ACCEPTED:
            await self._request_speech(
                row, STAGE_WELCOME, row["addressee"], _welcome_directive(row)
            )

    async def _request_speech(self, row: dict, stage: str, chat_id: int, directive: str) -> None:
        """Речь Альфреда — chat_loop-задачей службы tasks; её task_result
        вернётся в on_speech_result, и только ПОСЛЕ речи уйдёт форма/
        оповещение. Задачу поставить не удалось — сразу заглушка + форма;
        удалось — страховочный таймер на случай, если результат не придёт
        вовсе (служба упала посреди, LLM висит)."""
        node_link = self._get_node_link()
        scheduled = False
        if node_link is not None:
            try:
                await ai_tools.schedule_agent_dialogue(
                    node_link,
                    chat_id,
                    [{"role": "user", "content": directive}],
                    ai_tools.reminder_reason(self._settings.llm),
                    self._settings.llm.request_timeout_s,
                    meta_extra={"pending_action_id": row["id"], "pending_action_stage": stage},
                )
                scheduled = True
            except (ServiceUnavailableError, ProtoError) as exc:
                log.warning("pending_actions: речь #%s/%s не поставлена: %s", row["id"], stage, exc)
        if not scheduled:
            await self._deliver(row["id"], stage, None)
            return
        self._arm_speech_fallback(row["id"], stage, self._speech_fallback_s)

    def _arm_speech_fallback(self, action_id: int, stage: str, delay: float) -> None:
        self._spawn(
            self._after(delay, self._speech_fallback(action_id, stage)),
            f"pa-speech-{action_id}-{stage}",
        )

    async def _speech_fallback(self, action_id: int, stage: str) -> None:
        if (action_id, stage) in self._speech_claimed:
            return
        await self._deliver(action_id, stage, None)

    async def claim_speech(self, action_id: int, stage: str) -> bool:
        """Вызывается bot/node_events.py ДО отправки речи Альфреда. False —
        форма уже ушла (страховочный таймер успел раньше с заглушкой):
        опоздавшую речь не шлём, иначе она встанет ПОСЛЕ формы."""
        column = _STAGE_COLUMN.get(stage)
        if column is None:
            return False
        async with self._lock(action_id):
            row = await self._store.get_pending_action(action_id)
            if row is None or row[column] is not None or (action_id, stage) in self._speech_claimed:
                return False
            self._speech_claimed.add((action_id, stage))
            return True

    async def on_speech_result(
        self, action_id: int, stage: str, dialogue_id: int | None, message_thread_id=None
    ) -> None:
        """Речь ушла (dialogue_id — её тред) или не получилась (None →
        заглушка). Дальше — детерминированная часть."""
        await self._deliver(action_id, stage, dialogue_id, message_thread_id)
        self._speech_claimed.discard((action_id, stage))

    async def _deliver(
        self, action_id: int, stage: str, dialogue_id: int | None, message_thread_id=None
    ) -> None:
        column = _STAGE_COLUMN[stage]
        async with self._lock(action_id):
            row = await self._store.get_pending_action(action_id)
            if row is None or row[column] is not None:
                return
            if stage == STAGE_OFFER:
                if row["status"] != "pending":
                    return
                chat_id = row["addressee"]
                text, markup = render_offer_form(row), _offer_keyboard(action_id)
            elif stage == STAGE_WELCOME:
                chat_id = row["addressee"]
                text, markup = render_welcome_notice(row), None
            else:
                chat_id = row["initiator"]
                text, markup = render_outcome_notice(row), None
            if dialogue_id is None:
                placeholder_id = await self._notifier.send_direct(
                    chat_id, NO_WORDS_TEXT, message_thread_id=message_thread_id
                )
                if placeholder_id is not None:
                    dialogue_id = placeholder_id
                    await self._record_turn(chat_id, placeholder_id, dialogue_id, NO_WORDS_PLAIN)
            message_id = await self._notifier.send_direct(
                chat_id, text, reply_markup=markup, message_thread_id=message_thread_id
            )
            if message_id is None:
                log.warning("pending_actions: %s #%s не ушла в chat=%s", stage, action_id, chat_id)
                return
            await self._store.set_pending_action_message(action_id, column, message_id)
            await self._record_turn(chat_id, message_id, dialogue_id or message_id, text)

    # --- старт процесса ---

    async def recover(self) -> None:
        """После рестарта бота: просроченное — истечь; открытое — таймер в
        памяти (будильник tasks мог не встать); недоставленное — дослать
        (формы черновиков сразу, речь+форму — страховочным таймером от
        момента события, как если бы процесс не прерывался)."""
        now = datetime.now(tz=UTC)
        for row in await self._store.open_pending_actions():
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at <= now:
                await self.expire(row["id"])
            else:
                self._arm_expiry_timer(row["id"], expires_at)
        for row in await self._store.open_pending_actions():
            if row["status"] == "draft" and row["draft_message_id"] is None:
                await self._deliver_draft(row["id"], None)
        for row in await self._store.pending_actions_needing_delivery():
            for stage in _undelivered_stages(row):
                since = row["submitted_at"] if stage == STAGE_OFFER else row["decided_at"]
                elapsed = (now - datetime.fromisoformat(since)).total_seconds() if since else 0.0
                self._arm_speech_fallback(
                    row["id"], stage, max(self._speech_fallback_s - elapsed, 0.0)
                )

    async def aclose(self) -> None:
        for task in list(self._timers):
            task.cancel()
        for task in list(self._timers):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # --- служебное ---

    def _lock(self, action_id: int) -> asyncio.Lock:
        return self._locks.setdefault(action_id, asyncio.Lock())

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    @staticmethod
    async def _after(delay: float, coro: Awaitable[None]) -> None:
        try:
            await asyncio.sleep(delay)
            await coro
        except asyncio.CancelledError:
            if asyncio.iscoroutine(coro):
                coro.close()
            raise
        except Exception:  # noqa: BLE001
            log.exception("pending_actions: сбой отложенного действия")

    async def _edit(self, row: dict, column: str, text: str) -> None:
        message_id = row.get(column)
        if message_id is None:
            return
        chat_id = row["initiator"] if column == "draft_message_id" else row["addressee"]
        await self._notifier.edit_text(chat_id, message_id, text)

    async def _record_turn(
        self, chat_id: int, message_id: int, dialogue_id: int | None, text: str
    ) -> None:
        await self._store.record_ai_turn(
            chat_id,
            message_id,
            dialogue_id if dialogue_id is not None else message_id,
            "assistant",
            _plain(text),
            datetime.now(tz=UTC),
        )


def _initiator_needs_notice(row: dict) -> bool:
    # Отмену сам инициатор и нажал — оповещать его нечем.
    return not (row["status"] == VERDICT_CANCELLED and row["decided_by"] == row["initiator"])


def _undelivered_stages(row: dict) -> list[str]:
    """Какие речь+сообщения по записи должны были уйти, но не ушли."""
    if row["status"] == "pending":
        return [STAGE_OFFER] if row["offer_message_id"] is None else []
    if row["status"] in OPEN_STATUSES:
        return []
    stages: list[str] = []
    if row["notice_message_id"] is None and _initiator_needs_notice(row):
        stages.append(STAGE_OUTCOME)
    if row["status"] == VERDICT_ACCEPTED and row.get("welcome_message_id") is None:
        stages.append(STAGE_WELCOME)
    return stages


def _event(row: dict) -> dict[str, Any]:
    data: dict[str, Any] = {"id": row["id"], "kind": row["kind"], "status": row["status"]}
    if row["status"] not in OPEN_STATUSES:
        data["verdict"] = row["status"]
        data["decided_by"] = row["decided_by"]
    return data


def open_forms_note(rows: list[dict], chat_id: int) -> str | None:
    """Скрытая строка в системную заметку живого /ai (45.4): у собеседника
    открытые формы — на текстовое «да/нет» Альфред должен отправить к
    кнопкам, а не делать вид, что принял решение сам."""
    items: list[str] = []
    for row in rows:
        payload = row["payload"]
        if row["status"] == "draft" and row["initiator"] == chat_id:
            items.append(
                "черновик предложения знакомства адресату "
                f"({payload.get('addressee_name')}) — ждёт кнопки «Отправить»/«Отмена»"
            )
        elif row["status"] == "pending" and row["addressee"] == chat_id:
            items.append(
                "входящее предложение знакомства от "
                f"«{payload.get('initiator_name')}» — ждёт кнопки «Принять»/«Отклонить»"
            )
        elif row["status"] == "pending":
            items.append(
                "отправленное предложение знакомства адресату "
                f"({payload.get('addressee_name')}) — ждём ответа адресата"
            )
    if not items:
        return None
    return (
        "У собеседника есть открытые формы подтверждения: "
        + "; ".join(items)
        + ". Решения по ним принимаются ТОЛЬКО кнопками в самих формах. Если "
        "собеседник отвечает на такую форму текстом («да», «не хочу» и т.п.), "
        "не делай вид, что решение принято, — попроси нажать нужную кнопку в "
        "форме выше."
    )
