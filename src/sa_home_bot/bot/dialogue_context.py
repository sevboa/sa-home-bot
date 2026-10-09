"""История /ai для модели: сборка, оценка окна, сжатие, дословный возврат
деталей (Этап 50, 2026-09-30, IMPLEMENTATION_PLAN.md).

Живая поломка, ради которой это сделано: история треда уходила в модель
целиком (ai_turns без LIMIT, семь одинаковых копий сборки в
bot/handlers/ai.py), промпт дорастал до ~31.8k из 32768 токенов окна, модель
уходила в thinking, окно кончалось посреди генерации (done_reason=length) —
пользователь получал «Альбегта» (пустой ответ) или ответ, молча обрезанный на
полуслове; вдобавок Ollama при переполнении сама выкидывает начало истории,
никому не говоря. База промпта ~12k (персонаж, декларации тулов, роутер), а
83% роста — длинные ответы Альфреда.

Как устроено:

* **Сборка** (load_history) — одна функция на все пути хендлера: [краткое
  содержание начала разговора отдельным system-сообщением] + ходы после
  ``upto_message_id`` этого содержания. Старые (не из последних
  ``context_keep_recent_turns``) ответы Альфреда укорачиваются до
  ``context_old_reply_max_chars`` — полную версию при нужде вернёт
  дословный возврат ниже. ai_turns не меняется никогда: полный текст всех
  ходов остаётся в БД.
* **Оценка до генерации** (estimate_prompt_tokens) — по последнему замеру
  prompt_eval_count треда плюс прирост в символах и по чистым символам
  (база + текст / ``context_chars_per_token``); берётся бо́льшая из двух:
  prefix-кэш Ollama может занижать prompt_eval_count, а оценка по символам
  не знает точной базы — ошибаемся в сторону запаса.
* **Порог превышен** — в ЭТОТ ЖЕ ответ Альфреду добавляется инструкция
  (STEP_AWAY_INSTRUCTION): ответить как обычно и в конце, в образе, сказать,
  что ему нужно ненадолго отлучиться, придумав повод самому. После доставки
  ответа хендлер запускает фоновое сжатие (DialogueCompressor.schedule).
* **Сжатие** — отдельный LLM-запрос службы llm с ролью ``summarizer`` (свой
  system без персонажа, think off, temperature 0): прежнее содержание +
  старые ходы → новое содержание ~1–1.5k токенов, в ai_dialogue_summaries.
  Сжимается только история разговора — системный промпт, тулы и заметка
  не трогаются.
* **Дословный возврат** (recall_verbatim) — перед генерацией FTS5-поиск
  (trigram, ai_turns_fts) по ходам треда, которых нет в истории дословно
  (сжатые или укороченные), под текущую реплику; найденные сообщения
  целиком, парой «реплика собеседника + ответ Альфреда», блоком «Из начала
  разговора (дословно)» в пределах ``context_recall_budget_chars``. Так
  «медведь с зелёными глазами», потерявший цвет глаз в пересказе, на
  вопрос про медведя возвращается исходными сообщениями.
* **Страховка** (bot/ai_flow.py::request_alfred) — если ответ всё же
  вернулся с done_reason=length / пустым из-за переполнения: синхронное
  сжатие, короткая заготовленная реплика в образе (STEP_AWAY_LINES) и
  перегенерация; не помогло — отрезаются самые старые ходы по бюджету
  (trim_to_budget).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.llm_chat import ChatStats
from sa_home_bot.proto.messages import Address, ProtoError

log = logging.getLogger(__name__)

# Дублирует llm/service.py::ACTION_CHAT/ROLE_SUMMARIZER — тот же приём, что
# в llm_chat.py: бот не импортирует модуль службы llm (тяжёлые зависимости).
ACTION_CHAT = "chat"
ROLE_SUMMARIZER = "summarizer"

SUMMARY_PREFIX = "Краткое содержание начала разговора: "
RECALL_HEADER = (
    "Из начала разговора (дословно) — сообщения, которые относятся к текущей "
    "реплике; в кратком содержании их детали могли потеряться:"
)
# Метка укороченного старого ответа Альфреда в истории для модели: явная, а
# не «…», чтобы модель не приняла обрыв за свою манеру и не подражала ему.
SHORTENED_MARK = " […]"

# Инструкция в ЭТОТ ЖЕ ответ при превышении порога (решение пользователя
# 2026-09-30): ответ как обычно, а в конце — в образе, своим поводом, без
# единого технического слова. Примеры поводов — только примеры: модель
# должна придумать уместный по ходу разговора.
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

# Страховка: реплика, пока Альфред «отлучился» (синхронное сжатие +
# перегенерация). Заготовки, а не вывод модели — модель как раз и
# захлебнулась. Написаны БЕЗ буквы «р»: Логопед (llm/speech_therapy.py,
# картавость) обрабатывает только вывод модели, а эти строки идут мимо него —
# без «р» они верны и для картавого Альфреда, и для «чистой речи» гостя
# (Этап 47), проверяется тестом.
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

# Поиск для дословного возврата: сколько кандидатов брать из FTS (часть из
# них отсеется как уже присутствующие в истории дословно).
_RECALL_CANDIDATES = 24
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Короче трёх букв триграммный индекс не ищет в принципе.
_MIN_WORD_CHARS = 3
# Служебные слова и обращения: как поисковые слова они тянут случайные ходы.
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
    """История треда для модели (список ``{"role", "content"}``) плюс то, что
    о ней знает сборка: где кончается сжатое, какие ходы лежат дословно.

    Подкласс списка, а не отдельный объект рядом: история проходит через
    хендлеры (фото/стикер подменяют последний ход, пустой реплай дописывает
    директиву) и в request_alfred как обычный список — подмены
    request_alfred в тестах и все места, где с историей работают как со
    списком, ничего не замечают. ``compress_after`` — выход: request_alfred
    ставит его, когда сработал порог, хендлер после доставки ответа
    запускает фоновое сжатие."""

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
        # message_id, по который (включительно) ходы ушли в краткое содержание.
        self.summary_upto = summary_upto
        # Ходы, чей полный текст уже есть в истории — их не возвращаем.
        self.verbatim_ids: set[int] = verbatim_ids or set()
        # Наибольший message_id среди ходов, которых нет в истории дословно
        # (сжатые или укороченные) — граница поиска дословного возврата.
        self.max_hidden_id = max_hidden_id
        # Сколько ходов уже сейчас можно сжать (старше последних N).
        self.compressible = compressible
        self.compress_after = False


def _shorten(text: str, limit: int) -> str:
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + SHORTENED_MARK


async def load_history(
    store: Store,
    settings: Settings,
    chat_id: int,
    dialogue_id: int,
    *,
    before_message_id: int | None = None,
    wait: bool = True,
) -> DialogueHistory:
    """История треда для модели — единая сборка для всех путей хендлера.

    ``wait`` — если по этому треду прямо сейчас идёт сжатие, дождаться его
    (не дольше ``context_compress_wait_s``), чтобы ход пошёл уже с новым
    кратким содержанием. Не дождались — идём со старым: оно всегда целостно
    (новая версия пишется одной строкой только после успешного пересказа), а
    ходы после его upto_message_id подтягиваются из ai_turns целиком — ничего
    не теряется, история лишь длиннее. ``before_message_id`` — только ходы
    раньше этого сообщения (перегенерация в страховке: текущий ход
    вызывающий подставляет сам)."""
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
            content = _shorten(content, limit)
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


# Тулы, чей след виден в истории: вызов и результат после хода собеседника.
# Живая находка 2026-10-03: в истории были только тексты — «покажи кабинет»
# → «Вот, прошу. Кажется, свет…» (подпись к снимку), — и на следующую такую
# же просьбу модель отвечала тем же текстом, не вызывая take_photo: образец
# в контексте говорил, что снимок — это слова. Только тулы, которые что-то
# присылают в чат (снимок, картинка, карточка вещи): их не повторить словами.
# Живая находка 2026-10-09: тулы людей — id из find_person жил один ход;
# на «да, хочу связь» модель звала request_acquaintance с выдуманным
# 123456789. Их след тоже в истории, длиннее: id кандидатов не обрезать.
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
    """{message_id хода собеседника: [вызов тулов, результаты]} — в формате
    раундов run_chat_loop (llm_chat.py)."""
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


# --- оценка окна ---


def estimate_prompt_tokens(settings: Settings, sent_chars: int, state: dict | None) -> int:
    """Оценка промпта хода в токенах ДО генерации.

    ``sent_chars`` — всё, что бот сейчас отправит сверх постоянной базы
    (история, заметка, дословный возврат, инструкции). ``state`` — последний
    замер треда (Store.dialogue_context_state): prompt_eval_count первого
    раунда прошлого хода и сколько символов тогда ушло. Берём бо́льшую из
    двух оценок — по замеру + приросту и по чистым символам (см. докстринг
    модуля про кэш префикса)."""
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
    """Ответ испорчен переполнением окна: генерация упёрлась в окно
    (done_reason=length — обрывок или пустота) либо пустой ответ при
    промпте, занявшем почти всё окно."""
    if stats.truncated:
        return True
    window = stats.num_ctx or num_ctx
    return not raw.strip() and stats.max_prompt_tokens >= int(window * 0.9)


def trim_to_budget(messages: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """Крайняя мера страховки: самые старые ходы отрезаются, пока история
    не влезет в ``max_chars``. Краткое содержание (первое сообщение) и
    текущий ход (последнее) остаются всегда."""
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
    # Отрезанное начало не должно оставить раунд тула без хода собеседника.
    while kept[:-1] and (kept[0].get("role") == "tool" or "tool_calls" in kept[0]):
        kept.pop(0)
    return head + kept


def pick_step_away_line() -> tuple[str, str]:
    """(HTML, markdown) — случайная реплика страховки в образе."""
    line = random.choice(STEP_AWAY_LINES)
    return f"<b>Альфред:</b> {line}", f"**Альфред:** {line}"


# --- дословный возврат деталей ---


def _stem(word: str) -> str:
    """Начало слова для триграммного поиска — тот же грубый приём, что у
    службы memory (memory/service.py::_stem): «медведя» → «медвед» находит
    и «медведь», «глаза» → «глаз» находит «глазами»."""
    if len(word) >= 6:
        return word[:-2]
    if len(word) >= 4:
        return word[:-1]
    return word


def fts_query(text: str) -> str:
    """Реплика → выражение MATCH для ai_turns_fts: значимые слова через OR,
    каждое в кавычках как фраза (пунктуация и служебные слова FTS5 из
    живого текста не ломают разбор), стоп-слова и короткие — прочь. «ё» →
    «е», как и в самом индексе (schema.sql::ai_turns_fts_insert): «зелёные»
    и «зеленые» в переписке пишут вперемешку."""
    words = [
        w
        for w in _WORD_RE.findall(text.lower().replace("ё", "е"))
        if len(w) >= _MIN_WORD_CHARS and w not in _STOP_WORDS and not w.isdigit()
    ]
    stems = dict.fromkeys(_stem(w) for w in words)
    return " OR ".join(f'"{s}"' for s in stems)


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
    """Блок «Из начала разговора (дословно)» под текущую реплику и число
    возвращённых сообщений. None — нечего возвращать (всё и так в истории
    дословно, реплика без значимых слов, ничего не нашлось)."""
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
    except Exception:  # noqa: BLE001 — сбой поиска не должен ронять ход
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
        # Пара «реплика собеседника + ответ Альфреда»: без неё находка
        # вырвана из контекста (вопрос без ответа или ответ без вопроса).
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
            # Пара не влезает — хотя бы само найденное сообщение, если оно
            # не из уже выбранных и влезает целиком (дословно, а не обрывком).
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


# --- сжатие ---


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
    except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
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
    """Сжатие истории тредов — по одному на тред одновременно.

    Защита от двойного запуска двумя слоями: ``schedule`` не заводит вторую
    фоновую задачу, пока жива первая, а само сжатие идёт под замком треда —
    синхронное сжатие страховки дождётся фонового и, перечитав БД, увидит,
    что сжимать уже нечего. Новое краткое содержание пишется одной строкой
    только после успешного пересказа, поэтому ход, который не дождался
    сжатия, всегда видит целостную картину (см. load_history)."""

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
        """Фоновое сжатие после доставки ответа. False — по треду сжатие
        уже идёт, вторую задачу не заводим."""
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
        """Дождаться идущего фонового сжатия треда, не дольше ``timeout``."""
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
        except Exception:  # noqa: BLE001 — сбой сжатия уже залогирован задачей
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
        """Синхронное сжатие (страховка). True — записано новое краткое
        содержание (хотя бы по одной части)."""
        target = _Target(node_link, store, settings, chat_id, dialogue_id, dst, num_ctx)
        return await self._compress(target, keep_recent=keep_recent, min_turns=1)

    async def _background(self, target: _Target) -> None:
        try:
            await self._compress(
                target, min_turns=target.settings.llm.context_min_compress_turns
            )
        except Exception:  # noqa: BLE001 — фон: сбой не должен всплыть в пустоту
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
        # Дословный хвост начинается с реплики собеседника, не с ответа
        # Альфреда: иначе ответ остался бы без вопроса, на который он дан.
        while 0 < split < len(pending) and pending[split]["role"] == "assistant":
            split += 1
        old = pending[: max(split, 0)]
        if len(old) < min_turns:
            return False
        # Одна часть — не больше ~55% окна вместе с прежним содержанием:
        # запрос сжатия сам не должен переполнить окно.
        chunk_chars = min(
            cfg.context_summary_chunk_chars,
            max(
                4000,
                int(target.num_ctx * 0.55 * cfg.context_chars_per_token)
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
                datetime.now(tz=UTC),
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


# Один на процесс бота (тот же приём, что ai_flow._profile_cache): треды
# живут в одном процессе, общий замок/реестр задач и нужен.
COMPRESSOR = DialogueCompressor()
