from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import Any

from aiogram.types import Message

from sa_home_bot import wake_core
from sa_home_bot.bot import dialogue_context
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.ai_context import (  # noqa: F401
    ALFRED_TIMEZONE,
    LENGTH_NOTE,
    build_context_note,
    display_name,
    people_context_lines,
    recall_facts,
    recall_graph_facts,
    replied_picture_note,
)
from sa_home_bot.bot.ai_lines import (  # noqa: F401
    ALBERT_ASLEEP,
    ALBERT_ASLEEP_MD,
    ALBERT_HICCUP,
    ALBERT_HICCUP_MD,
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
    SURFING_TEXT,
    SURFING_TOOL,
    THINKING_TEXT,
    THINKING_TEXT_PLAIN,
    TOOL_STATUS_DEFAULT,
    TOOL_STATUS_TEXT,
)
from sa_home_bot.bot.lifecycle import notify_tool_call
from sa_home_bot.bot.notifier import Notifier, notify_admins, typing_action
from sa_home_bot.bot.pending_actions import open_forms_note
from sa_home_bot.bot.rich_stream import (
    RichStreamSession,
    finalize_status_or_answer,
    push_status_or_answer,
)
from sa_home_bot.bot.service_errors import UNREACHABLE_ERRORS
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.graph_memory import protocol as graph_memory_protocol
from sa_home_bot.graph_memory.client import add_episode
from sa_home_bot.llm.model_profiles import REASON_LEVELS, ModelProfileSummary
from sa_home_bot.llm.prompt import parse_router_level, wrap_context_note
from sa_home_bot.llm_chat import ChatStats, run_chat_loop
from sa_home_bot.people.book import PeopleBook
from sa_home_bot.proto.addresses import LLM_ADDRESS, LLM_NODE, LLM_SERVICE, node_address
from sa_home_bot.proto.errors import ServiceUnavailableError, is_service_unavailable
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.utils.clock import utc_now

log = logging.getLogger(__name__)

SILENT_REPLY = "\x00silent"

WAKE_POLL_TIMEOUT_S = wake_core.WAKE_POLL_TIMEOUT_S
WAKE_POLL_INTERVAL_S = wake_core.WAKE_POLL_INTERVAL_S

PROFILE_TTL_S = 300.0
_profile_cache: dict[str, tuple[float, ModelProfileSummary | None]] = {}
_seen_num_ctx: dict[str, int] = {}

DISMISS_TEXTS = {
    ai_tools.DISMISS_MODEL: "<i>Альфред откланивается и уходит к себе в подсобку</i>",
    ai_tools.DISMISS_SLEEP: "<i>Альфред откланивается; свет гаснет, машина засыпает</i>",
    ai_tools.DISMISS_OFF: "<i>Альфред откланивается; свет гаснет, машина выключается</i>",
}
DISMISS_NODE_ACTIONS = {
    ai_tools.DISMISS_SLEEP: "suspend",
    ai_tools.DISMISS_OFF: "poweroff",
}
LLM_SLEEP_ACTION = "sleep"
DISMISS_TIMEOUT_S = 15.0
DISMISS_FAILED_TEXT = (
    "<b>Альбегт:</b> Альфред уже попрощался, но машину погасить не вышло — "
    "она осталась на ногах, сэр/мадам"
)

_URL_RE = re.compile(r"https?://\S+")


class ActiveAiChats:
    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}

    def register(self, chat_id: int | None, task: asyncio.Task | None) -> None:
        if chat_id is not None and task is not None:
            self._tasks[chat_id] = task

    def unregister(self, chat_id: int | None, task: asyncio.Task | None) -> None:
        if chat_id is not None and self._tasks.get(chat_id) is task:
            del self._tasks[chat_id]

    def snapshot(self) -> dict[int, asyncio.Task]:
        return dict(self._tasks)


class SpeechRemarkBox:
    text: str | None = None


def _llm_key(dst: Address) -> str:
    return f"{dst.node}/{dst.service}"


async def _llm_profile(node_link: ServiceLink, dst: Address) -> ModelProfileSummary | None:
    key = _llm_key(dst)
    now = time.monotonic()
    hit = _profile_cache.get(key)
    if hit is not None and now - hit[0] < PROFILE_TTL_S:
        return hit[1]
    summary: ModelProfileSummary | None = None
    try:
        desc = await node_link.describe(dst)
        if desc is not None and desc.model_profile:
            summary = ModelProfileSummary.from_payload(desc.model_profile)
    except UNREACHABLE_ERRORS:
        summary = None
    _profile_cache[key] = (now, summary)
    return summary


async def _use_router(settings: Settings, node_link: ServiceLink, dst: Address) -> bool:
    if "mode" in settings.llm.model_fields_set:
        return settings.llm.mode == "router_think"
    profile = await _llm_profile(node_link, dst)
    if profile is not None:
        return profile.router
    return True


async def resolve_num_ctx(settings: Settings, store: Store, dst: Address) -> int:
    seen = _seen_num_ctx.get(_llm_key(dst))
    if seen:
        return seen
    known = await store.latest_known_num_ctx()
    if known:
        return known
    return settings.llm.num_ctx


def llm_address() -> Address:
    return LLM_ADDRESS


def photo_key_for(message: Message) -> str:
    return f"{message.chat.id}_{message.message_id}"


async def piggyback_dialogue_episode(
    node_link: ServiceLink | None, chat_id: int | None, user_text: str, assistant_text: str
) -> None:
    if node_link is None or chat_id is None:
        return
    user_text = user_text.strip()
    assistant_text = assistant_text.strip()
    if not user_text or not assistant_text:
        return
    text = f"{user_text} → {assistant_text}"
    await add_episode(node_link, chat_id, text, graph_memory_protocol.EPISODE_SOURCE_DIALOGUE_TURN)
    urls = dict.fromkeys(_URL_RE.findall(user_text) + _URL_RE.findall(assistant_text))
    for _url in urls:
        await add_episode(node_link, chat_id, text, graph_memory_protocol.EPISODE_SOURCE_LINK)


async def perform_dismissal(
    message: Message,
    node_link: ServiceLink,
    mode: str,
    book: SubscriptionBook,
    notifier: Notifier,
) -> None:
    try:
        await node_link.command(
            LLM_SLEEP_ACTION, {"quiet": True}, dst=LLM_ADDRESS, timeout=DISMISS_TIMEOUT_S
        )
    except UNREACHABLE_ERRORS as exc:
        log.warning("ai_flow: роспуск — модель не выгрузилась: %s", exc)
        if mode == ai_tools.DISMISS_MODEL:
            await message.answer(DISMISS_FAILED_TEXT)
            await notify_admins(book, notifier, f"⚠️ роспуск Альфреда: llm.sleep — {exc}")
            return

    action = DISMISS_NODE_ACTIONS.get(mode)
    if action is not None:
        try:
            await node_link.command(
                action, {}, dst=node_address(LLM_NODE), timeout=DISMISS_TIMEOUT_S
            )
        except UNREACHABLE_ERRORS as exc:
            log.warning("ai_flow: роспуск — %s не удался: %s", action, exc)
            await message.answer(DISMISS_FAILED_TEXT)
            await notify_admins(book, notifier, f"⚠️ роспуск Альфреда: {action} — {exc}")
            return

    await message.answer(DISMISS_TEXTS[mode])


def _tools_done_note(executed: list[tuple[str, dict[str, Any], str]]) -> str:
    lines = [f"- {name}({args}) → {result[:600]}" for name, args, result in executed]
    return (
        "В этом ходе ты уже вызвал инструменты, повторно их не вызывай — вот "
        "результаты:\n" + "\n".join(lines)
    )


async def _current_turn_text(store: Store, message: Message) -> str:
    if message.chat is None:
        return ""
    turn = await store.ai_turn(message.chat.id, message.message_id)
    if turn is None or turn.get("role") != "user":
        return ""
    return str(turn.get("content") or "")


class _AlfredTurn:
    def __init__(
        self,
        message: Message,
        node_link: ServiceLink,
        store: Store,
        settings: Settings,
        history: list[dict[str, Any]],
        dialogue_id: int,
        book: SubscriptionBook,
        notifier: Notifier,
        dismissal: ai_tools.DismissalBox | None,
        tool_calls: ToolCalls | None,
        speech_remark: SpeechRemarkBox | None,
        rich_session: RichStreamSession | None,
        pending_actions: Any | None,
        interactive_turn: Any | None,
        interactives: Any | None,
    ) -> None:
        self.message = message
        self.node_link = node_link
        self.store = store
        self.settings = settings
        self.history = history
        self.dialogue_id = dialogue_id
        self.book = book
        self.notifier = notifier
        self.dismissal = dismissal
        self.tool_calls = tool_calls
        self.speech_remark = speech_remark
        self.rich_session = rich_session
        self.pending_actions = pending_actions
        self.interactive_turn = interactive_turn
        self.interactives = interactives

        self.dst = LLM_ADDRESS
        self.timeout = settings.llm.request_timeout_s
        self.chat_key = message.chat.id if message.chat is not None else None
        self.log_chat_id = self.chat_key if self.chat_key is not None else "?"
        self.subscription = book.for_chat(message.chat.id) if message.chat else None
        self.people: PeopleBook
        self.context_note = ""
        self.speech_user_id = getattr(interactive_turn, "user_id", None)
        self.speech_clear = getattr(interactive_turn, "speech_clear_now", None) or getattr(
            interactive_turn, "speech_clear", None
        )
        self.is_scene = bool(getattr(interactive_turn, "scene", False))
        self.surfing_announced = False
        self.executed_tools: list[tuple[str, dict[str, Any], str]] = []

        compression = settings.llm
        self.compress_cfg = compression
        self.dhistory = history if isinstance(history, dialogue_context.DialogueHistory) else None
        self.managed = compression.context_compression and self.chat_key is not None
        self.recall_note: str | None = None
        self.step_away = False
        self.num_ctx = compression.num_ctx
        self.sent_chars = 0

    async def run(self) -> str | None:
        await self._prepare_context_note()
        await self._plan_context_window()
        outcome = await wake_core.ensure_service_ready(
            self.node_link,
            self.store,
            LLM_NODE,
            LLM_SERVICE,
            wake_timeout_s=WAKE_POLL_TIMEOUT_S,
            wake_poll_interval_s=WAKE_POLL_INTERVAL_S,
            warmup_timeout_s=self.settings.llm.warmup_timeout_s,
            on_phase_change=self._on_phase_change,
        )
        if outcome == wake_core.CRASHED:
            await self._announce_albert(ALBERT_UNAVAILABLE, ALBERT_UNAVAILABLE_MD)
            await notify_admins(
                self.book,
                self.notifier,
                f"🔥 /ai (chat={self.log_chat_id}): нода «{LLM_NODE}» жива, служба "
                f"«{LLM_SERVICE}» не отвечает — WoL не поможет, нужен ручной перезапуск службы",
            )
            return None
        if outcome == wake_core.UNREACHABLE:
            await self._announce_albert(ALBERT_UNAVAILABLE, ALBERT_UNAVAILABLE_MD)
            return None
        if outcome == wake_core.WARMUP_FAILED:
            await self._announce_albert(ALBERT_ASLEEP, ALBERT_ASLEEP_MD)
            await notify_admins(
                self.book,
                self.notifier,
                f"⚠️ /ai (chat={self.log_chat_id}): прогрев {LLM_NODE}/{LLM_SERVICE} не подтверждён",
            )
            return None
        return await self._ask_ready_service()

    async def _ask_ready_service(self) -> str | None:
        try:
            return await self._ask()
        except ProtoError as exc:
            if is_service_unavailable(exc):
                await self._announce_albert(ALBERT_UNAVAILABLE, ALBERT_UNAVAILABLE_MD)
            else:
                await self._announce_albert(ALBERT_HICCUP, ALBERT_HICCUP_MD)
                await notify_admins(
                    self.book,
                    self.notifier,
                    f"⚠️ /ai (chat={self.log_chat_id}): {exc.code} — {exc.message}",
                )
            return None
        except ServiceUnavailableError:
            await self._announce_albert(ALBERT_UNAVAILABLE, ALBERT_UNAVAILABLE_MD)
            return None

    async def _prepare_context_note(self) -> None:
        message = self.message
        has_web_search = SURFING_TOOL in ai_tools.tools_for(self.subscription).handlers
        self.people = await PeopleBook.load(self.store, self.book)
        memory_facts, graph_facts, people_lines = await self._gather_facts_and_people()
        note = await build_context_note(
            message,
            self.store,
            self.dialogue_id,
            has_web_search=has_web_search,
            memory_facts=memory_facts,
            graph_facts=graph_facts,
            people_lines=people_lines,
        )
        extra_notes = [
            await self._open_forms_note(),
            await replied_picture_note(message, self.store),
            getattr(self.interactive_turn, "note", None),
        ]
        for extra, separator in zip(extra_notes, (" ", "\n\n", "\n\n"), strict=True):
            if extra:
                note = f"{note}{separator}{extra}" if note else extra
        self.context_note = f"{note}\n\n{LENGTH_NOTE}" if note else LENGTH_NOTE

    async def _open_forms_note(self) -> str | None:
        if self.pending_actions is None or self.message.chat is None:
            return None
        return open_forms_note(
            await self.store.open_pending_actions(chat_id=self.message.chat.id),
            self.message.chat.id,
        )

    async def _gather_facts_and_people(self) -> tuple[list[str], list[str], list[str]]:
        message = self.message
        text = message.text or ""
        recalls = [
            recall_facts(self.node_link, self.chat_key, text),
            recall_graph_facts(self.node_link, self.chat_key, text),
        ]
        sender = message.from_user
        if sender is not None and not self.people.card(sender.id) and len(self.history) <= 1:
            sender_name = display_name(sender)
            if sender_name:
                recalls.append(
                    recall_graph_facts(
                        self.node_link,
                        self.chat_key,
                        f"Кто такой(-ая) {sender_name} и как к нему/ней обращаться?",
                    )
                )
        people_task = people_context_lines(message, self.store, self.book, self.people)
        *recall_results, people_lines = await asyncio.gather(*recalls, people_task)
        memory_facts = recall_results[0]
        graph_facts = recall_results[1] + (recall_results[2] if len(recall_results) > 2 else [])
        return memory_facts, graph_facts, people_lines

    async def _plan_context_window(self) -> None:
        cfg = self.compress_cfg
        if self.managed and self.dhistory is not None:
            self.recall_note, recalled = await dialogue_context.recall_verbatim(
                self.store,
                self.settings,
                self.chat_key,
                self.dialogue_id,
                await _current_turn_text(self.store, self.message),
                self.dhistory,
            )
            if recalled:
                log.info(
                    "ai_flow: дословный возврат — %d сообщ. из начала разговора (chat=%s)",
                    recalled,
                    self.log_chat_id,
                )
            self.num_ctx = await resolve_num_ctx(self.settings, self.store, self.dst)
        extra_chars = len(self.context_note) + len(self.recall_note or "")
        self.sent_chars = dialogue_context.content_chars(list(self.history)) + extra_chars
        if not (self.managed and self.dhistory is not None):
            return
        state = await self.store.dialogue_context_state(self.chat_key, self.dialogue_id)
        estimated = dialogue_context.estimate_prompt_tokens(self.settings, self.sent_chars, state)
        limit_tokens = dialogue_context.threshold_tokens(self.settings, self.num_ctx)
        if limit_tokens <= cfg.context_base_tokens:
            log.debug(
                "ai_flow: окно %d слишком мало для порога (база %d) — пропуск (chat=%s)",
                self.num_ctx,
                cfg.context_base_tokens,
                self.log_chat_id,
            )
        elif (
            estimated > limit_tokens
            and self.dhistory.compressible >= cfg.context_min_compress_turns
            and not dialogue_context.COMPRESSOR.is_running(self.chat_key, self.dialogue_id)
        ):
            self.step_away = True
            self.dhistory.compress_after = True
            self.sent_chars += len(dialogue_context.STEP_AWAY_INSTRUCTION)
            log.info(
                "ai_flow: окно треда chat=%s dialogue=%s ~%d ток. из %d (порог %d, замер %s) "
                "— Альфред отлучится, после ответа сожмём %d ходов",
                self.log_chat_id,
                self.dialogue_id,
                estimated,
                self.num_ctx,
                limit_tokens,
                state.get("prompt_tokens") if state else "нет",
                self.dhistory.compressible,
            )

    def _notes(self, extra: tuple[str, ...] = ()) -> list[str]:
        notes = [n for n in (self.recall_note, self.context_note) if n]
        return notes + [n for n in extra if n]

    async def _save_stats(self, stats: ChatStats, chars: int) -> None:
        if stats.num_ctx:
            _seen_num_ctx[_llm_key(self.dst)] = stats.num_ctx
        if self.chat_key is None or stats.first_prompt_tokens is None:
            return
        try:
            await self.store.save_dialogue_context_state(
                self.chat_key,
                self.dialogue_id,
                prompt_tokens=stats.first_prompt_tokens,
                sent_chars=chars,
                num_ctx=stats.num_ctx,
                done_reason=stats.done_reason,
                at=utc_now(),
            )
        except Exception:  # noqa: BLE001
            log.exception("ai_flow: не удалось сохранить замер окна (chat=%s)", self.log_chat_id)

    def _tool_context(self) -> ai_tools.ToolContext:
        message = self.message
        return ai_tools.ToolContext(
            chat_id=self.chat_key,
            dialogue_id=self.dialogue_id,
            trigger_message_id=message.message_id if message.chat else None,
            settings=self.settings,
            node_link=self.node_link,
            subscription=self.subscription,
            dismissal=self.dismissal,
            message_thread_id=message.message_thread_id,
            book=self.book,
            notifier=self.notifier,
            store=self.store,
            author=display_name(message.from_user),
            people=self.people,
            pending_actions=self.pending_actions,
            interactives=self.interactives,
            user_id=message.from_user.id if message.from_user else None,
            is_private=message.chat is not None and message.chat.type == "private",
            reply_to_message_id=(
                message.reply_to_message.message_id if message.reply_to_message else None
            ),
        )

    async def _record_tool_call(self, name: str, call_args: dict[str, Any], result: str) -> None:
        self.executed_tools.append((name, call_args, result))
        await notify_tool_call(
            self.book, self.notifier, name, args=call_args, result=result, debug=self.tool_calls
        )
        if self.rich_session is None and name == SURFING_TOOL and not self.surfing_announced:
            self.surfing_announced = True
            await self.message.answer(SURFING_TEXT)
        if self.chat_key is None:
            return
        await self.store.record_tool_call(
            chat_id=self.chat_key,
            dialogue_id=self.dialogue_id,
            trigger_message_id=self.message.message_id,
            tool_name=name,
            args=call_args,
            result=result,
            at=utc_now(),
        )

    async def _record_speech_remark(self, remark: str) -> None:
        if self.speech_remark is not None:
            self.speech_remark.text = remark

    async def _announce_tool_start(self, name: str, call_args: dict[str, Any]) -> None:
        if self.rich_session is None:
            return
        await self.rich_session.push_status(TOOL_STATUS_TEXT.get(name, TOOL_STATUS_DEFAULT))

    def _typing_while_asking(self) -> contextlib.AbstractAsyncContextManager[None]:
        message = self.message
        if message.chat is None:
            return contextlib.nullcontext()
        return typing_action(message.bot, message.chat.id, message.message_thread_id)

    async def _generate(self, *args: Any, **kwargs: Any) -> str:
        if self.rich_session is not None:
            return await run_chat_loop(*args, **kwargs)
        async with self._typing_while_asking():
            return await run_chat_loop(*args, **kwargs)

    async def _persona_pass(
        self,
        messages: list[dict[str, Any]],
        tool_ctx: ai_tools.ToolContext,
        reason: str,
        photo_key: str | None,
        stats: ChatStats,
        allow_tools: bool,
    ) -> str:
        raw = await self._generate(
            self.node_link,
            self.dst,
            self.timeout,
            messages,
            tool_ctx,
            reason,
            telegram_chat_id=self.chat_key,
            log_chat_id=self.log_chat_id,
            on_tool_call=self._record_tool_call,
            on_speech_remark=self._record_speech_remark,
            on_partial=self.rich_session.on_partial if self.rich_session is not None else None,
            on_tool_start=self._announce_tool_start,
            photo_key=photo_key,
            speech_user_id=self.speech_user_id,
            speech_clear=self.speech_clear,
            stats=stats,
            allow_tools=allow_tools,
        )
        return SILENT_REPLY if tool_ctx.end_turn else raw

    async def _router_pass(
        self,
        messages: list[dict[str, Any]],
        tool_ctx: ai_tools.ToolContext,
        photo_key: str | None,
        stats: ChatStats,
        allow_tools: bool,
    ) -> int:
        decision = await run_chat_loop(
            self.node_link,
            self.dst,
            self.timeout,
            messages,
            tool_ctx,
            "off",
            telegram_chat_id=self.chat_key,
            log_chat_id=self.log_chat_id,
            on_tool_call=self._record_tool_call,
            on_tool_start=self._announce_tool_start,
            role="router",
            photo_key=photo_key,
            stats=stats,
            allow_tools=allow_tools,
        )
        return parse_router_level(decision)

    def _messages_with_notes(
        self, hist: list[dict[str, Any]], notes: list[str]
    ) -> list[dict[str, Any]]:
        if not notes:
            return list(hist)
        return [*hist[:-1], *(wrap_context_note(note) for note in notes), hist[-1]]

    def _photo_key_of(self, hist: list[dict[str, Any]]) -> str | None:
        current_turn = hist[-1] if hist else None
        if isinstance(current_turn, dict) and "raw_image" in current_turn:
            return photo_key_for(self.message)
        return None

    async def _ask_once(
        self,
        hist: list[dict[str, Any]],
        stats: ChatStats,
        notes: list[str],
        *,
        allow_tools: bool = True,
    ) -> str:
        base_messages = self._messages_with_notes(hist, notes)
        photo_key = self._photo_key_of(hist)
        tool_ctx = self._tool_context()

        if not await _use_router(self.settings, self.node_link, self.dst):
            single_reason = "high" if self.settings.llm.single_call_think else "off"
            if self.is_scene:
                single_reason = "off"
            return await self._persona_pass(
                list(base_messages), tool_ctx, single_reason, photo_key, stats, allow_tools
            )

        router_messages = list(base_messages)
        level = await self._router_pass(router_messages, tool_ctx, photo_key, stats, allow_tools)
        if tool_ctx.end_turn:
            return SILENT_REPLY
        if self.is_scene:
            level = 0
        log.info("ai_flow: router -> THINK:%d (chat=%s)", level, self.log_chat_id)
        if level >= 1:
            await push_status_or_answer(
                self.message, self.rich_session, THINKING_TEXT_PLAIN, THINKING_TEXT
            )
        return await self._persona_pass(
            router_messages, tool_ctx, REASON_LEVELS[level], photo_key, stats, allow_tools
        )

    async def _announce_step_away(self) -> None:
        html_line, md_line = dialogue_context.pick_step_away_line()
        await finalize_status_or_answer(self.message, self.rich_session, md_line, html_line)

    async def _rescue(self, first: ChatStats) -> str:
        assert self.chat_key is not None
        cfg = self.compress_cfg
        log.warning(
            "ai_flow: окно переполнено (chat=%s dialogue=%s, промпт %d из %s, стоп %s) — "
            "сжимаем синхронно и перегенерируем",
            self.log_chat_id,
            self.dialogue_id,
            first.max_prompt_tokens,
            first.num_ctx or self.num_ctx,
            first.done_reason,
        )
        started = time.monotonic()
        await dialogue_context.COMPRESSOR.compress_now(
            self.node_link,
            self.store,
            self.settings,
            self.chat_key,
            self.dialogue_id,
            self.dst,
            num_ctx=first.num_ctx or self.num_ctx,
            keep_recent=max(2, cfg.context_keep_recent_turns // 2),
        )
        if self.dhistory is not None:
            self.dhistory.compress_after = False
        await self._announce_step_away()
        prefix = await dialogue_context.load_history(
            self.store,
            self.settings,
            self.chat_key,
            self.dialogue_id,
            before_message_id=self.message.message_id,
            wait=False,
        )
        retry_hist = dialogue_context.DialogueHistory(
            [*prefix, self.history[-1]],
            summary_upto=prefix.summary_upto,
            verbatim_ids=prefix.verbatim_ids,
            max_hidden_id=prefix.max_hidden_id,
        )
        self.recall_note, recalled = await dialogue_context.recall_verbatim(
            self.store,
            self.settings,
            self.chat_key,
            self.dialogue_id,
            await _current_turn_text(self.store, self.message),
            retry_hist,
        )
        tools_ran = list(self.executed_tools)
        extra = (_tools_done_note(tools_ran),) if tools_ran else ()
        notes = self._notes(extra)
        retry_stats = ChatStats()
        raw = await self._ask_once(retry_hist, retry_stats, notes, allow_tools=not tools_ran)
        if raw == SILENT_REPLY:
            return raw
        await self._save_stats(
            retry_stats,
            dialogue_context.content_chars(list(retry_hist)) + sum(map(len, notes)),
        )
        overflowed = dialogue_context.looks_overflowed(raw, retry_stats, self.num_ctx)
        log.info(
            "ai_flow: перегенерация после сжатия — %.1f с, промпт %d → %d ток., "
            "возвращено дословно %d, %s (chat=%s)",
            time.monotonic() - started,
            first.max_prompt_tokens,
            retry_stats.max_prompt_tokens,
            recalled,
            "снова переполнено" if overflowed else "ок",
            self.log_chat_id,
        )
        if not overflowed:
            return raw
        budget_chars = max(
            4000,
            int(
                (
                    dialogue_context.threshold_tokens(self.settings, self.num_ctx)
                    - cfg.context_base_tokens
                )
                * cfg.context_chars_per_token
            )
            - sum(map(len, notes)),
        )
        trimmed = dialogue_context.trim_to_budget(list(retry_hist), budget_chars)
        log.warning(
            "ai_flow: и после сжатия переполнено — режем историю %d → %d сообщ. (chat=%s)",
            len(retry_hist),
            len(trimmed),
            self.log_chat_id,
        )
        last_stats = ChatStats()
        raw = await self._ask_once(trimmed, last_stats, notes, allow_tools=not tools_ran)
        if raw == SILENT_REPLY:
            return raw
        await self._save_stats(last_stats, dialogue_context.content_chars(trimmed))
        if dialogue_context.looks_overflowed(raw, last_stats, self.num_ctx):
            return ""
        return raw

    async def _ask(self) -> str:
        extra = (dialogue_context.STEP_AWAY_INSTRUCTION,) if self.step_away else ()
        stats = ChatStats()
        try:
            raw = await self._ask_once(self.history, stats, self._notes(extra))
        except ProtoError:
            await self._save_stats(stats, self.sent_chars)
            if not (self.managed and stats.truncated):
                raise
            raw = ""
        else:
            await self._save_stats(stats, self.sent_chars)
        if raw == SILENT_REPLY:
            return raw
        if self.managed and dialogue_context.looks_overflowed(raw, stats, self.num_ctx):
            return await self._rescue(stats)
        return raw

    async def _announce_steps(self) -> None:
        await push_status_or_answer(self.message, self.rich_session, STEPS_TEXT_PLAIN, STEPS_TEXT)

    async def _announce_albert(self, text_html: str, text_md: str) -> None:
        await finalize_status_or_answer(self.message, self.rich_session, text_md, text_html)

    async def _on_phase_change(self, phase: str) -> None:
        if phase == wake_core.PHASE_WOKEN:
            await finalize_status_or_answer(
                self.message, self.rich_session, ARNOLD_WAKING_MD, ARNOLD_WAKING
            )
        else:
            await self._announce_steps()


async def request_alfred(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    settings: Settings,
    history: list[dict[str, Any]],
    dialogue_id: int,
    book: SubscriptionBook,
    notifier: Notifier,
    dismissal: ai_tools.DismissalBox | None = None,
    tool_calls: ToolCalls | None = None,
    speech_remark: SpeechRemarkBox | None = None,
    rich_session: RichStreamSession | None = None,
    *,
    pending_actions: Any | None = None,
    interactive_turn: Any | None = None,
    interactives: Any | None = None,
) -> str | None:
    turn = _AlfredTurn(
        message,
        node_link,
        store,
        settings,
        history,
        dialogue_id,
        book,
        notifier,
        dismissal,
        tool_calls,
        speech_remark,
        rich_session,
        pending_actions,
        interactive_turn,
        interactives,
    )
    return await turn.run()
