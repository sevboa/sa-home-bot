from __future__ import annotations

import argparse
import asyncio
import time
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiohttp import web

DEFAULT_IDLE_S = 300.0
DEFAULT_MAX_WAIT_S = 1800.0
DEFAULT_POLL_S = 5.0
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
DEFAULT_UPSTREAM = "http://127.0.0.1:8080"

MODELS_PATH = "/v1/models"
CHAT_PATH = "/v1/chat/completions"
AUTH_HEADER = "Authorization"
BUSY_MESSAGE = "модель занята, переключение невозможно"

UPSTREAM_CONNECT_S = 15
UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(
    total=None, sock_connect=UPSTREAM_CONNECT_S, sock_read=None
)


class UnknownModel(ValueError):
    pass


class SwitchBusy(RuntimeError):
    pass


class LaunchFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class SwitchSettings:
    launchers: dict[str, list[str]]
    default: str
    idle_s: float = DEFAULT_IDLE_S
    max_wait_s: float = DEFAULT_MAX_WAIT_S
    poll_s: float = DEFAULT_POLL_S


async def run_launcher(argv: list[str]) -> int:
    process = await asyncio.create_subprocess_exec(*argv)
    return await process.wait()


class StrataSwitch:
    def __init__(
        self,
        settings: SwitchSettings,
        run: Callable[[list[str]], Awaitable[int]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.current: str | None = None
        self.active = 0
        self.last_request_end: float | None = None
        self._run = run or run_launcher
        self._clock = clock
        self._lock = asyncio.Lock()

    async def acquire(self, model: str) -> None:
        launcher = self.settings.launchers.get(model)
        if launcher is None:
            raise UnknownModel(f"модель {model!r} не настроена в launchers")
        if model == self.current and not self._lock.locked():
            self.active += 1
            return
        async with self._lock:
            if model == self.current:
                self.active += 1
                return
            await self._wait_until_free()
            code = await self._run(launcher)
            if code != 0:
                self.current = None
                raise LaunchFailed(f"скрипт {launcher[0]!r} вернул код {code}")
            self.current = model
            self.active += 1

    def release(self) -> None:
        self.active = max(0, self.active - 1)
        self.last_request_end = self._clock()

    async def start(self) -> None:
        await self.acquire(self.settings.default)
        self.release()

    async def _wait_until_free(self) -> None:
        deadline = self._clock() + self.settings.max_wait_s
        while not self._is_free():
            if self._clock() >= deadline:
                raise SwitchBusy(BUSY_MESSAGE)
            await asyncio.sleep(self.settings.poll_s)

    def _is_free(self) -> bool:
        if self.current is None:
            return True
        if self.active > 0:
            return False
        if self.last_request_end is None:
            return True
        return self._clock() - self.last_request_end >= self.settings.idle_s


def json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def relay_chat(
    request: web.Request, payload: dict[str, object], upstream: str
) -> web.StreamResponse:
    headers: dict[str, str] = {}
    if AUTH_HEADER in request.headers:
        headers[AUTH_HEADER] = request.headers[AUTH_HEADER]
    session = aiohttp.ClientSession(timeout=UPSTREAM_TIMEOUT)
    try:
        async with session.post(upstream + CHAT_PATH, json=payload, headers=headers) as resp:
            if payload.get("stream"):
                return await relay_stream(request, resp)
            data = await resp.read()
            return web.Response(body=data, status=resp.status, content_type=resp.content_type)
    finally:
        await session.close()


async def relay_stream(request: web.Request, resp: aiohttp.ClientResponse) -> web.StreamResponse:
    headers = {"Cache-Control": "no-cache"}
    if resp.content_type:
        headers["Content-Type"] = resp.content_type
    response = web.StreamResponse(status=resp.status, headers=headers)
    await response.prepare(request)
    async for chunk in resp.content.iter_any():
        await response.write(chunk)
    await response.write_eof()
    return response


def build_app(switch: StrataSwitch, upstream: str) -> web.Application:
    async def models(request: web.Request) -> web.Response:
        data = [{"id": name, "object": "model"} for name in switch.settings.launchers]
        return web.json_response({"object": "list", "data": data})

    async def chat(request: web.Request) -> web.StreamResponse:
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return json_error(400, "тело запроса: ожидается JSON")
        if not isinstance(payload, dict):
            return json_error(400, "тело запроса: ожидается JSON-объект")
        model = payload.get("model")
        if not isinstance(model, str) or model not in switch.settings.launchers:
            return json_error(400, f"модель {model!r} не настроена")
        try:
            await switch.acquire(model)
        except SwitchBusy as exc:
            return json_error(503, str(exc))
        except LaunchFailed as exc:
            return json_error(502, str(exc))
        try:
            return await relay_chat(request, payload, upstream)
        except (aiohttp.ClientError, TimeoutError) as exc:
            return json_error(502, f"Strata недоступна: {type(exc).__name__}")
        finally:
            switch.release()

    app = web.Application()
    app.router.add_get(MODELS_PATH, models)
    app.router.add_post(CHAT_PATH, chat)
    return app


def load_settings(path: str | Path) -> SwitchSettings:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    raw_launchers = data.get("launchers") or {}
    launchers = {str(name): [str(item) for item in argv] for name, argv in raw_launchers.items()}
    default = str(data.get("default") or "")
    if default not in launchers:
        raise UnknownModel(f"default {default!r} нет в launchers")
    return SwitchSettings(
        launchers=launchers,
        default=default,
        idle_s=float(data.get("idle_s", DEFAULT_IDLE_S)),
        max_wait_s=float(data.get("max_wait_s", DEFAULT_MAX_WAIT_S)),
        poll_s=float(data.get("poll_s", DEFAULT_POLL_S)),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Маршрутизатор моделей Strata")
    parser.add_argument("--config", required=True, help="путь к TOML с launchers")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    switch = StrataSwitch(load_settings(args.config))
    app = build_app(switch, args.upstream)

    async def load_default(app: web.Application) -> None:
        await switch.start()

    app.on_startup.append(load_default)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
