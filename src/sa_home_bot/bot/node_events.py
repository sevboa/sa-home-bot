from __future__ import annotations

import html
import json
import logging
import time
from collections.abc import Callable

from aiogram.types import InlineKeyboardMarkup

from sa_home_bot.bot import (
    commands,
    vpn_check_view,
    vpn_devices,
    vpn_link_notify,
    vpn_nodes,
    vpn_notify,
)
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.ai_lines import (
    ALBERT_ASLEEP,
    ALBERT_ASLEEP_MD,
    ALBERT_TASK_MISSED,
    ALBERT_TASK_MISSED_MD,
    ALBERT_UNAVAILABLE,
    ALBERT_UNAVAILABLE_MD,
    ARNOLD_WAKING,
    ARNOLD_WAKING_MD,
    CLOSING_TEXT,
    RESTART_TEXT,
    STEPS_TEXT,
    STEPS_TEXT_PLAIN,
)
from sa_home_bot.bot.alfred_voice import ALFRED_PREFIX_HTML
from sa_home_bot.bot.handlers.vpn import resolve_request_callback
from sa_home_bot.bot.lifecycle import broadcast_all, broadcast_system, notify_tool_call
from sa_home_bot.bot.notifier import Notifier, broadcast, notify_admins
from sa_home_bot.bot.pending_actions import PendingActions
from sa_home_bot.bot.rich_stream import RichDraftPolicy, RichStreamSession
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.bot.ui.keyboards import single_button
from sa_home_bot.config import Settings
from sa_home_bot.db.store import OUTBOX_KIND_DELIVER, OUTBOX_KIND_TASK, Store
from sa_home_bot.node.events import (
    ACTION_CLOSE_SSH_SESSIONS,
    EVENT_IDLE_POWER_BLOCKED,
    EVENT_LLM_IDLE_SLEEP,
    EVENT_LLM_SERVICE_RESTART,
    EVENT_LLM_SPEECH_CURED,
    EVENT_NODE_DOWN,
    EVENT_NODE_JOINED,
    EVENT_NODE_LEAVING,
    EVENT_NODE_RETURNED,
    EVENT_NODE_UP,
    EVENT_RESTART_APPLIED,
    EVENT_SINGLETON_ACTIVATED,
    EVENT_SINGLETON_YIELDED,
    EVENT_UPDATE_FINISHED,
)
from sa_home_bot.proto.addresses import NODE_SERVICE, TASKS_ADDRESS
from sa_home_bot.proto.errors import SERVICE_CALL_ERRORS
from sa_home_bot.proto.messages import Envelope
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol as task_protocol
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)


_NODE_STATE_EVENTS = (EVENT_NODE_DOWN, EVENT_NODE_UP, EVENT_NODE_LEAVING, EVENT_NODE_RETURNED)
_SINGLETON_EVENTS = (EVENT_SINGLETON_ACTIVATED, EVENT_SINGLETON_YIELDED)
_VERSION_EVENTS = (EVENT_UPDATE_FINISHED, EVENT_RESTART_APPLIED)
_VPN_CHECK_EVENTS = (
    vpn_protocol.EVENT_VPN_CHECK_FAILED,
    vpn_protocol.EVENT_VPN_CHECK_RECOVERED,
)
_VPN_LINK_EVENTS = (vpn_protocol.EVENT_VPN_LINK_CHANGED, vpn_protocol.EVENT_VPN_OUTAGE)
_VPN_SERIES_EVENTS = (
    vpn_protocol.EVENT_VPN_SERIES_FAILED,
    vpn_protocol.EVENT_VPN_SERIES_RECOVERED,
)

VPN_EVENT_DEDUP_S = 120.0


def _vpn_event_key(name: str, data: dict) -> str:
    filtered = {k: v for k, v in data.items() if k != "active"}
    return name + json.dumps(filtered, sort_keys=True, default=str)


def _seen_recently(recent: dict[str, float], key: str) -> bool:
    now = time.monotonic()
    cutoff = now - VPN_EVENT_DEDUP_S
    for known in list(recent.keys()):
        if recent[known] < cutoff:
            del recent[known]
    if key in recent:
        return True
    recent[key] = now
    return False


def _format_alfred_reply(raw: str) -> str:
    return ALFRED_PREFIX_HTML + html.escape(raw.strip())


class TaskRichSessions:
    def __init__(self, notifier: Notifier, config: Settings | None) -> None:
        self._notifier = notifier
        self._config = config
        self._sessions: dict[int, RichStreamSession] = {}

    def get(
        self, task_id: int | None, chat_id: int, thread_id: int | None
    ) -> RichStreamSession | None:
        if task_id is None or self._config is None or self._config.llm.response_mode != "rich":
            return None
        session = self._sessions.get(task_id)
        if session is None:
            session = RichStreamSession(
                self._notifier.bot,
                chat_id,
                message_thread_id=thread_id,
                policy=RichDraftPolicy.from_llm_config(self._config.llm),
            )
            self._sessions[task_id] = session
        return session

    def pop(self, task_id: int | None) -> None:
        if task_id is not None:
            self._sessions.pop(task_id, None)


async def _handle_task_prewake(notifier: Notifier, data: dict, sessions: TaskRichSessions) -> None:
    meta = data.get("meta") or {}
    if meta.get("kind") != task_protocol.TASK_KIND_LLM_CHAT:
        return
    chat_id = meta.get("chat_id")
    if chat_id is None:
        return
    thread_id = meta.get("message_thread_id")
    task_id = data.get("task_id")
    status = data.get("status")
    session = sessions.get(task_id, chat_id, thread_id)
    if status == "waking":
        if session is not None:
            await session.push_status(STEPS_TEXT_PLAIN)
        else:
            await notifier.send_direct(chat_id, STEPS_TEXT, message_thread_id=thread_id)
    elif status == "ready":
        if session is not None:
            await session.finalize_status(ARNOLD_WAKING_MD)
        else:
            await notifier.send_direct(chat_id, ARNOLD_WAKING, message_thread_id=thread_id)
    elif status == "failed":
        unreachable = data.get("reason") == "unreachable"
        if session is not None:
            md_text = ALBERT_UNAVAILABLE_MD if unreachable else ALBERT_ASLEEP_MD
            await session.finalize_status(md_text)
            await session.aclose()
            sessions.pop(task_id)
        else:
            text = ALBERT_UNAVAILABLE if unreachable else ALBERT_ASLEEP
            await notifier.send_direct(chat_id, text, message_thread_id=thread_id)


async def _handle_deliver_message(notifier: Notifier, store: Store, data: dict) -> None:
    chat_id = data.get("chat_id")
    html_text = data.get("html")
    if chat_id is None or not html_text:
        return
    pair = data.get("require_acquaintance")
    if pair is not None:
        try:
            sender, target = (int(x) for x in pair)
        except (TypeError, ValueError):
            log.warning("deliver_message: битый require_acquaintance=%r — не шлём", pair)
            return
        if not await ai_tools.are_acquainted(store, sender, target):
            log.warning(
                "deliver_message: chat=%s не знаком с chat=%s — сообщение из tasks не отправлено",
                sender,
                target,
            )
            return
    thread_id = data.get("message_thread_id")
    message_id = await notifier.send_direct(
        chat_id, html_text, message_thread_id=thread_id, outbox_kind=OUTBOX_KIND_DELIVER
    )
    if message_id is None:
        return
    plain_text = data.get("plain") or html_text
    await store.record_ai_turn(chat_id, message_id, message_id, "assistant", plain_text, utc_now())


async def _maybe_fire_event_waiter(get_node_link, node_id: str, event_type: str) -> None:
    node_link = get_node_link() if get_node_link is not None else None
    if node_link is None:
        return
    try:
        await node_link.command(
            task_protocol.ACTION_MATCH_EVENT,
            {"node": node_id, "event_type": event_type},
            dst=TASKS_ADDRESS,
        )
    except SERVICE_CALL_ERRORS as exc:
        log.warning(
            "match_event(%s, %s) не удался (%s), останется на due_at", node_id, event_type, exc
        )


async def _report_task_failure(
    notifier: Notifier,
    data: dict,
    session: RichStreamSession | None,
    book: SubscriptionBook | None,
    chat_id: int,
    trigger_message_id: int | None,
    thread_id: int | None,
) -> None:
    if session is not None:
        await session.finalize_status(ALBERT_TASK_MISSED_MD, reply_to_message_id=trigger_message_id)
    else:
        await notifier.send_direct(
            chat_id,
            ALBERT_TASK_MISSED,
            reply_to_message_id=trigger_message_id,
            message_thread_id=thread_id,
        )
    if book is not None:
        await notify_admins(
            book,
            notifier,
            f"⚠️ Отложенная задача (chat={chat_id}) не выполнена: "
            f"{html.escape(str(data.get('error') or 'причина не указана'))}",
        )


async def _deliver_task_answer(
    notifier: Notifier,
    store: Store,
    data: dict,
    session: RichStreamSession | None,
    chat_id: int,
    trigger_message_id: int | None,
    thread_id: int | None,
) -> None:
    meta = data["meta"]
    raw = (data.get("result") or {}).get("response", "")
    if session is not None:
        sent = await session.finalize(raw, reply_to_message_id=trigger_message_id)
        sent_id = sent.message_id if sent is not None else None
    else:
        sent_id = await notifier.send_direct(
            chat_id,
            _format_alfred_reply(raw),
            reply_to_message_id=trigger_message_id,
            message_thread_id=thread_id,
            outbox_kind=OUTBOX_KIND_TASK,
        )
    dialogue_id = meta.get("dialogue_id")
    if dialogue_id is None:
        dialogue_id = sent_id
    if sent_id is not None and dialogue_id is not None:
        await store.record_ai_turn(chat_id, sent_id, dialogue_id, "assistant", raw, utc_now())


async def _handle_task_result(
    notifier: Notifier,
    store: Store,
    data: dict,
    sessions: TaskRichSessions,
    book: SubscriptionBook | None = None,
    pending_actions: PendingActions | None = None,
) -> None:
    meta = data.get("meta") or {}
    if meta.get("kind") == task_protocol.TASK_KIND_PENDING_ACTION_EXPIRE:
        action_id = meta.get("pending_action_id")
        if pending_actions is not None and isinstance(action_id, int):
            await pending_actions.expire(action_id)
        return
    if meta.get("kind") != task_protocol.TASK_KIND_LLM_CHAT:
        return
    chat_id = meta.get("chat_id")
    if chat_id is None:
        return
    trigger_message_id = meta.get("trigger_message_id")
    thread_id = meta.get("message_thread_id")
    task_id = data.get("task_id")
    session = sessions.get(task_id, chat_id, thread_id)
    try:
        action_id = meta.get("pending_action_id")
        action_stage = meta.get("pending_action_stage")
        if pending_actions is not None and isinstance(action_id, int) and action_stage:
            await _handle_action_speech(
                notifier, store, data, sessions, pending_actions, action_id, action_stage, book
            )
        elif not data.get("ok"):
            await _report_task_failure(
                notifier, data, session, book, chat_id, trigger_message_id, thread_id
            )
        else:
            await _deliver_task_answer(
                notifier, store, data, session, chat_id, trigger_message_id, thread_id
            )
    finally:
        if session is not None:
            await session.aclose()
        sessions.pop(task_id)


async def _handle_action_speech(
    notifier: Notifier,
    store: Store,
    data: dict,
    sessions: TaskRichSessions,
    pending_actions: PendingActions,
    action_id: int,
    stage: str,
    book: SubscriptionBook | None,
) -> None:
    meta = data.get("meta") or {}
    chat_id = meta["chat_id"]
    task_id = data.get("task_id")
    session = sessions.get(task_id, chat_id, None)
    raw = (data.get("result") or {}).get("response", "") if data.get("ok") else ""
    claimed = await pending_actions.claim_speech(action_id, stage)
    if not data.get("ok") or not raw.strip() or not claimed:
        if session is not None:
            await session.aclose()
        sessions.pop(task_id)
        if not data.get("ok") and book is not None:
            await notify_admins(
                book,
                notifier,
                f"⚠️ Речь Альфреда к форме #{action_id} (chat={chat_id}) не удалась: "
                f"{html.escape(str(data.get('error') or 'пустой ответ'))} — ушла заглушка",
            )
        if claimed:
            await pending_actions.on_speech_result(action_id, stage, None)
        return
    if session is not None:
        sent = await session.finalize(raw)
        sent_id = sent.message_id if sent is not None else None
    else:
        sent_id = await notifier.send_direct(chat_id, _format_alfred_reply(raw))
    sessions.pop(task_id)
    if sent_id is not None:
        await store.record_ai_turn(chat_id, sent_id, sent_id, "assistant", raw, utc_now())
    await pending_actions.on_speech_result(action_id, stage, sent_id)


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
            f"⬆️ Нода «{node_id}» обновлена до v{version} — нужен перезапуск (nodectl restart_node)."
        )
    return f"⚠️ Обновление ноды «{node_id}» не удалось: {html.escape(error or '')}"


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


_CHECK_ERROR_MAX = 120


def _site(target: str) -> str:
    return f"<code>{html.escape(target.removeprefix('https://'))}</code>"


def _short_site(target: str) -> str:
    return html.escape(target.removeprefix("https://").removeprefix("www."))


def _active_problems_block(active: list[dict]) -> str:
    if not active:
        return "\nДругих проблем нет."
    lines = ["\n⚠️ Ещё не работает:"]
    for item in active:
        direction = html.escape(f"{item['node']} → {item['server']} {item['transport']}")
        sites = (
            "все сайты" if item["whole"] else ", ".join(_short_site(t) for t in item["targets"])
        )
        lines.append(f"{direction}: {sites}")
    return "\n".join(lines)


def _check_error(error: object) -> str:
    text = str(error or "").strip() or "без подробностей"
    if len(text) > _CHECK_ERROR_MAX:
        text = text[: _CHECK_ERROR_MAX - 1] + "…"
    return html.escape(text)


def render_vpn_check(name: str, data: dict) -> str | None:
    node = data.get("node")
    failed = name == vpn_protocol.EVENT_VPN_CHECK_FAILED
    targets = data.get("targets")
    if targets is None and data.get("target"):
        targets = [{"target": data["target"], "error": data.get("error")}]
    if not node or not targets:
        return None
    if not failed:
        targets = [t if isinstance(t, dict) else {"target": t} for t in targets]
    total = data.get("total")
    every = data.get("all_failed") if failed else data.get("all_ok")
    whole = bool(every and total and len(targets) == total)
    tunnel = ""
    if data.get("server"):
        tunnel = f" → <code>{html.escape(str(data['server']))}</code>"
        if data.get("transport"):
            tunnel += f" {html.escape(str(data['transport']))}"
    head = f"VPN: пробник <code>{html.escape(str(node))}</code>{tunnel}"
    if failed:
        head = f"⚠️ {head} — недоступны " + ("все сайты" if whole else "сайты")
        if data.get("consecutive"):
            head += f" ({data['consecutive']} проверки подряд)"
    else:
        head = f"✅ {head} — снова доступны " + ("все сайты" if whole else "сайты")
    lines = [head + ":"]
    errors = {_check_error(t.get("error")) for t in targets} if failed else set()
    if not failed or len(errors) == 1:
        tail = f" — {errors.pop()}" if failed else ""
        lines.append(", ".join(_site(str(t["target"])) for t in targets) + tail)
    else:
        lines += [f"• {_site(str(t['target']))} — {_check_error(t.get('error'))}" for t in targets]
    if not failed and "active" in data:
        lines.append(_active_problems_block(data["active"]))
    return "\n".join(lines)


def render_vpn_series(name: str, data: dict) -> str | None:
    node, server, transport = data.get("node"), data.get("server"), data.get("transport")
    if not node or not server or not transport:
        return None
    label = vpn_devices.TECHNICAL_TRANSPORT_NAME.get(str(transport), str(transport))
    where = (
        f"Из РФ (<code>{html.escape(str(node))}</code>)",
        f"{html.escape(label)} → <code>{html.escape(str(server))}</code>",
    )
    if name == vpn_protocol.EVENT_VPN_SERIES_RECOVERED:
        return f"✅ {where[0]} снова работает {where[1]}"
    problem = vpn_check_view.series_problem(str(data.get("verdict") or ""), data.get("at_bytes"))
    text = f"⚠️ {where[0]} не работает {where[1]}: {html.escape(problem)}"
    if data.get("consecutive"):
        text += f" ({data['consecutive']} замера подряд)"
    return text


_VPN_GUEST_EVENTS = (
    vpn_protocol.EVENT_VPN_QUOTA_WARNING,
    vpn_protocol.EVENT_VPN_QUOTA_EXCEEDED,
    vpn_protocol.EVENT_VPN_ACCESS_RESTORED,
    vpn_protocol.EVENT_VPN_EXTRA_RESOLVED,
)
_VPN_ADMIN_EVENTS = (
    vpn_protocol.EVENT_VPN_EXTRA_REQUESTED,
    vpn_protocol.EVENT_VPN_NODE_QUOTA_WARNING,
    vpn_protocol.EVENT_VPN_PEER_ISSUED,
    vpn_protocol.EVENT_VPN_SERVER_RESTORED,
)


async def _other_countries_work(node_link, chat_id: int, node: str) -> bool:
    if node_link is None:
        return False
    try:
        answered = await vpn_nodes.fanout(
            node_link, vpn_protocol.ACTION_USAGE, {"chat_id": chat_id}
        )
    except Exception:
        return False
    return vpn_notify.others_with_quota(answered, node)


async def _record_and_notify_admins(
    book: SubscriptionBook,
    notifier: Notifier,
    store: Store,
    name: str,
    node_id: str | None,
    text: str,
    **options,
) -> None:
    await store.record_event(name, node_id, text, utc_now())
    await notify_admins(book, notifier, text, **options)


async def _handle_vpn_event(name, data, book, notifier, store, node_link) -> None:
    country = vpn_notify.country_of(data)
    chat_id = data.get("chat_id")
    if name in _VPN_GUEST_EVENTS:
        if not chat_id:
            return
        markup = None
        if name == vpn_protocol.EVENT_VPN_QUOTA_WARNING:
            text = vpn_notify.quota_warning_text(country, int(data.get("remaining_bytes") or 0))
            markup = vpn_notify.grant_keyboard(country)
        elif name == vpn_protocol.EVENT_VPN_QUOTA_EXCEEDED:
            others = await _other_countries_work(node_link, chat_id, country.node)
            text = vpn_notify.quota_exceeded_text(country, others_work=others)
            markup = vpn_notify.grant_keyboard(country)
        elif name == vpn_protocol.EVENT_VPN_ACCESS_RESTORED:
            text = vpn_notify.access_restored_text(country)
        else:
            text = vpn_notify.extra_resolved_text(country, bool(data.get("approved")))
        await notifier.send_direct(chat_id, text, reply_markup=markup)
        return
    if name == vpn_protocol.EVENT_VPN_EXTRA_REQUESTED:
        request_id = data.get("request_id")
        if not chat_id or request_id is None:
            return
        gb = float(data.get("bytes") or 0) / 1_000_000_000
        text = vpn_notify.extra_requested_text(country, chat_id, gb, request_id)
        await _record_and_notify_admins(
            book,
            notifier,
            store,
            name,
            None,
            text,
            reply_markup=resolve_request_callback(int(request_id)),
        )
    elif name == vpn_protocol.EVENT_VPN_NODE_QUOTA_WARNING:
        text = vpn_notify.node_quota_text(
            country, int(data.get("used_bytes") or 0), int(data.get("limit_bytes") or 0)
        )
        await _record_and_notify_admins(book, notifier, store, name, None, text)
    elif name == vpn_protocol.EVENT_VPN_PEER_ISSUED:
        if not chat_id:
            return
        text = vpn_notify.peer_issued_text(country, chat_id, str(data.get("device_label") or ""))
        await _record_and_notify_admins(book, notifier, store, name, None, text)
    elif name == vpn_protocol.EVENT_VPN_SERVER_RESTORED:
        items = vpn_notify.real_affected(data.get("affected"))
        for guest, devices in vpn_notify.group_by_chat(items).items():
            try:
                await notifier.send_direct(
                    guest,
                    vpn_notify.restored_guest_text(country, devices),
                    reply_markup=vpn_notify.restored_guest_keyboard(country, devices),
                )
            except Exception:
                log.warning("vpn: не доставил «сервер переустановлен» в %s", guest, exc_info=True)
        text = vpn_notify.restored_owner_text(country, items)
        await _record_and_notify_admins(book, notifier, store, name, data.get("node"), text)


def build_close_ssh_keyboard(node_id: str) -> InlineKeyboardMarkup:
    return single_button(
        "🔌 Закрыть SSH-сессии и выключить",
        commands.action_callback(ACTION_CLOSE_SSH_SESSIONS, node_id=node_id, service=NODE_SERVICE),
    )


def _render_swarm_event(name: str, data: dict, env: Envelope) -> tuple[str, str] | None:
    if name == EVENT_NODE_JOINED:
        node_id = data.get("node_id")
        if not node_id:
            return None
        return render_node_joined(node_id, data.get("endpoint") or "?"), node_id
    if name in _NODE_STATE_EVENTS:
        node_id = data.get("node")
        if not node_id:
            return None
        if name == EVENT_NODE_DOWN:
            return render_node_down(node_id, float(data.get("down_s") or 0)), node_id
        if name == EVENT_NODE_UP:
            return render_node_up(node_id), node_id
        if name == EVENT_NODE_LEAVING:
            return render_node_leaving(node_id), node_id
        return render_node_returned(node_id), node_id
    if name in _SINGLETON_EVENTS:
        slot, node_id = data.get("slot"), data.get("node")
        if not slot or not node_id:
            return None
        if name == EVENT_SINGLETON_ACTIVATED:
            return render_singleton_activated(slot, node_id), node_id
        return render_singleton_yielded(slot, node_id), node_id
    if name in _VERSION_EVENTS:
        if env.src is None or not env.src.node:
            return None
        node_id = env.src.node
        if name == EVENT_UPDATE_FINISHED:
            text = render_update_finished(
                node_id, bool(data.get("ok")), data.get("version"), data.get("error")
            )
            return text, node_id
        to_version = data.get("to")
        if not to_version:
            return None
        return render_restart_applied(node_id, data.get("from"), to_version), node_id
    return None


async def _notify_chats(notifier: Notifier, data: dict, text: str) -> None:
    for chat_id in data.get("chat_ids", []):
        await notifier.send_direct(chat_id, text)


def build_node_event_handler(
    book: SubscriptionBook,
    notifier: Notifier,
    store: Store,
    get_node_link: Callable[[], ServiceLink | None] | None = None,
    tool_calls: ToolCalls | None = None,
    config: Settings | None = None,
    pending_actions: PendingActions | None = None,
):
    task_sessions = TaskRichSessions(notifier, config)
    recent_vpn_events: dict[str, float] = {}

    async def handle_idle_power_blocked(data: dict) -> None:
        node_id = data.get("node")
        sessions = data.get("sessions") or []
        if not node_id or not sessions:
            return
        await _record_and_notify_admins(
            book,
            notifier,
            store,
            EVENT_IDLE_POWER_BLOCKED,
            node_id,
            render_idle_power_blocked(node_id, sessions),
            reply_markup=build_close_ssh_keyboard(node_id),
        )

    async def handle(env: Envelope) -> None:
        name = env.payload.get("event")
        data = env.payload.get("data", {})
        if name == task_protocol.EVENT_TASK_PREWAKE:
            await _handle_task_prewake(notifier, data, task_sessions)
        elif name == task_protocol.EVENT_TASK_RESULT:
            await _handle_task_result(notifier, store, data, task_sessions, book, pending_actions)
        elif name == task_protocol.EVENT_DELIVER_MESSAGE:
            await _handle_deliver_message(notifier, store, data)
        elif name == task_protocol.EVENT_TOOL_CALL:
            await notify_tool_call(
                book,
                notifier,
                data.get("name", "?"),
                args=data.get("args"),
                result=data.get("result"),
                debug=tool_calls,
            )
        elif name == EVENT_LLM_IDLE_SLEEP:
            await _notify_chats(notifier, data, CLOSING_TEXT)
        elif name == EVENT_LLM_SERVICE_RESTART:
            await _notify_chats(notifier, data, RESTART_TEXT)
        elif name == EVENT_LLM_SPEECH_CURED:
            await broadcast_all(book, notifier, render_speech_cured())
        elif name == EVENT_IDLE_POWER_BLOCKED:
            await handle_idle_power_blocked(data)
        elif name in _VPN_GUEST_EVENTS or name in _VPN_ADMIN_EVENTS:
            node_link = get_node_link() if get_node_link else None
            await _handle_vpn_event(name, data, book, notifier, store, node_link)
        elif name in _VPN_LINK_EVENTS:
            if _seen_recently(recent_vpn_events, vpn_link_notify.dedup_key(name, data)):
                return
            text = vpn_link_notify.render(name, data, vpn_nodes.known_server_label)
            if text is not None:
                node_id = data.get("node") or data.get("server")
                await store.record_event(name, node_id, text, utc_now())
                await broadcast(book.accepting(name), notifier, text)
        elif name in _VPN_CHECK_EVENTS or name in _VPN_SERIES_EVENTS:
            if _seen_recently(recent_vpn_events, _vpn_event_key(name, data)):
                return
            render = render_vpn_check if name in _VPN_CHECK_EVENTS else render_vpn_series
            check_text = render(name, data)
            if check_text is not None:
                await _record_and_notify_admins(
                    book, notifier, store, name, data.get("node"), check_text
                )
        else:
            rendered = _render_swarm_event(name, data, env)
            if rendered is None:
                return
            text, node_id = rendered
            await store.record_event(name, node_id, text, utc_now())
            await broadcast_system(book, notifier, text)
            if name in _VERSION_EVENTS:
                await _maybe_fire_event_waiter(get_node_link, node_id, name)

    return handle
