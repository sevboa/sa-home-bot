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
    mode: str | None = None


@dataclass
class ToolContext:
    chat_id: int | None
    dialogue_id: int | None
    trigger_message_id: int | None
    settings: Settings
    node_link: ServiceLink | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    subscription: Subscription | None = None
    dismissal: DismissalBox | None = None
    book: Any | None = None
    notifier: Any | None = None
    store: Any | None = None
    author: str | None = None
    people: Any | None = None
    message_thread_id: int | None = None
    woken_by: tuple[str, str] | None = None
    emit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
    tool_result_cache: dict[str, str] = field(default_factory=dict)
    pending_actions: Any | None = None
    interactives: Any | None = None
    user_id: int | None = None
    is_private: bool = False
    end_turn: bool = False
    reply_to_message_id: int | None = None


ToolHandler = Callable[["ToolContext", dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class CommandRight:
    name: str

    def granted(self, subscription: Subscription) -> bool:
        return subscription.allows_command(self.name)


@dataclass(frozen=True)
class ActionRight:
    action: str
    service: str

    def granted(self, subscription: Subscription) -> bool:
        return subscription.allows_action(self.action, self.service)


ToolRight = CommandRight | ActionRight


@dataclass(frozen=True)
class VariantRights:
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
    name: str
    handler: ToolHandler
    declaration: dict[str, Any]
    requires: ToolRight | None = None
    variants: VariantRights | None = None
    telegram_only: bool = False

    def declaration_for(self, subscription: Subscription) -> dict[str, Any] | None:
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
    declarations: list[dict[str, Any]]
    handlers: dict[str, ToolHandler]


CANARY_TOOLS = frozenset({"search", "walk", "go", "act"})

SERIAL_TOOLS = frozenset({"act", "search"})
SERIAL_DEFERRED = (
    "Не выполнено: такие действия идут строго по одному. Сначала дождись итога "
    "предыдущего действия, потом повтори это отдельным вызовом."
)


def for_context(toolkit: ToolKit, ctx: ToolContext) -> ToolKit:
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
