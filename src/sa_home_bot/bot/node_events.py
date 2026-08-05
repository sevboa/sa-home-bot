"""Приём событий сервиса node: ``node_joined`` (рой пополнился),
``update_finished`` (самообновление ноды завершилось — файлы на диске
обновлены, процесс НЕ перезапущен, это делает человек через restart_node),
``llm_idle_sleep`` (служба llm сама погасила контейнер по простою —
llm/service.py, ретранслируется сюда тем же механизмом, что и события
пиров, см. node/app.py::build_router — локальным службам тоже включён
on_event), ``llm_service_restart`` (сам процесс службы llm останавливается
— деплой/апдейт/ручной restart, см. llm/app.py::run_llm/LlmService.
notify_restart — тот же список активных chat_id, что и у idle_sleep),
``task_prewake``/``task_result`` (служба tasks — отложенные задачи роя, см.
sa_home_bot.tasks; у той нет доступа к Telegram, доставка и запись в
ai_turns делаются здесь, по meta, которую сама служба tasks не читает),
``tool_call`` (та же служба tasks — факт вызова инструмента моделью внутри
уже сработавшей chat_loop-задачи, self-scheduled remind; имя тула, аргументы
и результат, см. tasks/protocol.py::EVENT_TOOL_CALL).
``idle_power_blocked`` (node/service.py::maybe_auto_poweroff_idle — простой
Alfred дошёл до автовыключения ноды, но открыта SSH-сессия, выключение
отложено) — адресно админам (notify_admins), с кнопкой «закрыть сессии и
выключить» (node/service.py::ACTION_CLOSE_SSH_SESSIONS).
``llm_speech_cured`` (llm/speech_therapy.py::SpeechTherapist — Логопед
довёл вероятность искажения до 0%, разовое событие) — буквально всем
подпискам (`bot/lifecycle.py::broadcast_all`), включая гостей без opt-in.

``vpn_quota_warning``/``vpn_quota_exceeded``/``vpn_access_restored`` —
адресно гостю (vpn/service.py, Этап 33 IMPLEMENTATION_PLAN.md);
``vpn_extra_requested``/``vpn_node_quota_warning``/``vpn_peer_issued`` —
адресно админам; ``vpn_extra_resolved`` — гостю, чью заявку решили.

``node_joined``/``update_finished`` — тип в чат ``system`` (тот же канал,
что старт/останов, `bot/lifecycle.py`), рассылаются всем подпискам.
``llm_idle_sleep``/``llm_service_restart``/``task_prewake``/``task_result``
— адресно, только в конкретный chat_id из данных события (не через
event_types подписки: адрес уже точный, дублировать его подписками
незачем). ``tool_call`` — через подписки с ``event_types=["alfred_tool_call"]``
(`bot/lifecycle.py::notify_tool_call`) — тот же путь, что и для живого /ai
(bot/ai_flow.py::request_alfred), адресата в самом событии нет.
``idle_power_blocked`` — адресно админам (см. выше), не через подписки:
это не про конкретный чат-диалог, а служебный алерт для тех, кто вообще
управляет роем. Остальные события ноды (``service_started``/
``service_failed`` и т.п.) сюда не заведены — отдельная функциональность,
вне рамок этого модуля.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands
from sa_home_bot.bot.ai_flow import (
    ALBERT_ASLEEP,
    ALBERT_TASK_MISSED,
    ALBERT_UNAVAILABLE,
    ARNOLD_WAKING,
    CLOSING_TEXT,
    RESTART_TEXT,
    STEPS_TEXT,
)
from sa_home_bot.bot.handlers.vpn import resolve_request_callback
from sa_home_bot.bot.lifecycle import broadcast_all, broadcast_system, notify_tool_call
from sa_home_bot.bot.notifier import Notifier, notify_admins
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Address, Envelope, ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol as task_protocol
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

EVENT_NODE_JOINED = "node_joined"
EVENT_NODE_DOWN = "node_down"
EVENT_NODE_UP = "node_up"
# Этап 23: штатный уход (нода прощается сама, мгновенно) и возвращение
# после НЕаварийной недоступности (объявитель, node/watch.py) — оба
# информационные и осмысленны для любого типа машины, в отличие от
# аварийных node_down/node_up, которые рождаются только для always_on.
EVENT_NODE_LEAVING = "node_leaving"
EVENT_NODE_RETURNED = "node_returned"
EVENT_UPDATE_FINISHED = "update_finished"
# Литерал, не импорт из node/service.py (та же причина, что у остальных
# событий здесь: не источник правды, просто совпадающая строка, а модуль
# ноды тяжёлый). Эмитится один раз на старте процесса, когда версия
# сменилась с прошлого известного старта (node/app.py::
# _announce_version_if_changed) — единственный сигнал "нода РЕАЛЬНО
# исполняет новую версию", в отличие от update_finished (файлы на диске, до
# рестарта). Матчится против remind(after_event=...) — см.
# _maybe_fire_event_waiter ниже.
EVENT_RESTART_APPLIED = "restart_applied"
EVENT_SINGLETON_ACTIVATED = "singleton_activated"
EVENT_SINGLETON_YIELDED = "singleton_yielded"
# Строковые литералы, не импорт из llm/service.py — та же конвенция, что и
# для событий выше (это не "источник правды", просто совпадающая строка;
# импорт бота из пакета llm ради одной константы того не стоит).
EVENT_LLM_IDLE_SLEEP = "llm_idle_sleep"
EVENT_LLM_SERVICE_RESTART = "llm_service_restart"
# Логопед (llm/speech_therapy.py) вылечил Альфреда полностью — разовое
# событие, адресат — буквально все чаты (broadcast_all, не broadcast_system:
# гости не имеют opt-in по event_types, но пропустить их эту новость нельзя).
EVENT_LLM_SPEECH_CURED = "llm_speech_cured"
# Тот же приём (строковый литерал) для события node/service.py и для id
# действия, которое рисует кнопка ниже — ACTION_CLOSE_SSH_SESSIONS.
EVENT_IDLE_POWER_BLOCKED = "idle_power_blocked"
_CLOSE_SSH_SESSIONS_ACTION = "close_ssh_sessions"


def _format_alfred_reply(raw: str) -> str:
    return f"<b>Альфред:</b> {html.escape(raw.strip())}"


async def _handle_task_prewake(notifier: Notifier, data: dict) -> None:
    meta = data.get("meta") or {}
    if meta.get("kind") != task_protocol.TASK_KIND_LLM_CHAT:
        return  # незнакомый вид задачи — доставлять/показывать нечего
    chat_id = meta.get("chat_id")
    if chat_id is None:
        return
    # Живой баг 2026-08-05: без этого «шаги»/«Агнольд»/«Альбегт» от tasks
    # уезжали в общий топик личного чата, а не туда, где реально идёт
    # переписка (см. tool_remind — meta несёт message_thread_id, докстринг
    # Notifier.send_direct). None вне топика — как и было.
    thread_id = meta.get("message_thread_id")
    status = data.get("status")
    if status == "waking":
        await notifier.send_direct(chat_id, STEPS_TEXT, message_thread_id=thread_id)
    elif status == "ready":
        await notifier.send_direct(chat_id, ARNOLD_WAKING, message_thread_id=thread_id)
    elif status == "failed":
        text = ALBERT_UNAVAILABLE if data.get("reason") == "unreachable" else ALBERT_ASLEEP
        await notifier.send_direct(chat_id, text, message_thread_id=thread_id)


async def _maybe_fire_event_waiter(get_node_link, node_id: str, event_type: str) -> None:
    """Сверить событие роя с ожидающими задачами службы tasks — та сама
    хранит ожидания и решает, срабатывать ли (см. tasks/protocol.py::
    ACTION_MATCH_EVENT — решение пользователя 2026-08-05: раньше ожидание
    жило в БД бота, и remind, вызванный ИЗ УЖЕ СРАБОТАВШЕГО хода — а это
    самый частый случай при обновлении нескольких нод по цепочке — не мог
    его поставить вовсе, там нет доступа к БД бота). Здесь остаётся только
    один RPC, без знания про task_id."""
    node_link = get_node_link() if get_node_link is not None else None
    if node_link is None:
        return
    dst = Address(node=task_protocol.NODE_ID, service=task_protocol.SERVICE_NAME)
    try:
        await node_link.command(
            task_protocol.ACTION_MATCH_EVENT, {"node": node_id, "event_type": event_type}, dst=dst
        )
    except (ServiceUnavailableError, ProtoError) as exc:
        log.warning(
            "match_event(%s, %s) не удался (%s), останется на due_at", node_id, event_type, exc
        )


async def _handle_task_result(
    notifier: Notifier, store: Store, data: dict, book: SubscriptionBook | None = None
) -> None:
    # Очистка event_waiters (если была) — забота самой службы tasks
    # (tasks/service.py::_fire_one/_fire_now), не бота: у бота больше нет
    # доступа к этой таблице (см. _maybe_fire_event_waiter выше).
    meta = data.get("meta") or {}
    if meta.get("kind") != task_protocol.TASK_KIND_LLM_CHAT:
        return
    chat_id = meta.get("chat_id")
    if chat_id is None:
        return
    trigger_message_id = meta.get("trigger_message_id")
    # Живой баг 2026-08-05: без этого ответ tasks уезжал в общий топик
    # личного чата, а не в тот, откуда пришёл исходный запрос (см.
    # tool_remind — meta несёт message_thread_id).
    thread_id = meta.get("message_thread_id")
    if not data.get("ok"):
        await notifier.send_direct(
            chat_id,
            ALBERT_TASK_MISSED,
            reply_to_message_id=trigger_message_id,
            message_thread_id=thread_id,
        )
        # Пользователю — персонаж, админу — причина. Живой сбой 2026-07-30:
        # «Альбегт» был единственным следом провалившейся задачи, и разбор
        # свёлся к чтению логов трёх машин, включая Windows-службу.
        if book is not None:
            await notify_admins(
                book,
                notifier,
                f"⚠️ Отложенная задача (chat={chat_id}) не выполнена: "
                f"{html.escape(str(data.get('error') or 'причина не указана'))}",
            )
        return
    raw = (data.get("result") or {}).get("response", "")
    sent_id = await notifier.send_direct(
        chat_id,
        _format_alfred_reply(raw),
        reply_to_message_id=trigger_message_id,
        message_thread_id=thread_id,
    )
    dialogue_id = meta.get("dialogue_id")
    if sent_id is not None and dialogue_id is not None:
        await store.record_ai_turn(
            chat_id, sent_id, dialogue_id, "assistant", raw, datetime.now(tz=UTC)
        )


def render_node_joined(node_id: str, endpoint: str) -> str:
    return f"🕸 К рою присоединилась нода «{node_id}» ({endpoint})."


def _human_downtime(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"


def render_node_down(node_id: str, down_s: float) -> str:
    return (
        f"🔌 Нода «{node_id}» не отвечает уже {_human_downtime(down_s)} — "
        f"а эта машина должна быть в сети всегда."
    )


def render_node_up(node_id: str) -> str:
    return f"✅ Нода «{node_id}» снова на связи."


def render_node_leaving(node_id: str) -> str:
    return f"🌙 Нода «{node_id}» выключается штатно."


def render_node_returned(node_id: str) -> str:
    return f"👋 Нода «{node_id}» вернулась в строй."


def render_singleton_activated(slot: str, node_id: str) -> str:
    return f"🎛 Службу «{slot}» приняла нода «{node_id}»."


def render_singleton_yielded(slot: str, node_id: str) -> str:
    return f"🎛 Нода «{node_id}» уступила службу «{slot}» — вернулась основная."


def render_update_finished(node_id: str, ok: bool, version: str | None, error: str | None) -> str:
    if ok:
        return (
            f"⬆️ Нода «{node_id}» обновлена до v{version} — "
            f"нужен перезапуск (nodectl restart_node)."
        )
    return f"⚠️ Обновление ноды «{node_id}» не удалось: {error}"


def render_restart_applied(node_id: str, from_version: str | None, to_version: str) -> str:
    if from_version:
        return f"🔄 Нода «{node_id}» перезапустилась на v{to_version} (была v{from_version})."
    return f"🔄 Нода «{node_id}» на связи, версия v{to_version}."


def render_speech_cured() -> str:
    return (
        "🎉 <b>Альфред полностью излечился от картавости!</b>\n"
        "Логопед объявляет курс лечения завершённым."
    )


def render_idle_power_blocked(node_id: str, sessions: list[str]) -> str:
    lines = "\n".join(f"• {html.escape(s)}" for s in sessions)
    return (
        f"⚠️ Нода «{node_id}» простаивает (Alfred заснул по тайм-ауту), но не "
        f"выключаюсь — открыта SSH-сессия:\n{lines}\n"
        "Возможно, кто-то работает за машиной."
    )


def _vpn_grant_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ 100 ГБ",
                    callback_data=commands.action_callback(
                        "grant_extra", service=vpn_protocol.SERVICE_NAME
                    ),
                )
            ]
        ]
    )


def build_close_ssh_keyboard(node_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔌 Закрыть SSH-сессии и выключить",
                    callback_data=commands.action_callback(
                        _CLOSE_SSH_SESSIONS_ACTION, node_id=node_id, service="node"
                    ),
                )
            ]
        ]
    )


def build_node_event_handler(
    book: SubscriptionBook,
    notifier: Notifier,
    store: Store,
    get_node_link: Callable[[], ServiceLink | None] | None = None,
    tool_calls: ToolCalls | None = None,
):
    """Callback для ServiceLink(node).on_event.

    ``get_node_link`` — геттер, не сам линк: этот хендлер собирается ДО
    того, как ServiceLink(node) существует (сам этот callback передаётся
    ему в конструктор — см. app.py), поэтому нужен нулевой уровень
    отложенности, а не прямая ссылка на объект, которого пока нет.

    ``tool_calls`` — тот же ``ToolCalls`` (bot/tool_debug.py), которым
    пользуется живой /ai (bot/ai_flow.py::request_alfred): кнопке
    «развернуть» на дебаг-сообщении о вызове тула из службы tasks нужен тот
    же процесс-локальный склад входа/выхода, что и у живого диалога — сама
    служба tasks Telegram не видит и кнопку нарисовать не может (живой баг
    2026-08-05, см. tasks/protocol.py::EVENT_TOOL_CALL).
    """

    async def handle(env: Envelope) -> None:
        name = env.payload.get("event")
        data = env.payload.get("data", {})
        if name == task_protocol.EVENT_TASK_PREWAKE:
            await _handle_task_prewake(notifier, data)
            return
        if name == task_protocol.EVENT_TASK_RESULT:
            await _handle_task_result(notifier, store, data, book)
            return
        if name == task_protocol.EVENT_TOOL_CALL:
            await notify_tool_call(
                book,
                notifier,
                data.get("name", "?"),
                args=data.get("args"),
                result=data.get("result"),
                debug=tool_calls,
            )
            return
        # Кого касается событие — для журнала (db/schema.sql::swarm_events,
        # store.record_event ниже); заполняется в соответствующей ветке,
        # None у событий не про конкретную ноду (сейчас таких, доходящих до
        # записи в журнал, нет — оставлено на будущее).
        event_node_id: str | None = None
        if name == EVENT_NODE_JOINED:
            node_id = data.get("node_id")
            if not node_id:
                return
            event_node_id = node_id
            text = render_node_joined(node_id, data.get("endpoint") or "?")
        elif name in (EVENT_NODE_DOWN, EVENT_NODE_UP, EVENT_NODE_LEAVING, EVENT_NODE_RETURNED):
            # Объект события — нода из payload, а не src: node_down/node_up/
            # node_returned объявляет сосед-объявитель (node/watch.py::
            # _is_announcer), node_leaving нода шлёт про себя, но данные
            # устроены одинаково.
            node_id = data.get("node")
            if not node_id:
                return
            event_node_id = node_id
            if name == EVENT_NODE_DOWN:
                text = render_node_down(node_id, float(data.get("down_s") or 0))
            elif name == EVENT_NODE_UP:
                text = render_node_up(node_id)
            elif name == EVENT_NODE_LEAVING:
                text = render_node_leaving(node_id)
            else:
                text = render_node_returned(node_id)
        elif name in (EVENT_SINGLETON_ACTIVATED, EVENT_SINGLETON_YIELDED):
            slot, node_id = data.get("slot"), data.get("node")
            if not slot or not node_id:
                return
            event_node_id = node_id
            # Переезд бота между нодами сообщает уже поднявшийся экземпляр —
            # тот, что уступил, к этому моменту закрывает сессию.
            text = (
                render_singleton_activated(slot, node_id)
                if name == EVENT_SINGLETON_ACTIVATED
                else render_singleton_yielded(slot, node_id)
            )
        elif name == EVENT_UPDATE_FINISHED:
            # Событие описывает саму себя — src это и есть обновившаяся
            # нода (в отличие от node_joined, где src — сосед, а объект
            # события — третья нода); работает и для пиров — ретрансляция
            # событий уже устроена (см. node/app.py:_relay_peer_event).
            if env.src is None or not env.src.node:
                return
            event_node_id = env.src.node
            text = render_update_finished(
                env.src.node, bool(data.get("ok")), data.get("version"), data.get("error")
            )
        elif name == EVENT_RESTART_APPLIED:
            # Та же природа src, что у update_finished выше.
            if env.src is None or not env.src.node:
                return
            event_node_id = env.src.node
            to_version = data.get("to")
            if not to_version:
                return
            text = render_restart_applied(env.src.node, data.get("from"), to_version)
        elif name == EVENT_LLM_IDLE_SLEEP:
            # Адресно — только в перечисленные chat_id, не всем подпискам
            # (не через broadcast_system): служба сама знает точный список
            # чатов, где были запросы за это тёплое окно (llm/service.py).
            for chat_id in data.get("chat_ids", []):
                await notifier.send_direct(chat_id, CLOSING_TEXT)
            return
        elif name == EVENT_LLM_SERVICE_RESTART:
            for chat_id in data.get("chat_ids", []):
                await notifier.send_direct(chat_id, RESTART_TEXT)
            return
        elif name == EVENT_LLM_SPEECH_CURED:
            await broadcast_all(book, notifier, render_speech_cured())
            return
        elif name == EVENT_IDLE_POWER_BLOCKED:
            # Адресно админам (notify_admins), не всем подпискам — служебный
            # алерт про автовыключение, не про конкретный диалог.
            node_id = data.get("node")
            sessions = data.get("sessions") or []
            if not node_id or not sessions:
                return
            idle_text = render_idle_power_blocked(node_id, sessions)
            await store.record_event(name, node_id, idle_text, datetime.now(tz=UTC))
            await notify_admins(
                book,
                notifier,
                idle_text,
                reply_markup=build_close_ssh_keyboard(node_id),
            )
            return
        elif name == vpn_protocol.EVENT_VPN_QUOTA_WARNING:
            chat_id = data.get("chat_id")
            if chat_id is None:
                return
            remaining_gb = float(data.get("remaining_bytes") or 0) / 1_000_000_000
            await notifier.send_direct(
                chat_id,
                f"📶 VPN: осталось ~{remaining_gb:.1f} ГБ трафика до конца месяца.",
                reply_markup=_vpn_grant_keyboard(),
            )
            return
        elif name == vpn_protocol.EVENT_VPN_QUOTA_EXCEEDED:
            chat_id = data.get("chat_id")
            if chat_id is None:
                return
            await notifier.send_direct(
                chat_id,
                "⛔️ VPN: месячный лимит трафика исчерпан, доступ приостановлен. "
                "Можно добавить ещё через /vpn или дождаться начала месяца.",
                reply_markup=_vpn_grant_keyboard(),
            )
            return
        elif name == vpn_protocol.EVENT_VPN_ACCESS_RESTORED:
            chat_id = data.get("chat_id")
            if chat_id is None:
                return
            await notifier.send_direct(chat_id, "✅ VPN: доступ восстановлен.")
            return
        elif name == vpn_protocol.EVENT_VPN_EXTRA_REQUESTED:
            chat_id = data.get("chat_id")
            request_id = data.get("request_id")
            if chat_id is None or request_id is None:
                return
            gb = float(data.get("bytes") or 0) / 1_000_000_000
            extra_text = (
                f"✋ VPN: гость <code>{chat_id}</code> просит ещё {gb:.0f} ГБ "
                f"(заявка №{request_id})."
            )
            await store.record_event(name, None, extra_text, datetime.now(tz=UTC))
            await notify_admins(
                book,
                notifier,
                extra_text,
                reply_markup=resolve_request_callback(int(request_id)),
            )
            return
        elif name == vpn_protocol.EVENT_VPN_EXTRA_RESOLVED:
            chat_id = data.get("chat_id")
            if chat_id is None:
                return
            approved = bool(data.get("approved"))
            await notifier.send_direct(
                chat_id,
                "✅ VPN: заявка на доп. трафик одобрена."
                if approved
                else "🚫 VPN: заявка на доп. трафик отклонена.",
            )
            return
        elif name == vpn_protocol.EVENT_VPN_NODE_QUOTA_WARNING:
            used_gb = float(data.get("used_bytes") or 0) / 1_000_000_000
            limit_gb = float(data.get("limit_bytes") or 0) / 1_000_000_000
            node_quota_text = (
                f"⚠️ VPN: канал jeeves близок к месячному лимиту тарифа — "
                f"{used_gb:.0f} / {limit_gb:.0f} ГБ."
            )
            await store.record_event(name, None, node_quota_text, datetime.now(tz=UTC))
            await notify_admins(book, notifier, node_quota_text)
            return
        elif name == vpn_protocol.EVENT_VPN_PEER_ISSUED:
            chat_id = data.get("chat_id")
            if chat_id is None:
                return
            device = html.escape(str(data.get("device_label") or ""))
            issued_text = f"🔐 VPN: выдан доступ гостю <code>{chat_id}</code> ({device})."
            await store.record_event(name, None, issued_text, datetime.now(tz=UTC))
            await notify_admins(book, notifier, issued_text)
            return
        else:
            return
        await store.record_event(name, event_node_id, text, datetime.now(tz=UTC))
        await broadcast_system(book, notifier, text)
        if name in (EVENT_UPDATE_FINISHED, EVENT_RESTART_APPLIED) and event_node_id:
            await _maybe_fire_event_waiter(get_node_link, event_node_id, name)

    return handle
