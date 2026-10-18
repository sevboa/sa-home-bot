from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Any

from sa_home_bot.bot.alfred_voice import ALFRED_PREFIX_HTML, ALFRED_PREFIX_MD
from sa_home_bot.bot.service_errors import UNREACHABLE_ERRORS
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.llm_chat import ACTION_CHAT, ChatStats
from sa_home_bot.proto.addresses import ROLE_SUMMARIZER
from sa_home_bot.proto.messages import Address
from sa_home_bot.text_search import MIN_WORD_CHARS, WORD_RE, fts_or_query
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.utils.text import clip, stem

log = logging.getLogger(__name__)


SUMMARY_PREFIX = "Краткое содержание начала разговора: "
RECALL_HEADER = (
    "Из начала разговора (дословно) — сообщения, которые относятся к текущей "
    "реплике; в кратком содержании их детали могли потеряться:"
)
SHORTENED_MARK = " […]"

STEP_AWAY_INSTRUCTION = (
    "Служебное указание (собеседнику о нём не говори): ответь на реплику "
    "собеседника как обычно и полностью. В самом конце ответа, одной-двумя "
    "фразами в образе дворецкого, скажи, что тебе нужно ненадолго отлучиться, "
    "и сам придумай уместный повод по ходу разговора и обстановке — например, "
    "прибраться, раздать указания прислуге, проверить что-то по дому (это "
    "лишь примеры, придумай своё). Пообещай скоро вернуться. Никаких "
    "технических слов: ни памяти, ни контекста, ни переполнения, ни "
    "обрезки, ни сжатия — только бытовой повод дворецкого."
)

STEP_AWAY_LINES: tuple[str, ...] = (
    "Извините великодушно — позвольте мне на минутку отлучиться. Сейчас же буду.",
    "Одну минуту — нужно дать указания слугам. Я мигом.",
    "Позвольте, я на минутку отлучусь — в гостиной следует навести лоск.",
    "Позвольте мне ненадолго отлучиться: на кухне, кажется, закипел чайник. "
    "Сейчас же буду.",
    "Минуточку — только погашу свечи в холле, и я весь к вашим услугам.",
)

SUMMARIZER_SYSTEM_PROMPT = (
    "Ты ведёшь краткое содержание переписки собеседника (или нескольких "
    "собеседников) с дворецким Альфредом. Тебе дают прежнее краткое "
    "содержание (если оно есть) и следующие реплики разговора. Напиши ОДНО "
    "обновлённое краткое содержание всего разговора на русском, в третьем "
    "лице, сжатым связным текстом или пунктами.\n"
    "Обязательно сохрани:\n"
    "- имена людей, питомцев, мест, предметов и названия — дословно;\n"
    "- факты о собеседнике: кто он, что любит, его планы и обстоятельства;\n"
    "- принятые решения, договорённости и обещания (кто, что, к какому сроку);\n"
    "- детали описаний, о которых могут спросить позже: цвета, размеры, "
    "числа, даты, адреса, приметы;\n"
    "- вопросы, оставшиеся без ответа, и тему, на которой разговор остановился.\n"
    "Выбрасывай приветствия, любезности, повторы и манеру речи. Не выдумывай "
    "ничего, чего нет в репликах. Без вступлений и пояснений — только само "
    "краткое содержание."
)

_RECALL_CANDIDATES = 24
EMPTY_REPLY_OVERFLOW_PROMPT_SHARE = 0.9
SUMMARY_REQUEST_WINDOW_SHARE = 0.55
MIN_SUMMARY_CHUNK_CHARS = 4000
_STOP_WORDS = frozenset(
    """
    что как это этот эта эти это того тот там тут так также такой такая такие
    уже ещё еще если когда чтобы или для без под над при про через после перед
    между они она оно его её ему ней ним них нам вам вас нас мне меня мой моя мои
    моё мое ее твой твоя твои тебе тебя себя свой своя свои был была было были быть
    будет будут есть нет вот кто чем чём где куда откуда почему зачем какой
    какая какие какое каким который которая которые которое очень можно нужно
    надо просто сейчас теперь потом тоже только даже всё все всех весь вся ведь
    ага угу ладно хорошо давай давайте скажи расскажи знаешь помнишь напомни
    альфред сэр мадам пожалуйста спасибо привет здравствуй здравствуйте
    говорили обсуждали было раньше тогда
    """.split()
)


def content_chars(messages: list[dict[str, Any]]) -> int:
    return sum(len(m["content"]) for m in messages if isinstance(m.get("content"), str))


class DialogueHistory(list):
    def __init__(
        self,
        messages: Any = (),
        *,
        summary_upto: int | None = None,
        verbatim_ids: set[int] | None = None,
        max_hidden_id: int | None = None,
        compressible: int = 0,
    ) -> None:
        super().__init__(messages)
        self.summary_upto = summary_upto
        self.verbatim_ids: set[int] = verbatim_ids or set()
        self.max_hidden_id = max_hidden_id
        self.compressible = compressible
        self.compress_after = False


async def load_history(
    store: Store,
    settings: Settings,
    chat_id: int,
    dialogue_id: int,
    *,
    before_message_id: int | None = None,
    wait: bool = True,
) -> DialogueHistory:
    cfg = settings.llm
    if wait and cfg.context_compression:
        await COMPRESSOR.wait(chat_id, dialogue_id, cfg.context_compress_wait_s)
    rows = [r for r in await store.ai_turns_for_dialogue(chat_id, dialogue_id) if r["content"]]
    if before_message_id is not None:
        rows = [r for r in rows if r["message_id"] < before_message_id]
    summary = (
        await store.latest_dialogue_summary(chat_id, dialogue_id)
        if cfg.context_compression
        else None
    )
    upto = int(summary["upto_message_id"]) if summary else None
    tail = [r for r in rows if upto is None or r["message_id"] > upto]
    old_count = max(0, len(tail) - cfg.context_keep_recent_turns)
    limit = cfg.context_old_reply_max_chars if cfg.context_compression else 0

    calls = await _history_tool_calls(store, chat_id, dialogue_id)
    messages: list[dict[str, Any]] = []
    if summary:
        messages.append({"role": "system", "content": SUMMARY_PREFIX + summary["summary"]})
    verbatim: set[int] = set()
    hidden = [upto] if upto is not None else []
    for index, row in enumerate(tail):
        content = row["content"]
        if limit and index < old_count and row["role"] == "assistant" and len(content) > limit:
            content = clip(content, limit, mark=SHORTENED_MARK, at_word=True)
            hidden.append(row["message_id"])
        else:
            verbatim.add(row["message_id"])
        messages.append({"role": row["role"], "content": content})
        if row["role"] == "user":
            messages.extend(calls.get(row["message_id"], ()))
    return DialogueHistory(
        messages,
        summary_upto=upto,
        verbatim_ids=verbatim,
        max_hidden_id=max(hidden) if hidden else None,
        compressible=old_count,
    )


PEOPLE_HISTORY_TOOLS = frozenset(
    {
        "find_person",
        "my_acquaintances",
        "note_person",
        "request_acquaintance",
        "tell",
        "notify_guest",
    }
)
HISTORY_TOOLS = frozenset({"take_photo", "generate_image", "show_items"}) | PEOPLE_HISTORY_TOOLS
HISTORY_TOOL_RESULT_MAX = 200
PEOPLE_HISTORY_TOOL_RESULT_MAX = 600


async def _history_tool_calls(
    store: Store, chat_id: int, dialogue_id: int
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for call in await store.tool_calls_for_dialogue(chat_id, dialogue_id):
        if call["tool_name"] not in HISTORY_TOOLS or call["trigger_message_id"] is None:
            continue
        try:
            args = json.loads(call["args_json"] or "{}")
        except ValueError:
            args = {}
        grouped.setdefault(int(call["trigger_message_id"]), []).append(
            {**call, "args": args if isinstance(args, dict) else {}}
        )
    history: dict[int, list[dict[str, Any]]] = {}
    for message_id, group in grouped.items():
        history[message_id] = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": c["tool_name"], "arguments": c["args"]}} for c in group
                ],
            },
            *(
                {
                    "role": "tool",
                    "name": c["tool_name"],
                    "content": str(c["result"] or "")[
                        : PEOPLE_HISTORY_TOOL_RESULT_MAX
                        if c["tool_name"] in PEOPLE_HISTORY_TOOLS
                        else HISTORY_TOOL_RESULT_MAX
                    ],
                }
                for c in group
            ),
        ]
    return history


def estimate_prompt_tokens(settings: Settings, sent_chars: int, state: dict | None) -> int:
    cfg = settings.llm
    cpt = cfg.context_chars_per_token
    by_chars = cfg.context_base_tokens + math.ceil(sent_chars / cpt)
    if not state or not state.get("prompt_tokens"):
        return by_chars
    by_measure = int(state["prompt_tokens"]) + math.ceil(
        (sent_chars - int(state.get("sent_chars") or 0)) / cpt
    )
    return max(by_chars, by_measure)


def threshold_tokens(settings: Settings, num_ctx: int) -> int:
    return int(num_ctx * settings.llm.context_compress_ratio)


def looks_overflowed(raw: str, stats: ChatStats, num_ctx: int) -> bool:
    if stats.truncated:
        return True
    window = stats.num_ctx or num_ctx
    nearly_full = stats.max_prompt_tokens >= int(window * EMPTY_REPLY_OVERFLOW_PROMPT_SHARE)
    return not raw.strip() and nearly_full


def trim_to_budget(messages: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    if not messages:
        return []
    head = []
    first = messages[0]
    if first.get("role") == "system" and str(first.get("content", "")).startswith(SUMMARY_PREFIX):
        head = [first]
    rest = messages[len(head) :]
    total = content_chars(head)
    kept: list[dict[str, Any]] = []
    for msg in reversed(rest):
        size = len(msg["content"]) if isinstance(msg.get("content"), str) else 0
        if kept and total + size > max_chars:
            break
        kept.append(msg)
        total += size
    kept.reverse()
    while kept[:-1] and (kept[0].get("role") == "tool" or "tool_calls" in kept[0]):
        kept.pop(0)
    return head + kept


def pick_step_away_line() -> tuple[str, str]:
    line = random.choice(STEP_AWAY_LINES)
    return f"{ALFRED_PREFIX_HTML}{line}", f"{ALFRED_PREFIX_MD}{line}"


def fts_query(text: str) -> str:
    words = [
        w
        for w in WORD_RE.findall(text.lower().replace("ё", "е"))
        if len(w) >= MIN_WORD_CHARS and w not in _STOP_WORDS and not w.isdigit()
    ]
    return fts_or_query(dict.fromkeys(stem(w) for w in words))


def _speaker(row: dict[str, Any]) -> str:
    if row["role"] == "assistant":
        return "Альфред"
    return row.get("user_name") or "Собеседник"


async def recall_verbatim(
    store: Store,
    settings: Settings,
    chat_id: int,
    dialogue_id: int,
    query_text: str,
    history: DialogueHistory,
) -> tuple[str | None, int]:
    budget = settings.llm.context_recall_budget_chars
    if budget <= 0 or history.max_hidden_id is None or not query_text.strip():
        return None, 0
    match = fts_query(query_text)
    if not match:
        return None, 0
    try:
        hits = await store.search_dialogue_turns(
            chat_id,
            dialogue_id,
            match,
            max_message_id=history.max_hidden_id,
            limit=_RECALL_CANDIDATES,
        )
    except Exception:
        log.exception("dialogue_context: поиск по ai_turns_fts упал (chat=%s)", chat_id)
        return None, 0
    if not hits:
        return None, 0
    rows = [r for r in await store.ai_turns_for_dialogue(chat_id, dialogue_id) if r["content"]]
    index = {r["message_id"]: i for i, r in enumerate(rows)}
    chosen: dict[int, dict[str, Any]] = {}
    used = 0
    for hit in hits:
        pos = index.get(int(hit["message_id"]))
        if pos is None:
            continue
        row = rows[pos]
        group = [row]
        if row["role"] == "user" and pos + 1 < len(rows) and rows[pos + 1]["role"] == "assistant":
            group.append(rows[pos + 1])
        elif row["role"] == "assistant" and pos > 0 and rows[pos - 1]["role"] == "user":
            group.insert(0, rows[pos - 1])
        group = [
            r
            for r in group
            if r["message_id"] not in history.verbatim_ids and r["message_id"] not in chosen
        ]
        if not group:
            continue
        size = sum(len(r["content"]) for r in group)
        if used + size > budget:
            group = [r for r in group if r is row]
            size = sum(len(r["content"]) for r in group)
            if not group or used + size > budget:
                continue
        for r in group:
            chosen[r["message_id"]] = r
        used += size
        if used >= budget:
            break
    if not chosen:
        return None, 0
    lines = [f"{_speaker(r)}: {r['content']}" for _, r in sorted(chosen.items())]
    return RECALL_HEADER + "\n" + "\n\n".join(lines), len(chosen)


@dataclass
class _Target:
    node_link: ServiceLink
    store: Store
    settings: Settings
    chat_id: int
    dialogue_id: int
    dst: Address
    num_ctx: int


def _chunks(rows: list[dict[str, Any]], max_chars: int) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        length = len(row["content"])
        if current and size + length > max_chars:
            out.append(current)
            current, size = [], 0
        current.append(row)
        size += length
    if current:
        out.append(current)
    return out


async def _summarize(target: _Target, previous: str, rows: list[dict[str, Any]]) -> str | None:
    cfg = target.settings.llm
    parts = []
    if previous:
        parts.append(f"Прежнее краткое содержание:\n{previous}")
    parts.append(
        "Следующие реплики разговора (по порядку):\n"
        + "\n\n".join(f"{_speaker(r)}: {r['content']}" for r in rows)
    )
    parts.append(
        "Напиши обновлённое краткое содержание всего разговора целиком — "
        f"не длиннее {cfg.context_summary_max_chars} символов."
    )
    args = {
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
        "role": ROLE_SUMMARIZER,
        "system": SUMMARIZER_SYSTEM_PROMPT,
        "chat_id": target.chat_id,
    }
    try:
        result = await target.node_link.command(
            ACTION_CHAT, args, dst=target.dst, timeout=cfg.request_timeout_s
        )
    except UNREACHABLE_ERRORS as exc:
        log.warning(
            "dialogue_context: сжатие не удалось (chat=%s, dialogue=%s): %s",
            target.chat_id,
            target.dialogue_id,
            exc,
        )
        return None
    text = str(result.get("response") or "").strip() if isinstance(result, dict) else ""
    if isinstance(result, dict) and result.get("done_reason") == "length":
        log.warning(
            "dialogue_context: краткое содержание упёрлось в потолок длины (chat=%s)",
            target.chat_id,
        )
    return text or None


class DialogueCompressor:
    def __init__(self) -> None:
        self._tasks: dict[tuple[int, int], asyncio.Task[Any]] = {}
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

    def is_running(self, chat_id: int, dialogue_id: int) -> bool:
        task = self._tasks.get((chat_id, dialogue_id))
        lock = self._locks.get((chat_id, dialogue_id))
        return (task is not None and not task.done()) or (lock is not None and lock.locked())

    def schedule(
        self,
        node_link: ServiceLink,
        store: Store,
        settings: Settings,
        chat_id: int,
        dialogue_id: int,
        dst: Address,
        *,
        num_ctx: int,
    ) -> bool:
        key = (chat_id, dialogue_id)
        if self.is_running(chat_id, dialogue_id):
            log.info(
                "dialogue_context: сжатие chat=%s dialogue=%s уже идёт — повторно не запускаем",
                chat_id,
                dialogue_id,
            )
            return False
        target = _Target(node_link, store, settings, chat_id, dialogue_id, dst, num_ctx)
        task = asyncio.create_task(self._background(target))
        self._tasks[key] = task

        def _forget(done: asyncio.Task[Any], key: tuple[int, int] = key) -> None:
            if self._tasks.get(key) is done:
                del self._tasks[key]

        task.add_done_callback(_forget)
        return True

    async def wait(self, chat_id: int, dialogue_id: int, timeout: float) -> None:
        task = self._tasks.get((chat_id, dialogue_id))
        if task is None or task.done():
            return
        log.info(
            "dialogue_context: ход chat=%s dialogue=%s ждёт идущего сжатия", chat_id, dialogue_id
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            log.warning(
                "dialogue_context: сжатие chat=%s dialogue=%s не успело за %.0f с — "
                "ход идёт со старым кратким содержанием",
                chat_id,
                dialogue_id,
                timeout,
            )
        except Exception:
            pass

    async def compress_now(
        self,
        node_link: ServiceLink,
        store: Store,
        settings: Settings,
        chat_id: int,
        dialogue_id: int,
        dst: Address,
        *,
        num_ctx: int,
        keep_recent: int | None = None,
    ) -> bool:
        target = _Target(node_link, store, settings, chat_id, dialogue_id, dst, num_ctx)
        return await self._compress(target, keep_recent=keep_recent, min_turns=1)

    async def _background(self, target: _Target) -> None:
        try:
            await self._compress(
                target, min_turns=target.settings.llm.context_min_compress_turns
            )
        except Exception:
            log.exception(
                "dialogue_context: фоновое сжатие упало (chat=%s, dialogue=%s)",
                target.chat_id,
                target.dialogue_id,
            )

    async def _compress(
        self, target: _Target, *, keep_recent: int | None = None, min_turns: int = 1
    ) -> bool:
        key = (target.chat_id, target.dialogue_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._compress_locked(target, keep_recent=keep_recent, min_turns=min_turns)

    async def _compress_locked(
        self, target: _Target, *, keep_recent: int | None, min_turns: int
    ) -> bool:
        cfg = target.settings.llm
        store = target.store
        started = time.monotonic()
        rows = [
            r
            for r in await store.ai_turns_for_dialogue(target.chat_id, target.dialogue_id)
            if r["content"]
        ]
        summary = await store.latest_dialogue_summary(target.chat_id, target.dialogue_id)
        upto = int(summary["upto_message_id"]) if summary else 0
        previous = summary["summary"] if summary else ""
        pending = [r for r in rows if r["message_id"] > upto]
        keep = keep_recent if keep_recent is not None else cfg.context_keep_recent_turns
        split = len(pending) - keep
        while 0 < split < len(pending) and pending[split]["role"] == "assistant":
            split += 1
        old = pending[: max(split, 0)]
        if len(old) < min_turns:
            return False
        chunk_chars = min(
            cfg.context_summary_chunk_chars,
            max(
                MIN_SUMMARY_CHUNK_CHARS,
                int(target.num_ctx * SUMMARY_REQUEST_WINDOW_SHARE * cfg.context_chars_per_token)
                - cfg.context_summary_max_chars,
            ),
        )
        chunks = _chunks(old, chunk_chars)
        before_chars = len(previous) + sum(len(r["content"]) for r in old)
        written = 0
        for chunk in chunks:
            new_summary = await _summarize(target, previous, chunk)
            if new_summary is None:
                break
            await store.add_dialogue_summary(
                target.chat_id,
                target.dialogue_id,
                int(chunk[-1]["message_id"]),
                new_summary,
                utc_now(),
            )
            previous = new_summary
            written += 1
        log.info(
            "dialogue_context: сжатие chat=%s dialogue=%s — ходов %d (частей %d/%d), "
            "%d симв. → %d симв., %.1f с",
            target.chat_id,
            target.dialogue_id,
            len(old),
            written,
            len(chunks),
            before_chars,
            len(previous),
            time.monotonic() - started,
        )
        return written > 0


COMPRESSOR = DialogueCompressor()
