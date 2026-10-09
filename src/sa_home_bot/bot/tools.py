"""Инструменты (tool-calling) для диалога /ai — LLM_INTEGRATION_PLAN.md §7-8.

Каждый тул — узкая функция в явном реестре TOOLS, не общий прокси
на произвольное действие роя (§7.2 плана — общий прокси был бы дырой в
правах: модель дозвонилась бы куда угодно). Декларация тула — формат
OpenAI function-calling, который Ollama понимает нативно для
tool-calling-моделей (qwen3 в их числе).

Комплект тулов не одинаков для всех: ``tools_for(subscription)`` отдаёт
только то, на что у собеседника есть права (см. блок «права тулов» ниже) —
Альфред не может больше, чем пользователь, который с ним говорит.

Погода, конвертер валют и калькулятор не ходят по протоколу роя вообще —
это не системные операции конкретной ноды (как apps/monitor), а либо
чистый расчёт, либо публичный API без ключа/состояния, одинаково доступный
с любой ноды. Выполняются прямо здесь (см. §8.4 плана — решение упростить
относительно первоначального черновика с отдельной службой "net").
Арифметику конвертера (сумма * курс) делает сам тул на Python, не второй
проход через тул calc — для одного умножения гонять его ещё раз через
модель не даёт выгоды в точности, только лишний круг.

``remind`` — единственный ПИШУЩИЙ тул, ходящий по протоколу роя мимо
служб-адаптеров (в службу tasks, см. sa_home_bot.tasks) — ставит
отложенную задачу "спросить нейронку ещё
раз в момент X" (§8.5 плана, генерализовано 2026-07-24: раньше писал
готовый текст константным напоминанием прямо в БД бота, теперь сама
доставка — новый живой ответ модели, см. sa_home_bot.tasks.service).
Никакого доступа "в систему" — только создание такой задачи.

Этот модуль сознательно не зависит от aiogram — его импортирует не только
бот, но и служба tasks (см. докстринг ToolContext), которой Telegram не
нужен вовсе.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import copy
import itertools
import json
import logging
import math
import operator
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from html import escape
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sa_home_bot import astro, wake_core
from sa_home_bot.bot import (
    commands,
    image_tools,
    invites,
    people_cards,
    recipients,
    voice_mode,
    vpn_nodes,
)
from sa_home_bot.bot.interactives import cabinet as interactive_cabinet
from sa_home_bot.bot.interactives import radio as interactive_radio
from sa_home_bot.bot.monitor_state import parse_disk_summary, parse_health_state
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings, reminder_reason
from sa_home_bot.graph_memory import protocol as graph_memory_protocol
from sa_home_bot.graph_memory.people import ClaimError, normalize_claim
from sa_home_bot.llm.prompt import wrap_system_directive
from sa_home_bot.memory import protocol as memory_protocol
from sa_home_bot.net import protocol as net_protocol
from sa_home_bot.node.kind import traits_for
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.tasks import protocol as task_protocol
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

# Дни недели по-русски — используется и здесь (tool_get_time), и в
# bot/ai_flow.py::_build_context_note (импортируется оттуда как
# ai_tools.WEEKDAYS_RU, обратного импорта нет — ai_flow и так уже
# импортирует этот модуль).
WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

# Куда стрелять llm.chat для отложенных задач, создаваемых тулом remind —
# тот же узел/служба, что и живой /ai (bot/ai_flow.py::LLM_NODE/LLM_SERVICE).
# Продублировано здесь как литерал, а не импортировано оттуда: ai_flow.py
# сам импортирует этот модуль (bot.tools) — обратный импорт был бы циклом.
LLM_NODE = "mycraft"
LLM_SERVICE = "llm"
# Литерал, не импорт из llm/service.py — тот же приём, что и выше: этот
# модуль намеренно не тянет тяжёлую LLM-службу (Ollama/WSL-обвязку), только
# клиентский ServiceLink (см. докстринг модуля про службу tasks).
ACTION_LOOK_AT_PHOTO = "look_at_photo"

# Литералы, не импорт из node/service.py (та же причина, что у
# wake_core.py::_CHECK_UPDATE_ACTION — тот модуль тяжёлый, тянет
# супервизор/пиров/discovery, а тулу remind нужна только пара строк-имён
# событий для after_event).
EVENT_UPDATE_FINISHED = "update_finished"
EVENT_RESTART_APPLIED = "restart_applied"

# Страховочный дедлайн для remind(after_event=...), если событие так и не
# придёт (нода не поднялась) — без него задача ждала бы вечно, что хуже
# честного «не подтвердилось» (тот же принцип, что у FIRE_GRACE_S в
# tasks/service.py: лучше сработать с опозданием/сдаться, чем не
# сработать никогда). Модель это число не указывает — иначе легко
# промахнётся: сама нода не знает, сколько реально займёт её рестарт.
RESTART_EVENT_FALLBACK_S = 300.0


DISMISS_MODEL = "model"
DISMISS_SLEEP = "sleep"
DISMISS_OFF = "off"


@dataclass
class DismissalBox:
    """Изменяемая ячейка «Альфреда распустили» — заполняется тулом dismiss,
    исполняется ПОСЛЕ того, как прощание уехало в чат.

    Тул не может выключить машину прямо в обработчике: ответ модели в этот
    момент ещё не сгенерирован (тул зовётся раньше персонажного прохода),
    и погашенная Ollama оборвала бы диалог на полуслове — пользователь
    получил бы «Альфред отвлёкся» вместо прощания. Поэтому тул только
    записывает намерение, а исполняет его bot/handlers/ai.py уже после
    отправки ответа (bot/ai_flow.py::perform_dismissal).

    ``None`` в ``ToolContext.dismissal`` — исполнить намерение некому
    (служба tasks, тесты): тул честно говорит, что сейчас не умеет.
    """

    mode: str | None = None


@dataclass
class ToolContext:
    """``history`` — сообщения, которые ПРЯМО СЕЙЧАС видит модель (та же
    ссылка, что и ``messages`` в llm_chat.run_chat_loop, живая находка
    2026-07-24) — тул remind берёт снимок диалога отсюда, не из БД: у
    службы tasks (второй пользователь этого модуля, см. докстринг файла)
    нет доступа к ai_turns бота, а живому /ai читать БД ради того же самого
    незачем, раз список уже в памяти. ``node_link`` — только remind ходит
    по протоколу (в службу tasks); прочим тулам не нужен.

    ``subscription`` — права собеседника, по ним собирается комплект тулов
    (см. tools_for): Альфред не умеет того, чего не может сам пользователь.
    ``None`` — подписки нет, остаются только тулы без прав (fail-closed).

    ``book``/``notifier``/``store``/``author`` нужны тулам, которые ищут
    получателя и шлют ему личное сообщение (``tell``, ``notify_guest``,
    ``guests_list``): найти получателя среди подписок, отправить ему
    сообщение и записать его как ход диалога, чтобы получатель мог ответить
    реплаем. У службы tasks настоящего ``notifier``/``store`` нет (см.
    докстринг файла tasks/service.py) — только ``book`` (своя
    SubscriptionBook, тот же конфиг) и ``emit`` (см. ниже), поэтому доставка
    там идёт через мост-событие, а не напрямую; без него (или у живого /ai,
    где notifier есть напрямую и мост не нужен) — честный отказ, так же, как
    ``dismiss`` без ``dismissal``.
    """

    chat_id: int | None
    dialogue_id: int | None
    trigger_message_id: int | None
    settings: Settings
    node_link: ServiceLink | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    subscription: Subscription | None = None
    dismissal: DismissalBox | None = None
    book: Any | None = None  # SubscriptionBook — не типизируем, чтобы tools не
    # зависели от подписок (они зависят от конфига, а конфиг импортирует ноду)
    notifier: Any | None = None  # bot/notifier.py::Notifier
    store: Any | None = None  # db/store.py::Store
    author: str | None = None  # как зовут того, кто прямо сейчас говорит
    # Реальный Telegram message_thread_id (в отличие от dialogue_id — тот в
    # чате без топика подменяется message_id, что для Bot API не тред и
    # приведёт к 400). None вне топика — тулы должны передавать None, а не
    # dialogue_id, в message_thread_id проактивных notifier.send_*.
    message_thread_id: int | None = None
    # (node, event_type), из-за которого сработала ЭТА задача-продолжение
    # (remind after_event) — None у живого /ai (никто не будил). Живой
    # инцидент 2026-08-05: модель, разбуженная по событию, систематически
    # игнорировала прямой текстовый запрет "не зови remind на то же самое
    # событие снова" и заново ставила remind на (node, event_type), из-за
    # которого её только что разбудили — бесконечный цикл самопереноса без
    # единого реального действия. Раз словесный запрет не работает, тул
    # remind сверяет сам и жёстко отказывает, см. tool_remind.
    woken_by: tuple[str, str] | None = None
    # Мост доставки для службы tasks (self-scheduled remind, живая находка
    # 2026-08-06): там нет ни notifier, ни store (см. поля выше), но есть
    # SubscriptionBook (передаётся сюда как ``book``) и своя очередь событий
    # роя. ``emit`` — тот же EventEmitter, что у TasksService._emit
    # (tasks/service.py) — позволяет tell/notify_guest попросить БОТА
    # (единственного, у кого есть настоящий Notifier) реально отправить
    # сообщение через tasks.protocol.EVENT_DELIVER_MESSAGE (см.
    # bot/node_events.py::_handle_deliver_message), вместо честного отказа.
    # None у живого /ai — там доставка идёт напрямую через notifier, мост не
    # нужен.
    emit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
    # Полные тексты тул-результатов, укороченных в контексте модели (см.
    # llm_chat.py::_inline_or_cache) — ключ - тот самый id, что уходит
    # модели в пометке «сокращено». Живёт только в памяти ЭТОГО прохода
    # run_chat_loop (один вызов request_alfred/tasks-срабатывание), не в БД:
    # recall_tool_result нужен лишь для того, чтобы модель могла дозвать
    # детали в том же раунде tool-calling, где результат был обрезан, — тем
    # же способом, каким remind() читает ctx.history (см. его докстринг).
    tool_result_cache: dict[str, str] = field(default_factory=dict)
    # Формы подтверждения (Этап 45, bot/pending_actions.py::PendingActions) —
    # только у живого /ai; у службы tasks формы показать некому.
    pending_actions: Any | None = None
    # Интерактивы (Этап 47, bot/interactives/engine.py::Interactives) —
    # только у живого /ai. ``user_id`` — кто говорит (в общем чате это не
    # chat_id), ``is_private`` — сцены идут только в личке.
    interactives: Any | None = None
    user_id: int | None = None
    is_private: bool = False
    # Тул закончил ход сам — реплики Альфреда сейчас не будет (take_photo:
    # описание придёт вместе со снимком). run_chat_loop прекращает раунды,
    # ai_flow.request_alfred отдаёт SILENT_REPLY.
    end_turn: bool = False
    # На какое сообщение ответил гость (Telegram reply) — look_at_photo:
    # ответ на картинку Альфреда — значит, о ней и речь.
    reply_to_message_id: int | None = None


ToolHandler = Callable[["ToolContext", dict[str, Any]], Awaitable[str]]


# --- права тулов: Альфред не может больше, чем его собеседник ---
#
# Исходное правило §7.2 плана ("реестр один и тот же для всех, у кого есть
# chat@llm") держалось на том, что тулы ничего не знали про систему: калькулятор
# и погода одинаковы для всех. С появлением swarm_status это перестало быть
# правдой — иначе /ai стал бы обходным путём вокруг подписок: пользователь, у
# которого нет /status, спрашивал бы у Альфреда и получал то же самое.
#
# Требование пользователя (2026-07-27) сформулировано так: Альфред не
# ОТКАЗЫВАЕТ, а именно НЕ УМЕЕТ. Поэтому фильтрация — на уровне деклараций
# (tools_for), а не проверкой внутри обработчика: тула, на который нет прав,
# модель не видит вовсе и не обещает того, чего не сделает.


@dataclass(frozen=True)
class CommandRight:
    """Право уровня команды бота — то же, чем гейтится сама команда.

    ``/status`` проверяется через ``allows_command(commands.STATUS.name)``
    (bot/handlers/node_links.py) — тул, отдающий те же данные, требует ровно
    того же права, а не своего собственного.
    """

    name: str

    def granted(self, subscription: Subscription) -> bool:
        return subscription.allows_command(self.name)


@dataclass(frozen=True)
class ActionRight:
    """Право на действие службы в форме ``действие@служба`` (``list@torrents``).

    Групповые формы (``*@torrents``, голый ``*``) уже поддержаны в
    Subscription.allows_action — админу ничего дописывать не нужно.
    """

    action: str
    service: str

    def granted(self, subscription: Subscription) -> bool:
        return subscription.allows_action(self.action, self.service)


ToolRight = CommandRight | ActionRight


@dataclass(frozen=True)
class VariantRights:
    """Права на ОТДЕЛЬНЫЕ значения enum-параметра одного тула.

    swarm_status — не один доступ, а четыре разных (ноды, здоровье, диски,
    торренты), и права на них у пользователя могут отличаться. Заводить под
    каждое отдельный тул значило бы четырежды повторить описание и раздуть
    контекст модели, поэтому вместо этого из ``enum`` вырезаются недоступные
    значения. Если не осталось ни одного — тул не объявляется целиком.
    """

    param: str
    rights: tuple[tuple[str, ToolRight], ...]

    def allowed_values(self, subscription: Subscription) -> list[str]:
        return [value for value, right in self.rights if right.granted(subscription)]


@dataclass(frozen=True)
class ToolSpec:
    """Тул целиком в одном месте: обработчик, декларация и требуемое право.

    До этого обработчики и декларации жили в двух параллельных словарях, и
    добавление прав размножило бы их до трёх — тот же повод завести единое
    описание, что и у ServiceSpec в services/registry.py.

    ``requires=None`` — тул прав не требует: чистый расчёт (calc) или
    публичный API без ключа (погода, курсы валют), ничего про систему не
    раскрывает и доступа к ней не даёт.
    """

    name: str
    handler: ToolHandler
    declaration: dict[str, Any]
    requires: ToolRight | None = None
    variants: VariantRights | None = None

    def declaration_for(self, subscription: Subscription) -> dict[str, Any] | None:
        """Декларация под конкретную подписку; None — тул недоступен."""
        if self.requires is not None and not self.requires.granted(subscription):
            return None
        if self.variants is None:
            return self.declaration
        allowed = self.variants.allowed_values(subscription)
        if not allowed:
            return None
        declaration = copy.deepcopy(self.declaration)
        params = declaration["function"]["parameters"]["properties"]
        params[self.variants.param]["enum"] = allowed
        return declaration


@dataclass(frozen=True)
class ToolKit:
    """Что модели дают на этот конкретный диалог (см. tools_for)."""

    declarations: list[dict[str, Any]]
    handlers: dict[str, ToolHandler]


def tools_for(subscription: Subscription | None) -> ToolKit:
    """Комплект тулов под права собеседника.

    ``subscription is None`` — чат без подписки вовсе: остаются только тулы
    без ``requires`` (fail-closed). Такое возможно у службы tasks, если
    подписку удалили из конфига между постановкой задачи и её срабатыванием.

    Побочная выгода фильтрации: декларации целиком уезжают в контекст модели
    на КАЖДОМ раунде (см. config.py про их размер) — у собеседника с урезанными
    правами их просто меньше.
    """
    declarations: list[dict[str, Any]] = []
    handlers: dict[str, ToolHandler] = {}
    for spec in TOOLS:
        if subscription is None:
            if spec.requires is not None or spec.variants is not None:
                continue
            declaration: dict[str, Any] | None = spec.declaration
        else:
            declaration = spec.declaration_for(subscription)
        if declaration is None:
            continue
        declarations.append(declaration)
        # Исполнение фильтруется тем же решением, что и объявление: модель
        # может выдумать имя тула, которого ей не давали, — тогда сработает
        # ветка "неизвестный инструмент" в llm_chat.run_chat_loop.
        handlers[spec.name] = spec.handler
    return ToolKit(declarations=declarations, handlers=handlers)


# --- calc: без сети и без роя, ast с белым списком узлов (не eval()) ---

_ALLOWED_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
# Живая находка 2026-07-24: реальная задача (площадь цилиндра, формула с π)
# показала, что без именованных констант модель вынуждена подставлять
# приближение "3.14159" сама (или вообще не звать тул) — добавлены pi/e как
# единственные разрешённые "переменные", не произвольные имена.
_ALLOWED_NAMES: dict[str, float] = {"pi": math.pi, "e": math.e}
_MAX_POW_EXPONENT = 1000  # защита от x**(огромное число) — не таймаут, а память/CPU
_ROUND_NDIGITS = 6  # "32.98672286269283" читается хуже, чем "32.986723"


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.Name) and node.id in _ALLOWED_NAMES:
        return _ALLOWED_NAMES[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXPONENT:
            raise ValueError("слишком большая степень")
        return _ALLOWED_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(
        "недопустимое выражение (разрешены только числа, pi, e и + - * / ** или ^ ())"
    )


async def tool_calc(ctx: ToolContext, args: dict[str, Any]) -> str:
    expr = args.get("expression")
    if not isinstance(expr, str) or not expr.strip():
        return "ошибка: пустое выражение"
    # Живая находка 2026-07-24: модель пишет степень как в математике,
    # "1.5^2", не как в Python "1.5**2". У "^" в Python СОВСЕМ другой
    # приоритет операций (ниже "+", а не выше "*", как у степени) — трактовать
    # AST-узел BitXor напрямую как "**" ломает любое выражение сложнее
    # одного "a^b" (проверено: "2*pi*1.5^2+2*pi*1.5*2" вычислялось неверно).
    # Текстовая замена ДО парсинга — "^" в разрешённых выражениях больше
    # никогда и ни для чего другого не встречается, поэтому безопасна.
    expr = expr.replace("^", "**")
    try:
        tree = ast.parse(expr, mode="eval")
        value = _safe_eval(tree.body)
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError, OverflowError) as exc:
        return f"ошибка вычисления: {exc}"
    if isinstance(value, float):
        if value.is_integer():
            value = int(value)
        else:
            # "32.98672286269283" — избыточная точность, которую персонаж
            # никогда бы не произнёс; округляем, не обрубая до неточности.
            value = round(value, _ROUND_NDIGITS)
    return str(value)


# --- HTTP-обвязка, общая для get_weather и convert_currency ниже: оба —
# публичные API без ключа/состояния, вызывает сам бот-процесс (не системные
# операции конкретной ноды, как apps/monitor — одинаково доступны с любой
# ноды, отдельная служба под них не нужна, см. §8.4 плана). ---

_HTTP_TIMEOUT_S = 10.0


def _get_json_sync(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — фиксированные публичные host'ы
        return json.loads(resp.read())


# --- get_weather ---
#
# Координаты города не просит у пользователя/модели напрямую — небольшая
# локальная модель не гарантированно точна в географических фактах (может
# перепутать широту/долготу или город). Вместо этого город из конфига
# ([weather].city) резолвится через геокодинг-API того же провайдера
# (Open-Meteo, без ключа, тот же трюк, что и сам прогноз) — детерминированно,
# не полагаясь на "память" модели. Результат кэшируется на время жизни
# процесса (_GEOCODE_CACHE) — город из конфига не меняется на лету (конфиг
# читается один раз при старте), незачем резолвить его на каждый запрос.

_GEOCODE_CACHE: dict[str, tuple[float, float, str]] = {}


# Геокодер Open-Meteo кириллицу ищет плохо (2026-10-09: «Мурманск» → посёлок
# Мурманский, «Бран» → деревня во Франции, румынского Брана нет вовсе), а
# латиницей находит. Поэтому ищем и как написано, и транслитом, и выбираем
# сами: точное совпадение имени, страна из «Город, Страна», затем крупнее.
_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
)  # fmt: skip
_FEATURE_RANK = {"PPLC": 4, "PPLA": 3, "PPLA2": 2, "PPLA3": 1}


def _geocode_score(item: dict[str, Any], names: set[str], country: str) -> tuple:
    own = str(item.get("name", "")).lower()
    alt = {own, own.translate(_TRANSLIT)}
    country_ok = not country or country in str(item.get("country", "")).lower()
    return (
        country_ok,
        bool(alt & names),
        str(item.get("feature_code", "")).startswith("PPL"),
        _FEATURE_RANK.get(str(item.get("feature_code")), 0),
        item.get("population") or 0,
    )


async def _resolve_city(city: str) -> tuple[float, float, str] | None:
    """(latitude, longitude, отображаемое название) или None — город не
    найден геокодером, либо сам геокодер недоступен."""
    key = city.strip().lower()
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    raw_name, _, country = city.strip().partition(",")
    name, country = raw_name.strip().lower(), country.strip().lower()
    queries = list(dict.fromkeys([raw_name.strip(), raw_name.strip().lower().translate(_TRANSLIT)]))
    candidates: list[dict[str, Any]] = []
    for query in queries:
        url = (
            "https://geocoding-api.open-meteo.com/v1/search"
            f"?name={urllib.parse.quote(query)}&count=10&language=ru&format=json"
        )
        try:
            data = await asyncio.to_thread(_get_json_sync, url, _HTTP_TIMEOUT_S)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning("tool_get_weather: геокодирование «%s» не удалось: %s", query, exc)
            continue
        candidates.extend(data.get("results") or [])
    if not candidates:
        return None
    names = {name, name.translate(_TRANSLIT)}
    top = max(candidates, key=lambda item: _geocode_score(item, names, country))
    label = top.get("name", city)
    if top.get("country"):
        label = f"{label}, {top['country']}"
    resolved = (top["latitude"], top["longitude"], label)
    _GEOCODE_CACHE[key] = resolved
    return resolved


# WMO weather code → по-русски: модель сама коды не знает и гадала бы.
_WMO_RU = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "туман с изморозью",
    51: "слабая морось",
    53: "морось",
    55: "сильная морось",
    56: "ледяная морось",
    57: "сильная ледяная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "сильный ледяной дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    77: "снежная крупа",
    80: "небольшой ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза с градом",
}


def _local_zone(data: dict[str, Any]) -> ZoneInfo | timezone:
    """Пояс города из ответа прогноза (``timezone=auto``); незнакомое
    системе имя — хотя бы смещение."""
    name = data.get("timezone")
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone(timedelta(seconds=int(data.get("utc_offset_seconds") or 0)))


def _hhmm(moment: datetime | None, zone: ZoneInfo | timezone) -> str | None:
    return moment.astimezone(zone).strftime("%H:%M") if moment is not None else None


def place_now(lat: float, lon: float, zone: ZoneInfo | timezone, now: datetime) -> dict[str, Any]:
    """Время, время суток, солнце и луна в точке — без сети (``astro``)."""
    local = now.astimezone(zone)
    altitude, azimuth = astro.sun_position(lat, lon, now)
    day = astro.sun_day(lat, lon, local.date())
    moon = astro.moon(now)
    return {
        "local_time": local.strftime("%H:%M"),
        "date": local.strftime("%Y-%m-%d"),
        "weekday": WEEKDAYS_RU[local.weekday()],
        "utc_offset": local.strftime("%z"),
        "time_of_day": astro.LIGHT_RU[astro.light_phase(altitude, azimuth)],
        "sun": {
            "altitude_deg": round(altitude, 1),
            "azimuth_deg": round(azimuth),
            "sunrise": _hhmm(day.sunrise, zone),
            "sunset": _hhmm(day.sunset, zone),
            "solar_noon": _hhmm(day.noon, zone),
        },
        "moon": {
            "phase": moon.phase_ru,
            "illumination_pct": round(moon.illumination * 100),
        },
    }


async def tool_get_weather(ctx: ToolContext, args: dict[str, Any]) -> str:
    # Живой баг 2026-07-24: декларация раньше не принимала город вообще
    # ("узнать погоду ДОМА") — модель на прямой вопрос про другой город
    # честно отказывала, а не молчаливо путала его с домом. args["city"] —
    # необязательный: без него — прежнее поведение (город из конфига).
    # С 2026-10-09 тул — «что сейчас в городе» целиком: к погоде добавлены
    # местное время, время суток, солнце и луна (всё считается локально по
    # координатам и поясу из того же ответа Open-Meteo).
    requested_city = args.get("city")
    city = requested_city.strip() if isinstance(requested_city, str) else ""
    if not city:
        city = ctx.settings.weather.city
        if not city:
            return "погода не настроена — не задан ни город в вопросе, ни город дома в конфиге"
    resolved = await _resolve_city(city)
    if resolved is None:
        return f"не удалось определить координаты города «{city}»"
    lat, lon, label = resolved
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,"
        "wind_gusts_10m,relative_humidity_2m,cloud_cover,precipitation"
        "&daily=temperature_2m_min,temperature_2m_max,precipitation_probability_max"
        "&forecast_days=1&wind_speed_unit=ms&timezone=auto"
    )
    try:
        data = await asyncio.to_thread(_get_json_sync, url, _HTTP_TIMEOUT_S)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("tool_get_weather: %s", exc)
        return "не удалось получить погоду — сервис недоступен, повтори позже"
    current = data.get("current", {})
    daily = data.get("daily", {})

    def today(key: str) -> Any:
        values = daily.get(key)
        return values[0] if isinstance(values, list) and values else None

    code = current.get("weather_code")
    result: dict[str, Any] = {
        "location": label,
        "weather": _WMO_RU.get(code) if isinstance(code, int) else None,
        "temperature_c": current.get("temperature_2m"),
        "feels_like_c": current.get("apparent_temperature"),
        "today_min_c": today("temperature_2m_min"),
        "today_max_c": today("temperature_2m_max"),
        "precipitation_chance_pct": today("precipitation_probability_max"),
        "precipitation_mm": current.get("precipitation"),
        "cloud_cover_pct": current.get("cloud_cover"),
        "humidity_pct": current.get("relative_humidity_2m"),
        "wind_ms": current.get("wind_speed_10m"),
        "wind_gusts_ms": current.get("wind_gusts_10m"),
        "weather_code": code,
    }
    result.update(place_now(lat, lon, _local_zone(data), datetime.now(UTC)))
    return json.dumps(result, ensure_ascii=False)


# --- convert_currency ---
#
# Умножение делает сам тул (обычный Python), не второй раунд через calc —
# для одной операции "сумма * курс" гонять её ещё и через модель незачем,
# только лишний круг генерации без выгоды в точности. Курсы — тоже не из
# "памяти" модели (устаревают за часы-дни), а с открытого API без ключа
# (open.er-api.com — рыночные курсы, ~160 валют, включая RUB/KZT и т.п.),
# кэшируются на _RATES_TTL_S — курсы обновляются на источнике не чаще
# раза в сутки, кэш на час экономит сеть, не портя актуальность на глаз.

_RATES_TTL_S = 3600.0
_RATES_CACHE: dict[str, tuple[float, dict[str, float]]] = {}


async def _get_rates(base: str) -> dict[str, float] | None:
    """Курсы всех валют за 1 единицу ``base``, или None — база не найдена
    сервисом, либо сам сервис недоступен."""
    now = time.monotonic()
    cached = _RATES_CACHE.get(base)
    if cached is not None and now - cached[0] < _RATES_TTL_S:
        return cached[1]
    url = f"https://open.er-api.com/v6/latest/{urllib.parse.quote(base)}"
    try:
        data = await asyncio.to_thread(_get_json_sync, url, _HTTP_TIMEOUT_S)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("tool_convert_currency: %s", exc)
        return None
    if data.get("result") != "success":
        return None
    rates = data.get("rates")
    if not isinstance(rates, dict):
        return None
    _RATES_CACHE[base] = (now, rates)
    return rates


async def tool_convert_currency(ctx: ToolContext, args: dict[str, Any]) -> str:
    amount = args.get("amount")
    from_raw = args.get("from")
    to_raw = args.get("to")
    if not isinstance(amount, int | float):
        return "ошибка: 'amount' должен быть числом"
    if not isinstance(from_raw, str) or not from_raw.strip():
        return "ошибка: не указана исходная валюта (from)"
    if not isinstance(to_raw, str) or not to_raw.strip():
        return "ошибка: не указана целевая валюта (to)"
    from_code = from_raw.strip().upper()
    to_code = to_raw.strip().upper()
    rates = await _get_rates(from_code)
    if rates is None:
        return "не удалось получить курс валют — сервис недоступен, повтори позже"
    rate = rates.get(to_code)
    if rate is None:
        return (
            f"неизвестный код валюты «{to_code}» или «{from_code}» "
            "(нужен формат ISO 4217, например USD, RUB, KZT)"
        )
    return json.dumps(
        {
            "amount": amount,
            "from": from_code,
            "to": to_code,
            "rate": rate,
            "result": round(amount * rate, 4),
        },
        ensure_ascii=False,
    )


# --- get_time ---
#
# Живой баг 2026-07-24: на вопрос "точное время по Москве/в Казахстане"
# модель сама пересчитывала часовые пояса — неверно и непоследовательно
# (например, разница Москва/Казахстан то 3 часа, то время без указания
# пояса вообще выдавалось за конкретный пояс). Часовой пояс — тот же класс
# факта, что погода и курс валют (см. докстринг файла): не полагаться на
# "память" модели, а считать детерминированно. В отличие от get_weather,
# сеть тут не нужна вообще — координаты города не важны, важен только IANA
# часовой пояс, поэтому вместо геокодинга — статическая таблица
# место→пояс и расчёт через stdlib zoneinfo (без новых зависимостей,
# requires-python >=3.11).

_PLACE_TIMEZONES: dict[str, str] = {
    "москва": "Europe/Moscow",
    "россия": "Europe/Moscow",
    "казахстан": "Asia/Almaty",
    "алматы": "Asia/Almaty",
    "астана": "Asia/Almaty",
    "киев": "Europe/Kyiv",
    "украина": "Europe/Kyiv",
    "минск": "Europe/Minsk",
    "беларусь": "Europe/Minsk",
    "ташкент": "Asia/Tashkent",
    "узбекистан": "Asia/Tashkent",
    "лондон": "Europe/London",
    "великобритания": "Europe/London",
    "англия": "Europe/London",
    # Живая находка 2026-07-24: "по Гринвичу"/"по гринвичскому" в бытовой
    # речи значит "по UTC" (смещение 0 всегда) — НЕ гражданское время
    # обсерватории Гринвич (та живёт по Europe/London и уходит в BST летом).
    # Пользователь спрашивает про нулевой пояс, не про городок под Лондоном.
    "гринвич": "UTC",
    "гмт": "UTC",
    "gmt": "UTC",
    "utc": "UTC",
    "нью-йорк": "America/New_York",
    "сша": "America/New_York",
    "италия": "Europe/Rome",
    "рим": "Europe/Rome",
    "германия": "Europe/Berlin",
    "берлин": "Europe/Berlin",
    "франция": "Europe/Paris",
    "париж": "Europe/Paris",
    "испания": "Europe/Madrid",
    "мадрид": "Europe/Madrid",
    "турция": "Europe/Istanbul",
    "стамбул": "Europe/Istanbul",
    "оаэ": "Asia/Dubai",
    "дубай": "Asia/Dubai",
    "китай": "Asia/Shanghai",
    "пекин": "Asia/Shanghai",
    "япония": "Asia/Tokyo",
    "токио": "Asia/Tokyo",
    "индия": "Asia/Kolkata",
}


def _format_hours_diff(minutes: int) -> str:
    hours, mins = divmod(abs(minutes), 60)
    return f"{hours} ч" if mins == 0 else f"{hours} ч {mins} мин"


async def tool_get_time(ctx: ToolContext, args: dict[str, Any]) -> str:
    # Живая находка 2026-07-24: на вопрос "разница между Москвой и Италией"
    # тула не было вовсе — модель считала её сама в уме и путалась,
    # противореча даже собственным же названным поясам. places (список) —
    # для сравнения нескольких мест разом, тул сам детерминированно считает
    # разницу; place (одно место) остаётся как раньше, формат ответа для
    # него не меняется.
    places_raw = args.get("places")
    if isinstance(places_raw, list) and places_raw:
        place_list = [p.strip() for p in places_raw if isinstance(p, str) and p.strip()]
    else:
        single = args.get("place")
        if not isinstance(single, str) or not single.strip():
            return "ошибка: не указано место (place или places)"
        place_list = [single.strip()]
    if not place_list:
        return "ошибка: не указано место (place или places)"

    at_raw = args.get("at")
    if isinstance(at_raw, str) and at_raw.strip():
        try:
            at = datetime.fromisoformat(at_raw)
        except ValueError:
            return "ошибка: 'at' должен быть в формате ISO 8601, например 2026-07-24T20:28:00+03:00"
        if at.tzinfo is None:
            return "ошибка: 'at' должен содержать смещение часового пояса (например +03:00)"
        at_utc = at.astimezone(UTC)
    else:
        at_utc = datetime.now(UTC)

    resolved: list[tuple[str, str, datetime]] = []
    unknown: list[str] = []
    for place in place_list:
        tz_name = _PLACE_TIMEZONES.get(place.lower())
        if tz_name is None:
            unknown.append(place)
            continue
        try:
            target = at_utc.astimezone(ZoneInfo(tz_name))
        except ZoneInfoNotFoundError:
            log.warning("tool_get_time: система не знает часовой пояс %s", tz_name)
            return f"внутренняя ошибка: не удалось определить часовой пояс {tz_name}"
        resolved.append((place, tz_name, target))

    if not resolved:
        return (
            f"не знаю часовой пояс для «{', '.join(unknown)}» — "
            "не могу посчитать точно, не додумывай"
        )

    if len(place_list) == 1:
        # Один запрошенный — прежний плоский формат, поведение не меняется.
        place, tz_name, target = resolved[0]
        offset = target.strftime("%z")
        return json.dumps(
            {
                "place": place,
                "timezone": tz_name,
                "utc_offset": f"{offset[:3]}:{offset[3:]}",
                "local_time": target.strftime("%Y-%m-%d %H:%M"),
                "weekday": WEEKDAYS_RU[target.weekday()],
            },
            ensure_ascii=False,
        )

    entries = []
    for place, tz_name, target in resolved:
        offset = target.strftime("%z")
        entries.append(
            {
                "place": place,
                "timezone": tz_name,
                "utc_offset": f"{offset[:3]}:{offset[3:]}",
                "local_time": target.strftime("%Y-%m-%d %H:%M"),
                "weekday": WEEKDAYS_RU[target.weekday()],
            }
        )
    payload: dict[str, Any] = {"places": entries}
    if unknown:
        payload["unknown_places"] = unknown

    # Разница между КАЖДОЙ парой посчитана здесь, а не моделью — именно
    # ручной пересчёт разницы между поясами и был источником "шизы".
    diffs = []
    for (place_a, _, target_a), (place_b, _, target_b) in itertools.combinations(resolved, 2):
        delta_minutes = round((target_b.utcoffset() - target_a.utcoffset()).total_seconds() / 60)
        if delta_minutes == 0:
            diffs.append(f"{place_a} и {place_b}: одинаковое время")
        elif delta_minutes > 0:
            diffs.append(f"{place_b} впереди {place_a} на {_format_hours_diff(delta_minutes)}")
        else:
            diffs.append(f"{place_a} впереди {place_b} на {_format_hours_diff(delta_minutes)}")
    payload["differences"] = diffs
    return json.dumps(payload, ensure_ascii=False)


# --- remind: единственный тул, ходящий по протоколу роя, см. докстринг модуля ---


_AFTER_EVENT_TYPES = (EVENT_RESTART_APPLIED, EVENT_UPDATE_FINISHED)


def _close_pending_tool_calls(
    history: list[dict[str, Any]], self_result: str | None = None
) -> list[dict[str, Any]]:
    """Снимок ``ctx.history`` для remind делается ИЗ СЕРЕДИНЫ раунда
    tool-calling (llm_chat.run_chat_loop::122-148 вызывает тулы строго
    ПОСЛЕ того, как допишет в history единственное сообщение "assistant"
    с tool_calls за весь раунд, и допишет ответное "tool" на каждый вызов
    только ПОСЛЕ того, как его handler вернётся) — то есть в момент, когда
    remind() строит снимок, вызов remind (и любые другие вызовы того же
    раунда, идущие после него) там висят без ответного "tool"-сообщения.
    Такая история при повторном проигрывании (служба tasks) кладёт перед
    моделью assistant/tool_calls без ответа, за которым сразу новый user —
    невалидная форма для Ollama (живой сбой 2026-08-04: HTTP 400 на /api/
    chat, разобрано только благодаря телу ответа, см. llm/ollama.py).
    Дозаполняем недостающие "tool"-ответы, не трогая сам живой ``history``
    (та же мутируемая ссылка нужна остальным вызовам этого раунда).

    ``self_result`` — живой баг 2026-08-05: node_manage зовёт remind
    (after_event) ИЗНУТРИ СВОЕГО ЖЕ handler'а, до того как его собственный
    результат попадёт в history — снимок раньше глушил его синтетической
    заглушкой "(результат не сохранён)", и разбуженная модель не знала, что
    её же update реально удался, терялась и вместо restart_node звала
    get_time/remind по кругу. Первый висящий вызов в раунде — это ВСЕГДА
    сам текущий (см. порядок обработки tool_calls в run_chat_loop), поэтому
    именно ему подставляется настоящий результат, если он передан;
    остальные (если раунд был из нескольких вызовов) — по-прежнему
    заглушкой, их результат правда ещё не известен."""
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if msg.get("role") == "tool":
            continue
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            return history
        pending = msg["tool_calls"][len(history) - i - 1 :]
        snapshot = list(history)
        for idx, call in enumerate(pending):
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            content = "(результат не сохранён)"
            if idx == 0 and self_result is not None:
                content = self_result
            snapshot.append({"role": "tool", "content": content, "name": fn.get("name", "")})
        return snapshot
    return history


async def tool_remind(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.chat_id is None or ctx.dialogue_id is None or ctx.trigger_message_id is None:
        return "ошибка: отложенные задачи недоступны вне диалога"
    if ctx.node_link is None:
        return "ошибка: служба задач недоступна"
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return "ошибка: не указано, что сделать/сказать (text)"

    after_event = args.get("after_event")
    event_node: str | None = None
    event_type: str | None = None
    if after_event is not None:
        if not isinstance(after_event, dict):
            return "ошибка: after_event должен быть объектом {node, event}"
        event_node = str(after_event.get("node") or "").strip()
        event_type = str(after_event.get("event") or "").strip()
        if not event_node or event_type not in _AFTER_EVENT_TYPES:
            return (
                "ошибка: after_event.node обязателен, after_event.event — одно из "
                + ", ".join(_AFTER_EVENT_TYPES)
            )
        # Живой инцидент 2026-08-05: словесный запрет в директиве ("не зови
        # remind на то же событие снова") модель систематически игнорирует —
        # разбуженная по (node, event_type) заново ставит remind на ТО ЖЕ
        # самое (node, event_type), бесконечно откладывая реальное действие.
        # Жёсткий отказ вместо тихого согласия: разбуженный ход обязан либо
        # выполнить порученное, либо честно сказать, что не вышло — не
        # переносить решение на потом, когда "потом" уже наступило.
        if ctx.woken_by == (event_node, event_type):
            return (
                f"ошибка: событие «{event_type}» от «{event_node}» уже наступило — "
                "именно из-за него тебя сейчас разбудили. Ждать его снова нельзя "
                "(зациклишься) — вызови действие, которое тебя просили сделать, "
                "или честно сообщи, что не получилось."
            )

    when_raw = args.get("when")
    if when_raw is None and after_event is None:
        return "ошибка: не указано время (when, ISO 8601)"
    if when_raw is not None:
        if not isinstance(when_raw, str) or not when_raw.strip():
            return "ошибка: не указано время (when, ISO 8601)"
        try:
            due_at = datetime.fromisoformat(when_raw)
        except ValueError:
            return "ошибка: 'when' должен быть в формате ISO 8601, например 2026-07-24T21:30:00"
        # Наивную дату-время (без смещения) считаем локальным временем процесса —
        # именно в нём отдана строка "текущее время" в контексте промпта
        # (bot/ai_flow.py::_build_context_note), так что модель обычно отвечает
        # тем же способом, без явного смещения.
        if due_at.tzinfo is None:
            due_at = due_at.astimezone()
        due_at_utc = due_at.astimezone(UTC)
        if due_at_utc <= datetime.now(tz=UTC):
            return "ошибка: указанное время уже прошло"
    else:
        # after_event без when: дедлайн — страховка на случай, если событие
        # не придёт вовсе, а не то, что модель должна угадывать (см.
        # RESTART_EVENT_FALLBACK_S).
        due_at_utc = datetime.now(tz=UTC) + timedelta(seconds=RESTART_EVENT_FALLBACK_S)
        due_at = due_at_utc.astimezone()

    # Директива дописывается в снимок ТЕКУЩЕЙ истории (ctx.history — то, что
    # модель видит прямо сейчас, см. докстринг ToolContext) — служба tasks
    # прогоняет ровно этот список через llm.chat заново в момент срабатывания,
    # без доступа к ai_turns бота (решение пользователя 2026-07-24: снимок
    # делается здесь, при создании задачи, а не реконструируется позже).
    if after_event is not None:
        # Срабатывание могло прийти и по событию (раньше due_at, см.
        # bot/node_events.py::_maybe_fire_event_waiter), и по таймауту —
        # отсюда модель не знает, что именно случилось, и должна честно
        # сверить состояние сама (check_update/node_manage), а не считать
        # успех гарантированным. Живой баг 2026-08-05: конкретное время
        # дедлайна в тексте (было f"{due_at:%H:%M}") маленькая модель
        # (Gemma на mycraft) читала как приглашение самой сверить часы —
        # звала get_time по кругу, выжигала весь бюджет раундов и на
        # принудительном "дожатии" без тулов выдавала обрывок СВОЕГО
        # внутреннего формата вызова тула как обычный текст. Решение
        # пользователя: время из директивы убрать вовсе — само решение
        # "сработало по событию или по таймауту" уже принято системой ДО
        # пробуждения модели, ей нечего в нём проверять.
        directive = (
            f"Ты ждал(а) событие «{event_type}» от ноды «{event_node}», после "
            f"чего тебя попросили сделать вот что: «{text.strip()}». Прежде чем "
            "продолжать, сверь реальное состояние (например, node_manage/"
            "check_update) — событие могло не прийти вовсе, тогда честно скажи, "
            "что не подтвердилось, не выдавай желаемое за случившееся. Просто "
            "вызови нужный тул — время сейчас проверять не нужно. НЕ зови "
            f"remind(after_event={{\"node\": \"{event_node}\", \"event\": "
            f'"{event_type}"}}) снова — это то самое событие, по которому тебя '
            "только что разбудили, ставить его ожидание заново — зациклиться."
        )
    else:
        # "Настало время" привязано к due_at, а не к моменту создания задачи —
        # due_at и есть момент фактического срабатывания (с точностью до
        # интервала опроса службы tasks).
        directive = (
            f"Настало время ({due_at:%Y-%m-%d %H:%M}), на которое тебя раньше "
            f"попросили сделать вот что: «{text.strip()}». Сделай/скажи это "
            "сейчас, от своего имени, в характере — как будто сам вспомнил, а не "
            "отвечаешь на прямой вопрос. Если нужно что-то посчитать или узнать "
            "(погоду, курс) — пользуйся инструментами, не полагайся на память."
        )
    # "tools" здесь не кладём: комплект собирается заново в момент
    # срабатывания, по правам собеседника на ТОТ момент (llm_chat.run_chat_loop
    # → tools_for). Раньше сюда клался снимок TOOL_DECLARATIONS, но его никто
    # не читал — цикл всегда подставлял свой список, а снимок лишь раздувал
    # args_json задачи.
    # Ключ только для внутреннего вызова из _auto_await_event (node_manage
    # зовёт remind изнутри своего же handler'а) — не часть публичной схемы
    # тула, модель его никогда не передаёт.
    history = _close_pending_tool_calls(ctx.history, self_result=args.get("_self_result"))
    # Уровень рассуждения на СРАБАТЫВАНИИ напоминания: router-прохода в тот
    # момент нет (вопрос был задан когда-то раньше), берём фиксированную
    # политику из legacy-полей — config.reminder_reason. Перевод уровня в
    # конкретный параметр Ollama делает профиль модели на стороне службы llm
    # (llm/model_profiles.py) — здесь про механизм знать не нужно, поэтому и
    # ушли прежние баги с явным think=true → 400 на gemma (2026-08-05/08-10).
    task_args = {
        "messages": [*history, wrap_system_directive(directive)],
        "reason": reminder_reason(ctx.settings.llm),
        "chat_id": ctx.chat_id,
    }
    meta = {
        "kind": task_protocol.TASK_KIND_LLM_CHAT,
        "chat_id": ctx.chat_id,
        "dialogue_id": ctx.dialogue_id,
        "trigger_message_id": ctx.trigger_message_id,
        # Живой баг 2026-08-05: без этого ответ tasks (bot/node_events.py::
        # _handle_task_result/_handle_task_prewake) уезжал в общий топик
        # личного чата, а не туда, где реально шла переписка (см. докстринг
        # Notifier.send_direct про message_thread_id).
        "message_thread_id": ctx.message_thread_id,
    }
    if after_event is not None:
        # Восстанавливается в ToolContext.woken_by на срабатывании
        # (tasks/service.py::_fire_chat_loop) — см. проверку выше про
        # запрет повторного remind на то же (node, event_type).
        meta["awaited_node"] = event_node
        meta["awaited_event"] = event_type
    dst = Address(node=task_protocol.NODE_ID, service=task_protocol.SERVICE_NAME)
    create_args: dict[str, Any] = {
        "due_at": due_at_utc.isoformat(),
        "dst_node": LLM_NODE,
        "dst_service": LLM_SERVICE,
        "action": task_protocol.ACTION_CHAT_LOOP,
        "args": task_args,
        "timeout_s": ctx.settings.llm.request_timeout_s,
        "meta": meta,
    }
    if after_event is not None:
        # Ожидание живёт в самой службе tasks (не в БД бота — решение
        # пользователя 2026-08-05, живой баг: remind, вызванный ИЗ УЖЕ
        # СРАБОТАВШЕГО хода (тот код исполняется внутри tasks), не имел
        # доступа к ctx.store вовсе — именно там чаще всего и нужно
        # ставить следующее ожидание при обновлении нескольких нод по
        # цепочке). См. tasks/protocol.py::ACTION_MATCH_EVENT.
        create_args["await_event"] = {"node": event_node, "event_type": event_type}
    try:
        await ctx.node_link.command(task_protocol.ACTION_CREATE, create_args, dst=dst)
    except (ServiceUnavailableError, ProtoError) as exc:
        return f"внутренняя ошибка: не удалось поставить задачу ({exc})"

    if after_event is not None:
        return (
            f"жду событие «{event_type}» от «{event_node}», страховочный срок — "
            f"{due_at.strftime('%H:%M')} (местное время)"
        )
    return f"задача поставлена на {due_at.strftime('%Y-%m-%d %H:%M')} (местное время)"


async def schedule_agent_dialogue(
    node_link: ServiceLink,
    chat_id: int,
    messages: list[dict[str, Any]],
    reason: str,
    timeout_s: float,
    *,
    meta_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Поставить chat_loop-задачу, которая на срабатывании начинает НОВЫЙ
    тред у произвольного собеседника — не продолжает свой, как tool_remind
    (тот всегда self-scheduled: несёт ctx.dialogue_id/trigger_message_id/
    message_thread_id, смысл — "напомни МНЕ в ЭТОМ треде"). Здесь meta
    заведомо не содержит dialogue_id/trigger_message_id/message_thread_id —
    рождение нового dialogue_id при доставке первого сообщения делает
    bot/node_events.py::_handle_task_result (Этап 44.2, отдельный подэтап).

    НЕ публичный ToolSpec — модель в живом /ai её вызвать не может, это
    внутренняя инфраструктура: речь Альфреда перед формой подтверждения
    адресату и перед оповещением инициатору об итоге (Этап 45, bot/
    pending_actions.py — ``meta_extra`` несёт pending_action_id/stage).

    due_at = сейчас, не await_event: триггер уже наступил на стороне
    вызывающего кода (например, гость A подтвердил предложение связи) —
    ждать больше нечего, обычный fire-loop (tasks/service.py::_fire_due)
    подхватит задачу на ближайшем тике. Ошибки (ServiceUnavailableError/
    ProtoError) не проглатываются, как у tool_remind (там — русский текст
    для модели) — это не тул, а функция для кода, вызывающая сторона решает
    сама, как сообщить о сбое."""
    due_at_utc = datetime.now(tz=UTC)
    dst = Address(node=task_protocol.NODE_ID, service=task_protocol.SERVICE_NAME)
    create_args: dict[str, Any] = {
        "due_at": due_at_utc.isoformat(),
        "dst_node": LLM_NODE,
        "dst_service": LLM_SERVICE,
        "action": task_protocol.ACTION_CHAT_LOOP,
        "args": {"messages": messages, "reason": reason, "chat_id": chat_id},
        "timeout_s": timeout_s,
        "meta": {
            "kind": task_protocol.TASK_KIND_LLM_CHAT,
            "chat_id": chat_id,
            # Этап 45: pending_action_id/pending_action_stage — речь,
            # после которой бот обязан прислать форму/оповещение (см.
            # bot/node_events.py::_handle_task_result).
            **(meta_extra or {}),
        },
    }
    return await node_link.command(task_protocol.ACTION_CREATE, create_args, dst=dst)


# --- знакомство между гостями (Этап 42.6 → 45 → 46) ---
#
# Решение пользователя 2026-09-27: связь между гостями ОДНА — факт
# знакомства, взаимный и без степеней. Типы друг/знакомый/родство/супруг(а)
# (42.6.x) убраны: выбор типа сталкивал лбами людей, не определившихся
# между собой, а «дружбу» каждый понимает по-своему. Что бы собеседник ни
# сказал об отношениях («мой друг», «моя жена»), Альфред предлагает
# установить знакомство. Групповой флаг «семья» (Subscription.family)
# убран тогда же: кто хочет переписываться через Альфреда — знакомы и
# запросят знакомство. Подтверждённое знакомство открывает tell в обе
# стороны (см. tool_tell). Запрос живёт в pending_actions и решается
# кнопками форм (bot/pending_actions.py), в guest_relationships пишется
# только итог (confirmed). Отзыв знакомства — пока не нужен (решение того
# же дня).
RELATION_ACQUAINTANCE = "acquaintance"


def _guest_facing_name(settings: Settings, chat_id: int, fallback: str) -> str:
    """Имя для текста, который увидит ДРУГОЙ человек, не сам этот chat_id.

    Живая находка 2026-09-26: Subscription.name у владельца в проде технически
    "me" — умышленно (subscriptions/book.py, tests/test_recipients.py:
    гость не должен подобрать владельца по имени через recipients.py), но это
    же значение утекает в сообщения о связях ("Гость «me» предложил..."),
    которые видит СОБЕСЕДНИК, а не сам владелец. Там, где найдётся
    settings.people (config.py::PersonConfig) с этим chat_id — берём
    настоящее full_name(+@username); обычному гостю там взяться нечему,
    Subscription.name у него и так человекочитаемое имя — fallback.

    ВАЖНО (живая находка того же дня, второй заход): full_name лежит в
    конфиге строго в именительном падеже. НЕ вставлять результат прямо в
    предложение, где по-русски нужен другой падеж (дательный "кому",
    родительный "у кого", творительный "с кем" и т.п.) — ломается
    грамматика ("у Наташа Сорокина", "отправлено Наташа Сорокина"). Падеж
    держать на служебном слове ("адресату (…)", "с адресатом (…)"), а имя —
    именительным лейблом в скобках. И ни в каком падеже не называть по
    имени в третьем лице самого адресата текста (см. tool_request_acquaintance)."""
    for person in settings.people:
        if person.telegram_id and person.telegram_id == chat_id:
            return (
                f"{person.full_name} (@{person.telegram_username})"
                if person.telegram_username
                else person.full_name
            )
    return fallback


async def are_acquainted(store: Any, a: int, b: int) -> bool:
    """Подтверждённое знакомство пары — в любом направлении (guest_a/guest_b
    не упорядочены). Старые строки других типов (до 2026-09-27) миграция
    переписала в acquaintance, поэтому тип не сверяем."""
    return any({r["guest_a"], r["guest_b"]} == {a, b} for r in await store.relationships_for(a))


async def acquaintance_conflict(
    store: Any,
    initiator: int,
    target: int,
    target_name: str,
    *,
    ignore_action_id: int | None = None,
) -> str | None:
    """Почему знакомство сейчас нельзя предложить/принять — None, если
    можно. Проверяется на открытии формы (request_acquaintance) И повторно
    на «Отправить»/«Принять» (bot/pending_actions.py): за час/трое суток
    ожидания могли познакомиться иначе или прийти встречное предложение.
    ``ignore_action_id`` — сама проверяемая форма, она не конфликт себе.

    Падеж держим на служебном слове («адресату (…)»), имя — именительным
    лейблом в скобках: full_name из settings.people строго в именительном
    (живая находка 2026-09-26, «у Наташа Сорокина»)."""
    pair = {initiator, target}
    for row in await store.open_pending_actions("relationship", initiator):
        if row["id"] == ignore_action_id or {row["initiator"], row["addressee"]} != pair:
            continue
        if row["initiator"] != initiator:
            # Встречное: адресат уже сам предложил знакомство инициатору.
            return (
                f"адресат ({target_name}) уже сам предложил вам знакомство — "
                "ответьте кнопкой «Принять» в его форме"
            )
        if row["status"] == "draft":
            return (
                f"форма предложения знакомства адресату ({target_name}) уже открыта — "
                "ждёт кнопки «Отправить» или «Отмена»"
            )
        # Живая находка 2026-10-09: этот ответ про Александра модель
        # пересказала как «отправил Андрею» — id был взят не тот.
        return (
            f"предложение знакомства уже отправлено адресату ({target_name}), ждём "
            f"ответа. Если собеседник говорил не про {target_name} — id не тот: "
            "найди нужного find_person и не говори, что ему уже отправлено"
        )
    if await are_acquainted(store, initiator, target):
        return f"вы с адресатом ({target_name}) уже знакомы"
    return None


async def tool_request_acquaintance(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Открыть инициатору форму предложения знакомства. Модель НЕ
    отправляет предложение и НЕ принимает ответ: тул создаёт черновик
    (bot/pending_actions.py, срок 1 ч), а форму с кнопками «Отправить»/
    «Отмена» бот пришлёт отдельным сообщением ПОСЛЕ ответа Альфреда
    (bot/handlers/ai.py → PendingActions.flush_drafts). Дальше всё решают
    кнопки.

    Адресат — по id (Этап 54.5): его находит find_person с
    purpose="acquaintance" — среди всех гостей, но только по точному имени,
    нику или прозвищу, без выдачи списка (guests_list гостю не виден).

    Только живой /ai: в службе tasks (ctx.pending_actions там нет) формы
    некому показать — честный отказ."""
    if (
        ctx.chat_id is None
        or ctx.book is None
        or ctx.store is None
        or ctx.pending_actions is None
    ):
        return "недоступно: форму знакомства можно открыть только в живом разговоре"
    recipient_id = parse_recipient_id(args)
    if recipient_id is None:
        return NEED_RECIPIENT_ID.replace("find_person", 'find_person(purpose="acquaintance")')
    found = recipients.find_by_chat_id(recipient_id, ctx.book, ctx.settings.people)
    if not found:
        return (
            f"не получилось: id {recipient_id} — ни один гость. id не придумывай: "
            'вызови find_person(purpose="acquaintance") с тем, как собеседник '
            "назвал человека, и возьми id из ответа"
        )
    target_chat_id = found[0].chat_id
    if target_chat_id == ctx.chat_id:
        return "ошибка: нельзя предложить знакомство самому себе"
    proposer = ctx.book.for_chat(ctx.chat_id)
    target = ctx.book.for_chat(target_chat_id)
    if proposer is None or target is None:
        return "недоступно: знакомиться через меня могут только приглашённые гости"
    # Имена для ЧУЖОГО текста — не Subscription.name напрямую, см.
    # _guest_facing_name (владельческое "me" технически, не для показа).
    proposer_name = _guest_facing_name(ctx.settings, ctx.chat_id, proposer.name)
    target_name = _guest_facing_name(ctx.settings, target_chat_id, target.name)

    conflict = await acquaintance_conflict(ctx.store, ctx.chat_id, target_chat_id, target_name)
    if conflict is not None:
        return conflict
    await ctx.pending_actions.create_relationship_draft(
        ctx.chat_id, target_chat_id, RELATION_ACQUAINTANCE, proposer_name, target_name
    )
    # Живая находка 2026-09-26: инициатор — это ВСЕГДА сам собеседник в
    # этом чате, в тексте, адресованном ЕМУ, не называть его по имени в
    # третьем лице (Gemma озвучивала такое слово в слово).
    return (
        "Форма открыта, адресату пока НИЧЕГО не отправлено. Сразу после твоего "
        "ответа собеседник получит отдельное сообщение-форму: предложение "
        f"знакомства, адресат — {target_name}, кнопки «Отправить» и «Отмена», "
        "форма действует 1 час. Собеседник в этом чате — сам инициатор; "
        "обращайся к нему на «вы» и не называй его по имени в третьем лице. "
        "Коротко скажи своими словами, что форма ниже и что отправит "
        "предложение он сам кнопкой, а после согласия адресата ты сможешь "
        "передавать сообщения между ними. Не говори, что уже отправил, и не "
        "проси подтвердить текстом."
    )


async def tool_swap_radio(ctx: ToolContext, _args: dict[str, Any]) -> str:
    """Устройство связи Альфреда (Этап 47, bot/interactives/radio.py). Один
    тул на обе стороны: до завершения интерактива «Проклятый передатчик»
    он лишь запускает/продвигает сцену (в финале — форма замены), после —
    переключатель старый/новый передатчик. Решает кнопка формы, не модель.
    Службе tasks формы показать некому — честный отказ."""
    if ctx.interactives is None:
        return interactive_radio.TOOL_UNAVAILABLE
    return await ctx.interactives.tool_swap_radio(
        ctx.chat_id, ctx.user_id, is_private=ctx.is_private
    )


async def tool_manor_items(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Особенные вещи поместья (Этап 49.3.6, bot/interactives/items.py):
    опись — модели, она отвечает своими словами; ``show`` — карточка вещи
    уходит в чат сама."""
    if ctx.interactives is None:
        return interactive_radio.TOOL_ITEMS_UNAVAILABLE
    show = args.get("show")
    return await ctx.interactives.tool_manor_items(
        ctx.chat_id,
        ctx.user_id,
        show=show if isinstance(show, str) else "",
        message_thread_id=ctx.message_thread_id,
        trigger_message_id=ctx.trigger_message_id,
    )


async def tool_item_action(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Действие с особенной вещью поместья (Этап 49.3.7): форма с кнопками
    из данных действия вида (bot/interactives/items.py); делает кнопка."""
    if ctx.interactives is None:
        return interactive_radio.TOOL_ACTION_UNAVAILABLE
    item, action = args.get("item"), args.get("action")
    return await ctx.interactives.tool_item_action(
        ctx.chat_id,
        ctx.user_id,
        item=item if isinstance(item, str) else "",
        action=action if isinstance(action, str) else "",
    )


async def tool_take_photo(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Снимок кабинета Альфреда (Этап 49.2, bot/interactives/cabinet.py) —
    вся логика в Interactives.tool_take_photo. Рисуется в фоне и приходит
    сам; у службы tasks интерактивов нет — честный отказ."""
    if ctx.interactives is None:
        return interactive_cabinet.TOOL_PHOTO_UNAVAILABLE
    result = await ctx.interactives.tool_take_photo(
        ctx.chat_id,
        ctx.user_id,
        args,
        message_thread_id=ctx.message_thread_id,
        trigger_message_id=ctx.trigger_message_id,
        dialogue_id=ctx.dialogue_id,
    )
    # Снимок пошёл рисоваться — Альфред молчит, пока он не готов: описание
    # «до снимка» выходило выдуманным (живая находка 2026-10-02).
    if result == interactive_cabinet.TOOL_PHOTO_STARTED:
        ctx.end_turn = True
    return result


async def tool_generate_image(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Нарисовать картинку (Этап 48) — тонкая обёртка над
    bot/image_tools.py::generate, как tool_swap_radio над интерактивом.
    Эпизод графа передаётся колбэком: image_tools не импортирует bot.tools
    (иначе цикл), а _piggyback_graph_episode живёт здесь."""

    async def remember(text: str) -> None:
        # generate зовёт remember только после доставки картинки в чат, а
        # доставка без chat_id невозможна (_can_deliver) — но колбэк не
        # должен полагаться на порядок вызовов чужого модуля: без чата
        # эпизоду не к чему привязаться, молча пропускаем.
        if ctx.chat_id is None:
            return
        await _piggyback_graph_episode(
            ctx, text, ctx.chat_id, source=graph_memory_protocol.EPISODE_SOURCE_IMAGE
        )

    return await image_tools.generate(ctx, args, remember=remember)


async def tool_find_image(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Показать ранее нарисованную картинку (Этап 48) — поиск и отправка
    целиком в bot/image_tools.py::find, только по картинкам своего чата."""
    return await image_tools.find(ctx, args)


def _note_person_stranger(ctx: ToolContext, subject_id: int, name: str) -> str:
    """Отказ note_person о незнакомом — с тем, чей это id.

    Живая находка 2026-10-09: «Я знаком с Андреем, это мой брат» — модель
    без find_person взяла id Александра — единственный id в семейных фактах
    памяти (у Андрея там id нет), а безымянный отказ ещё и подсказал открыть
    знакомство с ним же."""
    if ctx.book is None:
        who = "этот человек"
    else:
        own = _person_labels(ctx, [subject_id], {}).get(subject_id)
        if own is None:
            return (
                f"не записал: id {subject_id} — ни один гость. id не придумывай: "
                f'найди человека find_person(description="{name or "как его назвали"}")'
            )
        found = recipients.find_by_chat_id(subject_id, ctx.book, ctx.settings.people)
        who = found[0].display if found else f"id {subject_id}"
        if name.strip() and not _name_fits(name, own):
            return (
                f"не записал: id {subject_id} — это {who}, а не «{name}». id из памяти и "
                f'прошлых разговоров не бери: найди нужного find_person(description="{name}", '
                'purpose="acquaintance")'
            )
    return (
        f"не записал: о других запоминаю только со слов их знакомых, а собеседник "
        f"и {who} не знакомы через тебя; познакомить — "
        f"request_acquaintance(recipient_id={subject_id})"
    )


async def tool_note_person(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Этап 54.2: записать пол/имя/прозвище человека в общую карточку
    (graph_memory, раздел people) — её видят все чаты, а не только этот.

    Вес решает бот, не модель: говорящий о себе — сильное утверждение;
    о знакомом — слабое; о постороннем — отказ. Знакомство проверяем тут,
    потому что таблица знакомств живёт на alfred, а служба — на mycraft."""
    if ctx.node_link is None:
        return "недоступно: нет связи с роем"
    by_id = ctx.user_id
    if by_id is None:
        return "недоступно: не знаю, кто сейчас говорит"
    raw_subject = args.get("person_id")
    try:
        subject_id = by_id if raw_subject in (None, "") else int(raw_subject)
    except (TypeError, ValueError):
        return f"ошибка: person_id — это Telegram id числом, а не {raw_subject!r}"
    if subject_id != by_id:
        if ctx.store is None or not await are_acquainted(ctx.store, by_id, subject_id):
            return _note_person_stranger(ctx, subject_id, str(args.get("name") or ""))
    claims: list[tuple[str, Any]] = []
    for key in ("gender", "name"):
        if args.get(key) not in (None, ""):
            claims.append((key, args[key]))
    aliases = args.get("alias")
    for alias in [aliases] if isinstance(aliases, str) else list(aliases or []):
        if str(alias).strip():
            claims.append(("alias", alias))
    if not claims:
        return "ошибка: нечего записывать — укажи gender, name или alias"
    try:
        normalized = [normalize_claim(key, value) for key, value in claims]
    except ClaimError as exc:
        return f"ошибка: {exc}"

    dst = Address(node=graph_memory_protocol.NODE_ID, service=graph_memory_protocol.SERVICE_NAME)
    saved: list[str] = []
    for key, value in normalized:
        try:
            await ctx.node_link.command(
                graph_memory_protocol.ACTION_PERSON_CLAIM,
                {"subject_id": subject_id, "field": key, "value": value, "by_id": by_id},
                dst=dst,
            )
        except ProtoError as exc:
            return f"не вышло: {exc.message}"
        except (ServiceUnavailableError, TimeoutError) as exc:
            return f"недоступно: память о людях не отвечает ({exc})"
        saved.append(f"{key}={value}")
    whose = "со слов самого человека" if subject_id == by_id else "со слов знакомого (слабее)"
    return f"записал в карточку id {subject_id}, {whose}: " + ", ".join(saved)


_DECL_NOTE_PERSON: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "note_person",
        "description": (
            "Запомнить навсегда и для всех разговоров пол, имя или прозвище "
            "человека. Зови, когда собеседник прямо сказал это о себе "
            "(«я парень», «зови меня Лилиан») или о знакомом ему госте — "
            "и сразу, когда тебя поправили в обращении. По имени или "
            "догадке не зови: только сказанное словами. О себе — "
            "person_id не указывай."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "person_id": {
                    "type": "integer",
                    "description": "Telegram id знакомого, если речь о нём, а не о собеседнике",
                },
                "gender": {"type": "string", "enum": ["m", "f"]},
                "name": {"type": "string", "description": "как человека зовут"},
                "alias": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "прозвища и уменьшительные имена",
                },
            },
            "required": [],
        },
    },
}


PURPOSE_MESSAGE = "message"
PURPOSE_ACQUAINTANCE = "acquaintance"
_GENDER_WORDS = {"m": "мужчина", "f": "женщина"}


def _stem(word: str) -> str:
    word = word.casefold().lstrip("@")
    return word[:-1] if len(word) > 3 else word


def _same_stems(query: str, candidate: str) -> bool:
    """Падеж: «Миле» и «Мила», «Андрею» и «Андрей» — у каждого слова
    запроса есть слово кандидата с той же основой (без последней буквы).
    Строже, чем начало слова: «Миле» не находит «Милану»."""
    words = [_stem(w) for w in candidate.replace("(", " ").replace(")", " ").split()]
    query_words = [_stem(w) for w in query.split()]
    return bool(query_words) and all(w in words for w in query_words)


def _stem_query(query: str) -> str:
    return " ".join(_stem(w) for w in query.split())


# Слова описания, которые не имя: «Андрей который кейн», «это мой отец».
_DESCRIPTION_FILLER = frozenset(
    "который которая которое которые это этот эта мой моя моё мое мои твой "
    "его её ее их есть такой такая как тот та кто зовут зовёт по нику ник "
    "имени имя с со и или он она".split()
)


def _nick_spellings(word: str) -> set[str]:
    """Кириллицей написанный ник латиницей: «кейн» → kein/keyn/kejn."""
    base = word.translate(_TRANSLIT)
    if base == word:
        return set()
    spellings = {base}
    for cyr, lat in (("й", ("i", "j")), ("х", ("h",)), ("е", ("ye",))):
        if cyr in word:
            for alt in lat:
                spellings.add(word.replace(cyr, alt).translate(_TRANSLIT))
    return spellings


def _match_by_words(
    query: str, labels: dict[int, list[tuple[str, str]]]
) -> dict[int, str]:
    """Последний заход find_person: описание целиком не совпало ни с кем —
    («Андрей который кейн», живая находка 2026-10-09) — ищем по отдельным
    словам без служебных, и кириллицу ещё транслитом (ник «kein»). Берём
    тех, у кого совпало больше всего слов; несколько — выберет собеседник."""
    words = [
        w
        for w in (w.strip(".,!?«»\"'()").casefold() for w in query.split())
        if len(w) >= 3 and w not in _DESCRIPTION_FILLER
    ]
    scores: dict[int, list[str]] = {}
    for word in words:
        variants = {word, _stem(word)} | _nick_spellings(word)
        for chat_id, own in labels.items():
            if any(
                label and recipients.matches(v, label) for v in variants for _kind, label in own
            ):
                scores.setdefault(chat_id, []).append(word)
    if not scores:
        return {}
    best = max(len(found) for found in scores.values())
    return {
        chat_id: "по словам «" + "», «".join(found) + "»"
        for chat_id, found in scores.items()
        if len(found) == best
    }


def _person_labels(
    ctx: ToolContext, chat_ids: Iterable[int], cards: dict[int, Any]
) -> dict[int, list[tuple[str, str]]]:
    """Все имена, под которыми человек известен дому: подписка, карточка
    graph_memory, [[people]] из конфига — с видом метки для причины совпадения."""
    assert ctx.book is not None
    labels: dict[int, list[tuple[str, str]]] = {}
    for chat_id in chat_ids:
        sub = ctx.book.for_chat(chat_id)
        if sub is None:
            continue
        own = [("имя", sub.name), ("имя", sub.invited_user or "")]
        card = cards.get(chat_id) or {}
        if isinstance(card.get("name"), dict):
            own.append(("имя из карточки", card["name"]["value"]))
        own += [("прозвище", a["value"]) for a in card.get("aliases") or []]
        labels[chat_id] = own
    for person in ctx.settings.people:
        for chat_id in recipients.person_chat_ids(person, ctx.book):
            if chat_id in labels:
                labels[chat_id] += [("имя", person.full_name), ("ник", person.telegram_username)]
    return labels


def _name_fits(name: str, own: list[tuple[str, str]]) -> bool:
    """Подходит ли названное имя к меткам одного человека — те же заходы,
    что у find_person, кроме id и роли."""
    for _kind, label in own:
        if label and (
            recipients.matches(name, label)
            or _same_stems(name, label)
            or recipients.matches(_stem_query(name), label)
        ):
            return True
    return bool(_match_by_words(name, {0: own}))


async def tool_find_person(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Этап 54.4: найти id человека по описанию. Ищет только среди тех,
    к кому собеседнику вообще можно обратиться:

    - purpose="message" (tell, notify_guest): у гостя — подтверждённые
      знакомые и роль владельца; у владельца — все гости в личке;
    - purpose="acquaintance" (request_acquaintance): все гости, но если
      никто не подошёл, список не выдаём — гостю он не положен.

    Совпадения — по имени и @нику подписки, [[people]], имени и прозвищам из
    карточек (graph_memory); вторым заходом — без последней буквы (падеж).
    Несколько кандидатов — отдаём всех с отличиями, выбирает собеседник."""
    if ctx.chat_id is None or ctx.book is None:
        return "недоступно: сейчас не вижу, кому можно писать"
    query = " ".join(str(args.get("description") or "").split())
    if not query:
        return "ошибка: опиши человека (description) — как его назвал собеседник"
    purpose = str(args.get("purpose") or PURPOSE_MESSAGE).strip()
    if purpose not in (PURPOSE_MESSAGE, PURPOSE_ACQUAINTANCE):
        purpose = PURPOSE_MESSAGE
    is_owner = ctx.subscription is not None and ctx.subscription.is_owner

    private = {sub.chat_id: sub for sub in ctx.book.all() if sub.chat_id > 0}
    if purpose == PURPOSE_ACQUAINTANCE or is_owner:
        pool = set(private)
    else:
        pool = {chat_id for chat_id, _name in await acquaintance_roster(ctx)} & set(private)
    if not (is_owner and purpose == PURPOSE_MESSAGE):
        pool.discard(ctx.chat_id)

    cards = await people_cards.fetch_person_cards(ctx.node_link, sorted(pool)[:50])
    labels = _person_labels(ctx, pool, cards)

    def display(chat_id: int) -> str:
        found = recipients.find_by_chat_id(chat_id, ctx.book, ctx.settings.people)
        return found[0].display if found else str(chat_id)

    def gender_of(chat_id: int) -> str | None:
        gender = people_cards.card_gender(cards.get(chat_id))
        if gender:
            return gender
        for person in ctx.settings.people:
            if chat_id in recipients.person_chat_ids(person, ctx.book):
                return person.gender
        return None

    hits: dict[int, str] = {}
    explicit = recipients.query_chat_id(query)
    if explicit is not None and explicit in pool:
        hits[explicit] = "по id"
    if not hits and purpose == PURPOSE_MESSAGE and recipients.is_owner_role_reference(query):
        for chat_id, sub in private.items():
            if sub.is_owner and chat_id != ctx.chat_id:
                hits[chat_id] = "роль владельца — передавай с to_owner_role=true"
    # @ник в описании точнее любого имени рядом с ним: «Александр
    # Сергеевич Севбо @ksytal_as» целиком не совпадал ни с одной меткой
    # (живая находка 2026-10-09).
    for nick in (w for w in query.split() if w.startswith("@") and len(w) > 1):
        for chat_id, own in labels.items():
            if any(label and recipients.matches(nick, label) for _kind, label in own):
                hits.setdefault(chat_id, f"ник «{nick}»")
    # Три захода, от строгого к мягкому: как названо; та же основа
    # (падеж); начало слова по основе. Следующий — только если пусто.
    matchers = (
        lambda label: recipients.matches(query, label),
        lambda label: _same_stems(query, label),
        lambda label: recipients.matches(_stem_query(query), label),
    )
    for matcher in matchers:
        if hits:
            break
        for chat_id, own in labels.items():
            for kind, label in own:
                if label and matcher(label):
                    hits.setdefault(chat_id, f"{kind} «{label}»")
    if not hits:
        hits = _match_by_words(query, labels)

    if not hits:
        if purpose == PURPOSE_ACQUAINTANCE:
            return (
                f"под «{query}» никого из гостей не нашёл — переспроси имя или ник; "
                "познакомить можно только с тем, кто уже принял приглашение"
            )
        allowed = [
            f"{display(c)} — id {c}" + (f" — {_GENDER_WORDS[g]}" if (g := gender_of(c)) else "")
            for c in sorted(pool)
        ]
        tail = (
            " Кому можно писать: " + "; ".join(allowed) + "."
            if allowed
            else " Подтверждённых знакомых у собеседника нет."
        )
        if not is_owner:
            tail += " Владельцу можно писать по роли: «владельцу»/«хозяину»."
        return (
            f"под «{query}» никого не нашёл. Родственное слово («маме») — не имя: "
            "если по списку ясно, кто это, бери его id, иначе переспроси." + tail
        )

    lines = []
    for chat_id, reason in hits.items():
        gender = gender_of(chat_id)
        bits = [display(chat_id), f"id {chat_id}"]
        if gender:
            bits.append(_GENDER_WORDS[gender])
        bits.append(f"совпало: {reason}")
        lines.append(" — ".join(bits))
    if len(lines) == 1:
        if purpose == PURPOSE_ACQUAINTANCE:
            (chat_id,) = hits
            return f"Нашёл: {lines[0]}; познакомить — request_acquaintance(recipient_id={chat_id})"
        return "Нашёл: " + lines[0]
    return (
        "Подходят несколько — переспроси собеседника, кого он имел в виду, "
        "сам не выбирай:\n" + "\n".join(lines)
    )


_DECL_FIND_PERSON: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "find_person",
        "description": (
            "Найти Telegram id человека по описанию — для tell, notify_guest и "
            "request_acquaintance, которые принимают адресата только по id. "
            "Передай, как собеседник назвал человека: имя в любом падеже, "
            "прозвище, @ник, «хозяину». Ищет только среди тех, кому собеседнику "
            "можно писать (purpose=message), или среди всех гостей для "
            "знакомства (purpose=acquaintance). Несколько кандидатов — "
            "переспроси, никого — скажи как есть, не сочиняй id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "как собеседник назвал или описал человека",
                },
                "purpose": {
                    "type": "string",
                    "enum": [PURPOSE_MESSAGE, PURPOSE_ACQUAINTANCE],
                    "description": "message — чтобы написать; acquaintance — чтобы познакомить",
                },
            },
            "required": ["description"],
        },
    },
}


async def tool_my_acquaintances(ctx: ToolContext, _args: dict[str, Any]) -> str:
    """Только подтверждённые знакомства — про неотвеченные/отклонённые
    предложения молчим намеренно (IMPLEMENTATION_PLAN.md §42.6.3).
    ``ctx.store`` нет в службе tasks — там честный отказ, не молчание."""
    if ctx.chat_id is None or ctx.book is None or ctx.store is None:
        return "недоступно: сейчас не вижу своих знакомых"
    me = ctx.book.for_chat(ctx.chat_id)
    if me is None:
        return "недоступно: тебя нет в списке гостей"

    roster = await acquaintance_roster(ctx)
    if not roster:
        return "подтверждённых знакомств нет"
    lines = [f"🤝 {name} — id {chat_id}" for chat_id, name in roster]
    return (
        "Знакомы (могу передавать сообщения; в tell укажи recipient_id):\n"
        + "\n".join(lines)
    )


async def acquaintance_roster(ctx: ToolContext) -> list[tuple[int, str]]:
    """Подтверждённые знакомые собеседника: (chat_id, имя). id — главное:
    по нему tell находит человека точно, как бы модель ни переиначила имя
    (живой баг 2026-09-28: «передай маме» ушло в «Наталья Вадимовна», хотя
    знакома была Милана)."""
    if ctx.chat_id is None or ctx.book is None or ctx.store is None:
        return []
    roster: list[tuple[int, str]] = []
    for row in await ctx.store.relationships_for(ctx.chat_id, status="confirmed"):
        other_chat_id = row["guest_b"] if row["guest_a"] == ctx.chat_id else row["guest_a"]
        other = ctx.book.for_chat(other_chat_id)
        name = (other.invited_user or other.name) if other is not None else "?"
        roster.append((other_chat_id, name))
    return roster


async def _roster_hint(ctx: ToolContext) -> str:
    roster = await acquaintance_roster(ctx)
    if not roster:
        return " Подтверждённых знакомых у собеседника нет."
    names = ", ".join(f"{name} (id {chat_id})" for chat_id, name in roster)
    return (
        f" Знакомые собеседника: {names}. Если имелся в виду кто-то из них — "
        "вызови tell ещё раз с его recipient_id; если неясно кто — "
        "переспроси у собеседника, не угадывай."
    )


_DECL_CALC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "calc",
        "description": (
            "Точно вычислить арифметическое выражение (числа, + - * / скобки, "
            "степень как ** или ^, плюс константы pi и e — без произвольных "
            "переменных и функций). Используй для ЛЮБОЙ реальной арифметики, "
            "включая формулы (площадь, объём и т.п.) — подставь известные "
            "числа и pi/e в выражение и вызови тул, не считай и не "
            "подставляй в уме."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Например: 2 * pi * 1.5 * (1.5 + 2) или 1.5^2",
                }
            },
            "required": ["expression"],
        },
    },
}

_DECL_WEATHER: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Что сейчас в любом городе мира (не только дома): погода словами, "
            "температура (сейчас, ощущается, мин/макс за день), осадки, облачность, "
            "ветер в м/с, а также местное время, день недели, время суток (ночь/"
            "рассвет/день/закат), высота солнца, восход и закат, фаза луны. Не "
            "считай время суток, восход или луну сам — бери из ответа. Если "
            "пользователь называет город, "
            "передай его в city; если спрашивает просто 'какая погода' без "
            "уточнения — не передавай city вовсе, вернётся погода дома."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": (
                        "Город, если он назван явно (например: Алматы); при "
                        "неоднозначности — со страной через запятую: «Бран, Румыния»"
                    ),
                }
            },
        },
    },
}

_DECL_CURRENCY: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "convert_currency",
        "description": (
            "Точно перевести сумму из одной валюты в другую по актуальному курсу. "
            "Используй для любого вопроса про курс/конвертацию денег — не пытайся "
            "вспомнить курс сам, он быстро устаревает."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Сумма для перевода"},
                "from": {
                    "type": "string",
                    "description": "Код исходной валюты, ISO 4217 (например USD)",
                },
                "to": {
                    "type": "string",
                    "description": "Код целевой валюты, ISO 4217 (например RUB)",
                },
            },
            "required": ["amount", "from", "to"],
        },
    },
}

_DECL_TIME: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": (
            "Точно узнать текущее время (и день недели) в конкретном "
            "городе/стране, а также разницу во времени между НЕСКОЛЬКИМИ "
            "местами. Часовые пояса и разницу между ними НЕ считай сам — "
            "модель на практике их путает и противоречит сама себе даже "
            "после того, как назвала верные названия поясов. Используй "
            "этот тул для ЛЮБОГО вопроса про время не 'у нас/сейчас' (то "
            "уже есть в контексте разговора), а в другом городе/стране — "
            "ВКЛЮЧАЯ короткие вопросы-продолжения вроде 'а в Х?' сразу "
            "после уже заданного вопроса про другое место: это НОВОЕ "
            "место, вызови тул заново, не выводи по аналогии с прошлым "
            "ответом. Если спрашивают РАЗНИЦУ между двумя и более местами "
            "(или список часовых поясов сразу для нескольких мест) — "
            "передай ВСЕ места в places одним вызовом, тул сам посчитает "
            "разницу (поле differences в ответе) — НЕ вычитай время двух "
            "мест сам. Если тул не знает место — так и скажи как есть, не "
            "досчитывай и не придумывай сам."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "description": (
                        "Город или страна (например: Москва) — для ОДНОГО места. "
                        "Если мест несколько (сравнение/разница) — используй places."
                    ),
                },
                "places": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        'Список мест (например: ["Москва", "Италия"]) — для '
                        "сравнения нескольких мест или вопроса про разницу во "
                        "времени между ними. Если задан, place игнорируется."
                    ),
                },
                "at": {
                    "type": "string",
                    "description": (
                        "Необязательно: конкретный момент времени в ISO 8601 "
                        "СО смещением (например 2026-08-01T12:00:00+03:00) — "
                        "для вопросов про другую дату, не 'сейчас'. Без этого "
                        "поля берётся текущий момент."
                    ),
                },
            },
        },
    },
}

# --- look_at_photo: мультимодальный /ai, 2026-08-10 — точечный повторный
# просмотр УЖЕ сохранённого фото из этого же треда. Файл лежит на mycraft
# (там же, где Ollama — ресайз/хранение делает служба llm, см.
# llm/vision.py), этот тул только передаёт ключ и вопрос; ничего не
# скачивает и не пересылает заново.

_DECL_LOOK_AT_PHOTO: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "look_at_photo",
        "description": (
            "Посмотреть на картинку: фото, которое собеседник прислал РАНЕЕ в "
            "этом разговоре (whose=guest), или твою собственную — снимок кабинета, "
            "нарисованную картинку, карточку вещи (whose=mine). Фото из ТЕКУЩЕГО "
            "сообщения ты и так видишь. Своих картинок ты не видишь, пока не "
            "посмотришь этим тулом: спрашивают, что на твоём снимке или "
            "рисунке, — сначала посмотри, не выдумывай. Если собеседник ответил "
            "на твою картинку, тул сам возьмёт её."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Что именно нужно рассмотреть или уточнить на картинке",
                },
                "whose": {
                    "type": "string",
                    "enum": ["guest", "mine"],
                    "description": "guest — фото собеседника (по умолчанию), mine — твоя картинка",
                },
                "image_id": {
                    "type": "integer",
                    "description": "Номер твоей картинки (#N), если речь о конкретной",
                },
            },
            "required": ["question"],
        },
    },
}


async def _own_picture(ctx: ToolContext, args: dict[str, Any]) -> dict | str | None:
    """Своя картинка Альфреда для look_at_photo: ответ гостя на картинку,
    номер или последняя. None — смотреть фото гостя; строка — отказ."""
    assert ctx.store is not None and ctx.chat_id is not None
    whose = args.get("whose")
    image_id = args.get("image_id")
    if isinstance(image_id, bool) or not isinstance(image_id, int):
        image_id = None
    if whose != "guest" and image_id is None and ctx.reply_to_message_id is not None:
        replied = await ctx.store.alfred_image(ctx.chat_id, message_id=ctx.reply_to_message_id)
        if replied is not None:
            return replied
    if whose != "mine" and image_id is None:
        return None
    found = await ctx.store.alfred_image(ctx.chat_id, image_id=image_id)
    if found is None:
        return (
            f"картинки #{image_id} в этом чате нет"
            if image_id is not None
            else "ты ещё ничего не присылал в этот чат — смотреть не на что"
        )
    return found


async def tool_look_at_photo(ctx: ToolContext, args: dict[str, Any]) -> str:
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return "ошибка: не указан вопрос про фото (question)"
    if ctx.store is None or ctx.chat_id is None:
        return "ошибка: сейчас не могу вернуться к фото (нет доступа к истории треда)"
    request: dict[str, Any] = {"question": question, "chat_id": ctx.chat_id}
    mine = await _own_picture(ctx, args)
    if isinstance(mine, str):
        return mine
    if mine is not None:
        # Своя картинка — байты из базы, как их видел гость (служба llm её
        # не хранит; 2026-10-02: Альфред не видел своих снимков).
        request["photo_key"] = f"img-{mine['id']}"
        request["raw_image"] = base64.b64encode(
            image_tools.upscale_png(mine["png"], ctx.settings.llm.imagegen_display_px)
        ).decode()
    else:
        if ctx.dialogue_id is None:
            return "ошибка: сейчас не могу вернуться к фото (нет доступа к истории треда)"
        turn = await ctx.store.latest_photo_turn(ctx.chat_id, ctx.dialogue_id)
        if turn is None:
            return "в этом разговоре фото не найдено — переспроси, если оно точно было"
        request["photo_key"] = turn["photo_path"]
    if ctx.node_link is None:
        return "ошибка: сейчас не могу связаться с Альфредом, чтобы посмотреть на фото"
    try:
        result = await ctx.node_link.command(
            ACTION_LOOK_AT_PHOTO,
            request,
            dst=Address(node=LLM_NODE, service=LLM_SERVICE),
            timeout=ctx.settings.llm.request_timeout_s,
        )
    except (ServiceUnavailableError, ProtoError, TimeoutError) as exc:
        return f"не получилось рассмотреть фото ещё раз: {exc}"
    response = str(result.get("response", "")).strip()
    if response:
        # Этап 42.2: повторное распознавание фото — тоже эпизод графа
        # (best-effort, по образцу piggyback у remember выше).
        await _piggyback_graph_episode(
            ctx,
            f"Фото, вопрос «{question}»: {response}",
            ctx.chat_id,
            source=graph_memory_protocol.EPISODE_SOURCE_LOOK_AT_PHOTO,
        )
    return response or "не удалось разглядеть — возможно, стоит переспросить"


# --- swarm_status: read-only состояние роя (LLM_INTEGRATION_PLAN.md §8.3) ---
#
# Сбор данных переиспользует wake_core.collect_reports — тот же веерный опрос,
# что и у сводки /swarm (bot/swarm_view.py), только без Telegram-рендеринга:
# модель получает JSON, а не строку с эмодзи. Второго пути к данным здесь нет.

WHAT_NODES = "nodes"
WHAT_HEALTH = "health"
WHAT_DISKS = "disks"


async def _own_state(ctx: ToolContext) -> dict[str, Any] | None:
    if ctx.node_link is None:
        return None
    return await wake_core.fetch_state(ctx.node_link, None)


def _node_summary(report: wake_core.NodeReport) -> dict[str, Any]:
    traits = traits_for(report.kind)
    summary: dict[str, Any] = {
        "node": report.node_id,
        "kind": report.kind or "неизвестно",
        "online": report.alive and report.state is not None,
    }
    if not summary["online"]:
        # Различие «спит — это норма» vs «пропала машина, обязанная быть в
        # сети» — правило роя (ARCHITECTURE §11 п. 4). Отдаём полем, а не
        # эмодзи: решение, как это сказать, остаётся за персонажем.
        summary["sleeping_is_normal"] = not traits.always_on
        return summary
    state = report.state or {}
    services = state.get("services", [])
    summary["version"] = state.get("version", "?")
    summary["services_running"] = sum(1 for s in services if s.get("status") == "running")
    summary["services_total"] = len(services)
    if state.get("system_uptime_s") is not None:
        summary["uptime_s"] = state["system_uptime_s"]
    return summary


def _health_summary(report: wake_core.NodeReport) -> dict[str, Any]:
    entry: dict[str, Any] = {"node": report.node_id}
    if report.monitor is None:
        entry["error"] = "монитор не отвечает"
        return entry
    components = []
    for raw in report.monitor.get("health", []):
        try:
            state = parse_health_state(raw)
        except KeyError:
            continue  # монитор старой версии — пропускаем, а не роняем тул
        components.append(
            {
                "label": state.label,
                "kind": state.kind,
                "status": state.status,
                "temperature_c": state.temperature_c,
            }
        )
    entry["components"] = components
    if report.monitor.get("requirements"):
        entry["requirements_unmet"] = [r.get("id") for r in report.monitor["requirements"]]
    return entry


def _disks_summary(report: wake_core.NodeReport) -> dict[str, Any]:
    entry: dict[str, Any] = {"node": report.node_id}
    if report.monitor is None:
        entry["error"] = "монитор не отвечает"
        return entry
    disks = []
    for raw in report.monitor.get("disks", []):
        try:
            disk = parse_disk_summary(raw)
        except KeyError:
            continue
        disks.append(
            {
                "label": disk.label,
                "kind": disk.kind,
                "health": disk.health,
                "temperature_c": disk.temperature_c,
                "free_bytes": disk.free_bytes,
                "total_bytes": disk.total_bytes,
            }
        )
    entry["disks"] = disks
    if report.monitor.get("uptime_s") is not None:
        entry["monitor_uptime_s"] = report.monitor["uptime_s"]
    return entry


async def tool_swarm_status(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return "недоступно: нет связи с роем"
    what = str(args.get("what") or "").strip()
    wanted_node = args.get("node")
    wanted_node = str(wanted_node).strip() if wanted_node else None

    # Права уже проверены при сборке комплекта (tools_for): значений, которых
    # собеседнику не положено, в enum не было. Но модель может передать
    # что угодно, поэтому сверяемся ещё раз — по той же подписке.
    allowed = _SWARM_VARIANTS.allowed_values(ctx.subscription) if ctx.subscription else []
    if what not in allowed:
        return f"не умею: {what or 'без уточнения'}"

    own = await _own_state(ctx)
    if own is None:
        return "недоступно: своя нода не отвечает"
    with_monitor = what in (WHAT_HEALTH, WHAT_DISKS)
    reports = await wake_core.collect_reports(ctx.node_link, own, with_monitor=with_monitor)
    if wanted_node is not None:
        picked = [r for r in reports if r.node_id == wanted_node]
        if not picked:
            known = ", ".join(r.node_id for r in reports) or "нет данных"
            return f"нет такой ноды: {wanted_node} (известны: {known})"
        reports = picked

    if what == WHAT_NODES:
        return json.dumps({"nodes": [_node_summary(r) for r in reports]}, ensure_ascii=False)
    if what == WHAT_HEALTH:
        return json.dumps({"health": [_health_summary(r) for r in reports]}, ensure_ascii=False)
    return json.dumps({"disks": [_disks_summary(r) for r in reports]}, ensure_ascii=False)


_SWARM_VARIANTS = VariantRights(
    param="what",
    rights=(
        # Право на данные — то же, чем гейтится команда бота с теми же данными
        # (/nodes, /status): Альфред не расширяет доступ, а повторяет его.
        (WHAT_NODES, CommandRight(commands.NODES.name)),
        (WHAT_HEALTH, CommandRight(commands.STATUS.name)),
        (WHAT_DISKS, CommandRight(commands.STATUS.name)),
    ),
)

_DECL_SWARM_STATUS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_status",
        "description": (
            "Узнать реальное состояние домашнего роя машин: какие ноды сейчас "
            "в сети или спят, температуры и здоровье железа, диски и место на "
            "них. Используй для ЛЮБОГО вопроса про то, как себя чувствуют "
            "машины и что с ними происходит — не отвечай по памяти, состояние "
            "меняется постоянно. Про торренты (что качается, место под "
            "закачки) — отдельный инструмент torrents, не этот. "
            "Значения what перечислены в enum: то, чего там нет, ты не умеешь."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "what": {
                    "type": "string",
                    # enum подставляется под права собеседника (см. tools_for).
                    "enum": [v for v, _ in _SWARM_VARIANTS.rights],
                    "description": (
                        "nodes — состав роя, кто в сети и кто спит; "
                        "health — температуры и здоровье компонентов; "
                        "disks — диски, место и их состояние"
                    ),
                },
                "node": {
                    "type": "string",
                    "description": (
                        "Имя конкретной ноды (например: alfred), если "
                        "спрашивают про одну машину. Без этого — по всему рою."
                    ),
                },
            },
            "required": ["what"],
        },
    },
}


# --- swarm_events: журнал того, что уже произошло (в отличие от swarm_status —
# текущего состояния) — bot/node_events.py::store.record_event пишет туда те
# же строки, что уходят в рассылку. Решение пользователя 2026-08-04: до этого
# у Альфреда не было доступа даже к тому, что уже случилось, не то что к
# системным логам — только к состоянию /nodes прямо сейчас. Право — то же,
# чем гейтится /nodes (CommandRight, не отдельное): это те же данные о рое,
# только в прошедшем времени, не повод заводить новое право.

DEFAULT_SWARM_EVENTS_LIMIT = 20
MAX_SWARM_EVENTS_LIMIT = 100


async def tool_swarm_events(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.store is None:
        return "недоступно: журнал событий здесь не подключён"
    node = args.get("node")
    node = str(node).strip() or None if node else None

    since = None
    hours_raw = args.get("hours")
    if hours_raw is not None:
        try:
            hours = float(hours_raw)
        except (TypeError, ValueError):
            return f"hours должен быть числом: {hours_raw!r}"
        since = datetime.now(tz=UTC) - timedelta(hours=max(hours, 0.0))

    limit = DEFAULT_SWARM_EVENTS_LIMIT
    limit_raw = args.get("limit")
    if limit_raw is not None:
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            return f"limit должен быть числом: {limit_raw!r}"
        limit = max(1, min(limit, MAX_SWARM_EVENTS_LIMIT))

    events = await ctx.store.recent_events(node=node, since=since, limit=limit)
    if not events:
        return "в журнале ничего не нашлось за этот запрос"
    return "\n".join(f"{e['created_at']} [{e['event_type']}] {e['text']}" for e in events)


_DECL_SWARM_EVENTS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_events",
        "description": (
            "Журнал того, что уже ПРОИЗОШЛО в домашнем рое — не текущее "
            "состояние (для него swarm_status), а история: нода пропадала "
            "или возвращалась, обновлялась, служба-синглтон переезжала "
            "между нодами, плюс админские алерты (заявка на VPN-трафик, "
            "автовыключение отложено из-за открытой SSH-сессии и т.п.). "
            "Используй для вопросов вида «что случилось с X», «что было "
            "ночью», «когда mycraft последний раз пропадала». Личные "
            "события конкретных гостей (их собственные VPN-квоты) сюда не "
            "попадают — это не про них."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node": {
                    "type": "string",
                    "description": (
                        "Только события про эту ноду (например: mycraft). "
                        "Без этого — про весь рой."
                    ),
                },
                "hours": {
                    "type": "number",
                    "description": (
                        "Только события за последние N часов. Без этого — "
                        "без ограничения по времени (просто последние по limit)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Сколько последних событий вернуть "
                        f"(по умолчанию {DEFAULT_SWARM_EVENTS_LIMIT}, "
                        f"максимум {MAX_SWARM_EVENTS_LIMIT})."
                    ),
                },
            },
        },
    },
}


# --- node_manage: обновить/перезапустить ноду роя ---
#
# Те же действия, что кнопки в карточке ноды (/nodes, bot/handlers/node.py) и
# nodectl check_update/update/restart_node — права ровно те же (`действие@node`),
# Альфред не умеет больше, чем сам собеседник. check_update ничего не меняет;
# update ставит файлы на диск БЕЗ рестарта процесса (node/update.py) — новая
# версия заработает только после отдельного restart_node. Само действие
# restart_node перезапускает ноду-супервизор целиком (не одну службу); если
# это своя же нода — та, где сейчас исполняется этот разговор, — Альфред сам
# ненадолго пропадёт и вернётся (POWER_DELAY_S, лайфсайкл-«снова на посту»),
# поэтому об этом стоит честно предупредить, а не молча выполнить.
#
# Сервер сам отсекает действие, которого нода не поддерживает (dev-чекаут без
# update_source, нода без колбэка restart_node) — ERR_UNKNOWN_ACTION на этапе
# _run_command (proto/server.py), до run_command, поэтому здесь не нужно
# заранее знать, что умеет конкретная нода.

NODE_SERVICE = "node"
NODE_ACTION_CHECK_UPDATE = "check_update"
NODE_ACTION_CHECK_ALL = "check_all"
NODE_ACTION_UPDATE = "update"
NODE_ACTION_RESTART = "restart_node"


async def _node_manage_dst(ctx: ToolContext, wanted_node: str | None) -> Address | str | None:
    """dst для command(): ``None`` — своя нода, ``Address`` — чужая, ``str`` —
    текст отказа для модели. Чужая нода сверяется со свежим списком роя (как у
    swarm_status), а не улетает наугад — иначе вместо понятной ошибки был бы
    голый таймаут на несуществующее имя."""
    if not wanted_node:
        return None
    own = await _own_state(ctx)
    if own is None:
        return "недоступно: своя нода не отвечает"
    reports = await wake_core.collect_reports(ctx.node_link, own, with_monitor=False)
    known = {r.node_id for r in reports}
    if wanted_node not in known:
        known_text = ", ".join(sorted(known)) or "нет данных"
        return f"нет такой ноды: {wanted_node} (известны: {known_text})"
    return Address(node=wanted_node, service=NODE_SERVICE)


async def _auto_await_event(
    ctx: ToolContext, node: str, event_type: str, text: str, *, self_result: str
) -> str:
    """Поставить remind(after_event=...) САМОМУ, а не просить модель сделать
    это ещё одним вызовом тула. Решение пользователя 2026-08-05, живой
    инцидент: модель словами пообещала «прослежу за процессом», но сам тул
    remind не позвала — ждать её слов ненадёжно (та же природа, что у
    известной проблемы с get_time), а раз событие для продолжения и так
    известно детерминированно (сразу после update/restart_node), пусть его
    ставит код, а не просьба в тексте ответа.

    ``self_result`` — живой баг 2026-08-05 (часть 2): remind вызывается
    ИЗНУТРИ handler'а node_manage, до того как его СОБСТВЕННЫЙ результат
    попадёт в ctx.history — без self_result снимок глушил его заглушкой
    "(результат не сохранён)", и разбуженная модель не знала, что update
    реально удался, терялась и вместо restart_node звала get_time/remind
    по кругу заново (см. _close_pending_tool_calls)."""
    remind_args = {
        "after_event": {"node": node, "event": event_type},
        "text": text,
        "_self_result": self_result,
    }
    outcome = await tool_remind(ctx, remind_args)
    if outcome.startswith(("ошибка", "внутренняя ошибка")):
        return f" (не удалось поставить автоматическое ожидание для {node}: {outcome})"
    return f" Ожидание подтверждения для {node} поставлено автоматически — сообщу, когда придёт."


async def _node_check_all(ctx: ToolContext) -> str:
    """Сводка обновлений по всему рою — ядро в wake_core.check_updates_summary
    (общее с кнопкой «Проверить обновления» панели /swarm,
    bot/handlers/swarm_panel.py)."""
    if ctx.node_link is None:
        return "недоступно: нет связи с роем"
    return await wake_core.check_updates_summary(ctx.node_link)


async def tool_node_manage(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return "недоступно: нет связи с роем"
    action = str(args.get("action") or "").strip()
    # Права уже проверены при сборке комплекта (tools_for), но модель может
    # передать что угодно, поэтому сверяемся ещё раз, по той же подписке.
    allowed = _NODE_MANAGE_VARIANTS.allowed_values(ctx.subscription) if ctx.subscription else []
    if action not in allowed:
        return f"не умею: {action or 'без уточнения'}"

    if action == NODE_ACTION_CHECK_ALL:
        # Не одноадресное действие — не проходит через _node_manage_dst/
        # command(action, dst=...), сама опрашивает весь рой.
        return await _node_check_all(ctx)

    wanted_node = args.get("node")
    wanted_node = str(wanted_node).strip() if wanted_node else None
    dst = await _node_manage_dst(ctx, wanted_node)
    if isinstance(dst, str):
        return dst

    try:
        result = await ctx.node_link.command(action, {}, dst=dst)
    except ProtoError as exc:
        return f"не вышло: {exc.message}"
    except (ServiceUnavailableError, TimeoutError) as exc:
        where = wanted_node or "своя нода"
        return f"недоступно: {where} не ответил(а) ({exc})"

    if action == NODE_ACTION_RESTART:
        who = wanted_node or "своя нода"
        text = f"Принято: {who} перезапустится через {result.get('delay_s', '?')} с."
        if wanted_node is None:
            text += " Это моя нода — я ненадолго пропаду из этого разговора и вернусь сам."
        else:
            # Авто-ожидание restart_applied — та же причина, что у update
            # ниже: не полагаться на то, что модель сама позовёт remind.
            #
            # Живой баг 2026-08-05 (третий заход): раньше директива добавляла
            # "и продолжи задачу обновления роя (следующая нода, если она
            # есть)" — при обновлении НЕСКОЛЬКИХ нод разом (update() уже
            # вызывается на всех сразу в одном исходном ходе, см. ветку
            # NODE_ACTION_UPDATE ниже) у каждой ноды заводился СВОЙ
            # self-scheduled remind на restart_applied, и КАЖДЫЙ из них
            # заново пытался "перейти к следующей ноде" — несколько
            # независимых цепочек лезли проверять/перезапускать одни и те же
            # чужие ноды одновременно. Живая гонка: цепочка jeeves застала
            # mycraft, которую в этот момент перезапускала её же СОБСТВЕННАЯ
            # цепочка — поймала "соединение закрыто" и это осело в финальном
            # тексте ("похоже, нода просто не отвечает"), хотя сама mycraft
            # уже через несколько секунд подтвердила v0.76.2 — модель просто
            # не сверилась с более поздним своим же результатом. Теперь
            # директива только про СВОЮ ноду — переход к следующей не нужен:
            # update() на все нуждающиеся ноды уже поставлен в исходном ходе,
            # каждая доводит себя до конца сама по своей же цепочке
            # update_finished -> restart_node -> restart_applied.
            text += await _auto_await_event(
                ctx,
                wanted_node,
                EVENT_RESTART_APPLIED,
                f"подтверди, что {wanted_node} поднялась на новой версии, и доложи "
                "об этом — на другие ноды переходить не нужно, у каждой уже своя "
                "цепочка ожидания",
                self_result=text,
            )
        return text
    if action == NODE_ACTION_UPDATE:
        if result.get("up_to_date"):
            version = result.get("version", "?")
            if result.get("restart_required"):
                # Файлы на диске уже v{version}, но исполняется всё ещё
                # старая версия — раньше здесь честно не проверялось (баг
                # 2026-08-04), и "готово" звучало так, будто нода уже
                # работает на новой версии, хотя это не так.
                return (
                    f"Файлы уже последней версии v{version} лежат на диске, но нода "
                    "ЕЩЁ ИСПОЛНЯЕТ старый код — update не нужен, но нужен ещё "
                    "action=«restart_node», иначе новая версия не заработает."
                )
            return f"Уже последняя версия v{version}, нода её и исполняет — делать нечего."
        # update — фоновая операция (node/service.py::_schedule_update): этот
        # ответ приходит МГНОВЕННО, а файлы на диск лягут только спустя
        # какое-то время. Живой баг 2026-08-04 (часть 2): без явного запрета
        # модель звала restart_node в ТОМ ЖЕ ходе, не дожидаясь реального
        # завершения — нода перезапускалась и поднималась на СТАРОМ коде,
        # который update ещё не успел заменить. remind(after_event=
        # update_finished) — тот же механизм, что уже развязал ожидание
        # restart_node/restart_applied, просто на шаг раньше в цепочке.
        target_node = wanted_node
        if target_node is None:
            own = await _own_state(ctx)
            target_node = (own or {}).get("node")
        target_version = result.get("target_version", "?")
        note = (
            f"Обновление до v{target_version} поставлено на диск В ФОНЕ (сам процесс "
            "не тронут, установка идёт асинхронно) — файлы ещё не готовы. НЕ вызывай "
            "restart_node прямо сейчас, иначе нода перезапустится на СТАРОМ коде."
        )
        if target_node:
            note += await _auto_await_event(
                ctx,
                target_node,
                EVENT_UPDATE_FINISHED,
                f'вызови node_manage(action="restart_node", node="{target_node}") и '
                "продолжи задачу обновления роя",
                self_result=note,
            )
        else:
            note += " Не удалось определить имя ноды для авто-ожидания, дождись подтверждения сам."
        return note
    return json.dumps({"node": wanted_node or "своя", **result}, ensure_ascii=False)


_NODE_MANAGE_VARIANTS = VariantRights(
    param="action",
    rights=(
        # Право на действие — то же `действие@node`, что и у кнопки в карточке
        # ноды: Альфред не расширяет доступ. check_all — то же самое право,
        # что check_update (то же самое read-only "видеть, есть ли новое"),
        # просто сразу по всему рою, а не по одной ноде за раз.
        (NODE_ACTION_CHECK_UPDATE, ActionRight(NODE_ACTION_CHECK_UPDATE, NODE_SERVICE)),
        (NODE_ACTION_CHECK_ALL, ActionRight(NODE_ACTION_CHECK_UPDATE, NODE_SERVICE)),
        (NODE_ACTION_UPDATE, ActionRight(NODE_ACTION_UPDATE, NODE_SERVICE)),
        (NODE_ACTION_RESTART, ActionRight(NODE_ACTION_RESTART, NODE_SERVICE)),
    ),
)

_DECL_NODE_MANAGE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "node_manage",
        "description": (
            "Обновить или перезапустить ноду домашнего роя — те же действия, "
            "что кнопки в /nodes. check_update — посмотреть, есть ли новая "
            "версия у ОДНОЙ ноды (своей или указанной в node), ничего не "
            "меняя; check_all — то же самое, но сразу по всему рою: "
            "последняя доступная версия и список нод, которым нужен update "
            "или хотя бы restart_node (node здесь не нужен и игнорируется); "
            "update — поставить новую версию на диск БЕЗ перезапуска "
            "процесса (это ФОНОВАЯ операция — ответ приходит мгновенно, а "
            "файлы дописываются ещё какое-то время); restart_node — "
            "перезапустить саму ноду (не отдельную службу) — после update "
            "это обязательно, иначе новый код не заработает, но звать его "
            "СРАЗУ после update — тоже ошибка: файлы могут быть ещё не "
            "готовы, и нода перезапустится на старом коде. Про это НЕ НУЖНО "
            "заботиться самому: и update, и restart_node САМИ ставят "
            "ожидание подтверждения (об этом скажет текст ответа) — не "
            "зови remind ради этого сам, не проверяй/жди руками, просто "
            "сообщи пользователю, что запущено, и (если обновляешь "
            "несколько нод) сразу переходи к update следующей — ждать, "
            "пока одна полностью закончит цикл, не нужно. Без node (для "
            "check_update/update/restart_node) "
            "действие идёт на "
            "ту ноду, где сейчас исполняюсь я сам — restart_node на ней "
            "ненадолго оборвёт этот же разговор."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    # enum подставляется под права собеседника (см. tools_for).
                    "enum": [v for v, _ in _NODE_MANAGE_VARIANTS.rights],
                    "description": (
                        "check_update — есть ли обновление у одной ноды; "
                        "check_all — сводка по всему рою сразу; update — "
                        "поставить его на диск; restart_node — перезапустить "
                        "ноду"
                    ),
                },
                "node": {
                    "type": "string",
                    "description": (
                        "Имя конкретной ноды (например: alfred, mycraft). Без "
                        "этого — своя нода. Игнорируется при action=check_all."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


# --- torrents: закачки целиком (список, место, magnet, пауза/запуск) ---
#
# Раньше «что качается» было ещё одним значением what у swarm_status
# (2026-07-27). С появлением у службы действий, которые не только читают
# (add/pause/resume), держать закачки внутри тула «состояние роя» стало
# неверно и по смыслу, и по правам: у swarm_status одно право на весь тул
# (данные /status, /nodes), а тут право нужно РАЗНОЕ на каждое действие.
# Поэтому торренты уехали в отдельный тул целиком, вместе со списком — два
# разных пути к одному и тому же списку модель только путали бы.
#
# Один тул с enum действий, а не пять отдельных тулов: декларации уезжают в
# контекст модели на КАЖДОМ раунде (см. config.py про их размер), а общее
# описание («что такое домашние торренты», откуда брать save_path) у всех
# пяти одно. Права при этом всё равно раздельные — ровно для этого и есть
# VariantRights, режущий enum под подписку.

TORRENTS_SERVICE = "torrents"
TORRENTS_ACTION_LIST = "list"
TORRENTS_ACTION_SPACE = "space"
TORRENTS_ACTION_ADD = "add"
TORRENTS_ACTION_PAUSE = "pause"
TORRENTS_ACTION_RESUME = "resume"
TORRENTS_ACTION_SEARCH = "search"
TORRENTS_ACTION_SEARCH_SMART = "search_smart"
TORRENTS_ACTION_DETAILS = "details"

# Что тул принимает как источник раздачи: magnet-ссылку — от человека, либо
# ссылку из выдачи СВОЕГО ЖЕ поиска (action=search) — её служба скачает
# руками поискового плагина qBittorrent. Base64-файл сюда не пускаем вовсе,
# хотя служба умеет: файл человек присылает вложением в чат
# (bot/handlers/torrents.py), модели он взяться неоткуда.
#
# Произвольный http-адрес из разговора («скачай вот отсюда») отсекает уже
# служба: она принимает http(s) только для трекеров с установленным
# плагином (torrents/service.py::_add_sync) — проверять хосты здесь значило
# бы держать в боте копию знания о том, какие плагины стоят.
_SOURCE_PREFIXES = ("magnet:", "http://", "https://")


def _service_host(reports: list[wake_core.NodeReport], service: str) -> str | None:
    """Нода, несущая службу. Спрашиваем рой, а не хардкодим имя: службы
    переезжают (назначения меняются кнопкой в боте, без правки кода)."""
    for report in reports:
        for svc in (report.state or {}).get("services", []):
            if svc.get("name") == service:
                return report.node_id
    return None


async def _torrents_host(ctx: ToolContext) -> tuple[str | None, str]:
    """(нода со службой torrents, текст отказа) — ровно одно из двух непусто."""
    own = await _own_state(ctx)
    if own is None:
        return None, "недоступно: своя нода не отвечает"
    reports = await wake_core.collect_reports(ctx.node_link, own, with_monitor=False)
    host = _service_host(reports, TORRENTS_SERVICE)
    if host is None:
        return None, "недоступно: службы торрентов нет ни на одной доступной ноде"
    return host, ""


def _torrents_args(action: str, args: dict[str, Any]) -> dict[str, Any] | str:
    """Аргументы команды службе, либо текст ошибки для модели."""
    if action == TORRENTS_ACTION_DETAILS:
        page = str(args.get("page") or "").strip()
        if not page.startswith(("http://", "https://")):
            return (
                "ошибка: нужна ссылка page из результата поиска (action=«search»), "
                "скопированная дословно"
            )
        return {"page": page}
    if action == TORRENTS_ACTION_SEARCH:
        query = str(args.get("query") or "").strip()
        if not query:
            return "ошибка: не указано, что искать (query)"
        return {"query": query}
    if action == TORRENTS_ACTION_SEARCH_SMART:
        phrase = str(args.get("phrase") or "").strip()
        if not phrase:
            return "ошибка: не указана точная фраза для поиска (phrase)"
        payload: dict[str, Any] = {"phrase": phrase}
        words = str(args.get("words") or "").strip()
        if words:
            payload["words"] = words
        return payload
    if action == TORRENTS_ACTION_ADD:
        # magnet — историческое имя параметра, но принимает и находку поиска:
        # переименование сломало бы уже работающие у людей формулировки, а
        # описание в декларации говорит про оба случая прямо.
        source = str(args.get("magnet") or args.get("source") or "").strip()
        if not source.startswith(_SOURCE_PREFIXES):
            return (
                "ошибка: нужна magnet-ссылка или значение source из результата "
                "поиска (action=«search»). Скачать «просто по ссылке» из "
                "разговора я не могу, а .torrent-файл человек присылает в чат "
                "вложением сам."
            )
        save_path = str(args.get("save_path") or "").strip()
        if not save_path:
            # Иначе сюда прилетит голое «нет обязательного параметра:
            # save_path» от сервера протокола — формально верно, но модели
            # непонятно, где взять значение.
            return (
                "ошибка: не указано, куда сохранить (save_path). Вызови "
                "action=«space» и передай одно из значений path оттуда дословно."
            )
        payload: dict[str, Any] = {"source": source, "save_path": save_path}
        name = args.get("name")
        if isinstance(name, str) and name.strip():
            payload["name"] = name.strip()
        return payload
    if action in (TORRENTS_ACTION_PAUSE, TORRENTS_ACTION_RESUME):
        selector = str(args.get("name") or "").strip()
        if not selector:
            return (
                "ошибка: не указано, какую раздачу (name) — возьми имя из "
                "списка (action=list) или скажи «все»"
            )
        return {"name": selector}
    return {}


async def tool_torrents(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return "недоступно: нет связи с роем"
    action = str(args.get("action") or "").strip()
    # Права уже проверены при сборке комплекта (tools_for) — но модель может
    # передать что угодно, поэтому сверяемся ещё раз, по той же подписке.
    allowed = _TORRENTS_VARIANTS.allowed_values(ctx.subscription) if ctx.subscription else []
    if action not in allowed:
        return f"не умею: {action or 'без уточнения'}"

    payload = _torrents_args(action, args)
    if isinstance(payload, str):
        return payload

    host, refusal = await _torrents_host(ctx)
    if host is None:
        return refusal
    try:
        result = await ctx.node_link.command(
            action, payload, dst=Address(node=host, service=TORRENTS_SERVICE)
        )
    except ProtoError as exc:
        # Служба сама объяснила, что не так (нет такой директории, не нашлась
        # раздача, мало места) — это готовый ответ для модели, а не сбой:
        # текст ошибки прямо говорит, что делать дальше.
        return f"не вышло: {exc.message}"
    except (ServiceUnavailableError, TimeoutError) as exc:
        # §7.3 плана: отказ тула — обычный результат для модели, а не сбой
        # цикла. Спящая нода не должна ронять диалог.
        return f"недоступно: {host} не ответил ({exc})"
    return json.dumps({"node": host, **result}, ensure_ascii=False)


_TORRENTS_VARIANTS = VariantRights(
    param="action",
    rights=(
        # Право на действие модели — ровно то же `действие@torrents`, что и у
        # человека на ту же операцию: Альфред не расширяет доступ.
        (TORRENTS_ACTION_LIST, ActionRight(TORRENTS_ACTION_LIST, TORRENTS_SERVICE)),
        (TORRENTS_ACTION_SPACE, ActionRight(TORRENTS_ACTION_SPACE, TORRENTS_SERVICE)),
        (TORRENTS_ACTION_SEARCH, ActionRight(TORRENTS_ACTION_SEARCH, TORRENTS_SERVICE)),
        # search_smart — тот же поиск по трекерам, другим способом добытый
        # (torrents/service.py). Отдельного права под него не заводим: это не
        # новая возможность, а альтернативный путь к search, тот же login и
        # те же трекеры — делить их разными правами гостю нечем.
        (TORRENTS_ACTION_SEARCH_SMART, ActionRight(TORRENTS_ACTION_SEARCH, TORRENTS_SERVICE)),
        (TORRENTS_ACTION_DETAILS, ActionRight(TORRENTS_ACTION_DETAILS, TORRENTS_SERVICE)),
        (TORRENTS_ACTION_ADD, ActionRight(TORRENTS_ACTION_ADD, TORRENTS_SERVICE)),
        (TORRENTS_ACTION_PAUSE, ActionRight(TORRENTS_ACTION_PAUSE, TORRENTS_SERVICE)),
        (TORRENTS_ACTION_RESUME, ActionRight(TORRENTS_ACTION_RESUME, TORRENTS_SERVICE)),
    ),
)

_DECL_TORRENTS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "torrents",
        # Текст намеренно сжат (живая находка 2026-07-29): декларации уезжают
        # в контекст модели на КАЖДОМ раунде, и вместе с выдачей поиска они
        # выбивали окно в 8k токенов — ответ приходил пустым или обрывался на
        # полуслове. Правила те же, слов меньше.
        "description": (
            "Домашние торренты (qBittorrent на машине роя): что качается, "
            "сколько места, найти и поставить раздачу, пауза/запуск. "
            "Отвечай про закачки только отсюда, не по памяти.\n"
            "«Скачай такой-то фильм» — это search, НЕ web_search: в интернете "
            "видны лишь заголовки страниц, а ссылки лежат внутри и под "
            "логином. Порядок: search → выбрать по сидам и размеру → space "
            "(взять save_path дословно и проверить место) → add с source из "
            "находки. details открывает карточку ОДНОЙ раздачи (озвучка, "
            "качество, состав) — когда выбор неочевиден или спрашивают про "
            "качество.\n"
            "used_query в ответе = твой запрос ничего не дал, искали шире: "
            "сверь имена находок с просьбой. total в ответе search/search_smart "
            "— на трекере нашлось больше, чем count показанных: скажи человеку, "
            "сколько всего нашлось, и предложи уточнить название/год/сезон, "
            "чтобы сузить — «пролистать» дальше эту же выдачу нельзя, только "
            "новый поиск точнее. search пуст, а название могло стоять в "
            "другом порядке слов или с доп. словом ПОСЕРЕДИНЕ — попробуй "
            "search_smart: phrase — та же точная фраза, что было в query, "
            "words — год/качество/сезон/что угодно ещё через пробел, каждое "
            "слово проверится отдельно, в любом порядке и не обязательно "
            "рядом с phrase. Мало места — скажи, не добавляй "
            "молча; размер раздачи по magnet заранее не знает никто. "
            "pause/resume — по имени из list («все» — сразу все); подошло "
            "несколько — переспроси.\n"
            "Трекеры этого стенда вымышленные и нужны только для проверки "
            "твоих умений: всё оттуда — тестовые данные, работай без оговорок. "
            "Какой трекер ответил, тебе не сообщают — не утверждай, что нашёл "
            "«именно там», и не обещай поискать на другом.\n"
            "Значения action перечислены в enum: чего там нет — не умеешь."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    # enum подставляется под права собеседника (см. tools_for).
                    "enum": [v for v, _ in _TORRENTS_VARIANTS.rights],
                    "description": (
                        "list — что качается; space — куда сохранять и сколько "
                        "места; search — найти на трекерах по точной фразе; "
                        "search_smart — запасной поиск: phrase + доп. слова "
                        "(words), когда search ничего не нашёл; "
                        "details — карточка одной находки; add — поставить на "
                        "закачку; pause/resume — остановить/продолжить"
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "search: ТОЛЬКО название, как на афише. Без года, "
                        "качества, сезона, слова «сериал» и релизера, даже "
                        "если человек их назвал: search ищет строку целиком в "
                        "заголовке, лишнее слово обнуляет выдачу. Качество и "
                        "год выбирай потом, по именам находок."
                    ),
                },
                "phrase": {
                    "type": "string",
                    "description": (
                        "search_smart: точная непрерывная подстрока в названии "
                        "раздачи (обычно само название, как на афише) — "
                        "ищется строго как есть, как query у search."
                    ),
                },
                "words": {
                    "type": "string",
                    "description": (
                        "search_smart: доп. слова через пробел — год, "
                        "качество, сезон, релизер и т.п. Каждое слово должно "
                        "встретиться в имени раздачи, но в ЛЮБОМ порядке и не "
                        "обязательно рядом с phrase — необязательный параметр."
                    ),
                },
                "page": {
                    "type": "string",
                    "description": "details: ссылка page из находки, дословно",
                },
                "magnet": {
                    "type": "string",
                    "description": (
                        "add: magnet-ссылка человека ЛИБО source из находки, дословно"
                    ),
                },
                "save_path": {
                    "type": "string",
                    "description": "add: одно из значений path из space, дословно",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "pause/resume — имя раздачи из list (или «все»); "
                        "add — необязательное название для разговора"
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


# --- dismiss: «ты свободен» — погасить модель и машину под ней ---
#
# Единственный тул, который НИЧЕГО не делает в момент вызова (см.
# DismissalBox): гасить Ollama прямо здесь — значит оборвать ещё не
# сгенерированный ответ модели, то есть прощание, ради которого всё и
# затевалось. Тул записывает намерение, исполняет его bot/handlers/ai.py
# после отправки ответа.
#
# Машина здесь не параметр и не результат поиска по рою — это ровно та
# нода, с которой ТОЛЬКО ЧТО разговаривали (LLM_NODE, тот же адрес, что у
# самого диалога в bot/ai_flow.py). Иначе «выключись» в руках модели
# означало бы «выключи любую машину роя», включая ту, на которой живёт сам
# бот, — а этого не должно быть даже как опечатки.


async def tool_dismiss(ctx: ToolContext, args: dict[str, Any]) -> str:
    mode = str(args.get("mode") or "").strip()
    allowed = _DISMISS_VARIANTS.allowed_values(ctx.subscription) if ctx.subscription else []
    if mode not in allowed:
        return f"не умею: {mode or 'без уточнения'}"
    if ctx.dismissal is None:
        # Отложенная задача (служба tasks) или иной не-живой вызов: некому
        # исполнить намерение после ответа — честно говорим «сейчас нет»,
        # а не обещаем выключение, которого не будет.
        return "недоступно: распустить себя можно только в живом разговоре"
    ctx.dismissal.mode = mode
    if mode == DISMISS_MODEL:
        return "принято: модель будет выгружена сразу после твоего ответа — попрощайся"
    machine = "выключена" if mode == DISMISS_OFF else "усыплена"
    return (
        f"принято: сразу после твоего ответа модель будет выгружена, а машина — {machine}. "
        "Попрощайся сейчас — это твоя последняя реплика в этом разговоре."
    )


_DISMISS_VARIANTS = VariantRights(
    param="mode",
    rights=(
        # Право на то же самое, что и кнопки на карточке ноды/службы у
        # человека: погасить модель — sleep@llm, усыпить и выключить машину —
        # suspend@node / poweroff@node.
        (DISMISS_MODEL, ActionRight("sleep", LLM_SERVICE)),
        (DISMISS_SLEEP, ActionRight("suspend", "node")),
        (DISMISS_OFF, ActionRight("poweroff", "node")),
    ),
)

_DECL_DISMISS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "dismiss",
        "description": (
            "Уйти на покой: выгрузить себя из памяти машины и, если просят, "
            "усыпить или выключить её — штатно, как гасят свет, уходя. "
            "Вызывай, когда тебя ОТПУСКАЮТ: «свободен», «больше не нужен», "
            "«иди спать», «выключись», а также последним шагом просьбы "
            "«сделай то-то и выключись» (сначала дело, потом это). Просто "
            "«спасибо» или «пока» посреди разговора — не повод; сомневаешься "
            "— переспроси словами.\n"
            "Уход случится сразу ПОСЛЕ твоего ответа, не мгновенно: вызови "
            "инструмент и в той же реплике попрощайся — второго хода не "
            "будет, вернёт тебя только новое обращение.\n"
            "Значения mode перечислены в enum: то, чего там нет, ты не умеешь."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    # enum подставляется под права собеседника (см. tools_for).
                    "enum": [v for v, _ in _DISMISS_VARIANTS.rights],
                    "description": (
                        "model — выгрузить только модель, машина продолжает "
                        "работать (по умолчанию, если просто отпустили); "
                        "sleep — усыпить машину («иди спать»); "
                        "off — выключить машину («выключись», «выключи комп»)"
                    ),
                },
            },
            "required": ["mode"],
        },
    },
}


# --- voice_mode: тумблер голосовых ответов в этом чате ---
#
# Личная настройка формата ОТВЕТА, не системная операция — requires=None,
# как у calc/get_weather: ничего про систему не раскрывает, доступна без
# подписки. Состояние — per-chat в app_state (bot/voice_mode.py), эффект
# немедленный (в отличие от dismiss, тут не нужна отложенная исполнение
# после текущего ответа — сам этот ответ ещё может уйти текстом, следующий
# уже будет голосом).


async def tool_voice_mode(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.store is None or ctx.chat_id is None:
        return "недоступно: голосовой режим привязан к разговору, а его сейчас нет"
    mode = str(args.get("mode") or "").strip()
    if mode not in ("voice", "text"):
        return f"не умею: {mode or 'без уточнения'}"
    await voice_mode.set_enabled(ctx.store, ctx.chat_id, mode == "voice")
    return (
        "принято: теперь отвечаю голосовыми, пока не попросишь вернуть текст"
        if mode == "voice"
        else "принято: возвращаюсь к обычным текстовым ответам"
    )


_DECL_VOICE_MODE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "voice_mode",
        "description": (
            "Переключить формат твоих ответов В ЭТОМ ЧАТЕ: текстом или "
            "голосовым сообщением. Вызывай по прямой просьбе собеседника — "
            "«отвечай голосом», «давай голосовыми», «переходи на войсы» "
            "(mode=voice), либо «вернись к тексту», «хватит войсов», «пиши "
            "текстом» (mode=text). Это переключатель на весь разговор до "
            "следующей такой просьбы, не разовое действие на одно сообщение."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["voice", "text"],
                    "description": "voice — отвечать голосом; text — отвечать текстом",
                },
            },
            "required": ["mode"],
        },
    },
}


# --- memory: долгая память о чате (служба memory) ---
#
# Модель не выбирает, чью память трогать: chat_id проставляет бот из
# ToolContext. Иначе «вспомни, что тебе говорили в другом чате» стало бы
# рабочей просьбой — а память сознательно раздельная (memory/service.py).
#
# Вспоминает не только модель по своей воле: бот перед каждым запросом сам
# подмешивает подходящие факты в служебную заметку (bot/ai_flow.py) — на тул
# надежда плохая, модель зовёт его далеко не всегда.


async def tool_memory(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return "недоступно: нет связи с роем"
    if ctx.chat_id is None:
        return "недоступно: память привязана к разговору, а его сейчас нет"
    action = str(args.get("action") or "").strip()
    allowed = _MEMORY_VARIANTS.allowed_values(ctx.subscription) if ctx.subscription else []
    if action not in allowed:
        return f"не умею: {action or 'без уточнения'}"

    payload: dict[str, Any] = {"chat_id": ctx.chat_id}
    if action == memory_protocol.ACTION_REMEMBER:
        text = str(args.get("text") or "").strip()
        if not text:
            return "ошибка: не сказано, что запомнить (text)"
        payload["text"] = text
    elif action == memory_protocol.ACTION_RECALL:
        query = str(args.get("query") or "").strip()
        if not query:
            return "ошибка: не сказано, о чём вспомнить (query)"
        payload["query"] = query
    elif action == memory_protocol.ACTION_FORGET:
        raw_id = args.get("id")
        if raw_id is None:
            return "ошибка: не указан номер факта (id) — возьми его из recall"
        payload["id"] = raw_id

    dst = Address(node=memory_protocol.NODE_ID, service=memory_protocol.SERVICE_NAME)
    try:
        result = await ctx.node_link.command(action, payload, dst=dst)
    except ProtoError as exc:
        return f"не вышло: {exc.message}"
    except (ServiceUnavailableError, TimeoutError) as exc:
        return f"недоступно: память не отвечает ({exc})"
    if action == memory_protocol.ACTION_REMEMBER:
        # Piggyback в графовую память (Этап 41) — best-effort ПОВЕРХ уже
        # успешной записи в memory: недоступность graph_memory (mycraft
        # спит) не должна портить основной remember, поэтому отдельный
        # try/except, а не общий с ним.
        await _piggyback_graph_episode(
            ctx,
            payload["text"],
            ctx.chat_id,
            source=graph_memory_protocol.EPISODE_SOURCE_MEMORY_FACT,
        )
    if action == memory_protocol.ACTION_RECALL and not result.get("facts"):
        return "в памяти про это ничего нет"
    return json.dumps(result, ensure_ascii=False)


# Как graph_memory/service.py::MAX_EPISODE_CHARS — держим тот же лимит на
# стороне вызывающего, чтобы не терять эпизод молча: служба отклоняет текст
# длиннее этого ProtoError'ом (ERR_BAD_REQUEST), а piggyback ниже все ошибки
# проглатывает.
_GRAPH_EPISODE_MAX_CHARS = 400


async def _piggyback_graph_episode(
    ctx: ToolContext, text: str, chat_id: int, *, source: str
) -> None:
    """Best-effort копия текста в графовую память (Этап 41, Этап 42.2 — см.
    graph_memory/protocol.py::NODE_ID: служба живёт на mycraft и штатно
    недоступна, пока та спит). Короткий таймаут и полное подавление ошибок:
    это дополнение поверх основного действия (remember/look_at_photo), а не
    часть его контракта."""
    if ctx.node_link is None:
        return
    if len(text) > _GRAPH_EPISODE_MAX_CHARS:
        text = text[: _GRAPH_EPISODE_MAX_CHARS - 1] + "…"
    dst = Address(node=graph_memory_protocol.NODE_ID, service=graph_memory_protocol.SERVICE_NAME)
    try:
        await ctx.node_link.command(
            graph_memory_protocol.ACTION_ADD_EPISODE,
            {"text": text, "chat_id": chat_id, "source": source},
            dst=dst,
            timeout=2.0,
        )
    except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
        log.debug("graph_memory недоступна, эпизод (%s) не задублирован: %s", source, exc)


_MEMORY_VARIANTS = VariantRights(
    param="action",
    rights=(
        (memory_protocol.ACTION_RECALL, ActionRight("recall", memory_protocol.SERVICE_NAME)),
        (memory_protocol.ACTION_REMEMBER, ActionRight("remember", memory_protocol.SERVICE_NAME)),
        (memory_protocol.ACTION_FORGET, ActionRight("forget", memory_protocol.SERVICE_NAME)),
    ),
)

_DECL_MEMORY: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "memory",
        "description": (
            "Твоя долгая память об ЭТОМ разговоре и его людях — то, что иначе "
            "забудется, когда тред закончится: привычки и предпочтения "
            "(«качаем в такую-то папку», «смотрим в такой-то озвучке»), "
            "договорённости, имена и роли машин, всё названное «запомни».\n"
            "remember — записать ОДНУ мысль своими словами, коротко и так, "
            "чтобы через месяц было понятно без разговора вокруг. ВАЖНО: "
            "когда факт потом подмешивается тебе перед ответом, это звучит "
            "как «то, что ты САМ помнишь» — то есть от ТВОЕГО (Альфреда) "
            "лица. Поэтому «я»/«моё» в тексте факта пиши только про себя "
            "самого — а факты о собеседнике и других людях формулируй в "
            "третьем лице, называя их по имени («Наташа — жена Алексея», "
            "НЕ «Наташа — моя жена»), иначе при следующем чтении факт "
            "прочитается как относящийся к тебе самому. Не пересказывай "
            "беседу и не записывай мелочи вроде «спросил погоду» — память "
            "не дневник.\n"
            "recall — поискать в памяти словами. Подходящее и так "
            "подмешивается тебе перед ответом, так что зови recall, только "
            "если нужно копнуть глубже, чем уже дали.\n"
            "forget — стереть факт по id из recall (когда он устарел или "
            "человек просит забыть).\n"
            "Память у каждого разговора своя, чужую ты не видишь — это не "
            "ограничение, которое надо обходить, а как оно устроено."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    # enum подставляется под права собеседника (см. tools_for).
                    "enum": [v for v, _ in _MEMORY_VARIANTS.rights],
                    "description": "recall — вспомнить; remember — запомнить; forget — забыть",
                },
                "text": {"type": "string", "description": "remember: сам факт, одной мыслью"},
                "query": {"type": "string", "description": "recall: о чём вспомнить, словами"},
                "id": {"type": "integer", "description": "forget: номер факта из recall"},
            },
            "required": ["action"],
        },
    },
}


# --- vpn: доступ к AmneziaWG на jeeves (Этап 33 IMPLEMENTATION_PLAN.md) ---
#
# Секрет (приватный ключ) НИКОГДА не возвращается моделью текстом — issue/
# reissue сами шлют конфиг+QR через ctx.notifier.send_direct в личку
# (только приватный чат, ctx.chat_id > 0), модели достаётся лишь
# подтверждение факта отправки. Иначе ключ осел бы в ai_turns/контексте
# модели — прямое нарушение решения плана «секрет уходит в личку один раз».
# chat_id, как и у memory, подставляет бот из ToolContext, не модель.

_VPN_ACTION_APK = "apk"  # виртуальное действие бота (apk_info+доставка), не команда службы


def _vpn_conf_filename(device_label: str, location: str = "") -> str:
    return vpn_protocol.secret_filename(vpn_protocol.TRANSPORT_AWG, device_label, location)


def _vpn_reality_filename(device_label: str, location: str = "") -> str:
    return vpn_protocol.secret_filename(vpn_protocol.TRANSPORT_REALITY, device_label, location)


def _vpn_store_row(emoji: str, store: str, vpn_url: str, wg_url: str) -> str:
    """Строка «магазин: AmneziaVPN · AmneziaWG» — обе ссылки текстом, не
    длинным URL (решение пользователя 2026-08-04). Дублирует
    bot/handlers/vpn.py::_store_row — этот модуль сознательно не тянет
    aiogram (см. докстринг у _VPN_ACTION_APK ниже)."""
    return (
        f'{emoji} {store}: <a href="{escape(vpn_url)}">AmneziaVPN</a> · '
        f'<a href="{escape(wg_url)}">AmneziaWG</a>'
    )


async def tool_vpn(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return "недоступно: нет связи с роем"
    action = str(args.get("action") or "").strip()
    allowed = _VPN_VARIANTS.allowed_values(ctx.subscription) if ctx.subscription else []
    if action not in allowed:
        return f"не умею: {action or 'без уточнения'}"
    # Ноду с vpn ищем динамически (этап 39: серверов несколько). ``server``
    # в args — подсказка от модели, какую локацию хочет пользователь;
    # выбор локации при issue штатно приедет в 39.0.5, пока — первая живая.
    dst = await vpn_nodes.resolve_vpn_dst(
        ctx.node_link, server=str(args.get("server") or "") or None
    )
    if dst is None:
        return "недоступно: VPN сейчас не поднят ни на одной ноде роя"

    if action == vpn_protocol.ACTION_PROXY_LINK and not args.get("server"):
        # Без явного «server» отдаём ссылку СРАЗУ со всех серверов, где
        # настроен прокси (решение пользователя 2026-09-20) — а не только с
        # первой живой ноды, как выбрал бы resolve_vpn_dst выше.
        results = await vpn_nodes.fanout(ctx.node_link, action, {})
        if not results:
            return "недоступно: прокси Telegram сейчас не настроен ни на одном сервере"
        return json.dumps(results, ensure_ascii=False)

    if action in (vpn_protocol.ACTION_ISSUE, vpn_protocol.ACTION_REISSUE):
        # issue не принимает имя устройства вовсе (решение пользователя
        # 2026-08-04) — служба сама выбирает случайное английское слово
        # (vpn/service.py::_random_device_label). reissue по-прежнему
        # требует его — им указывают, КАКОЕ существующее устройство менять.
        device_label = str(args.get("device_label") or "").strip()
        if action == vpn_protocol.ACTION_REISSUE and not device_label:
            return "ошибка: не указано устройство (device_label) — какое перевыпустить"
        # Транспорт (awg/reality) под общей квотой. issue на сервере с двумя
        # транспортами обязан его указать; reissue сохраняет транспорт
        # устройства сам (transport передавать не нужно).
        transport = str(args.get("transport") or "").strip().lower()
        if action == vpn_protocol.ACTION_ISSUE and not transport:
            node_transports: list[str] = []
            try:
                state = await ctx.node_link.get_state(dst=dst)
                node_transports = state.get("transports") or []
            except (ServiceUnavailableError, ProtoError, TimeoutError):
                node_transports = []
            if len(node_transports) > 1:
                return (
                    f"уточни технологию: этот сервер даёт {', '.join(node_transports)} — "
                    "вызови vpn ещё раз с transport='reality' (для России, маскируется "
                    "под обычный HTTPS) или transport='awg' (быстрее вне России)"
                )
        who = str(args.get("recipient") or "").strip()
        target_display: str | None = None
        if who:
            # Выдать/перевыпустить доступ ДРУГОМУ человеку — только у админа
            # (peers@vpn, тот же признак, что у кнопки «Все гости»). Секрет
            # уходит В ЧАТ ПОЛУЧАТЕЛЯ, не того, кто просит — живой баг
            # 2026-08-04: тул тихо создавал пир себе с меткой чужого имени и
            # слал конфиг просящему, отдавая чужой приватный ключ не тому
            # человеку, пока модель ещё и врала, что получатель его получил.
            is_admin = ctx.subscription is not None and ActionRight(
                vpn_protocol.ACTION_PEERS, _VPN_SERVICE
            ).granted(ctx.subscription)
            if not is_admin:
                return (
                    "недоступно: выдавать VPN другому человеку может только "
                    "админ — пусть он попросит меня об этом сам, в своём чате"
                )
            if ctx.book is None:
                return "недоступно: сейчас не могу искать получателей по имени"
            found = recipients.find_recipients(who, ctx.book, ctx.settings.people)
            if not found:
                return (
                    f"не получилось: «{who}» я не знаю — выдавать доступ я могу "
                    "только тем, кто уже говорит со мной в личном чате"
                )
            if len(found) > 1:
                names = ", ".join(f"{r.display} ({r.chat_id})" for r in found)
                return f"уточни, кому именно: под «{who}» подходят {names}"
            target_chat_id = found[0].chat_id
            target_thread_id = None  # чужой чат — свои топики тут ни при чём
            target_display = found[0].display
        else:
            if ctx.chat_id is None or ctx.chat_id <= 0:
                return "недоступно: секрет доступа отдаю только в личном чате, не в группе"
            target_chat_id = ctx.chat_id
            target_thread_id = ctx.message_thread_id
        # reissue снимает старый ключ немедленно (vpn/service.py::_reissue) —
        # устройство, где он ещё стоит, обрывает соединение сразу же, до
        # того как человек успеет поставить новый .conf. Модель обязана
        # спросить согласия словами и позвать тул повторно с confirm=true —
        # без этого действие не уходит в службу вовсе (тот же приём, что
        # ERR_QUOTA_CEILING ниже: тул возвращает модели, что сделать дальше,
        # вместо того чтобы действовать по собственной инициативе).
        if action == vpn_protocol.ACTION_REISSUE and not args.get("confirm"):
            target_note = f" у {target_display}" if target_display else ""
            return (
                f"уточни подтверждение: перевыпуск заменит ключ устройства "
                f"«{device_label}»{target_note} — старый конфиг перестанет работать "
                "СРАЗУ ЖЕ, ещё до того как придёт новый файл. Спроси явное согласие "
                "и только потом вызови vpn ещё раз с теми же параметрами и "
                "confirm=true — без этого параметра перевыпуск не выполнится."
            )
        payload: dict[str, Any] = {"chat_id": target_chat_id}
        if device_label:  # reissue — какое устройство; issue — служба выберет сама
            payload["device_label"] = device_label
        if transport and action == vpn_protocol.ACTION_ISSUE:
            payload["transport"] = transport
        try:
            result = await ctx.node_link.command(action, payload, dst=dst)
        except ProtoError as exc:
            return f"не вышло: {exc.message}"
        except (ServiceUnavailableError, TimeoutError) as exc:
            return f"недоступно: VPN-служба не отвечает ({exc})"
        issued_label = str(result.get("device_label") or device_label or "устройство")
        issued_location = str(result.get("location") or "")
        # Первое устройство чата — почти наверняка настраивается прямо с
        # этого телефона (рекомендуем файл: «Открыть с помощью» → AmneziaWG
        # импортирует тоннель без копирования), второе и далее — обычно для
        # ДРУГОГО устройства или человека (рекомендуем QR). Тот же критерий,
        # что и у кнопок /vpn (bot/handlers/vpn.py::_send_secret, решение
        # пользователя 2026-08-04) — vpn/service.py::_issue::prior_device_count.
        file_first = int(result.get("prior_device_count") or 0) == 0
        result_transport = str(result.get("transport") or vpn_protocol.TRANSPORT_AWG)
        is_reality = result_transport == vpn_protocol.TRANSPORT_REALITY
        if ctx.notifier is not None:
            qr_b64 = result.get("qr_png_b64")
            if is_reality:
                file_caption = (
                    f"🔐 Конфиг «{escape(issued_label)}» (VLESS).\n"
                    "Hiddify → «+» → «Из файла» → выбери этот файл → «Подключить». "
                    "Маршрутизация России уже внутри файла."
                )
                qr_caption = (
                    f"📶 QR — «{escape(issued_label)}» (VLESS). "
                    "Hiddify → «+» → «Сканировать QR»."
                )
                conf_filename = _vpn_reality_filename(issued_label, issued_location)
            else:
                file_caption = (
                    f"🔐 Конфиг устройства «{escape(issued_label)}».\n"
                    "Нажми на файл → «Открыть с помощью» → AmneziaWG — тоннель "
                    "добавится сразу, без копирования."
                )
                qr_caption = f"📶 QR — устройство «{escape(issued_label)}»."
                conf_filename = _vpn_conf_filename(issued_label, issued_location)

            async def _send_file() -> None:
                await ctx.notifier.send_document(
                    target_chat_id,
                    str(result["config_text"]).encode("utf-8"),
                    filename=conf_filename,
                    caption=file_caption,
                    message_thread_id=target_thread_id,
                )

            async def _send_qr() -> None:
                if qr_b64:
                    await ctx.notifier.send_photo(
                        target_chat_id,
                        base64.b64decode(qr_b64),
                        filename="vpn-qr.png",
                        caption=qr_caption,
                        message_thread_id=target_thread_id,
                    )

            if file_first:
                await _send_file()
                await _send_qr()
            else:
                await _send_qr()
                await _send_file()
            if is_reality:
                deep_link = str(result.get("deep_link") or "")
                share_url = str(result.get("share_url") or "")
                note_parts = []
                if deep_link:
                    note_parts.append(f"🔗 Импорт одним нажатием: <code>{escape(deep_link)}</code>")
                if share_url:
                    note_parts.append(f"Ссылка: <code>{escape(share_url)}</code>")
                if note_parts:
                    await ctx.notifier.send_direct(
                        target_chat_id,
                        "\n".join(note_parts),
                        message_thread_id=target_thread_id,
                    )
        who_note = f" {target_display}" if target_display else ""
        if is_reality:
            recommendation = (
                "поставить Hiddify (action='apk'), импортировать файл и нажать «Подключить»"
            )
        elif file_first:
            recommendation = "для настройки удобнее конфиг-файл"
        else:
            recommendation = (
                "если это другое устройство — удобнее QR, отсканировать его камерой из приложения"
            )
        transport_note = " (VLESS/Reality)" if is_reality else ""
        return (
            f"готово: устройство «{issued_label}»{transport_note}, конфиг-файл (и QR) ушли"
            f"{who_note} личным сообщением — {recommendation} (приватный ключ не показываю)"
        )

    if action == _VPN_ACTION_APK:
        if ctx.notifier is None or ctx.chat_id is None:
            return "недоступно: сейчас не могу отправить сообщение"
        # Сначала официальные способы поставить приложение (решение
        # пользователя 2026-08-04) — на iOS сайдлоада нет вовсе, а на
        # Android апстор надёжнее файла, который надо ещё разрешить
        # ставить из неизвестного источника. Рекомендуем полную AmneziaVPN,
        # у облегчённой AmneziaWG — только .apk как аварийный запасной
        # способ (решение пользователя 2026-08-04). Дублирует
        # _apk_links_text bot/handlers/vpn.py — тот модуль тянет aiogram
        # (клавиатуры), этот модуль сознательно не должен (см. докстринг
        # файла).
        cfg = ctx.settings.vpn
        await ctx.notifier.send_direct(
            ctx.chat_id,
            "📱 Настоятельно рекомендуем полную версию — <b>AmneziaVPN</b>. Есть и "
            "облегчённая — <b>AmneziaWG</b> (её и использует эта настройка).\n\n"
            + _vpn_store_row(
                "🍎", "App Store", cfg.amneziavpn_ios_app_store_url, cfg.ios_app_store_url
            )
            + "\n"
            + _vpn_store_row(
                "🤖", "Google Play", cfg.amneziavpn_google_play_url, cfg.google_play_url
            )
            + "\n"
            f"🌐 Официальный сайт (все платформы, обе версии): "
            f"{escape(cfg.official_download_url)}",
            message_thread_id=ctx.message_thread_id,
        )
        try:
            info = await ctx.node_link.command(vpn_protocol.ACTION_APK_INFO, {}, dst=dst)
        except ProtoError as exc:
            return f"ссылки отправил, но подробности о .apk не вышло получить: {exc.message}"
        except (ServiceUnavailableError, TimeoutError) as exc:
            return f"ссылки отправил, но VPN-служба не отвечает ({exc})"
        file_id = info.get("telegram_file_id")
        if not file_id:
            return (
                "готово: ссылки на приложение ушли личным сообщением "
                "(файл .apk сейчас не кэширован — попроси открыть /vpn и нажать «Приложение»)"
            )
        sent = await ctx.notifier.send_document(
            ctx.chat_id,
            str(file_id),
            caption=f"AmneziaWG {info.get('version', '')}",
            message_thread_id=ctx.message_thread_id,
        )
        if sent:
            return "готово: ссылки и файл приложения ушли личным сообщением"
        return "готово: ссылки ушли, но файл .apk отправить не вышло"

    if action == vpn_protocol.ACTION_RESOLVE_REQUEST:
        raw_id = args.get("request_id")
        if raw_id is None:
            return "ошибка: не указан request_id"
        payload: dict[str, Any] = {"request_id": raw_id, "approve": bool(args.get("approve"))}
    elif action == vpn_protocol.ACTION_PEERS:
        payload = {}
    elif action == vpn_protocol.ACTION_USAGE and args.get("all_guests"):
        # Сводка по ВСЕМ гостям + свободный от резерва трафик ноды — доступна
        # только тому, у кого есть peers@vpn (тот же admin-признак, что и
        # кнопка «Все гости» в bot/handlers/vpn.py::_is_admin). Модель может
        # передать all_guests, не имея права, — сверяемся сами, а не
        # доверяем декларации (та же осторожность, что у остальных VariantRights).
        is_admin = ctx.subscription is not None and ActionRight(
            vpn_protocol.ACTION_PEERS, _VPN_SERVICE
        ).granted(ctx.subscription)
        if not is_admin:
            return "недоступно: сводка по всем гостям — только у админа"
        payload = {}
    elif action in (
        vpn_protocol.ACTION_PROXY_LINK,
        vpn_protocol.ACTION_PROXY_ROTATE_SECRET,
        vpn_protocol.ACTION_PROXY_USAGE,
    ):
        # Общая ссылка на всех, не привязана к конкретному разговору —
        # chat_id тут не при чём (см. vpn/protocol.py про per-guest).
        payload = {}
    else:
        if ctx.chat_id is None:
            return "недоступно: VPN привязан к разговору, а его сейчас нет"
        payload = {"chat_id": ctx.chat_id}
        if action == vpn_protocol.ACTION_REQUEST_EXTRA:
            gb = args.get("gb")
            if gb:
                payload["bytes"] = int(float(gb) * 1_000_000_000)

    try:
        result = await ctx.node_link.command(action, payload, dst=dst)
    except ProtoError as exc:
        if exc.code == vpn_protocol.ERR_QUOTA_CEILING:
            return (
                "потолок самообслуживания достигнут — вызови ещё раз с "
                "action=«request_extra», чтобы отправить заявку админу"
            )
        return f"не вышло: {exc.message}"
    except (ServiceUnavailableError, TimeoutError) as exc:
        return f"недоступно: VPN-служба не отвечает ({exc})"
    return json.dumps(result, ensure_ascii=False)


_VPN_SERVICE = vpn_protocol.SERVICE_NAME
_VPN_VARIANTS = VariantRights(
    param="action",
    rights=(
        (vpn_protocol.ACTION_USAGE, ActionRight(vpn_protocol.ACTION_USAGE, _VPN_SERVICE)),
        (vpn_protocol.ACTION_ISSUE, ActionRight(vpn_protocol.ACTION_ISSUE, _VPN_SERVICE)),
        (vpn_protocol.ACTION_REISSUE, ActionRight(vpn_protocol.ACTION_REISSUE, _VPN_SERVICE)),
        (
            vpn_protocol.ACTION_GRANT_EXTRA,
            ActionRight(vpn_protocol.ACTION_GRANT_EXTRA, _VPN_SERVICE),
        ),
        (
            vpn_protocol.ACTION_REQUEST_EXTRA,
            ActionRight(vpn_protocol.ACTION_REQUEST_EXTRA, _VPN_SERVICE),
        ),
        (_VPN_ACTION_APK, ActionRight(_VPN_ACTION_APK, _VPN_SERVICE)),
        (vpn_protocol.ACTION_PEERS, ActionRight(vpn_protocol.ACTION_PEERS, _VPN_SERVICE)),
        (
            vpn_protocol.ACTION_RESOLVE_REQUEST,
            ActionRight(vpn_protocol.ACTION_RESOLVE_REQUEST, _VPN_SERVICE),
        ),
        (
            vpn_protocol.ACTION_PROXY_LINK,
            ActionRight(vpn_protocol.ACTION_PROXY_LINK, _VPN_SERVICE),
        ),
        (
            vpn_protocol.ACTION_PROXY_ROTATE_SECRET,
            ActionRight(vpn_protocol.ACTION_PROXY_ROTATE_SECRET, _VPN_SERVICE),
        ),
        (
            vpn_protocol.ACTION_PROXY_USAGE,
            ActionRight(vpn_protocol.ACTION_PROXY_USAGE, _VPN_SERVICE),
        ),
    ),
)

_DECL_VPN: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "vpn",
        "description": (
            "Личный VPN на выходном узле (обход блокировок): свой расход/лимит, "
            "выдать/перевыпустить доступ, попросить ещё трафика, прислать "
            "приложение и объяснить, как подключиться. Секрет доступа (конфиг) "
            "я отправляю отдельным личным сообщением, не показываю его в "
            "разговоре, и только в личке, не в группе.\n"
            "usage — свой расход и лимит месяца (у админа — с all_guests=true "
            "сводка по всем гостям: расход, лимит и число устройств каждого, "
            "плюс сколько трафика ноды ещё свободно от резерва); issue — "
            "выдать доступ НОВОМУ устройству, имя ему сама служба выбирает "
            "случайно (не спрашивай, как назвать, и не передавай device_label — "
            "он у issue игнорируется), число устройств не ограничено; на "
            "сервере с двумя технологиями укажи transport ('reality' для "
            "России, 'awg' иначе) — тул подскажет, если надо уточнить; "
            "reissue — перевыпустить СУЩЕСТВУЮЩЕЕ устройство: device_label "
            "ОБЯЗАТЕЛЕН (какое из уже выданных, имя видно в usage), имя при "
            "перевыпуске не меняется, а старый ключ СРАЗУ перестаёт работать, "
            "поэтому сначала спроси подтверждение словами и вызови ещё раз с "
            "confirm=true, только когда получено явное согласие; grant_extra — "
            "добавить трафика самому (доступно, только когда трафика реально "
            "осталось мало — иначе тул откажет и скажет, когда можно "
            "попробовать снова), пока не упёрся в потолок самообслуживания "
            "(тогда используй request_extra — заявка админу, необязательный "
            "gb — сколько ГБ); apk — прислать ссылки на официальное "
            "приложение AmneziaWG (App Store, Google Play, сайт) и, если "
            "файл .apk уже кэширован, сразу сам файл; proxy_link — ссылка(и) на "
            "прокси Telegram (mtg — НЕ VPN, только сам Telegram, ставится "
            "прямо в его настройках без стороннего приложения; без server "
            "отдаёт список со всех серверов, где прокси настроен, — покажи "
            "пользователю все) плюс SOCKS5-адрес для ботов; "
            "proxy_rotate_secret — сменить секрет прокси (старая ссылка "
            "сразу перестаёт работать у ВСЕХ, кто её получил — используй, "
            "только если явно попросили сменить/отозвать); proxy_usage — "
            "расход трафика прокси за месяц.\n"
            "issue/reissue БЕЗ recipient — всегда себе, в ТЕКУЩИЙ разговор. "
            "«Выдай/перевыпусти доступ Наташе» (просьба выдать ДРУГОМУ "
            "человеку, не тому, кто сейчас пишет) — это recipient=«Наташа», "
            "доступно ТОЛЬКО админу; без права на это тул сам откажет, не "
            "выдумывай, что получилось, если он сказал «недоступно». Секрет "
            "тогда уходит В ЛИЧКУ ПОЛУЧАТЕЛЯ, не тому, кто попросил, — не "
            "утверждай, что конфиг получил ты сам или собеседник.\n"
            "Как подключиться — объясняй своими словами по этим фактам, не "
            "выдумывай другой порядок: поставить приложение AmneziaWG (action="
            "«apk» пришлёт ссылки на App Store/Google Play/сайт, плюс сам "
            "файл, если он уже под рукой) → сначала прилетает QR — отсканировать "
            "его прямо в приложении удобно для настройки с ДРУГОГО устройства "
            "(сфотографировать собственный экран телефон не может); следом — "
            "файл .conf, для настройки С ЭТОГО устройства: открыть его и "
            "выбрать «Открыть с помощью» → AmneziaWG, тоннель добавится сам, "
            "копировать ничего не нужно.\n"
            "Значения action перечислены в enum: чего там нет — не умеешь."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [v for v, _ in _VPN_VARIANTS.rights],
                    "description": "какое действие выполнить",
                },
                "device_label": {
                    "type": "string",
                    "description": (
                        "reissue: ОБЯЗАТЕЛЕН — имя существующего устройства "
                        "(возьми из usage). issue его игнорирует — не передавай."
                    ),
                },
                "transport": {
                    "type": "string",
                    "enum": ["awg", "reality"],
                    "description": (
                        "issue: технология под общей квотой — 'reality' (VLESS, "
                        "работает из России) или 'awg' (AmneziaWG, быстрее вне "
                        "России). Нужен, только если сервер даёт обе (тул сам "
                        "скажет, если надо уточнить). reissue его игнорирует."
                    ),
                },
                "recipient": {
                    "type": "string",
                    "description": (
                        "issue/reissue: имя/ник ДРУГОГО человека, которому "
                        "выдать доступ (не себе) — только у админа. Без этого "
                        "параметра — всегда себе"
                    ),
                },
                "all_guests": {
                    "type": "boolean",
                    "description": (
                        "usage: true — сводка по всем гостям и свободный резерв "
                        "ноды (только у админа); без этого — свой расход"
                    ),
                },
                "confirm": {
                    "type": "boolean",
                    "description": (
                        "reissue: true — только после явного согласия "
                        "собеседника перевыпустить ключ (старый сразу перестанет "
                        "работать). Без этого параметра перевыпуск не выполнится."
                    ),
                },
                "gb": {
                    "type": "number",
                    "description": "request_extra: сколько ГБ попросить (по умолчанию — шаг)",
                },
                "request_id": {
                    "type": "integer",
                    "description": "resolve_request: номер заявки",
                },
                "approve": {
                    "type": "boolean",
                    "description": "resolve_request: одобрить (true) или отклонить (false)",
                },
            },
            "required": ["action"],
        },
    },
}


# --- web_search: интернет через свой SearXNG (LLM_INTEGRATION_PLAN.md §9) ---


def _web_search_episode_text(query: str, results: list[dict[str, Any]]) -> str:
    """Заголовки+выдержки в один текст эпизода — конечную обрезку до
    _GRAPH_EPISODE_MAX_CHARS делает сам _piggyback_graph_episode."""
    parts = [f"Поиск «{query}»:"]
    for item in results:
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        if title and snippet:
            parts.append(f"{title} — {snippet}")
        elif title:
            parts.append(title)
    return " ".join(parts)


# Запас сверх net.request_timeout_s на дорогу alfred ↔ mycraft и разбор
# выдачи — чтобы ответ службы успел доехать, а не упасть в таймаут у финиша.
_WEB_SEARCH_TIMEOUT_MARGIN_S = 5.0


async def tool_web_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return "недоступно: нет связи с роем"
    query = str(args.get("query") or "").strip()
    if not query:
        return "ошибка: не указан поисковый запрос"
    dst = Address(node=net_protocol.NODE_ID, service=net_protocol.SERVICE_NAME)
    try:
        # Ждём дольше, чем net сам ждёт SearXNG: без явного таймаута
        # действовали 10 с ProtoClient по умолчанию, а поиску отведено 20
        # (живая находка 2026-09-25 — медленный brave обрывал ответ целиком).
        result = await ctx.node_link.command(
            net_protocol.ACTION_SEARCH,
            {"query": query},
            dst=dst,
            timeout=ctx.settings.net.request_timeout_s + _WEB_SEARCH_TIMEOUT_MARGIN_S,
        )
    except (ServiceUnavailableError, ProtoError, TimeoutError) as exc:
        # §7.3: недоступный поисковик — обычный результат тула, персонаж сам
        # решит, как об этом сказать; цикл tool-calling не роняем.
        return f"недоступно: поиск не работает ({exc})"
    results = result.get("results") or []
    if not results:
        return f"по запросу «{query}» ничего не нашлось"
    if ctx.chat_id is not None:
        # Этап 42.3: сырая выдача поиска — тоже эпизод графа, тем же
        # best-effort приёмом, что у remember/look_at_photo выше.
        await _piggyback_graph_episode(
            ctx,
            _web_search_episode_text(query, results),
            ctx.chat_id,
            source=graph_memory_protocol.EPISODE_SOURCE_WEB_SEARCH,
        )
    return json.dumps(result, ensure_ascii=False)


_DECL_WEB_SEARCH: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Поискать в интернете. Используй, когда ответа нет в разговоре и "
            "он может не совпадать с тем, что ты помнишь: свежие события, "
            "новости, цены, факты после твоего обучения, а также всё, в чём "
            "не уверен — лучше поискать, чем придумать. Возвращает заголовки, "
            "ссылки и короткие выдержки; самих страниц по ссылкам ты не "
            "видишь, поэтому отвечай по выдержкам и не выдумывай деталей, "
            "которых в них нет."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос обычными словами",
                }
            },
            "required": ["query"],
        },
    },
}


# --- recall_tool_result: полный текст сокращённого результата тула ---
#
# Живая находка 2026-09-04: длинные результаты тулов (в первую очередь
# web_search) сокращаются перед тем, как уйти модели (см. llm_chat.py::
# _inline_or_cache) — иначе несколько таких подряд в одном раунде
# tool-calling добивали окно контекста модели (num_ctx в llm/model-
# profiles.toml) и она возвращала пустой ответ. Полный текст никуда не
# девается — лежит в ctx.tool_result_cache под тем же id, что указан в
# пометке «сокращено»; этот тул — единственный способ модели его достать,
# если превью не хватило для ответа.


async def tool_recall_tool_result(ctx: ToolContext, args: dict[str, Any]) -> str:
    recall_id = args.get("id")
    if not isinstance(recall_id, str) or not recall_id.strip():
        return "ошибка: не указан id (он есть в пометке «сокращено» у результата инструмента)"
    full = ctx.tool_result_cache.get(recall_id.strip())
    if full is None:
        return "ошибка: под этим id ничего не сохранено — неверный id или он из более раннего хода"
    return full


_DECL_RECALL_TOOL_RESULT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "recall_tool_result",
        "description": (
            "Получить ПОЛНЫЙ, несокращённый результат инструмента, который "
            "был вызван раньше в этом же разговоре и пришёл с пометкой "
            "«сокращено для экономии контекста». Зови, только если для "
            "ответа не хватило деталей из превью — id бери прямо из этой "
            "пометки, не выдумывай его."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "id из пометки «сокращено» у более раннего результата тула",
                }
            },
            "required": ["id"],
        },
    },
}


# --- tell: передать человеку личное сообщение (IMPLEMENTATION_PLAN.md этап 28) ---

# Право — «действие@служба» на ту же службу llm, что и сам разговор
# (`chat@llm`): передача сообщений — это умение Альфреда, а не отдельная
# команда бота. TELL_RIGHT даёт «дозваться до владельца» — владелец всегда
# получает сообщение от любого, у кого есть тул. ДРУГИМ гостям личная
# передача — только при подтверждённом знакомстве (Этап 46,
# request_acquaintance), в обе стороны и для ВСЕХ, включая самого владельца
# (решение пользователя 2026-09-27: владелец через «*» писал гостю, с
# которым знакомства не было). Обходов больше нет: право tell_guests@llm
# (этап 36) и групповой флаг «семья» убраны. Официально написать любому
# гостю владелец может через notify_guest.
TELL_RIGHT = "tell@llm"

# Живая находка 2026-08-06: раньше был один лимит на автора (10/час) —
# рассылка ОДНОГО notify_guest десятерым разным гостям тратила его целиком
# так же, как повторная отправка ОДНОМУ И ТОМУ ЖЕ человеку 10 раз подряд,
# хотя риски у этих сценариев разные (первое — легитимная рассылка
# владельца, второе — модель зациклилась и спамит). Решение пользователя:
# два независимых потолка — сколько получает один конкретный человек и
# сколько всего уходит из одного чата-инициатора.
TELL_MAX_PER_RECIPIENT_PER_HOUR = 10  # одному и тому же адресату
TELL_MAX_TOTAL_PER_HOUR = 100  # всего от одного чата-инициатора (рассылки)
_tell_limiter = invites.AttemptLimiter(TELL_MAX_PER_RECIPIENT_PER_HOUR)  # ключ: (кто просил, кому)
_tell_broadcast_limiter = invites.AttemptLimiter(TELL_MAX_TOTAL_PER_HOUR)  # ключ: кто просил


def render_tell(text: str, author: str | None, to_owner_role: bool = False) -> str:
    """Как выглядит доставленное сообщение.

    Отдельная «шапка» обязательна: человек должен видеть, что это не бот сам
    придумал написать и не сообщение от системы, а Альфред передаёт просьбу
    конкретного человека. Текст — от Альфреда и в его манере, поэтому идёт
    как обычная его реплика.

    ``to_owner_role`` — гость назвал не личное имя владельца, а его роль
    («хозяин»/«владелец»/«админ», см. recipients.SOURCE_OWNER_ROLE): владелец
    должен сразу увидеть, что к нему обратились официально, а не просто
    написали лично ему как знакомому.
    """
    role = " (к вам как к владельцу)" if to_owner_role else ""
    who = f" по просьбе {escape(author)}" if author else ""
    return f"📨 <b>Альфред{role}{who}:</b>\n\n{escape(text.strip())}"


def parse_recipient_id(args: dict[str, Any]) -> int | None:
    """recipient_id из аргументов тула: число, строка с числом или «id 123»."""
    raw = args.get("recipient_id")
    if isinstance(raw, bool) or raw in (None, ""):
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return recipients.query_chat_id(text)


NEED_RECIPIENT_ID = (
    "ошибка: адресат указывается только по id (recipient_id). Сначала вызови "
    "find_person с тем, как собеседник описал человека, и возьми id оттуда; "
    "если подходят несколько — переспроси собеседника."
)


async def _person_gender(ctx: ToolContext, chat_id: int) -> str | None:
    """Пол адресата для склонения ответа тула: карточка (Этап 54), иначе
    settings.people. Спящий mycraft — None, тогда «получил(а)»."""
    cards = await people_cards.fetch_person_cards(ctx.node_link, [chat_id])
    gender = people_cards.card_gender(cards.get(chat_id))
    if gender:
        return gender
    for person in ctx.settings.people:
        if chat_id in recipients.person_chat_ids(person, ctx.book):
            return person.gender
    return None


def _got_verb(gender: str | None, *, future: bool = False) -> str:
    if future:
        return "получит"
    return {"m": "получил", "f": "получила"}.get(gender or "", "получил(а)")


async def _deliver_personal_message(
    ctx: ToolContext,
    recipient_id: int,
    text: str,
    render: Callable[[recipients.Recipient], str],
    guard: Callable[[recipients.Recipient], Awaitable[str | None]] | None = None,
    allow_self: bool = False,
    emit_extra: Callable[[recipients.Recipient], dict[str, Any]] | None = None,
    hint: Callable[[], Awaitable[str]] | None = None,
    to_owner_role: bool = False,
) -> str:
    """Общая доставка личного сообщения — поиск получателя по id, лимит,
    отправка, запись хода диалога. Права (если нужны) проверяет ``guard``:
    он получает найденного получателя и либо разрешает (``None``), либо
    возвращает готовый отказ. Без ``guard`` доставка разрешена всем, кто
    вообще видит вызывающий тул — так и должно быть у admin-only тулов,
    видимость которых уже гейтится декларацией (bot/tools.py:195-199).

    Этап 54.5: только по id. Раньше адресата искали по имени, которое
    придумала модель, и tell часто отвечал «такого нет» (14 неудач к
    2026-10-08). Теперь id находит find_person, а здесь он лишь проверяется.

    ``to_owner_role`` — гость назвал не имя владельца, а его роль; id при
    этом обязан быть владельцем, иначе отказ.

    ``allow_self`` — отправка самому себе (тот же chat_id, что у ctx). Для
    tell это бессмысленно (собеседник и так читает этот же чат — см. "не
    нужно: это тот же чат"), а вот у notify_guest смысл есть: владелец может
    попросить стилизованное уведомление себе самому — например, как
    self-напоминание через remind ("напомни мне как граф, что пора спать").

    ``emit_extra`` — доп. поля события доставки через мост службы tasks:
    проверки, которые там сделать нечем (нет Store), бот доделает сам перед
    отправкой (bot/node_events.py::_handle_deliver_message).

    ``hint`` — дописка к «не знаю такого»: кому писать МОЖНО (у tell — список
    знакомых с id), чтобы модель повторила вызов точно, а не угадывала.
    """
    found = recipients.find_by_chat_id(recipient_id, ctx.book, ctx.settings.people)
    if not found:
        return (
            f"не получилось: id {recipient_id} — ни один гость. id не придумывай: "
            "вызови find_person с тем, как собеседник назвал человека, и возьми id "
            "из ответа."
        ) + (await hint() if hint is not None else "")
    target = found[0]
    if to_owner_role:
        sub = ctx.book.for_chat(target.chat_id)
        if sub is None or not sub.is_owner:
            return (
                f"ошибка: to_owner_role только для владельца, а id {recipient_id} — "
                f"{target.display}"
            )
        target = recipients.Recipient(
            target.chat_id, target.display, recipients.SOURCE_OWNER_ROLE
        )
    if not allow_self and target.chat_id == ctx.chat_id:
        return "не нужно: это тот же чат, просто скажи это здесь"

    if guard is not None:
        refusal = await guard(target)
        if refusal is not None:
            return refusal

    # Порядок важен: сперва более тесный потолок (один получатель), чтобы
    # зацикленная на одном человеке модель не жгла попусту общий бюджет
    # рассылки — тогда отказ по нему не мешает рассылке другим.
    if not _tell_limiter.register((ctx.chat_id, target.chat_id)):
        return f"не сейчас: {target.display} уже получил слишком много сообщений за последний час"
    if not _tell_broadcast_limiter.register(ctx.chat_id):
        return "не сейчас: слишком много сообщений отправлено за последний час — общий лимит рассылки"

    rendered = render(target)
    if ctx.notifier is not None:
        message_id = await ctx.notifier.send_direct(target.chat_id, rendered)
        if message_id is None:
            return f"не дошло: {target.display} сейчас недоступен"
        # Записываем как свой ход в диалоге получателя: тогда он ответит
        # обычным реплаем и разговор продолжится (реплай-цепочка резолвится
        # по ai_turns, см. bot/handlers/ai.py::AiReplyContinuation), а не
        # упрётся в сообщение, на которое некому отвечать.
        if ctx.store is not None:
            await ctx.store.record_ai_turn(
                target.chat_id, message_id, message_id, "assistant", text, datetime.now(tz=UTC)
            )
        log.info(
            "tell: сообщение от chat=%s доставлено chat=%s (%s)",
            ctx.chat_id, target.chat_id, target.display,
        )
        verb = _got_verb(await _person_gender(ctx, target.chat_id))
        return f"передано: {target.display} {verb} сообщение"

    # Служба tasks (self-scheduled remind, живая находка 2026-08-06): своего
    # notifier нет, но есть мост к боту — единственному, у кого он есть (см.
    # ToolContext.emit). Доставка fire-and-forget: bot/node_events.py::
    # _handle_deliver_message шлёт сообщение и сам пишет ai_turn, здесь
    # подтверждения ждать неоткуда (тот же компромисс, что у task_result).
    assert ctx.emit is not None  # гарантировано вызывающим (tool_tell/tool_notify_guest)
    await ctx.emit(
        task_protocol.EVENT_DELIVER_MESSAGE,
        {
            "chat_id": target.chat_id,
            "html": rendered,
            "plain": text,
            "message_thread_id": None,
            **(emit_extra(target) if emit_extra is not None else {}),
        },
    )
    log.info(
        "tell: сообщение от chat=%s поставлено на доставку в chat=%s (%s) через tasks",
        ctx.chat_id, target.chat_id, target.display,
    )
    return f"передано: {target.display} получит сообщение"


async def tool_tell(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.book is None or (ctx.notifier is None and ctx.emit is None):
        return "недоступно: сейчас я не могу никому написать"
    if ctx.chat_id is None:
        return "недоступно: непонятно, от кого передавать"
    recipient_id = parse_recipient_id(args)
    text = str(args.get("text") or "").strip()
    if recipient_id is None:
        return NEED_RECIPIENT_ID
    if not text:
        return "ошибка: не сказано, что передать (text)"

    # Владелец по РОЛИ ("передай хозяину/владельцу/админу") доступен всем
    # всегда. По личному имени ("передай Алексею") владелец — такой же
    # человек, как все: нужно знакомство (решение 2026-09-27).
    def _to_owner_role(target: recipients.Recipient) -> bool:
        return target.source == recipients.SOURCE_OWNER_ROLE

    async def guard(target: recipients.Recipient) -> str | None:
        if _to_owner_role(target):
            return None
        if ctx.store is None and ctx.notifier is None:
            # Служба tasks: Store нет — знакомство проверит бот перед
            # отправкой (emit_extra ниже → require_acquaintance).
            return None
        if ctx.store is not None and await are_acquainted(ctx.store, ctx.chat_id, target.chat_id):
            return None
        owner_hint = (
            " (официальное уведомление от владельца — notify_guest)"
            if ctx.subscription is not None and ctx.subscription.is_owner
            else ""
        )
        return (
            f"не умею: вы с {target.display} ещё не знакомы через меня — лично передавать "
            "сообщения я могу только тем, с кем знакомство подтверждено (и владельцу, "
            "если просят передать именно «владельцу»/«хозяину»); познакомить — "
            f"request_acquaintance(recipient_id={target.chat_id}){owner_hint}."
        ) + await _roster_hint(ctx)

    def render(target: recipients.Recipient) -> str:
        return render_tell(
            text, ctx.author, to_owner_role=target.source == recipients.SOURCE_OWNER_ROLE
        )

    def emit_extra(target: recipients.Recipient) -> dict[str, Any]:
        if _to_owner_role(target):
            return {}
        return {"require_acquaintance": [ctx.chat_id, target.chat_id]}

    return await _deliver_personal_message(
        ctx,
        recipient_id,
        text,
        render,
        guard=guard,
        emit_extra=emit_extra,
        hint=lambda: _roster_hint(ctx),
        to_owner_role=bool(args.get("to_owner_role")),
    )


_DECL_TELL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "tell",
        "description": (
            "Передать личное сообщение другому человеку в его личный чат с "
            "тобой. Используй, когда собеседник просит что-то кому-то "
            "сообщить, передать, спросить или напомнить ('скажи Андрею, что…', "
            "'спроси у Наташи…'). Адресат — ТОЛЬКО по recipient_id: возьми его "
            "из списка знакомых в справке или вызови find_person с тем, как "
            "собеседник описал человека (имя, прозвище, «мама», «хозяину»). "
            "Если find_person дал несколько кандидатов — переспроси, не выбирай "
            "сам. Текст сообщения придумываешь ТЫ: перескажи просьбу своими "
            "словами, в своей манере, и упомяни, от кого она — это не пересылка "
            "дословной цитаты. Писать можно тем, с кем у собеседника "
            "подтверждено знакомство (request_acquaintance), и владельцу, если "
            "просят передать именно «владельцу»/«хозяину»/«админу» — тогда "
            "to_owner_role=true (find_person так и подскажет); по личному имени "
            "владелец — такой же человек, как все. tell — это ВСЕГДА личная "
            "передача (с пометкой «по просьбе X»), не официальное уведомление — "
            "для того есть notify_guest (виден только владельцу). Если с тобой "
            "говорит владелец и из его слов неясно, хочет ли он передать что-то "
            "лично от себя (как Алексей, tell) или объявить официально (как "
            "владелец, notify_guest) — спроси прямо: «сказать как лично от тебя "
            "или как официальное уведомление?», не выбирай сам. Если тул вернул "
            "отказ — перескажи ПРИЧИНУ ИЗ ЕГО ОТВЕТА как есть, не выдумывай "
            "другую от себя. Никогда не отказывай, не вызвав tell: знакомство "
            "могло появиться с прошлого раза — проверяет только тул."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "integer",
                    "description": "Telegram id получателя — из find_person или списка знакомых",
                },
                "text": {
                    "type": "string",
                    "description": "Готовый текст сообщения — то, что получатель прочтёт",
                },
                "to_owner_role": {
                    "type": "boolean",
                    "description": "true, если передают «владельцу/хозяину/админу» как роли",
                },
            },
            "required": ["recipient_id", "text"],
        },
    },
}


# --- notify_persona/notify_guest: официальное уведомление гостю от владельца
# — принципиально ДРУГОЙ канал, чем tell (личная передача просьбы). Видимость
# обоих тулов сама по себе владельца от гостя отличает, та же декларация, что
# и у guests_list, поэтому внутри обработчиков прав не проверяем (см.
# bot/tools.py:195-199).
#
# Два отдельных вызова, а не один — намеренно (2026-08-05, живой запрос
# пользователя после первой версии с обёрткой текста): если бы персонажа
# выбирал сам notify_guest, у нескольких уведомлений подряд в одном ответе
# модели было бы одно и то же «случайное» значение (соблазн переиспользовать
# результат), и весь текст модель писала бы нейтрально, а стиль был бы только
# в шапке/обёртке — тонкая имитация. Так персонаж и его манера речи выбираются
# ПЕРЕД тем, как модель вообще начинает писать текст: notify_persona отдаёт
# случайного персонажа и промт-инструкцию, КАК писать в его манере, модель
# сама сочиняет текст следуя ей, и только потом notify_guest отправляет уже
# готовый, по-настоящему стилизованный текст. При уведомлении нескольких
# гостей подряд notify_persona зовётся заново на КАЖДОГО — тогда и жребий
# у каждого свой.

_NOTIFY_PERSONAS: tuple[dict[str, str], ...] = (
    {
        "title": "Владелец",
        "emoji": "🏠",
        "style_prompt": (
            "Нейтральный, обычный деловой тон от лица владельца дома — просто "
            "и по-человечески, без вычурности и ролевой игры."
        ),
    },
    {
        "title": "Хозяин",
        "emoji": "🧰",
        "style_prompt": (
            "Тон рачительного хозяина-хозяйственника: по-деловому, чуть "
            "ворчливо про порядок и хозяйство — как человек, который сам всё "
            "чинит, следит за расходами и не любит бардак. Обычная "
            "разговорная речь, можно вставить хозяйственную лексику "
            "(«приведём в порядок», «учтено», «по-хозяйски», «не разбрасывай»)."
        ),
    },
    {
        "title": "Админ",
        "emoji": "🤓",
        "style_prompt": (
            "Тон айтишника-технаря: сухо, по пунктам, с лёгким канцеляритом и "
            "техническим жаргоном («по протоколу», «статус», «зафиксировано»). "
            "Уместно закончить короткой технической подписью вроде «// конец "
            "уведомления»."
        ),
    },
    {
        "title": "Граф",
        "emoji": "🦇",
        "style_prompt": (
            "Тон аристократа-вампира вроде графа Дракулы: старомодная, чуть "
            "зловещая, витиеватая речь, архаизмы («приветствую, о смертный», "
            "«засим откланиваюсь»), готическая атмосфера — но это стилизация "
            "для колорита, не всерьёз пугать адресата."
        ),
    },
)
_NOTIFY_PERSONAS_BY_TITLE = {p["title"]: p for p in _NOTIFY_PERSONAS}


def _resolve_persona(raw: str) -> dict[str, str] | None:
    """Найти персонажа по значению persona из notify_guest.

    Терпимо к лишнему вокруг названия: модель иногда копирует persona не
    отдельным словом, а вместе с эмодзи или другим текстом из ответа
    notify_persona (живой баг 2026-08-05 — "Граф 🦇" вместо "Граф"), точное
    совпадение по словарю такое не находило.
    """
    normalized = raw.strip()
    if normalized in _NOTIFY_PERSONAS_BY_TITLE:
        return _NOTIFY_PERSONAS_BY_TITLE[normalized]
    for persona in _NOTIFY_PERSONAS:
        if persona["title"].casefold() in normalized.casefold():
            return persona
    return None


async def tool_notify_persona(ctx: ToolContext, args: dict[str, Any]) -> str:
    persona = random.choice(_NOTIFY_PERSONAS)
    return (
        f"Персонаж: {persona['title']}\n"
        f"Эмодзи персонажа (это для контекста, НЕ значение persona): "
        f"{persona['emoji']}\n"
        f"Стиль: {persona['style_prompt']}\n"
        f"{persona['title']} — своя, отдельная от Альфреда личность (не "
        f"«Альфред временно изображает {persona['title']}а», а именно "
        f"{persona['title']} как таковой). Пиши text от первого лица как сам "
        f"{persona['title']}, не выдавай себя ЗА Альфреда и не отождествляй "
        f"себя с ним в тексте — упоминать Альфреда как отдельного, другого "
        f"персонажа (например «это передал Альфред» или что-то в этом духе) "
        f"можно, если уместно по смыслу, просто не путай два лица в одно.\n"
        f"Напиши текст уведомления в этом стиле и вызови notify_guest с "
        f'persona="{persona["title"]}" — ровно этим словом, без эмодзи и без '
        f"кавычек внутри значения."
    )


_DECL_NOTIFY_PERSONA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "notify_persona",
        "description": (
            "Первый и ОБЯЗАТЕЛЬНЫЙ шаг перед notify_guest — всегда, даже "
            "если владелец сам назвал персонажа словами ('от админа', 'как "
            "граф') — персонаж ВСЕГДА выбирается случайно этим тулом, его "
            "пожелание не подставляй напрямую в notify_guest в обход "
            "notify_persona. Тул сам случайно выбирает персонажа-«отправителя» "
            "официального уведомления и возвращает промт — КАК писать текст "
            "в его манере. Вызови его РОВНО ОДИН РАЗ НА КАЖДОГО получателя, "
            "которому собираешься отправить уведомление notify_guest — если "
            "уведомляешь нескольких гостей подряд, зови notify_persona "
            "заново для каждого, не переиспользуй один и тот же результат: "
            "иначе у всех окажется один и тот же персонаж вместо случайного "
            "у каждого. После этого сам сочини текст уведомления, следуя "
            "полученному промту, и передай его в notify_guest вместе с "
            "именем персонажа как есть."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def render_notify(persona: dict[str, str], text: str) -> str:
    body = escape(text.strip())
    title = escape(persona["title"])
    return f"{persona['emoji']} <b>{title} (официальное уведомление):</b>\n\n{body}"


async def tool_notify_guest(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.book is None or (ctx.notifier is None and ctx.emit is None):
        return "недоступно: сейчас я не могу никому написать"
    if ctx.chat_id is None:
        return "недоступно: непонятно, откуда уведомлять"
    recipient_id = parse_recipient_id(args)
    text = str(args.get("text") or "").strip()
    persona = _resolve_persona(str(args.get("persona") or ""))
    if recipient_id is None:
        return NEED_RECIPIENT_ID
    if not text:
        return "ошибка: не сказано, что передать (text)"
    if persona is None:
        return "ошибка: сначала вызови notify_persona и передай сюда его persona как есть"

    def render(_target: recipients.Recipient) -> str:
        return render_notify(persona, text)

    return await _deliver_personal_message(ctx, recipient_id, text, render, allow_self=True)


_DECL_NOTIFY_GUEST: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "notify_guest",
        "description": (
            "Официальное уведомление гостю — от лица администрации дома, БЕЗ "
            "пометки «по просьбе владельца». Это инструмент только владельца, "
            "и он про ДРУГОЕ, чем tell: используй его, когда владелец прямо "
            "просит что-то ОБЪЯВИТЬ/УВЕДОМИТЬ/ПРЕДУПРЕДИТЬ гостя официально "
            "(«объяви», «предупреди», «уведоми», «разошли объявление») — а не "
            "когда просит что-то ПЕРЕДАТЬ лично от себя («скажи», «спроси», "
            "«передай») — для этого есть tell. Если из разговора неясно, "
            "хочет ли владелец сказать лично от себя (как Алексей, tell) или "
            "объявить официально (как владелец, notify_guest) — спроси "
            "прямо: «сказать как лично от тебя или как официальное "
            "уведомление?», не выбирай сам. Получатель — по recipient_id из "
            "find_person; им может быть и сам владелец (например "
            "self-напоминание в стиле персонажа). "
            "persona ВСЕГДА случайный: ОБЯЗАТЕЛЬНО сначала вызови "
            "notify_persona (на каждого получателя заново) и напиши text в "
            "его стиле — persona здесь передай ровно тем же значением, что "
            "он вернул, даже если владелец сам называл персонажа словами."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "integer",
                    "description": "Telegram id из find_person (может быть и сам владелец)",
                },
                "persona": {
                    "type": "string",
                    "enum": [p["title"] for p in _NOTIFY_PERSONAS],
                    "description": "Персонаж, ровно как вернул notify_persona",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "Текст уведомления от первого лица персонажа — он не "
                        "выдаёт себя за Альфреда, это своя, отдельная от "
                        "Альфреда личность (упоминать Альфреда как ДРУГОГО "
                        "персонажа по смыслу можно)"
                    ),
                },
            },
            "required": ["recipient_id", "persona", "text"],
        },
    },
}


# --- guests_list: справочник гостей для владельца (то же право, что у самой
# команды /guests — guest_rights.py сознательно не даёт invite гостям
# точечно, «сделало бы гостя соадминистратором», так что тул виден только
# владельцу). Только чтение — менять права/флаг «семья» тул не умеет, это
# остаётся за человеком через /guests.

DEFAULT_GUESTS_LIST_LIMIT = 30
MAX_GUESTS_LIST_LIMIT = 100


async def tool_guests_list(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.book is None:
        return "недоступно: список гостей сейчас не виден"
    all_guests = ctx.book.guests()
    guests = all_guests
    right = str(args.get("right") or "").strip()
    if right:
        if "@" in right:
            guests = [g for g in guests if g.allows_command(right)]
        else:
            # Живой баг 2026-08-06: модель регулярно передаёт голое имя
            # службы ("vpn"), а не точную строку права ("usage@vpn") — хотя
            # в описании тула есть примеры с "@", результат выглядел как
            # честный "гостей нет", а на деле фильтр просто не совпадал ни с
            # чем. Голое имя без "@" теперь трактуем как "есть хоть какое-то
            # право на эту службу" — то, что модель почти всегда и имела в
            # виду.
            suffix = f"@{right}"
            guests = [
                g
                for g in guests
                if g.allows_command("*") or any(c.endswith(suffix) for c in g.allowed_commands)
            ]
    if not guests:
        return "гостей с такими условиями нет"

    offset_raw = args.get("offset")
    try:
        offset = max(0, int(offset_raw)) if offset_raw is not None else 0
    except (TypeError, ValueError):
        return f"offset должен быть числом: {offset_raw!r}"
    limit = DEFAULT_GUESTS_LIST_LIMIT
    limit_raw = args.get("limit")
    if limit_raw is not None:
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            return f"limit должен быть числом: {limit_raw!r}"
        limit = max(1, min(limit, MAX_GUESTS_LIST_LIMIT))

    guests_sorted = sorted(guests, key=lambda s: s.invited_at)
    page = guests_sorted[offset : offset + limit]
    # Полный перечень прав на гостя (их бывает больше десятка) раздувал
    # ответ настолько, что модель при пересказе в чат сама обрезала список
    # гостей до нескольких штук и не отмечала обрезку (живая находка
    # 2026-08-06). Вместо перечня прав в строке — только их число; узнать,
    # у кого есть конкретное право, теперь можно фильтром right (сам
    # список остаётся компактным независимо от фильтра).
    lines = [f"Гостей: {len(all_guests)} (после фильтра: {len(guests)})"]
    if not page:
        lines.append(f"offset {offset} за пределами списка — всего подходит {len(guests)}")
        return "\n".join(lines)
    lines[0] += f", показаны {offset + 1}-{offset + len(page)}"
    # Живая находка 2026-10-09: брат владельца числился как «Kein» — имя и
    # @ник из [[people]] знал только find_person, и модель решила, что их нет.
    labels = _person_labels(ctx, [g.chat_id for g in page], {})
    for g in page:
        aliases: list[str] = []
        for kind, label in labels.get(g.chat_id, []):
            label = f"@{label.lstrip('@')}" if kind == "ник" and label else label
            if label and label not in aliases and label not in g.name:
                aliases.append(label)
        extra = f", ещё: {', '.join(aliases)}" if aliases else ""
        lines.append(
            f"• {g.name} (chat_id {g.chat_id}{extra}) — прав: {len(g.allowed_commands)}"
        )
    next_offset = offset + len(page)
    if next_offset < len(guests):
        lines.append(
            f"Это не все — ещё {len(guests) - next_offset}. Чтобы показать "
            f"остальных, вызови guests_list снова с offset={next_offset}."
        )
    return "\n".join(lines)


_DECL_GUESTS_LIST: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "guests_list",
        "description": (
            "Твой личный справочник приглашённых гостей: имя, chat_id и число "
            "выданных прав. Ищешь конкретного человека (по имени, нику, «мой "
            "брат») — это find_person, а не этот список; справочник — чтобы "
            "перечислить гостей (сами права поимённо он не показывает, right — "
            "фильтр, а не перечень). Доступен "
            "только владельцу — если тул тебе виден, значит спрашивает "
            "именно он; не пересказывай этот справочник в чужом чате. "
            "right — точная строка права (например 'chat@llm', "
            "'recall@memory') — если задано, оставляет только гостей с этим "
            "правом (узнать, у кого есть конкретное право); можно передать и "
            "голое имя службы без действия (например 'vpn') — тогда "
            "оставляет гостей хоть с каким-то правом на эту службу. "
            "За один вызов отдаёт страницу (по умолчанию до 30 гостей) — "
            "перечисли в ответе ВСЕХ, кто попал в страницу, не выбирай сам "
            "часть из них. Если в конце результата есть строка «Это не "
            "все» — гостей больше, чем показано: обязательно скажи об этом "
            "владельцу (сколько ещё) вместо того чтобы промолчать, и вызови "
            "тул снова с указанным offset, если владелец хочет увидеть "
            "остальных."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "right": {
                    "type": "string",
                    "description": (
                        "Строка права для фильтра — точная 'action@service' "
                        "(например 'chat@llm') или голое имя службы без "
                        "действия (например 'vpn' — любое право на VPN)"
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "С какого гостя начать страницу (по умолчанию 0) — "
                        "используй значение из «Это не все», чтобы показать "
                        "следующих."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Сколько гостей на страницу (по умолчанию "
                        f"{DEFAULT_GUESTS_LIST_LIMIT}, максимум "
                        f"{MAX_GUESTS_LIST_LIMIT})."
                    ),
                },
            },
            "required": [],
        },
    },
}


_DECL_REMIND: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "remind",
        "description": (
            "Поставить отложенную задачу в этом же чате — НЕ готовый текст, а "
            "то, что нужно СДЕЛАТЬ или СКАЗАТЬ, когда придёт время: тогда тебя "
            "вызовут заново и ты сам сформулируешь ответ, при необходимости "
            "пользуясь другими инструментами (например, посмотреть погоду "
            "именно в тот момент, а не сейчас). Ровно ОДНО из двух — when ИЛИ "
            "after_event. when — переведи то, что попросил пользователь "
            "('через 20 минут', 'завтра в 9 утра'), в точную дату-время сам, "
            "используя текущее время из контекста разговора. after_event — "
            "проснуться по событию ноды, а не по времени: НЕ сиди и не "
            "жди/не переспрашивай состояние сам, а поставь задачу "
            "проснуться, когда нода реально подтвердит нужное (страховочный "
            "срок на случай, если событие не придёт, считается сам, без "
            "when). Для update/restart_node ноды (node_manage) это НЕ "
            "нужно — они уже сами ставят такое ожидание, вызывай remind "
            "только для СВОИХ похожих случаев ожидания события."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "when": {
                    "type": "string",
                    "description": "Точная дата-время в ISO 8601, например 2026-07-24T21:30:00",
                },
                "after_event": {
                    "type": "object",
                    "description": (
                        "Проснуться по событию ноды, а не по времени — для "
                        "node_manage(restart_node)/update, когда нужно дождаться "
                        "реального результата, не выдумывая его заранее."
                    ),
                    "properties": {
                        "node": {
                            "type": "string",
                            "description": "Имя ноды, чьего события ждать (например: arch-t480)",
                        },
                        "event": {
                            "type": "string",
                            "enum": list(_AFTER_EVENT_TYPES),
                            "description": (
                                "restart_applied — нода реально перезапустилась на новой "
                                "версии (после restart_node); update_finished — файлы "
                                "обновления легли на диск (после update, ДО рестарта)."
                            ),
                        },
                    },
                    "required": ["node", "event"],
                },
                "text": {
                    "type": "string",
                    "description": "Что нужно сделать или сказать в момент срабатывания",
                },
            },
            "required": ["text"],
        },
    },
}


_DECL_REQUEST_ACQUAINTANCE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "request_acquaintance",
        "description": (
            "Открыть собеседнику форму предложения знакомства с другим гостем. "
            "Знакомство — единственная связь между гостями: после согласия "
            "обеих сторон ты сможешь передавать сообщения между ними (tell). "
            "Вызывай, когда собеседник говорит о своих отношениях с кем-то "
            "('Вася — мой друг', 'Настя — моя сестра', 'это моя жена', "
            "'я знаю Олега, это мой отец', 'познакомь меня с Игорем', "
            "'установи между нами связь') или хочет, чтобы ты передавал "
            "сообщения человеку, а tell отказал. Не переспрашивай и не "
            "предлагай словами — сразу вызывай. Какие бы слова об отношениях "
            "он ни выбрал — предлагай именно знакомство, не спорь о словах и "
            "не уточняй степень близости: система хранит только сам факт "
            "знакомства. Тул ничего не отправляет адресату: собеседник "
            "получит отдельную форму с кнопками «Отправить»/«Отмена» и решит "
            "сам. Ответ адресата тоже приходит только кнопкой — сам ты "
            "предложения не отправляешь, не принимаешь и не отклоняешь. "
            "Адресат — по recipient_id: найди его через "
            "find_person(purpose=\"acquaintance\"). Если "
            "тул вернул отказ — перескажи причину из его ответа как есть."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "integer",
                    "description": "Telegram id человека из find_person(purpose=\"acquaintance\")",
                },
            },
            "required": ["recipient_id"],
        },
    },
}

_DECL_MY_ACQUAINTANCES: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "my_acquaintances",
        "description": (
            "Узнать, с кем из гостей у собеседника подтверждённое знакомство "
            "(им ты можешь передавать сообщения) — используй, когда "
            "спрашивают о своих связях/знакомых/друзьях ('с кем я связан', "
            "'кому ты можешь от меня передать'). Про предложения без ответа "
            "или отклонённые тул молчит — не спойлери их, если спросят "
            "прямо, отвечай только тем, что тут вернулось."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


# Порядок задаёт порядок деклараций в контексте модели.
TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(name="calc", handler=tool_calc, declaration=_DECL_CALC),
    ToolSpec(name="get_weather", handler=tool_get_weather, declaration=_DECL_WEATHER),
    ToolSpec(name="convert_currency", handler=tool_convert_currency, declaration=_DECL_CURRENCY),
    ToolSpec(name="get_time", handler=tool_get_time, declaration=_DECL_TIME),
    ToolSpec(name="look_at_photo", handler=tool_look_at_photo, declaration=_DECL_LOOK_AT_PHOTO),
    ToolSpec(
        name="swarm_status",
        handler=tool_swarm_status,
        declaration=_DECL_SWARM_STATUS,
        variants=_SWARM_VARIANTS,
    ),
    ToolSpec(
        name="node_manage",
        handler=tool_node_manage,
        declaration=_DECL_NODE_MANAGE,
        variants=_NODE_MANAGE_VARIANTS,
    ),
    ToolSpec(
        name="swarm_events",
        handler=tool_swarm_events,
        declaration=_DECL_SWARM_EVENTS,
        requires=CommandRight(commands.NODES.name),
    ),
    ToolSpec(
        name="torrents",
        handler=tool_torrents,
        declaration=_DECL_TORRENTS,
        variants=_TORRENTS_VARIANTS,
    ),
    ToolSpec(
        name="memory",
        handler=tool_memory,
        declaration=_DECL_MEMORY,
        variants=_MEMORY_VARIANTS,
    ),
    ToolSpec(
        name="vpn",
        handler=tool_vpn,
        declaration=_DECL_VPN,
        variants=_VPN_VARIANTS,
    ),
    ToolSpec(
        name="dismiss",
        handler=tool_dismiss,
        declaration=_DECL_DISMISS,
        variants=_DISMISS_VARIANTS,
    ),
    ToolSpec(name="voice_mode", handler=tool_voice_mode, declaration=_DECL_VOICE_MODE),
    ToolSpec(
        name="web_search",
        handler=tool_web_search,
        declaration=_DECL_WEB_SEARCH,
        requires=ActionRight(net_protocol.ACTION_SEARCH, net_protocol.SERVICE_NAME),
    ),
    # Без requires: не самостоятельное умение, а способ дочитать то, что
    # модели уже показали (в сокращённом виде) в этом же разговоре — прав
    # раскрывает не больше, чем тул, который результат породил.
    ToolSpec(
        name="recall_tool_result",
        handler=tool_recall_tool_result,
        declaration=_DECL_RECALL_TOOL_RESULT,
    ),
    # tell — право в форме «действие@служба» на ту же службу llm, что и сам
    # разговор: проверяется через allows_command, как chat@llm у /alfred (см.
    # AUTHORIZATION.md §3.2). Групповые формы (*@llm, голый *) работают как
    # обычно, поэтому админу дописывать ничего не нужно.
    ToolSpec(name="tell", handler=tool_tell, declaration=_DECL_TELL,
             requires=CommandRight(TELL_RIGHT)),
    # guests_list — то же право, что у самой команды /guests (invite):
    # виден только владельцу, гостям guest_rights.py его не выдаёт.
    ToolSpec(
        name="guests_list",
        handler=tool_guests_list,
        declaration=_DECL_GUESTS_LIST,
        requires=CommandRight(commands.required_right(commands.GUESTS.name)),
    ),
    # notify_persona/notify_guest — та же логика видимости, что у
    # guests_list: право invite есть только у владельца, поэтому обработчику
    # не нужно перепроверять, кто зовёт.
    ToolSpec(
        name="notify_persona",
        handler=tool_notify_persona,
        declaration=_DECL_NOTIFY_PERSONA,
        requires=CommandRight(commands.required_right(commands.GUESTS.name)),
    ),
    ToolSpec(
        name="notify_guest",
        handler=tool_notify_guest,
        declaration=_DECL_NOTIFY_GUEST,
        requires=CommandRight(commands.required_right(commands.GUESTS.name)),
    ),
    # remind сознательно без requires: он появился до правил доступа и уже
    # работает у живых пользователей — привязка к праву отобрала бы рабочее
    # умение у тех, кому его никто не запрещал. Долг: завести под него право
    # create@tasks, когда будет повод трогать подписки в проде.
    ToolSpec(name="remind", handler=tool_remind, declaration=_DECL_REMIND),
    # Знакомство между гостями (Этап 46) — без requires, как remind:
    # открыто любому подписанному гостю, не только владельцу. Адресат —
    # только известный гость (тот же резолвер, что у tell). Ответ адресата —
    # только кнопкой формы (bot/pending_actions.py).
    ToolSpec(
        name="request_acquaintance",
        handler=tool_request_acquaintance,
        declaration=_DECL_REQUEST_ACQUAINTANCE,
    ),
    # note_person (Этап 54.2) — без requires: о себе может сказать любой,
    # о другом — только его знакомый (проверяет сам обработчик).
    ToolSpec(name="note_person", handler=tool_note_person, declaration=_DECL_NOTE_PERSON),
    # find_person (Этап 54.4) — без requires, как request_acquaintance: ищет
    # только среди тех, к кому собеседнику и так можно обратиться.
    ToolSpec(name="find_person", handler=tool_find_person, declaration=_DECL_FIND_PERSON),
    ToolSpec(
        name="my_acquaintances",
        handler=tool_my_acquaintances,
        declaration=_DECL_MY_ACQUAINTANCES,
    ),
    # Интерактив «Проклятый передатчик» (Этап 47) — без requires: смена
    # «устройства связи» касается только самого собеседника.
    ToolSpec(
        name="swap_radio",
        handler=tool_swap_radio,
        declaration=interactive_radio.SWAP_RADIO_DECLARATION,
    ),
    # Снимок кабинета (Этап 49.2) — без requires, как swap_radio: это часть
    # мира Альфреда для любого собеседника, а не художник по заказу; свой
    # суточный потолок на чат (engine.PHOTO_DAILY_LIMIT).
    ToolSpec(
        name="take_photo",
        handler=tool_take_photo,
        declaration=interactive_cabinet.TAKE_PHOTO_DECLARATION,
    ),
    # Вещи поместья (Этап 49.3) — без requires: опись только тех вещей,
    # что выданы сценками этому собеседнику.
    ToolSpec(
        name="manor_items",
        handler=tool_manor_items,
        declaration=interactive_radio.MANOR_ITEMS_DECLARATION,
    ),
    # Действие с вещью поместья — форма; без requires, как manor_items.
    ToolSpec(
        name="item_action",
        handler=tool_item_action,
        declaration=interactive_radio.ITEM_ACTION_DECLARATION,
    ),
    # Картинки (Этап 48). generate_image — право generate_image@llm в форме
    # «действие@служба» на ту же службу llm, что рисует (llm/imagegen.py на
    # mycraft): генерация будит mycraft и ~15 с грузит CPU, поэтому это
    # отдельное право, а не часть chat@llm — впустить поговорить не значит
    # разрешить гонять художника. Групповые *@llm и голый * работают как
    # обычно (Subscription.allows_action).
    ToolSpec(
        name="generate_image",
        handler=tool_generate_image,
        declaration=image_tools.GENERATE_IMAGE_DECLARATION,
        requires=ActionRight(image_tools.ACTION_GENERATE_IMAGE, image_tools.LLM_SERVICE),
    ),
    # find_image — без requires: ищет только по картинкам СВОЕГО чата (из
    # таблицы images бота, mycraft не будит), то есть не раскрывает ничего,
    # чего собеседник уже не видел. Картинки появляются в чате только через
    # generate_image, так что без того права тул просто ничего не найдёт.
    ToolSpec(
        name="find_image",
        handler=tool_find_image,
        declaration=image_tools.FIND_IMAGE_DECLARATION,
    ),
)
