from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp
from aiohttp import web

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.http import BEARER_PREFIX, Handler
from sa_home_bot.devlab.models.backends import ModelBackend, ModelUnavailableError
from sa_home_bot.devlab.models.hubclient import HubClient, HubUnavailableError
from sa_home_bot.devlab.models.ledger import Ledger
from sa_home_bot.devlab.models.limits import RateWindow
from sa_home_bot.devlab.usage import usage_cost

log = logging.getLogger(__name__)

TOKENS_PER_PRICE_UNIT = 1_000_000
ERROR_LOG_CHARS = 1500
SSE_DATA_PREFIX = b"data:"


class UsageSniffer:
    def __init__(self) -> None:
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd: float | None = None
        self._tail = b""

    def feed(self, chunk: bytes) -> None:
        lines = (self._tail + chunk).split(b"\n")
        self._tail = lines.pop()
        for line in lines:
            self._line(line.strip())

    def finish(self) -> None:
        self._line(self._tail.strip())
        self._tail = b""

    def read_usage(self, usage: Any) -> None:
        if isinstance(usage, dict):
            self.tokens_in = int(usage.get("prompt_tokens") or 0)
            self.tokens_out = int(usage.get("completion_tokens") or 0)
            self.cost_usd = usage_cost(usage)

    def _line(self, line: bytes) -> None:
        if not line.startswith(SSE_DATA_PREFIX):
            return
        try:
            event = json.loads(line[len(SSE_DATA_PREFIX) :])
        except ValueError:
            return
        if isinstance(event, dict):
            self.read_usage(event.get("usage"))


class Gateway:
    def __init__(
        self,
        backends: dict[str, ModelBackend],
        ledger: Ledger,
        hub: HubClient,
        windows: dict[str, RateWindow],
        url: str = "",
    ) -> None:
        self.url = url
        self.backends = backends
        self.ledger = ledger
        self.hub = hub
        self.windows = windows

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth])
        app.router.add_get(protocol.HTTP_HEALTH_PATH, self._health)
        app.router.add_get(protocol.HTTP_MODELS_PATH, self._models)
        app.router.add_post(protocol.HTTP_CHAT_PATH, self._chat)
        return app

    async def heartbeat_body(self) -> dict[str, Any]:
        models = []
        limits: dict[str, Any] = {}
        for name, backend in self.backends.items():
            spec = backend.spec
            entry: dict[str, Any] = {
                protocol.MODEL_NAME: name,
                protocol.MODEL_KIND: spec.kind,
                protocol.MODEL_FREE: spec.free,
                protocol.MODEL_PRICE_IN: spec.price_in_per_m,
                protocol.MODEL_PRICE_OUT: spec.price_out_per_m,
                protocol.MODEL_CONTEXT: spec.context,
                protocol.MODEL_REMOTE: spec.remote,
            }
            loaded = backend.loaded()
            if loaded is not None:
                entry[protocol.MODEL_LOADED] = loaded
            models.append(entry)
            left = None
            if spec.rpd:
                left = max(0, spec.rpd - await self.ledger.requests_today(name))
            limits[name] = {protocol.LIMIT_RPD_LEFT: left}
        return {
            protocol.FIELD_MODELS: models,
            protocol.FIELD_LIMITS: limits,
            protocol.FIELD_URL: self.url,
        }

    @web.middleware
    async def _auth(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path == protocol.HTTP_HEALTH_PATH:
            return await handler(request)
        header = request.headers.get("Authorization", "")
        token = (
            header[len(BEARER_PREFIX) :].strip() if header.lower().startswith(BEARER_PREFIX) else ""
        )
        if not token:
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Bearer"})
        try:
            principal = await self.hub.introspect(token, request.remote or "")
        except HubUnavailableError as exc:
            log.warning("Хаб недоступен для проверки токена: %s", exc)
            raise web.HTTPServiceUnavailable() from exc
        if principal is None:
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Bearer"})
        if principal.role != protocol.ROLE_EXECUTOR:
            raise web.HTTPForbidden()
        return await handler(request)

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({protocol.FIELD_OK: True})

    async def _models(self, request: web.Request) -> web.Response:
        data = [{"id": name, "object": "model"} for name in self.backends]
        return web.json_response({"object": "list", "data": data})

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return error_response(400, "тело запроса: ожидается JSON")
        if not isinstance(body, dict):
            return error_response(400, "тело запроса: ожидается JSON-объект")
        name = body.get("model")
        backend = self.backends.get(name) if isinstance(name, str) else None
        if backend is None:
            return error_response(404, f"модель {name!r} на шлюзе не настроена")
        job = request.headers.get(protocol.HEADER_JOB, "")
        spec = backend.spec
        if spec.rpd and await self.ledger.requests_today(backend.name) >= spec.rpd:
            return error_response(
                429, f"суточный лимит модели {backend.name!r} ({spec.rpd} запросов) исчерпан"
            )
        await self.windows[backend.name].acquire()
        await self.ledger.add(backend.name, job, requests=1)
        stream = bool(body.get("stream"))
        payload = body
        if stream:
            options = body.get("stream_options")
            payload = {
                **body,
                "stream_options": {
                    "include_usage": True,
                    **(options if isinstance(options, dict) else {}),
                },
            }
        try:
            upstream = await backend.chat(payload)
        except ModelUnavailableError as exc:
            return error_response(503, str(exc))
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("Провайдер %s недоступен: %s", backend.name, type(exc).__name__)
            return error_response(502, "провайдер недоступен")
        sniffer = UsageSniffer()
        try:
            async with upstream:
                if stream and upstream.status < 400:
                    return await self._relay_stream(request, upstream, sniffer)
                data = await upstream.read()
                if upstream.status < 400:
                    sniffer.read_usage(_json_usage(data))
                    if _json_error(data) is not None:
                        log.warning(
                            "Провайдер %s вернул HTTP %s с телом-ошибкой: %s",
                            backend.name,
                            upstream.status,
                            data[:ERROR_LOG_CHARS].decode("utf-8", "replace"),
                        )
                return web.Response(
                    body=data, status=upstream.status, content_type=upstream.content_type
                )
        finally:
            backend.finished()
            await self._record(backend, job, sniffer)

    async def _relay_stream(
        self, request: web.Request, upstream: aiohttp.ClientResponse, sniffer: UsageSniffer
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=upstream.status,
            headers={"Content-Type": upstream.content_type, "Cache-Control": "no-cache"},
        )
        await response.prepare(request)
        async for chunk in upstream.content.iter_any():
            sniffer.feed(chunk)
            await response.write(chunk)
        sniffer.finish()
        await response.write_eof()
        return response

    async def _record(self, backend: ModelBackend, job: str, sniffer: UsageSniffer) -> None:
        spec = backend.spec
        cost = sniffer.cost_usd
        if cost is None:
            cost = (
                sniffer.tokens_in * spec.price_in_per_m + sniffer.tokens_out * spec.price_out_per_m
            ) / TOKENS_PER_PRICE_UNIT
        await self.ledger.add(
            backend.name,
            job,
            tokens_in=sniffer.tokens_in,
            tokens_out=sniffer.tokens_out,
            cost_usd=cost,
        )


def _json_error(data: bytes) -> Any:
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    return parsed.get(protocol.FIELD_ERROR) if isinstance(parsed, dict) else None


def _json_usage(data: bytes) -> Any:
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    return parsed.get("usage") if isinstance(parsed, dict) else None


def error_response(status: int, message: str) -> web.Response:
    return web.json_response({protocol.FIELD_ERROR: message}, status=status)
