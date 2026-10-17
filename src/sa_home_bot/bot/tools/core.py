from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.people.book import PeopleBook
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.subscriptions.models import Subscription


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
    # Карточки людей (Этап 58, people/book.py::PeopleBook) — снимок на ход;
    # None — без БД (служба tasks, MCP-агент), тогда только имена подписок.
    people: Any | None = None
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

    def refusal_for(self, subscription: Subscription | None, value: str) -> str | None:
        allowed = self.allowed_values(subscription) if subscription else []
        if value in allowed:
            return None
        return f"не умею: {value or 'без уточнения'}"


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
    telegram_only: bool = False

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


# Тулы канарейки Этапа 59 (bot/interactives, 59.C): модель видит их только у
# гостей из llm.interactives_canary_user_ids. Остальным декларации не
# уезжают в контекст вовсе — поведение Альфреда прежнее.
CANARY_TOOLS = frozenset({"search", "walk", "go", "act"})

# Тулы, что выполняются по одному за раунд (Этап 59.7): исход каждого решает
# Ведущий, и модель должна увидеть его, прежде чем решать следующее действие.
# Лишние вызовы того же ответа модели не исполняются (llm_chat.run_chat_loop).
SERIAL_TOOLS = frozenset({"act", "search"})
SERIAL_DEFERRED = (
    "Не выполнено: такие действия идут строго по одному. Сначала дождись итога "
    "предыдущего действия, потом повтори это отдельным вызовом."
)


def for_context(toolkit: ToolKit, ctx: ToolContext) -> ToolKit:
    """Комплект тулов под конкретный ход: канареечные — только канарейкам.
    Без канареечных тулов в комплекте (или без ограничений) — тот же объект."""
    if not CANARY_TOOLS & toolkit.handlers.keys():
        return toolkit
    canary_ok = getattr(ctx.interactives, "canary_ok", None)
    if canary_ok is not None and canary_ok(ctx.user_id):
        return toolkit
    return ToolKit(
        declarations=[
            d for d in toolkit.declarations if d["function"]["name"] not in CANARY_TOOLS
        ],
        handlers={k: v for k, v in toolkit.handlers.items() if k not in CANARY_TOOLS},
    )


NO_SWARM_LINK = "недоступно: нет связи с роем"


def text_arg(args: dict[str, Any], key: str) -> str:
    return str(args.get(key) or "").strip()


def people_of(ctx: ToolContext) -> PeopleBook:
    if ctx.people is None:
        ctx.people = PeopleBook.empty(ctx.book)
    return ctx.people


def bounded_limit(raw: Any, default: int, maximum: int) -> int | str:
    if raw is None:
        return default
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return f"limit должен быть числом: {raw!r}"
    return max(1, min(limit, maximum))


async def call_service(
    ctx: ToolContext,
    action: str,
    payload: dict[str, Any],
    dst: Address,
    *,
    unavailable_subject: str,
    proto_refusal: Callable[[ProtoError], str | None] | None = None,
    **command_options: Any,
) -> dict[str, Any] | str:
    assert ctx.node_link is not None
    try:
        return await ctx.node_link.command(action, payload, dst=dst, **command_options)
    except ProtoError as exc:
        if proto_refusal is not None:
            special = proto_refusal(exc)
            if special is not None:
                return special
        return f"не вышло: {exc.message}"
    except (ServiceUnavailableError, TimeoutError) as exc:
        return f"недоступно: {unavailable_subject} ({exc})"
