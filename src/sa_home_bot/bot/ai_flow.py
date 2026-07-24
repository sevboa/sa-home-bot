"""Оркестрация диалога /ai: вызов службы llm с presence/wake-сценарием.

Персонажи и сценарий — из обсуждения с пользователем 2026-07-23: если нода
winpc недоступна, показать «шаги», молча разбудить через рой (существующий
механизм — см. bot/handlers/wake.py::wake_swarm_node_core), подождать до
WAKE_POLL_TIMEOUT_S, затем «Агнольд» (успех) или «Альбегт» (неудача). Имена
персонажей — фиксированные строки (не вывод модели): их произносит Альфред,
отсюда искажение «р→г» («Арнольд»→«Агнольд», «Альберт»→«Альбегт»); сами они
«р» выговаривают, поэтому их реплики пишутся без искажений.

Живая находка 2026-07-24: 30с было мало — winpc не всегда успевала выйти из
S3-сна (BIOS POST + загрузка Windows + старт службы sa-home-node + регистрация
"llm" пиром) за это время, пользователь получал «Альбегт» раньше, чем машина
реально просыпалась. Отдельно нашлась и первопричина частых засыпаний:
AC-таймер сна (`powercfg`) на winpc оказался снова включён (45 мин) — раньше
уже отключался (см. память "Windows PC"), видимо сбросился (обновление/смена
схемы питания). Таймер выключен заново, но код всё равно должен переживать
холодный подъём из S3, если сон когда-нибудь включится опять (вручную,
обновлением и т.п.) — таймаут увеличен с запасом.

Живая находка 2026-07-24: обращение к Альфреду часто идёт реплаем — либо
на его же прошлое сообщение с выделением (quote) конкретного слова/фразы,
либо (в группе) реплаем на ЧУЖОЕ сообщение с упоминанием бота, где сам
текст обращения смысла без контекста не имеет ("узнай точный счёт" —
счёт чего?). См. _reply_context_lines: добавляет в заметку (1) текст
чужого сообщения, если оно не часть уже идущей истории диалога, и (2)
выделенную цитату (Message.quote, Bot API "reply with quote") — второе
добавляется всегда, даже в своём треде, потому что история диалога
хранит полный текст хода, но не то, какую его часть выделили.

Живая находка 2026-07-24 (проверено вживую на реальном боте, не только в
тестах): реплай на чужое сообщение сработал сразу и верно (модель
процитировала нужный текст), а вот quote (выделенное слово) — нет,
модель назвала другое слово. Причина оказалась двойная:
1. Формулировка "в нём выделен фрагмент" ссылалась местоимением на
   строку про "чужое сообщение", а та для СВОЕГО треда как раз
   пропускается (текст уже есть в истории) — антецедент терялся.
   Переписано так, чтобы предложение про quote само называло источник
   ("из твоего же предыдущего сообщения"), без внешних отсылок.
2. Заметка вставлялась ПЕРЕД всей историей диалога — в уже не самом
   коротком треде это далеко от текущего хода, к которому она относится.
   Теперь вставляется прямо перед последним (текущим) сообщением, а не в
   начало — ближе к месту, где решается, что ответить.
Диагностика велась временным INFO-логом (chat/dialogue/reply_to/quote/
note) — подтвердил, что Telegram-данные приходят правильно, проблема
была чисто в промпте; лог убран после проверки.

Живая находка 2026-07-24: когда presence не показывал сна, но сам
chat-запрос всё равно падал (внутренний сбой Ollama, ERR_INTERNAL — не
«недоступна»), пользователю уходил голый технический текст без
персонажа ("Прошу прощения, не вышло — попробуйте чуть позже", от лица
"Альфреда", но без картавости — читалось чужеродно в общем чате).
Пользователь явно попросил не пускать такое в группу; раз полностью
исключить единичный сбой генерации нельзя (Ollama иногда даёт сетевой
сбой сразу после начала запроса), обёрнуто в того же персонажа —
ALBERT_HICCUP ("Альфред на секунду отвлёкся — повторите"), не голая
техника. См. также notify_admins — админ получает подробности отдельно
(в свою личку, не в общий чат — определяется по allowed_commands="*" в
подписке, у общих групповых чатов его нет).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from aiogram.types import Message, User

from sa_home_bot.bot import swarm_view
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.handlers.wake import wake_swarm_node_core
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import (
    ERR_INTERNAL,
    ERR_UNAVAILABLE,
    ERR_UNKNOWN_DST,
    Address,
    ProtoError,
)
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import WILDCARD

log = logging.getLogger(__name__)

LLM_NODE = "winpc"
LLM_SERVICE = "llm"
ACTION_CHAT = "chat"

STEPS_TEXT = "<i>Вы слышите приближающиеся шаги...</i>"
ARNOLD_WAKING = "<b>Агнольд:</b> Сейчас Альфред подойдёт"
ALBERT_UNAVAILABLE = (
    "<b>Альбегт:</b> К сожалению Альфреда нет на месте, попробуйте позже, сэр/мадам"
)
ALBERT_ASLEEP = "<b>Альбегт:</b> Альфред, кажется, уснул — обратитесь позже, сэр/мадам"
# Внутренний сбой генерации (нода жива, presence не показывал сна, но сам
# chat-запрос упал — например Ollama на секунду поперхнулась) — раньше сюда
# шёл технический "Прошу прощения, не вышло" без персонажа (голым текстом
# от лица "Альфреда", без картавости — читалось чужеродно); пользователь
# явно попросил (2026-07-24) не пускать техническое в чат. Решение: даже не
# зная точной причины, подаём это тем же персонажем — Альфред как будто на
# секунду отвлёкся, просим повторить.
ALBERT_HICCUP = (
    "<b>Альбегт:</b> Прошу прощения, Альфред на секунду отвлёкся — повторите, сэр/мадам"
)
# Закрытие треда, когда служба llm сама гасит контейнер по простою
# (llm/service.py::EVENT_IDLE_SLEEP) — не отсюда, а из bot/node_events.py
# (событие прилетает не в ответ на сообщение пользователя), но текст —
# часть того же персонажа, поэтому живёт здесь.
CLOSING_TEXT = "<i>Альфред не дождался обращения и уходит к себе в подсобку</i>"
# Перезапуск процесса службы llm (деплой/апдейт/ручной restart) — тот же
# приём, что и CLOSING_TEXT (llm/service.py::EVENT_SERVICE_RESTART, отсюда
# рассылает bot/node_events.py), другой повод и текст: явно НЕ выглядит как
# сбой — пользователь попросил именно это 2026-07-24.
RESTART_TEXT = "<i>У Альфреда появились другие дела</i>"


class ActiveAiChats:
    """Множество chat_id с прямо сейчас идущим /ai-запросом.

    Живая находка 2026-07-24: раньше RESTART_TEXT слался только при
    останове службы llm (winpc) — но останов самого БОТА (alfred, любой
    деплой/restart_node) с тем же успехом обрывает запрос на середине:
    `bot.session.close()` в _shutdown() закрывает HTTP-коннектор, и уже
    ушедший на LLM запрос падает с TelegramNetworkError/"Connector is
    closed" прямо в лицо пользователю — голым "что-то пошло не так", а не
    в характере. Раньше это было маловероятно (ответ занимал секунды), но
    v0.35.0 включил think_chat (~30-40с на раунд) — окно для такой коллизии
    выросло на порядок. Инстанс живёт в bot/app.py::run(), хендлеры
    (bot/handlers/ai.py::_ask_and_reply) регистрируют/снимают chat_id на
    время своего запроса; _shutdown() перед закрытием сессии рассылает
    RESTART_TEXT по снимку множества — тем самым пользователь получает
    "У Альфреда появились другие дела" ДО того, как соединение оборвётся,
    а не голую сетевую ошибку после. Сам оборвавшийся хендлер всё равно
    позже упадёт при попытке отправить настоящий ответ (сессия уже
    закрыта) — это ожидаемо и не страшно, тот путь уже устойчив к сбоям
    (log.exception, не роняет процесс), просто пользователь этого уже не
    увидит вторым сообщением поверх первого."""

    def __init__(self) -> None:
        self._chat_ids: set[int] = set()

    def add(self, chat_id: int | None) -> None:
        if chat_id is not None:
            self._chat_ids.add(chat_id)

    def discard(self, chat_id: int | None) -> None:
        if chat_id is not None:
            self._chat_ids.discard(chat_id)

    def snapshot(self) -> list[int]:
        return sorted(self._chat_ids)


WAKE_POLL_TIMEOUT_S = 90.0
WAKE_POLL_INTERVAL_S = 3.0
# Живая находка 2026-07-23: TCP-keepalive (proto/client.py) обнаруживает
# пропавшего пира не мгновенно (до ~50с — TCP_KEEPALIVE_IDLE_S=20 +
# INTERVAL_S=10 * COUNT=3), а get_state() без явного укороченного таймаута
# ждёт весь дефолт ProtoClient (10с) на каждый хоп. Если presence-проверка
# уже говорит "недоступна" — не тратим ещё раз время на полноценный _ask()
# (до request_timeout_s) с тем же исходом, сразу идём в сценарий wake.
_PRESENCE_CHECK_TIMEOUT_S = 3.0
# Сколько раз подряд можно уйти в tool_calls, прежде чем модель обязана
# дать финальный текстовый ответ — защита от зацикливания (LLM_INTEGRATION_
# PLAN.md §7.1 п.5). Превышение — тот же путь, что прочие внутренние сбои
# (ALBERT_HICCUP), не зависание запроса.
_MAX_TOOL_ROUNDS = 4

# Вариативное рассуждение (LLM_INTEGRATION_PLAN.md §7, живая находка
# 2026-07-24): включать think=true на КАЖДЫЙ запрос надёжно для расчётов,
# но раздувает даже "привет" до 30-40с. Вместо этого — быстрый проход
# (think=false) с инструкцией самой решить, нужно ли ей подумать над ИМЕННО
# этим вопросом; если да — вернуть только этот маркер, без попытки
# угадать ответ, и мы переспросим уже в режиме рассуждения. Строка выбрана
# заведомо не встречающейся в обычном тексте (не просто слово — реальный
# ответ про, скажем, шахматы или мышление никогда не выведет её случайно).
THINK_MARKER = "[[ТРЕБУЕТСЯ_РАЗМЫШЛЕНИЕ]]"
# Живая находка 2026-07-24 (проверено прямыми запросами к Ollama, не только
# юнит-тестами): мягкая формулировка ("если требует обдумывания, с которым
# не стоит спешить") на задаче про цилиндр промолчала и дала прямой (и
# неверный) ответ — модель переоценивает свою уверенность. Явное перечисление
# триггеров ("формула, геометрия, физика, многошаговый расчёт") плюс "ДАЖЕ
# если кажется, что знаешь ответ" — сработало надёжно. При этом тривиальная
# арифметика ("2+2") осталась в быстром проходе, не переоценивается в
# другую сторону — модель не бросается за маркером на каждое число.
_TRIAGE_INSTRUCTION = (
    "Прежде чем ответить на СЛЕДУЮЩЕЕ сообщение, проверь: есть ли в нём "
    "формула, геометрия, физика, многошаговый расчёт или логическая задача, "
    "где легко ошибиться? Если да — ДАЖЕ если тебе кажется, что ты и так "
    "знаешь ответ, НЕ отвечай и НЕ считай сам, а выведи ТОЛЬКО одну строку "
    f"без всего остального: {THINK_MARKER}. Если вопрос простой (бытовой, "
    "светский, о себе или о доме, тривиальная арифметика вроде «сколько "
    "будет 2+2») — отвечай сразу и как обычно, в характере."
)
# Показывается перед вторым (думающим) проходом — пользователь явно
# попросил лаконичную реплику в духе персонажа, не техническое "думаю...".
THINKING_TEXT = "<i>На лице Альфреда проступает задумчивость</i>"

_WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def _is_unavailable(exc: Exception) -> bool:
    if isinstance(exc, ServiceUnavailableError):
        return True
    return isinstance(exc, ProtoError) and exc.code in (ERR_UNAVAILABLE, ERR_UNKNOWN_DST)


async def notify_admins(book: SubscriptionBook, notifier: Notifier, text: str) -> None:
    """Диагностика падений /ai — в чаты с полным доступом (allowed_commands
    содержит "*"), не пользователю. Молчаливая деградация («Альбегт», нода
    просто спит) сюда не попадает — только настоящие сбои (см. вызовы ниже)."""
    for sub in book.all():
        if WILDCARD in sub.allowed_commands:
            await notifier.send_direct(sub.chat_id, text)


def display_name(user: User | None) -> str | None:
    """Имя для промпта LLM и для колонки ai_turns.user_name: имя(+фамилия)
    Telegram-профиля, плюс @username в скобках, если он есть (полезно, когда
    у двух собеседников совпадают имена)."""
    if user is None:
        return None
    name = user.full_name
    return f"{name} (@{user.username})" if user.username else name


# Реплай на явно длинное сообщение (например, кто-то процитировал большой
# форвард) режем — это справочная заметка, а не полноценный ход диалога,
# ей не место раздувать контекст модели ради текста, который сама история
# диалога всё равно не хранит.
_REPLY_QUOTE_MAX_CHARS = 800


async def _reply_context_lines(message: Message, store: Store, dialogue_id: int) -> list[str]:
    """Контекст о сообщении, на которое сейчас отвечают (Telegram reply) —
    то, что модель иначе никак не увидит.

    Два независимых случая (решение пользователя 2026-07-24):
    1. Реплай на "чужое" сообщение — не часть уже идущего треда Альфреда
       (другой dialogue_id или вообще не ход Альфреда, например ответ на
       обычное сообщение в группе с упоминанием бота) — его текст сюда не
       попал бы никаким другим путём, добавляем целиком (с автором).
    2. Reply-quote (Telegram "ответить с выделением") — конкретный
       фрагмент, который человек явно выделил перед ответом. Добавляем
       всегда, даже если само сообщение уже есть в истории диалога (свой
       же тред) — то, ЧТО именно выделено, история диалога не хранит."""
    reply_to = message.reply_to_message
    if reply_to is None:
        return []
    lines: list[str] = []

    known = await store.ai_turn(message.chat.id, reply_to.message_id) if message.chat else None
    is_own_thread = known is not None and known["dialogue_id"] == dialogue_id
    if not is_own_thread:
        text = reply_to.text or reply_to.caption
        if text:
            author = display_name(reply_to.from_user) or "неизвестный собеседник"
            if len(text) > _REPLY_QUOTE_MAX_CHARS:
                text = text[:_REPLY_QUOTE_MAX_CHARS] + "…"
            lines.append(f"Сообщение, на которое сейчас отвечают ({author}): «{text}».")

    quote = message.quote
    if quote is not None and quote.text:
        quote_text = quote.text
        if len(quote_text) > _REPLY_QUOTE_MAX_CHARS:
            quote_text = quote_text[:_REPLY_QUOTE_MAX_CHARS] + "…"
        # Живая находка 2026-07-24: формулировка должна называть источник
        # цитаты САМА — раньше был дефис-отсылка "в нём", которая указывала
        # на строку выше, а та строка для своего же треда как раз
        # пропускается (текст и так есть в истории) — "нём" повисало без
        # антецедента, и модель на практике путала выделенное слово с
        # каким-то другим (проверено вживую 2026-07-24: попросили
        # процитировать выделенное "привычки" — модель назвала "повтори").
        if is_own_thread:
            lines.append(
                f"Отвечая тебе, собеседник выделил и процитировал фрагмент "
                f"именно ИЗ ТВОЕГО ЖЕ предыдущего сообщения в этом треде — "
                f"вот он, дословно: «{quote_text}». Если тебя просят повторить "
                f"или процитировать «то слово»/«то, на что я ответил» — речь "
                f"именно про этот фрагмент, не подбирай другое слово из своей "
                f"памяти о переписке."
            )
        else:
            lines.append(
                f"Отвечая, собеседник выделил и процитировал в сообщении "
                f"выше именно этот фрагмент, дословно: «{quote_text}». Если "
                f"тебя просят повторить или процитировать «то, на что я "
                f"ответил» — речь именно про этот фрагмент."
            )

    return lines


async def _build_context_note(message: Message, store: Store, dialogue_id: int) -> str:
    """Служебная заметка для модели (не для пользователя): точное время
    сейчас (§8.1 плана — маленькие локальные модели плохо знают "сейчас", а
    время нужно почти на каждый запрос, отдельного тула для этого не
    заводим), кто сейчас пишет, кто начал этот тред, кто ещё обращался к
    Альфреду в этом чате, и что за сообщение цитируют/на что отвечают (см.
    _reply_context_lines).

    Пункты про тред/участников — только для групп: в личке собеседник
    всегда один и тот же, уточнять нечего. Строится заново на каждый запрос
    (не хранится в ai_turns) — участники чата могут появляться по ходу дела,
    а время не имеет смысла хранить вовсе."""
    now_local = datetime.now().astimezone()
    lines = [
        f"Точное время сейчас: {now_local:%Y-%m-%d %H:%M} "
        f"({_WEEKDAYS_RU[now_local.weekday()]})."
    ]

    sender_name = display_name(message.from_user)
    is_group = message.chat is not None and message.chat.type in ("group", "supergroup")
    if sender_name is not None:
        lines.append(f"Сейчас с тобой говорит: {sender_name}.")
    if sender_name is not None and is_group:
        starter = await store.ai_turn(message.chat.id, dialogue_id)
        starter_name = starter.get("user_name") if starter else None
        if starter_name and starter_name != sender_name:
            lines.append(
                f"Этот разговор начал(а) {starter_name}, а сейчас в него отвечает "
                f"{sender_name} — не тот человек, кто начинал. Это чужой тред: не "
                f"отказывай в ответе, но веди себя чуть сдержаннее, чем со "
                f"{starter_name} — обратись к {sender_name} по имени и мягко "
                f"дай понять, что это разговор {starter_name} (например: «Это, "
                f"кажется, беседа {starter_name}, сэр/мадам — но извольте, чем "
                f"могу быть полезен?» или предложи начать свой отдельный "
                f"разговор), не подхватывай тему с тем же радушием, как с тем, "
                f"кто начал."
            )

        participants = await store.chat_participants(message.chat.id)
        others = [
            p["user_name"]
            for p in participants
            if p["user_name"] and p["user_name"] not in (sender_name, starter_name)
        ]
        if others:
            lines.append("В этом чате к тебе также обращались: " + ", ".join(others) + ".")

    lines.extend(await _reply_context_lines(message, store, dialogue_id))

    lines.append(
        "Это справка для тебя, не пересказывай её вслух — просто учти при ответе "
        "(например, обратиться по имени), если уместно."
    )
    return " ".join(lines)


async def _run_chat_loop(
    node_link: ServiceLink,
    dst: Address,
    timeout: float,
    messages: list[dict[str, Any]],
    tool_ctx: ai_tools.ToolContext,
    think: bool,
    telegram_chat_id: int | None,
    log_chat_id: Any,
) -> str:
    """Один проход диалога с моделью (LLM_INTEGRATION_PLAN.md §7.1): раунды
    tool-calling (до _MAX_TOOL_ROUNDS), пока не придёт финальный текст.

    ``messages`` мутируется по ходу (дописываются tool_calls/результаты) —
    вызывающий передаёт отдельный список на каждый проход, если хочет
    сохранить исходную историю чистой (см. request_alfred: быстрый и
    думающий проходы используют РАЗНЫЕ списки, второй не должен унаследовать
    служебный шум/маркер первого)."""
    for _round in range(_MAX_TOOL_ROUNDS):
        args: dict[str, Any] = {
            "messages": messages,
            "tools": ai_tools.TOOL_DECLARATIONS,
            "think": think,
        }
        if telegram_chat_id is not None:
            # chat_id — не для маршрутизации (та по dst), а чтобы служба
            # знала, какие чаты уведомлять при llm_idle_sleep (см.
            # докстринг модуля и llm/service.py).
            args["chat_id"] = telegram_chat_id
        result = await node_link.command(ACTION_CHAT, args, dst=dst, timeout=timeout)
        tool_calls = result.get("tool_calls")
        if not tool_calls:
            return result.get("response", "")
        messages.append({"role": "assistant", "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            name = fn.get("name", "")
            call_args = fn.get("arguments") or {}
            handler = ai_tools.TOOL_HANDLERS.get(name)
            if handler is None:
                tool_result = f"неизвестный инструмент: {name}"
            else:
                try:
                    tool_result = await handler(tool_ctx, call_args)
                except Exception as exc:  # noqa: BLE001 — сбой тула не должен ронять диалог
                    log.exception("ai: тул %s упал (chat=%s)", name, log_chat_id)
                    tool_result = f"внутренняя ошибка инструмента: {exc}"
            messages.append({"role": "tool", "content": tool_result, "name": name})
    # Лимит раундов исчерпан — модель зациклилась на вызовах инструментов,
    # не дав финального текста. Тот же путь, что прочие внутренние сбои
    # (ALBERT_HICCUP в request_alfred), не тихое зависание запроса.
    raise ProtoError(ERR_INTERNAL, "превышен лимит раундов tool-calling")


async def request_alfred(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    settings: Settings,
    history: list[dict[str, str]],
    dialogue_id: int,
    book: SubscriptionBook,
    notifier: Notifier,
) -> str | None:
    """Сходить в llm.chat с presence/wake-сценарием.

    Возвращает сырой текст ответа модели, либо None — Альфреда не нашли
    (сообщение об этом пользователю уже отправлено здесь же, вызывающему
    отвечать больше нечего).
    """
    dst = Address(node=LLM_NODE, service=LLM_SERVICE)
    timeout = settings.llm.request_timeout_s
    chat_id = message.chat.id if message.chat else "?"
    context_note = await _build_context_note(message, store, dialogue_id)

    async def _ask() -> str:
        if context_note:
            # Живая находка 2026-07-24: заметка вставлялась ПЕРЕД всей
            # историей — далеко от текущего хода, если тред уже длинный.
            # Модель на практике хуже использует информацию, если она не
            # рядом с тем сообщением, к которому относится (проверено
            # вживую: цитата в заметке была верной, но модель ответила не
            # тем словом). history[-1] — всегда текущий ход пользователя
            # (см. вызовы request_alfred в bot/handlers/ai.py — history
            # собирается с ним последним).
            base_messages: list[dict[str, Any]] = [
                *history[:-1], {"role": "system", "content": context_note}, history[-1]
            ]
        else:
            base_messages = list(history)
        tool_ctx = ai_tools.ToolContext(
            chat_id=message.chat.id if message.chat else None, store=store, settings=settings
        )
        telegram_chat_id = message.chat.id if message.chat is not None else None

        # Вариативное рассуждение (см. THINK_MARKER/_TRIAGE_INSTRUCTION
        # выше): быстрый проход с think=false и инструкцией самой попросить
        # подумать, если вопрос того требует — свой список сообщений
        # (триаж-инструкция и возможные tool-раунды быстрого прохода не
        # должны попасть в думающий проход, если до него дойдёт).
        triage_messages = [*base_messages, {"role": "system", "content": _TRIAGE_INSTRUCTION}]
        fast_answer = await _run_chat_loop(
            node_link,
            dst,
            timeout,
            triage_messages,
            tool_ctx,
            think=False,
            telegram_chat_id=telegram_chat_id,
            log_chat_id=chat_id,
        )
        if THINK_MARKER not in fast_answer:
            return fast_answer

        # Модель попросила подумать — показать это в характере и
        # переспросить уже в режиме рассуждения; свежий список сообщений
        # без триаж-инструкции и без маркера, второй проход о протоколе
        # ничего не знает и просто отвечает по существу.
        await message.answer(THINKING_TEXT)
        return await _run_chat_loop(
            node_link,
            dst,
            timeout,
            list(base_messages),
            tool_ctx,
            think=True,
            telegram_chat_id=telegram_chat_id,
            log_chat_id=chat_id,
        )

    # Узнать заранее, не спит ли модель (idle-таймер llm/service.py) — если
    # да, предупредить о прогреве СРАЗУ, а не оставлять пользователя молча
    # ждать до request_timeout_s без всякой обратной связи. Узел при этом
    # доступен (просто отвечает не сразу) — это не сценарий wake ниже.
    # Короткий таймаут (см. _PRESENCE_CHECK_TIMEOUT_S) — это только быстрая
    # проверка, не повод ждать так же долго, как за настоящим ответом.
    steps_shown = False
    asleep_warmup = False
    known_unavailable = False
    try:
        state = await asyncio.wait_for(node_link.get_state(dst=dst), _PRESENCE_CHECK_TIMEOUT_S)
    except (ServiceUnavailableError, ProtoError, TimeoutError):
        state = None
        known_unavailable = True  # презумпция: раз даже get_state не достучался — недоступна
    if state is not None and state.get("asleep"):
        await message.answer(STEPS_TEXT)
        steps_shown = True
        asleep_warmup = True

    if not known_unavailable:
        try:
            return await _ask()
        except ServiceUnavailableError:
            pass
        except ProtoError as exc:
            if not _is_unavailable(exc):
                # Узел был доступен и мы знали, что модель спит (прогрев) —
                # если именно прогрев и не уложился, это не «внутренняя
                # ошибка» в глазах пользователя, а прямое продолжение
                # «шагов»: Альбегт, а не голое извинение Альфреда. Если же
                # прогрев не при чём (просто дал сбой сам chat-запрос,
                # см. ALBERT_HICCUP) — тоже персонаж, а не голая техника.
                await message.answer(ALBERT_ASLEEP if asleep_warmup else ALBERT_HICCUP)
                await notify_admins(
                    book, notifier, f"⚠️ /ai (chat={chat_id}): {exc.code} — {exc.message}"
                )
                return None

    # --- недоступна: шаги (если ещё не показали) -> молчаливый wake -> poll
    # до WAKE_POLL_TIMEOUT_S -> Агнольд/Альбегт ---
    if not steps_shown:
        await message.answer(STEPS_TEXT)
    outcome = await wake_swarm_node_core(node_link, store, LLM_NODE)
    became_available = outcome.ok and await swarm_view.wait_for_service(
        node_link, LLM_NODE, LLM_SERVICE, WAKE_POLL_TIMEOUT_S, WAKE_POLL_INTERVAL_S
    )
    if not became_available:
        await message.answer(ALBERT_UNAVAILABLE)
        return None

    await message.answer(ARNOLD_WAKING)
    # Второе «шаги»: машина проснулась (шаги были Агнольда), но контейнер с
    # моделью — отдельное, тоже не гарантированное ожидание. Если оно
    # провалится — это уже шаги Альбегта (см. ALBERT_ASLEEP ниже), не
    # переиспользуем первое сообщение, чтобы сюжетно оба провала были у
    # разных персонажей, а успех — молча «оказывается, шёл Агнольд».
    await message.answer(STEPS_TEXT)
    try:
        return await _ask()
    except ServiceUnavailableError:
        await message.answer(ALBERT_UNAVAILABLE)
        return None
    except ProtoError as exc:
        if _is_unavailable(exc):
            await message.answer(ALBERT_UNAVAILABLE)
        else:
            await message.answer(ALBERT_ASLEEP)
            await notify_admins(
                book, notifier, f"⚠️ /ai (chat={chat_id}, после wake): {exc.code} — {exc.message}"
            )
        return None
