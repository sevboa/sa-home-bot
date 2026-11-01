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
from sa_home_bot.devlab.executor.hub import ExecutorHub, HubRequestError

log = logging.getLogger(__name__)

CATALOG_TTL_S = 15.0
ROUTE_ROUNDS = 3
ROUTE_RETRY_S = 5.0
TOKENS_PER_PRICE_UNIT = 1_000_000


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    gateway: str
    url: str
    free: bool = False
    price_in: float = 0.0
    price_out: float = 0.0
    remote: bool = True

    @classmethod
    def from_hub(cls, data: dict[str, Any]) -> CatalogEntry:
        return cls(
            name=data[protocol.MODEL_NAME],
            gateway=data.get(protocol.MODEL_GATEWAY, ""),
            url=data.get(protocol.MODEL_URL) or "",
            free=bool(data.get(protocol.MODEL_FREE)),
            price_in=float(data.get(protocol.MODEL_PRICE_IN) or 0),
            price_out=float(data.get(protocol.MODEL_PRICE_OUT) or 0),
            remote=bool(data.get(protocol.MODEL_REMOTE, True)),
        )

    def price(self, tokens_in: int, tokens_out: int) -> float:
        if self.free:
            return 0.0
        return (tokens_in * self.price_in + tokens_out * self.price_out) / TOKENS_PER_PRICE_UNIT


class Catalog(Protocol):
    async def names(self) -> list[str]: ...

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

    async def names(self) -> list[str]:
        return sorted({entry.name for entry in await self._all() if entry.url})

    async def entries(self, model: str, fresh: bool = False) -> list[CatalogEntry]:
        return [e for e in await self._all(fresh) if e.name == model and e.url]


def price_reply(entry: CatalogEntry, reply: ModelReply) -> ModelReply:
    actual = reply.cost_usd
    if entry.free:
        return replace(reply, cost_usd=actual or 0.0, paid_free=bool(actual and actual > 0))
    if actual is not None:
        return reply
    return replace(reply, cost_usd=entry.price(reply.tokens_in, reply.tokens_out))


class FailoverChat:
    def __init__(
        self,
        catalog: Catalog,
        make_chat: Callable[[CatalogEntry], GatewayChat],
        model: str,
        rounds: int = ROUTE_ROUNDS,
        retry_s: float = ROUTE_RETRY_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._catalog = catalog
        self._make_chat = make_chat
        self._model = model
        self._rounds = rounds
        self._retry_s = retry_s
        self._sleep = sleep
        self._preferred: str | None = None

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        last: ModelError | None = None
        for attempt in range(self._rounds):
            if attempt:
                await self._sleep(self._retry_s)
            entries = await self._ordered(fresh=attempt > 0)
            if not entries:
                last = ModelError(f"модели {self._model!r} нет на живых шлюзах", transient=True)
                continue
            for entry in entries:
                try:
                    reply = await self._make_chat(entry).complete(messages, tools)
                except ModelError as exc:
                    if not exc.transient:
                        raise
                    log.warning("Шлюз %s модели %s: %s", entry.gateway, self._model, exc)
                    last = exc
                    continue
                self._preferred = entry.gateway
                return price_reply(entry, reply)
        assert last is not None
        raise ModelError(f"все шлюзы модели {self._model!r} отказали: {last}")

    async def _ordered(self, fresh: bool) -> list[CatalogEntry]:
        entries = await self._catalog.entries(self._model, fresh)
        return sorted(entries, key=lambda e: e.gateway != self._preferred)


class GatewayModels:
    def __init__(self, catalog: Catalog, token: str) -> None:
        self._catalog = catalog
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    def __call__(self, model: str, job_id: str) -> ChatModel:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        session = self._session

        def make(entry: CatalogEntry) -> GatewayChat:
            return GatewayChat(entry.url, self._token, model, job_id, session=session)

        return FailoverChat(self._catalog, make, model)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
