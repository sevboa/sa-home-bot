from __future__ import annotations

import argparse
import asyncio
import time
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

DEFAULT_IDLE_S = 300.0
DEFAULT_MAX_WAIT_S = 1800.0
DEFAULT_POLL_S = 5.0
DEFAULT_PRIORITY_GRACE_S = 10.0
PRIORITY_POLL_S = 0.2
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
    aliases: dict[str, str] = field(default_factory=dict)
    priority: frozenset[str] = frozenset()
    priority_grace_s: float = DEFAULT_PRIORITY_GRACE_S


def resolve(settings: SwitchSettings, model: str) -> str:
    if model in settings.launchers:
        return model
    target = settings.aliases.get(model)
    if target is not None and target in settings.launchers:
        return target
    raise UnknownModel(f"модель {model!r} не настроена в launchers")


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
        self.clock = clock
        self._lock = asyncio.Lock()

    async def acquire(self, model: str, immediate: bool = False) -> None:
        model = resolve(self.settings, model)
        launcher = self.settings.launchers[model]
        if model == self.current and not self._lock.locked():
            self.active += 1
            return
        async with self._lock:
            if model == self.current:
                self.active += 1
                return
            await self._wait_until_free(0.0 if immediate else self.settings.idle_s)
            await self._launch(model, launcher)
            self.active += 1

    async def _launch(self, model: str, launcher: list[str]) -> None:
        launch = asyncio.create_task(self._run(launcher))
        try:
            code = await asyncio.shield(launch)
        except asyncio.CancelledError:
            self._remember_launch(model, await asyncio.shield(launch))
            raise
        self._remember_launch(model, code)
        if code != 0:
            raise LaunchFailed(f"скрипт {launcher[0]!r} вернул код {code}")

    def _remember_launch(self, model: str, code: int) -> None:
        self.current = model if code == 0 else None

    def release(self) -> None:
        self.active = max(0, self.active - 1)
        self.last_request_end = self.clock()

    async def start(self) -> None:
        await self.acquire(self.settings.default)
        self.release()

    async def _wait_until_free(self, idle_gap: float) -> None:
        deadline = self.clock() + self.settings.max_wait_s
        while not self._is_free(idle_gap):
            if self.clock() >= deadline:
                raise SwitchBusy(BUSY_MESSAGE)
            await asyncio.sleep(self.settings.poll_s)

    def _is_free(self, idle_gap: float) -> bool:
        if self.current is None:
            return True
        if self.active > 0:
            return False
        if self.last_request_end is None:
            return True
        return self.clock() - self.last_request_end >= idle_gap


def json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


@dataclass(eq=False)
class NormalAttempt:
    task: asyncio.Task | None = None
    interrupted: bool = False
    protected: bool = False


class PriorityGate:
    def __init__(self, clock: Callable[[], float], poll_s: float, grace_s: float) -> None:
        self._clock = clock
        self._poll_s = poll_s
        self._grace_s = grace_s
        self.priority_active = 0
        self.last_priority_end: float | None = None
        self.interruptable: set[NormalAttempt] = set()
        self.uninterruptible = 0

    async def enter_priority(self) -> None:
        self.priority_active += 1
        while True:
            attempts = [attempt for attempt in self.interruptable if attempt.task is not None]
            if not attempts:
                break
            for attempt in attempts:
                attempt.interrupted = True
                attempt.task.cancel()
            pending = [attempt.task for attempt in attempts]
            while pending:
                _, pending = await asyncio.wait(pending)
        while self.uninterruptible > 0:
            await asyncio.sleep(PRIORITY_POLL_S)

    def leave_priority(self) -> None:
        self.priority_active = max(0, self.priority_active - 1)
        self.last_priority_end = self._clock()

    async def wait_normal(self) -> None:
        while self.priority_active > 0 or not self._grace_passed():
            await asyncio.sleep(self._poll_s)

    def _grace_passed(self) -> bool:
        return (
            self.last_priority_end is None
            or self._clock() - self.last_priority_end >= self._grace_s
        )

    def register(self, attempt: NormalAttempt) -> None:
        self.interruptable.add(attempt)

    def unregister(self, attempt: NormalAttempt) -> None:
        self.interruptable.discard(attempt)

    def protect_stream(self, attempt: NormalAttempt) -> None:
        self.interruptable.discard(attempt)
        attempt.protected = True
        self.uninterruptible += 1

    def finish_stream(self, attempt: NormalAttempt) -> None:
        if attempt.protected:
            attempt.protected = False
            self.uninterruptible = max(0, self.uninterruptible - 1)


async def relay_chat(
    request: web.Request,
    payload: dict[str, object],
    upstream: str,
    gate: PriorityGate | None = None,
    attempt: NormalAttempt | None = None,
) -> web.StreamResponse:
    headers: dict[str, str] = {}
    if AUTH_HEADER in request.headers:
        headers[AUTH_HEADER] = request.headers[AUTH_HEADER]
    session = aiohttp.ClientSession(timeout=UPSTREAM_TIMEOUT)
    try:
        async with session.post(upstream + CHAT_PATH, json=payload, headers=headers) as resp:
            if payload.get("stream"):
                return await relay_stream(request, resp, gate, attempt)
            data = await resp.read()
            return web.Response(body=data, status=resp.status, content_type=resp.content_type)
    finally:
        await session.close()


async def relay_stream(
    request: web.Request,
    resp: aiohttp.ClientResponse,
    gate: PriorityGate | None = None,
    attempt: NormalAttempt | None = None,
) -> web.StreamResponse:
    headers = {"Cache-Control": "no-cache"}
    if resp.content_type:
        headers["Content-Type"] = resp.content_type
    response = web.StreamResponse(status=resp.status, headers=headers)
    prepared = False
    try:
        async for chunk in resp.content.iter_any():
            if not chunk:
                continue
            if not prepared:
                if gate is not None and attempt is not None:
                    gate.protect_stream(attempt)
                await response.prepare(request)
                prepared = True
            await response.write(chunk)
        if not prepared:
            await response.prepare(request)
        await response.write_eof()
    finally:
        if gate is not None and attempt is not None:
            gate.finish_stream(attempt)
    return response


async def relay_priority(
    request: web.Request,
    payload: dict[str, object],
    upstream: str,
    switch: StrataSwitch,
    gate: PriorityGate,
) -> web.StreamResponse:
    await gate.enter_priority()
    try:
        try:
            await switch.acquire(str(payload.get("model")), immediate=True)
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
    finally:
        gate.leave_priority()


async def run_normal_attempt(
    request: web.Request,
    payload: dict[str, object],
    upstream: str,
    switch: StrataSwitch,
    gate: PriorityGate,
    attempt: NormalAttempt,
) -> web.StreamResponse:
    try:
        await switch.acquire(str(payload.get("model")))
    except SwitchBusy as exc:
        return json_error(503, str(exc))
    except LaunchFailed as exc:
        return json_error(502, str(exc))
    try:
        return await relay_chat(request, payload, upstream, gate, attempt)
    finally:
        switch.release()


async def relay_normal(
    request: web.Request,
    payload: dict[str, object],
    upstream: str,
    switch: StrataSwitch,
    gate: PriorityGate,
) -> web.StreamResponse:
    while True:
        await gate.wait_normal()
        attempt = NormalAttempt()
        attempt.task = asyncio.create_task(
            run_normal_attempt(request, payload, upstream, switch, gate, attempt)
        )
        gate.register(attempt)
        try:
            return await attempt.task
        except asyncio.CancelledError:
            if not attempt.interrupted:
                raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            return json_error(502, f"Strata недоступна: {type(exc).__name__}")
        finally:
            gate.unregister(attempt)
            if attempt.task is not None and not attempt.task.done():
                attempt.task.cancel()


def build_app(switch: StrataSwitch, upstream: str) -> web.Application:
    gate = PriorityGate(switch.clock, switch.settings.poll_s, switch.settings.priority_grace_s)

    async def models(request: web.Request) -> web.Response:
        names = list(switch.settings.launchers) + [
            name for name in switch.settings.aliases if name not in switch.settings.launchers
        ]
        data = [{"id": name, "object": "model"} for name in names]
        return web.json_response({"object": "list", "data": data})

    async def chat(request: web.Request) -> web.StreamResponse:
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return json_error(400, "тело запроса: ожидается JSON")
        if not isinstance(payload, dict):
            return json_error(400, "тело запроса: ожидается JSON-объект")
        requested = payload.get("model")
        try:
            model = resolve(switch.settings, requested) if isinstance(requested, str) else ""
            if not model:
                raise UnknownModel(f"модель {requested!r} не настроена")
        except UnknownModel as exc:
            return json_error(400, str(exc))
        payload["model"] = model
        if requested in switch.settings.priority or model in switch.settings.priority:
            return await relay_priority(request, payload, upstream, switch, gate)
        return await relay_normal(request, payload, upstream, switch, gate)

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
    raw_aliases = data.get("aliases") or {}
    aliases = {str(name): str(target) for name, target in raw_aliases.items()}
    for name, target in aliases.items():
        if target not in launchers:
            raise UnknownModel(f"псевдоним {name!r} ведёт к {target!r}, которого нет в launchers")
    priority = frozenset(str(name) for name in data.get("priority") or ())
    for name in priority:
        if name not in launchers and name not in aliases:
            raise UnknownModel(f"имя приоритета {name!r} нет ни в launchers, ни в aliases")
    return SwitchSettings(
        launchers=launchers,
        default=default,
        idle_s=float(data.get("idle_s", DEFAULT_IDLE_S)),
        max_wait_s=float(data.get("max_wait_s", DEFAULT_MAX_WAIT_S)),
        poll_s=float(data.get("poll_s", DEFAULT_POLL_S)),
        aliases=aliases,
        priority=priority,
        priority_grace_s=float(data.get("priority_grace_s", DEFAULT_PRIORITY_GRACE_S)),
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
