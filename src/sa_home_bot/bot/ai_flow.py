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
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.types import Message, User

from sa_home_bot import wake_core
from sa_home_bot.bot import swarm_view
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.lifecycle import notify_tool_call
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import PersonConfig, Settings
from sa_home_bot.db.store import Store
from sa_home_bot.llm.prompt import ROUTE_OK, THINK_MARKER
from sa_home_bot.llm_chat import run_chat_loop
from sa_home_bot.proto.messages import (
    ERR_UNAVAILABLE,
    ERR_UNKNOWN_DST,
    Address,
    ProtoError,
)
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import WILDCARD
from sa_home_bot.wake_core import wake_swarm_node_core

log = logging.getLogger(__name__)

LLM_NODE = "winpc"
LLM_SERVICE = "llm"

# Личное место жительства Альфреда (не дома семьи — своё, персонажное; см.
# персонажный промпт, живая находка 2026-07-25) — Бран, Трансильвания,
# Румыния (тот самый, у замка Дракулы). Часовой пояс — детерминированно в
# коде, той же логикой, что и время собеседника (_known_person_note): не
# поручаем модели арифметику с часовыми поясами. Город для get_weather —
# отдельно, промптом персонажа (см. llm-prompt.toml, вне репозитория).
ALFRED_TIMEZONE = "Europe/Bucharest"

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
ALBERT_HICCUP = "<b>Альбегт:</b> Прошу прощения, Альфред на секунду отвлёкся — повторите, сэр/мадам"
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
# Отложенная задача (тул remind, служба tasks) сработала, но Альфреда так и
# не нашли — не отсюда, а из bot/node_events.py по событию task_result
# (ok=False). Решение пользователя 2026-07-27: сюда доходит только после
# того, как служба будила цель заранее (PREWAKE_LEAD_S) И повторяла попытки
# ещё FIRE_GRACE_S после срока — то есть это уже настоящая неудача, а не
# «просто не успели прогреть» (см. tasks/service.py).
ALBERT_TASK_MISSED = (
    "<b>Альбегт:</b> Прошу прощения, не удалось найти Альфреда, чтобы он сделал, что обещал."
)


class ActiveAiChats:
    """chat_id → asyncio.Task с прямо сейчас идущим /ai-запросом.

    Живая находка 2026-07-24 (первый заход): раньше RESTART_TEXT слался
    только при останове службы llm (winpc) — но останов самого БОТА
    (alfred, любой деплой/restart_node) с тем же успехом обрывает запрос на
    середине: `bot.session.close()` в _shutdown() закрывает HTTP-коннектор,
    и уже ушедший на LLM запрос падает с TelegramNetworkError/"Connector is
    closed" прямо в лицо пользователю. Раньше это было маловероятно (ответ
    занимал секунды), но v0.35.0 включил think_chat (~30-40с на раунд) —
    окно для такой коллизии выросло на порядок.

    Живая находка 2026-07-24 (второй заход, живой баг на проде): версия
    выше только УВЕДОМЛЯЛА хендлер (хранила голый set[int]), не отменяя
    саму задачу — а aiogram диспетчеризует каждый апдейт своей asyncio-
    задачей, не дожидаясь её в `dp.stop_polling()`. Оборвавшийся хендлер
    продолжал жить как осиротевшая задача и, когда `_shutdown()` рвал
    node_link ДО рассылки RESTART_TEXT, сам падал на попытке дозвониться
    и слал голое "Прошу прощения, что-то пошло не так" — то до RESTART_
    TEXT, то после, то и другое разом (гонка, не гарантия). Теперь здесь
    хранится сама asyncio.Task — bot/app.py::_shutdown() рассылает
    RESTART_TEXT и АКТИВНО отменяет задачу (task.cancel()) РАНЬШЕ, чем
    рвёт связи со службами: CancelledError — не Exception, try/finally в
    bot/handlers/ai.py::_ask_and_reply корректно снимет регистрацию и
    выйдет молча, минуя `except Exception` и голый "что-то пошло не так"."""

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


# Сценарные константы presence/wake — общие с службой tasks, живут в
# wake_core.py (живая находка 2026-07-27: пока их было две копии, правку
# 2b8fae7 пришлось делать в обоих файлах, и они всё равно разъезжались —
# WAKE_POLL_TIMEOUT_S здесь и там уже значил разное). Здесь только
# реэкспорт под привычными именами, чтобы не трогать тесты и вызовы.
WAKE_POLL_TIMEOUT_S = wake_core.WAKE_POLL_TIMEOUT_S
WAKE_POLL_INTERVAL_S = wake_core.WAKE_POLL_INTERVAL_S
_PRESENCE_CHECK_TIMEOUT_S = wake_core.PRESENCE_TIMEOUT_S

# Вариативное рассуждение (LLM_INTEGRATION_PLAN.md §7, живая находка
# 2026-07-24): включать think=true на КАЖДЫЙ запрос надёжно для расчётов,
# но раздувает даже "привет" до 30-40с. Вместо этого — сначала лёгкий
# router-проход (think=false, ROUTER_SYSTEM_PROMPT без персонажа), который
# решает, нужен ли think и/или тул, и только потом один персонажный проход
# уже с известным think — без гадания через маркер в тексте персонажа.
#
# Живая находка 2026-07-25: раньше (до этой правки) решение "думать ли"
# принималось В ТОМ ЖЕ вызове, что и сам ответ персонажа (SYSTEM_PROMPT +
# отдельное system-сообщение _TRIAGE_INSTRUCTION поверх него) — модель на
# фоне 12 конкурирующих правил персонажа (тон, анти-повтор, конфликты,
# внешность и т.д.) регулярно проваливала именно триаж: на головоломку про
# кружку с запаянным верхом ответила с ходу и неверно, хотя нужный текст
# (_SPATIAL_REASONING в llm/prompt.py) физически был в контексте — само
# решение "мне нужно подумать" не было принято. Решение пользователя:
# вынести триаж в отдельный вызов с маленьким промптом без персонажа (см.
# llm/prompt.py::ROUTER_SYSTEM_PROMPT) — роутер ничем не отвлекается от
# самой задачи классификации. Дороже (гарантированный второй вызов модели
# на КАЖДОЕ сообщение, не только на сложные — явное решение пользователя,
# готового к этому ради надёжности), но проверено дешевле, чем always-on
# think=true (роутер сам всегда think=false и с коротким ответом).
#
# THINK_MARKER/ROUTE_OK (использованы ниже) живут в llm/prompt.py (не
# здесь, см. импорт в шапке файла) — это часть протокола между текстом
# ROUTER_SYSTEM_PROMPT и сравнением ниже, уместнее рядом с промптом,
# который их порождает.

# Показывается перед персонажным проходом, если роутер попросил think —
# пользователь явно попросил лаконичную реплику в духе персонажа, не
# техническое "думаю...".
THINKING_TEXT = "<i>На лице Альфреда проступает задумчивость</i>"


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


def _find_known_person(settings: Settings, user: User | None) -> PersonConfig | None:
    """Сопоставить отправителя с settings.people (config.py::PersonConfig).

    По username в первую очередь (у большинства он есть) — по telegram_id
    как фоллбэк, для тех, чей username не был известен человеку, который
    вписывал config.toml (у них там telegram_username = "")."""
    if user is None:
        return None
    username = (user.username or "").lower()
    for person in settings.people:
        if username and person.telegram_username.lower() == username:
            return person
    for person in settings.people:
        if person.telegram_id and person.telegram_id == user.id:
            return person
    return None


def _person_age(person: PersonConfig, today: date) -> int | None:
    """Точный возраст на сегодня — детерминированно в коде, не поручаем
    модели вычитание дат (та же логика, что и с часовыми поясами: живая
    находка про то, что маленькие модели систематически ошибаются в
    арифметике с датами)."""
    if not person.birth_date:
        return None
    try:
        birth = date.fromisoformat(person.birth_date)
    except ValueError:
        return None
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


# (субъект "он/она", предложный "у него/у неё", притяжательный "его/её" —
# для "него"/"неё" совпадает с предложным только у мужского рода, поэтому
# три отдельных формы, не выводим одну из другой).
_PRONOUNS = {"m": ("он", "него", "его"), "f": ("она", "неё", "её")}


def _known_person_note(person: PersonConfig) -> str | None:
    """Заметка о собеседнике, которого узнали по settings.people: точное
    местное время в его/её городе (не пояс сервера — верхняя строка) и
    точный возраст. Правила о ТОМ, как это использовать (обращение по
    нику, такт с возрастом дам, поправка тона на разницу в возрасте с
    самим Альфредом, родственные связи) — в характере персонажа
    (llm-prompt.toml, не дублируем здесь построчно)."""
    subject, prepositional, possessive = _PRONOUNS[person.gender]
    parts = [f"Ты знаешь этого собеседника — это {person.full_name}."]

    if person.timezone:
        try:
            local_now = datetime.now(ZoneInfo(person.timezone))
        except ZoneInfoNotFoundError:
            local_now = None
        if local_now is not None:
            city = f" в {person.city}" if person.city else ""
            parts.append(
                f"У {prepositional}{city} сейчас "
                f"{local_now:%H:%M} ({ai_tools.WEEKDAYS_RU[local_now.weekday()]}) — "
                f"это {possessive} часовой пояс, а не пояс сервера выше; называй "
                f"именно это время, если он{'а' if person.gender == 'f' else ''} "
                f"спросит который час или это иначе уместно."
            )

    age = _person_age(person, date.today())
    if age is not None:
        parts.append(f"Точный возраст {subject} сейчас: {age} лет.")

    return " ".join(parts)


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


async def _build_context_note(
    message: Message, store: Store, dialogue_id: int, settings: Settings | None = None
) -> str:
    """Служебная заметка для модели (не для пользователя): точное время
    сейчас (§8.1 плана — маленькие локальные модели плохо знают "сейчас", а
    время нужно почти на каждый запрос, отдельного тула для этого не
    заводим) — плюс отдельно личное время самого Альфреда (ALFRED_TIMEZONE,
    2026-07-25), кто сейчас пишет — включая местное время/возраст, если это
    известный собеседник из settings.people (см. _known_person_note), кто
    начал этот тред, кто ещё обращался к Альфреду в этом чате, и что за
    сообщение цитируют/на что отвечают (см. _reply_context_lines).

    Пункты про тред/участников — только для групп: в личке собеседник
    всегда один и тот же, уточнять нечего. Строится заново на каждый запрос
    (не хранится в ai_turns) — участники чата могут появляться по ходу дела,
    а время не имеет смысла хранить вовсе. settings — опциональный параметр
    (не Optional по смыслу, а ради существующих тестов/вызовов, где список
    известных людей не нужен): без него просто пропускаем сопоставление."""
    # Живой баг 2026-07-24: голая строка времени без указания пояса —
    # модель на вопрос "это в каком часовом поясе" отвечала наугад и
    # противоречила себе же. Явный UTC-оффсет плюс запрет досчитывать чужой
    # пояс (часовой пояс собеседника Telegram боту не передаёт — только
    # свой явный вопрос про "у нас" бот и знает; для других мест — см.
    # тул get_time в bot/tools.py, туда же вынесена таблица дней недели).
    now_local = datetime.now().astimezone()
    offset = now_local.strftime("%z")
    lines = [
        f"Точное время сейчас (пояс сервера, UTC{offset[:3]}:{offset[3:]}): "
        f"{now_local:%Y-%m-%d %H:%M} ({ai_tools.WEEKDAYS_RU[now_local.weekday()]}). "
        "Это ПОЯС СЕРВЕРА, не обязательно пояс собеседника — если тебя "
        "спросят, в каком поясе сейчас сам собеседник, и ниже нет отдельной "
        "строки про его/её местное время — честно скажи, что не знаешь, не "
        "додумывай."
    ]
    alfred_now = datetime.now(ZoneInfo(ALFRED_TIMEZONE))
    lines.append(
        f"Отдельно: ТВОЁ (Альфреда) личное время сейчас — там, где ты живёшь: "
        f"{alfred_now:%H:%M} ({ai_tools.WEEKDAYS_RU[alfred_now.weekday()]}). Своё "
        "место жительства ты не скрываешь, если прямо спросят, где ты живёшь или "
        "который час у ТЕБЯ (не у собеседника и не пояс сервера выше) — называй "
        "именно это время; про сам город — см. свой характер."
    )

    sender_name = display_name(message.from_user)
    is_group = message.chat is not None and message.chat.type in ("group", "supergroup")
    if sender_name is not None:
        lines.append(f"Сейчас с тобой говорит: {sender_name}.")
    known_person = _find_known_person(settings, message.from_user) if settings else None
    if known_person is not None:
        note = _known_person_note(known_person)
        if note:
            lines.append(note)
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
    context_note = await _build_context_note(message, store, dialogue_id, settings)

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
                *history[:-1],
                {"role": "system", "content": context_note},
                history[-1],
            ]
        else:
            base_messages = list(history)
        tool_ctx = ai_tools.ToolContext(
            chat_id=message.chat.id if message.chat else None,
            dialogue_id=dialogue_id,
            trigger_message_id=message.message_id if message.chat else None,
            settings=settings,
            node_link=node_link,
            # Права собеседника ограничивают комплект тулов: Альфред не должен
            # быть обходным путём вокруг подписок (bot/tools.py::tools_for).
            subscription=book.for_chat(message.chat.id) if message.chat else None,
        )
        telegram_chat_id = message.chat.id if message.chat is not None else None

        async def _record_tool_call(name: str, call_args: dict[str, Any], result: str) -> None:
            # Живая находка 2026-07-24: "шиза" с часовыми поясами была
            # недиагностируема — ai_turns хранит только финальный текст
            # ответа, не видно, звала ли модель тул вообще и что тот
            # вернул (см. schema.sql::ai_tool_calls). Пишет только живой
            # /ai — у него есть Store, у службы tasks его нет вовсе.
            await notify_tool_call(book, notifier, name)
            if telegram_chat_id is None:
                return
            await store.record_tool_call(
                chat_id=telegram_chat_id,
                dialogue_id=dialogue_id,
                trigger_message_id=message.message_id,
                tool_name=name,
                args=call_args,
                result=result,
                at=datetime.now(tz=UTC),
            )

        # Вариативное рассуждение (см. комментарий выше про THINK_MARKER):
        # сначала лёгкий router-проход (без персонажа, ROUTER_SYSTEM_PROMPT),
        # который решает think и/или вызывает нужный тул — router_messages
        # мутируется run_chat_loop (дописываются tool_calls/результаты) и
        # этот же список идёт дальше в персонажный проход, чтобы Альфред
        # видел, что именно вернул тул, а не гадал заново.
        router_messages = list(base_messages)
        route_decision = await run_chat_loop(
            node_link,
            dst,
            timeout,
            router_messages,
            tool_ctx,
            think=False,
            telegram_chat_id=telegram_chat_id,
            log_chat_id=chat_id,
            on_tool_call=_record_tool_call,
            role="router",
        )
        needs_think = THINK_MARKER in route_decision
        log.info(
            "ai_flow: router -> %s (chat=%s)",
            THINK_MARKER if needs_think else ROUTE_OK,
            chat_id,
        )

        if needs_think:
            await message.answer(THINKING_TEXT)
        # Живая находка 2026-07-25 (полный круг): сначала думали, что
        # think=None ("не слать флаг вообще") даст qwen3.5/3.6 самим решать
        # адаптивно — оказалось, без явного флага модель генерирует от 2 до
        # 1400+ невидимых токенов "размышления" совершенно непредсказуемо
        # (подтверждено логами Ollama на живых запросах). Отдельно
        # выяснилось, что настоящая причина прежних галлюцинаций была не в
        # think вовсе, а в нехватке памяти WSL2 (своп при инференсе, см.
        # память winpc-wsl-docker-instability) — почин. Прямой тест
        # ``think: false`` на исправленной среде подтвердил: параметр
        # РЕАЛЬНО работает (ответ без единого токена thinking) — решение
        # пользователя: вернуть явный булев think, как и было, роутер
        # решает за персонажа предсказуемо, а не полагаться на то, умеет
        # ли рендерер сам решать.
        return await run_chat_loop(
            node_link,
            dst,
            timeout,
            router_messages,
            tool_ctx,
            think=needs_think,
            telegram_chat_id=telegram_chat_id,
            log_chat_id=chat_id,
            on_tool_call=_record_tool_call,
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
