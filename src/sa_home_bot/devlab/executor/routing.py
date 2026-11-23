from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

import aiohttp

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.model import ChatModel, GatewayChat, ModelError, ModelReply
from sa_home_bot.devlab.agent.prompt import DEFAULT_STYLE
from sa_home_bot.devlab.executor.hub import ExecutorHub, HubRequestError

log = logging.getLogger(__name__)

CATALOG_TTL_S = 15.0
ROUTE_ROUNDS = 6
ROUTE_RETRY_S = 5.0
ROUTE_RETRY_MAX_S = 60.0
SILENCE_WAIT_S = 60.0
SILENCE_CUT_TEXT = "тишина видеокарты: запрос к локальной модели оборван"
TOKENS_PER_PRICE_UNIT = 1_000_000

MODELS_ANY = "any"
MODELS_LOCAL = "local"
MODELS_REMOTE = "remote"
MODEL_KINDS = (MODELS_ANY, MODELS_LOCAL, MODELS_REMOTE)


def parse_context(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return 0
    try:
        return max(int(raw), 0)
    except (ValueError, OverflowError):
        return 0


def _exhausted(used: dict[str, Any]) -> bool:
    def zero(key: str) -> bool:
        value = used.get(key)
        return isinstance(value, int) and not isinstance(value, bool) and value == 0

    return any(zero(key) for key in (protocol.LIMIT_RPD_LEFT, protocol.LIMIT_PROVIDER_LEFT))


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    gateway: str
    url: str
    free: bool = False
    price_in: float = 0.0
    price_out: float = 0.0
    remote: bool = True
    context: int = 0
    exhausted: bool = False

    @classmethod
    def from_hub(cls, data: dict[str, Any]) -> CatalogEntry:
        limits = data.get(protocol.FIELD_LIMITS)
        used = limits.get(data[protocol.MODEL_NAME]) if isinstance(limits, dict) else None
        return cls(
            name=data[protocol.MODEL_NAME],
            gateway=data.get(protocol.MODEL_GATEWAY, ""),
            url=data.get(protocol.MODEL_URL) or "",
            free=bool(data.get(protocol.MODEL_FREE)),
            price_in=float(data.get(protocol.MODEL_PRICE_IN) or 0),
            price_out=float(data.get(protocol.MODEL_PRICE_OUT) or 0),
            remote=bool(data.get(protocol.MODEL_REMOTE, True)),
            context=parse_context(data.get(protocol.MODEL_CONTEXT)),
            exhausted=isinstance(used, dict) and _exhausted(used),
        )

    def price(self, tokens_in: int, tokens_out: int) -> float:
        if self.free:
            return 0.0
        return (tokens_in * self.price_in + tokens_out * self.price_out) / TOKENS_PER_PRICE_UNIT


class Catalog(Protocol):
    async def names(self, kind: str = MODELS_ANY) -> list[str]: ...

    async def entries(self, model: str, fresh: bool = False) -> list[CatalogEntry]: ...


class ModelCatalog:
    def __init__(
        self,
        hub: ExecutorHub,
        ttl_s: float = CATALOG_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hub = hub
        self._ttl_s = ttl_s
        self._clock = clock
        self._loaded_at: float | None = None
        self._cache: list[CatalogEntry] = []

    async def _all(self, fresh: bool = False) -> list[CatalogEntry]:
        now = self._clock()
        if fresh or self._loaded_at is None or now - self._loaded_at >= self._ttl_s:
            try:
                raw = await self._hub.catalog()
            except HubRequestError as exc:
                if self._loaded_at is None:
                    raise
                log.warning("Каталог моделей не обновлён, беру прошлый: %s", exc)
                return self._cache
            self._cache = [CatalogEntry.from_hub(item) for item in raw]
            self._loaded_at = now
        return self._cache

    async def names(self, kind: str = MODELS_ANY) -> list[str]:
        all_entries = await self._all()
        if kind == MODELS_LOCAL:
            filtered = [e for e in all_entries if e.url and not e.remote and not e.exhausted]
        elif kind == MODELS_REMOTE:
            filtered = [e for e in all_entries if e.url and e.remote and not e.exhausted]
        else:
            filtered = [e for e in all_entries if e.url and not e.exhausted]
        return sorted({entry.name for entry in filtered})

    async def entries(self, model: str, fresh: bool = False) -> list[CatalogEntry]:
        return [e for e in await self._all(fresh) if e.name == model and e.url]


def price_reply(entry: CatalogEntry, reply: ModelReply) -> ModelReply:
    actual = reply.cost_usd
    if entry.free:
        return replace(reply, cost_usd=actual or 0.0, paid_free=bool(actual and actual > 0))
    if actual is not None:
        return reply
    return replace(reply, cost_usd=entry.price(reply.tokens_in, reply.tokens_out))


class SilenceWatch:
    def __init__(self) -> None:
        self.active = False
        self._tasks: set[asyncio.Future] = set()
        self._cut: set[asyncio.Future] = set()

    def update(self, active: bool) -> None:
        self.active = active
        if not active:
            return
        for task in list(self._tasks):
            if not task.done():
                self._cut.add(task)
                task.cancel()

    async def guard(self, call: Awaitable[ModelReply]) -> ModelReply:
        task = asyncio.ensure_future(call)
        self._tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task in self._cut and (current is None or current.cancelling() == 0):
                raise ModelError(SILENCE_CUT_TEXT, transient=True, silence=True) from None
            raise
        finally:
            self._tasks.discard(task)
            self._cut.discard(task)


class FailoverChat:
    def __init__(
        self,
        catalog: Catalog,
        make_chat: Callable[[CatalogEntry], GatewayChat],
        model: str,
        rounds: int = ROUTE_ROUNDS,
        retry_s: float = ROUTE_RETRY_S,
        retry_max_s: float = ROUTE_RETRY_MAX_S,
        silence_wait_s: float = SILENCE_WAIT_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        silence: SilenceWatch | None = None,
    ) -> None:
        self._silence = silence
        self._catalog = catalog
        self._make_chat = make_chat
        self._model = model
        self._rounds = rounds
        self._retry_s = retry_s
        self._retry_max_s = retry_max_s
        self._silence_wait_s = silence_wait_s
        self._sleep = sleep
        self._preferred: str | None = None

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        extra: dict[str, Any] | None = None,
    ) -> ModelReply:
        last: ModelError | None = None
        attempt = 0
        while attempt < self._rounds:
            if attempt:
                await self._sleep(min(self._retry_s * 2 ** (attempt - 1), self._retry_max_s))
            entries = await self._ordered(fresh=attempt > 0)
            if not entries:
                last = ModelError(f"модели {self._model!r} нет на живых шлюзах", transient=True)
                attempt += 1
                continue
            silent = False
            for entry in entries:
                try:
                    chat = self._make_chat(entry)
                    call = (
                        chat.complete(messages, tools, extra=extra)
                        if extra
                        else chat.complete(messages, tools)
                    )
                    if self._silence is not None and not entry.remote:
                        reply = await self._silence.guard(call)
                    else:
                        reply = await call
                except ModelError as exc:
                    if not exc.transient:
                        raise
                    local_in_silence = (
                        self._silence is not None and self._silence.active and not entry.remote
                    )
                    if exc.silence or local_in_silence:
                        log.info("Тишина видеокарты, модель %s ждёт: %s", self._model, exc)
                        silent = True
                        continue
                    log.warning("Шлюз %s модели %s: %s", entry.gateway, self._model, exc)
                    last = exc
                    continue
                self._preferred = entry.gateway
                return price_reply(entry, reply)
            if silent:
                await self._sleep(self._silence_wait_s)
                continue
            attempt += 1
        assert last is not None
        raise ModelError(f"все шлюзы модели {self._model!r} отказали: {last}")

    async def _ordered(self, fresh: bool) -> list[CatalogEntry]:
        entries = await self._catalog.entries(self._model, fresh)
        return sorted(entries, key=lambda e: e.gateway != self._preferred)


class GatewayModels:
    def __init__(self, catalog: Catalog, token: str, silence: SilenceWatch | None = None) -> None:
        self._silence = silence
        self._catalog = catalog
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    def __call__(self, model: str, job_id: str, prompt_style: str = DEFAULT_STYLE) -> ChatModel:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        session = self._session

        def make(entry: CatalogEntry) -> GatewayChat:
            return GatewayChat(
                entry.url, self._token, model, job_id, session=session, prompt_style=prompt_style
            )

        return FailoverChat(self._catalog, make, model, silence=self._silence)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
