from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from sa_home_bot.devlab.neighbor import NeighborGate, start_following, stop_following
from sa_home_bot.utils.lifespan import cancel_task

log = logging.getLogger(__name__)

DEFAULT_IDLE_S = 300.0
DEFAULT_MAX_WAIT_S = 1800.0
DEFAULT_POLL_S = 5.0
DEFAULT_PRIORITY_GRACE_S = 10.0
DEFAULT_COMPANION_IDLE_STOP_S = 1800.0
DEFAULT_FREEZE_MAX_S = 300.0
FREEZE_GUARD_POLL_S = 1.0
ENGINE_FLAG = "--serve"
PROC_ROOT = Path("/proc")
TCP_LISTEN_STATE = "0A"
PROC_NET_TCP = ("net/tcp", "net/tcp6")
PRIORITY_POLL_S = 0.2
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
DEFAULT_UPSTREAM = "http://127.0.0.1:8080"

MODELS_PATH = "/v1/models"
CHAT_PATH = "/v1/chat/completions"
VRAM_PATH = "/v1/vram"
STATUS_PATH = "/v1/switch/status"
RELEASE_PATH = "/v1/switch/companion/release"
COMPANION_NAME = "companion"
RESERVE_MIB_FIELD = "reserve_mib"
AUTH_HEADER = "Authorization"
BUSY_MESSAGE = "модель занята, переключение невозможно"
VRAM_BUSY_MESSAGE = "основная модель занята, видеопамять не перестроена"

UPSTREAM_CONNECT_S = 15
UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(
    total=None, sock_connect=UPSTREAM_CONNECT_S, sock_read=None
)
VRAM_TIMEOUT = aiohttp.ClientTimeout(total=60.0)
VRAM_OK_STATUS = 200
VRAM_BUSY_STATUS = 409

CLIENT_FIELD = "user"
CLIENT_LABEL_MAX_CHARS = 80


class UnknownModel(ValueError):
    pass


class SwitchBusy(RuntimeError):
    pass


class LaunchFailed(RuntimeError):
    pass


class VramFailed(RuntimeError):
    pass


class MainUnreachable(VramFailed):
    pass


def client_label(payload: dict[str, object]) -> str:
    raw = payload.get(CLIENT_FIELD)
    if not isinstance(raw, str) or not raw:
        return ""
    return raw[:CLIENT_LABEL_MAX_CHARS]


@dataclass
class Waiter:
    model: str
    since: float
    priority: bool
    client: str = ""


@dataclass(frozen=True)
class CompanionSettings:
    name: str
    upstream: str
    launcher: list[str]
    stopper: list[str]
    idle_stop_s: float = DEFAULT_COMPANION_IDLE_STOP_S
    main_reserve_active_mib: int = 0
    main_reserve_idle_mib: int = 0


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
    companion: CompanionSettings | None = None
    freeze_engine: bool = False
    freeze_max_s: float = DEFAULT_FREEZE_MAX_S
    llm_socket: str = ""


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


async def guarded_launch(
    run: Callable[[list[str]], Awaitable[int]],
    argv: list[str],
    remember: Callable[[int], None],
) -> None:
    launch = asyncio.create_task(run(argv))
    try:
        code = await asyncio.shield(launch)
    except asyncio.CancelledError:
        remember(await asyncio.shield(launch))
        raise
    remember(code)
    if code != 0:
        raise LaunchFailed(f"скрипт {argv[0]!r} вернул код {code}")


COMPANION_ERRORS = (VramFailed, LaunchFailed, aiohttp.ClientError, TimeoutError, OSError)


async def request_vram(upstream: str, reserve_mib: int, headers: dict[str, str]) -> int:
    session = aiohttp.ClientSession(timeout=VRAM_TIMEOUT)
    try:
        async with session.post(
            upstream + VRAM_PATH, json={RESERVE_MIB_FIELD: reserve_mib}, headers=headers
        ) as resp:
            await resp.read()
            return resp.status
    finally:
        await session.close()


async def apply_vram_reserve(
    upstream: str,
    reserve_mib: int,
    headers: dict[str, str],
    poll_s: float,
    max_wait_s: float,
    clock: Callable[[], float],
) -> None:
    deadline = clock() + max_wait_s
    while True:
        try:
            status = await request_vram(upstream, reserve_mib, headers)
        except aiohttp.ClientConnectorError as exc:
            raise MainUnreachable(
                f"Strata не отвечает на {VRAM_PATH}: {type(exc).__name__}"
            ) from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise VramFailed(
                f"Strata не приняла резерв видеопамяти: {type(exc).__name__}"
            ) from exc
        if status == VRAM_OK_STATUS:
            return
        if status != VRAM_BUSY_STATUS:
            raise VramFailed(f"Strata ответила {status} на {VRAM_PATH}")
        if clock() >= deadline:
            raise VramFailed(VRAM_BUSY_MESSAGE)
        await asyncio.sleep(poll_s)


class Companion:
    def __init__(
        self,
        settings: CompanionSettings,
        switch: StrataSwitch,
        main_upstream: str,
        run: Callable[[list[str]], Awaitable[int]],
        clock: Callable[[], float],
        poll_s: float,
        max_wait_s: float,
    ) -> None:
        self.settings = settings
        self.switch = switch
        self.main_upstream = main_upstream
        self.run = run
        self.clock = clock
        self.poll_s = poll_s
        self.max_wait_s = max_wait_s
        self.running = False
        self.active = 0
        self.serving: list[str] = []
        self.last_request: float | None = None
        self.headers: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None

    def begin_request(self, headers: dict[str, str], client: str = "") -> None:
        self.active += 1
        self.serving.append(client)
        self.last_request = self.clock()
        if headers:
            self.headers = dict(headers)

    def end_request(self, client: str = "") -> None:
        self.active = max(0, self.active - 1)
        if client in self.serving:
            self.serving.remove(client)
        self.last_request = self.clock()

    def is_idle(self) -> bool:
        if not self.running or self.active > 0 or self.last_request is None:
            return False
        return self.clock() - self.last_request >= self.settings.idle_stop_s

    async def _reserve_main(self, reserve_mib: int) -> None:
        try:
            await apply_vram_reserve(
                self.main_upstream,
                reserve_mib,
                self.headers,
                self.poll_s,
                self.max_wait_s,
                self.clock,
            )
        except MainUnreachable as exc:
            log.info("компаньон: основная модель не запущена, резерв пропущен: %s", exc)

    def _remember_launch(self, code: int) -> None:
        self.running = code == 0

    def _remember_stop(self, code: int) -> None:
        self.running = code != 0

    async def ensure_running(self) -> None:
        async with self._lock:
            if self.running:
                return
            await self._reserve_main(self.settings.main_reserve_active_mib)
            await guarded_launch(self.run, self.settings.launcher, self._remember_launch)

    async def _stop_locked(self) -> None:
        await guarded_launch(self.run, self.settings.stopper, self._remember_stop)
        await self._reserve_main(self.settings.main_reserve_idle_mib)

    async def stop_if_idle(self) -> None:
        if not self.is_idle():
            return
        async with self._lock:
            if not self.is_idle():
                return
            await self._stop_locked()

    async def release(self) -> str:
        if not self.running:
            return "not_running"
        if self.active > 0:
            return "busy"
        async with self._lock:
            if not self.running:
                return "not_running"
            if self.active > 0:
                return "busy"
            await self._stop_locked()
            return "stopped"

    async def idle_guard(self) -> None:
        while True:
            await asyncio.sleep(self.poll_s)
            try:
                await self.stop_if_idle()
            except COMPANION_ERRORS as exc:
                log.warning("компаньон: остановка по простою не удалась: %s", exc)

    def start_idle_guard(self) -> None:
        if self._idle_task is None:
            self._idle_task = asyncio.create_task(self.idle_guard())

    async def stop_idle_guard(self) -> None:
        await cancel_task(self._idle_task)
        self._idle_task = None


def build_companion(switch: StrataSwitch, upstream: str) -> Companion | None:
    settings = switch.settings.companion
    if settings is None:
        return None
    return Companion(
        settings=settings,
        switch=switch,
        main_upstream=upstream,
        run=switch.run,
        clock=switch.clock,
        poll_s=switch.settings.poll_s,
        max_wait_s=switch.settings.max_wait_s,
    )


COMPANION_KEY = web.AppKey(COMPANION_NAME, Companion)


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
        self.run = run or run_launcher
        self.clock = clock
        self.launching: str | None = None
        self.launch_started: float | None = None
        self.waiting: list[Waiter] = []
        self.serving: list[str] = []
        self._lock = asyncio.Lock()

    async def acquire(self, model: str, immediate: bool = False, client: str = "") -> None:
        model = resolve(self.settings, model)
        launcher = self.settings.launchers[model]
        if model == self.current and self.launching is None:
            self.active += 1
            self.serving.append(client)
            return
        waiter = Waiter(model, self.clock(), immediate, client)
        self.waiting.append(waiter)
        try:
            async with self._lock:
                if model == self.current:
                    self.active += 1
                    self.serving.append(client)
                    return
                await self._wait_until_free(0.0 if immediate else self.settings.idle_s)
                await self._launch(model, launcher)
                self.active += 1
                self.serving.append(client)
        finally:
            self.waiting.remove(waiter)

    async def _launch(self, model: str, launcher: list[str]) -> None:
        self.launching = model
        self.launch_started = self.clock()
        try:
            await guarded_launch(self.run, launcher, partial(self._remember_launch, model))
        finally:
            self.launching = None
            self.launch_started = None

    def _remember_launch(self, model: str, code: int) -> None:
        self.current = model if code == 0 else None

    def release(self, client: str = "") -> None:
        self.active = max(0, self.active - 1)
        if client in self.serving:
            self.serving.remove(client)
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


def parent_pid(stat: str) -> int:
    return int(stat.rsplit(")", 1)[1].split()[1])


def engine_pids(root: int, proc: Path = PROC_ROOT) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            ppid = parent_pid((entry / "stat").read_text())
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))
    found: list[int] = []
    queue = list(children.get(root, []))
    while queue:
        pid = queue.pop()
        queue.extend(children.get(pid, []))
        try:
            argv = (proc / str(pid) / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if ENGINE_FLAG.encode() in argv:
            found.append(pid)
    return sorted(found)


def listening_inodes(port: int, proc: Path = PROC_ROOT) -> set[str]:
    wanted = f"{port:04X}"
    inodes: set[str] = set()
    for table in PROC_NET_TCP:
        try:
            rows = (proc / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 10 or fields[3] != TCP_LISTEN_STATE:
                continue
            if fields[1].rsplit(":", 1)[-1] == wanted:
                inodes.add(fields[9])
    return inodes


def socket_owners(inodes: set[str], proc: Path = PROC_ROOT) -> list[int]:
    links = {f"socket:[{inode}]" for inode in inodes}
    owners: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target in links:
                owners.append(int(entry.name))
                break
    return sorted(owners)


def upstream_engine_pids(port: int, proc: Path = PROC_ROOT) -> list[int]:
    found: set[int] = set()
    for owner in socket_owners(listening_inodes(port, proc), proc):
        found.update(engine_pids(owner, proc))
    return sorted(found)


def upstream_port(url: str) -> int:
    parts = urlsplit(url)
    return parts.port or (443 if parts.scheme == "https" else 80)


class EngineFreezer:
    def __init__(
        self,
        find: Callable[[], list[int]],
        clock: Callable[[], float],
        max_s: float,
        send: Callable[[int, int], None] = os.kill,
    ) -> None:
        self._find = find
        self._clock = clock
        self._max_s = max_s
        self._send = send
        self.holders = 0
        self.frozen: list[int] = []
        self.frozen_since: float | None = None
        self.expired = False

    def hold(self) -> None:
        self.holders += 1
        if self.holders == 1 and not self.frozen:
            self.expired = False
            self.frozen = self._find()
            self.frozen_since = self._clock()
            self._signal(self.frozen, signal.SIGSTOP)
            log.info("основная модель заморожена на время приоритета: %s", self.frozen)

    def release(self) -> None:
        self.holders = max(0, self.holders - 1)
        if self.holders == 0:
            self.thaw()
            self.expired = False

    def thaw(self) -> None:
        pids = self.frozen
        if not pids:
            return
        self.frozen = []
        self.frozen_since = None
        self._signal(pids, signal.SIGCONT)
        log.info("основная модель разморожена: %s", pids)

    def expire_if_stuck(self) -> bool:
        if self.frozen_since is None or self._clock() - self.frozen_since < self._max_s:
            return False
        log.warning(
            "приоритет держит основную модель дольше %.0f с, размораживаю по freeze_max_s",
            self._max_s,
        )
        self.expired = True
        self.thaw()
        return True

    async def guard(self, poll_s: float = FREEZE_GUARD_POLL_S) -> None:
        while True:
            await asyncio.sleep(poll_s)
            self.expire_if_stuck()

    def _signal(self, pids: list[int], sig: int) -> None:
        for pid in pids:
            try:
                self._send(pid, sig)
            except ProcessLookupError:
                continue
            except PermissionError:
                log.warning("нет прав послать сигнал %s процессу %s", sig, pid)


class PriorityGate:
    def __init__(
        self,
        clock: Callable[[], float],
        poll_s: float,
        grace_s: float,
        freezer: EngineFreezer | None = None,
    ) -> None:
        self._clock = clock
        self._poll_s = poll_s
        self._grace_s = grace_s
        self.freezer = freezer
        self.priority_active = 0
        self.last_priority_end: float | None = None
        self.interruptable: set[NormalAttempt] = set()
        self.uninterruptible = 0

    async def enter_priority(self, preempt: bool = True) -> None:
        self.priority_active += 1
        if preempt or self.freezer is None:
            await self._preempt()

    def freeze_engine(self) -> bool:
        if self.freezer is None:
            return False
        self.freezer.hold()
        return True

    def unfreeze_engine(self) -> None:
        if self.freezer is not None:
            self.freezer.release()

    async def enter_hold(self) -> None:
        await self.enter_priority(preempt=False)
        self.freeze_engine()

    async def leave_hold(self) -> None:
        self.unfreeze_engine()
        self.leave_priority()

    async def _preempt(self) -> None:
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


def auth_headers(request: web.Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    if AUTH_HEADER in request.headers:
        headers[AUTH_HEADER] = request.headers[AUTH_HEADER]
    return headers


async def relay_chat(
    request: web.Request,
    payload: dict[str, object],
    upstream: str,
    gate: PriorityGate | None = None,
    attempt: NormalAttempt | None = None,
) -> web.StreamResponse:
    headers = auth_headers(request)
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
    label = client_label(payload)
    try:
        try:
            await switch.acquire(str(payload.get("model")), immediate=True, client=label)
        except SwitchBusy as exc:
            return json_error(503, str(exc))
        except LaunchFailed as exc:
            return json_error(502, str(exc))
        try:
            return await relay_chat(request, payload, upstream)
        except (aiohttp.ClientError, TimeoutError) as exc:
            return json_error(502, f"Strata недоступна: {type(exc).__name__}")
        finally:
            switch.release(label)
    finally:
        gate.leave_priority()


async def relay_companion(
    request: web.Request,
    payload: dict[str, object],
    companion: Companion,
    gate: PriorityGate,
) -> web.StreamResponse:
    await gate.enter_priority(preempt=not companion.running)
    label = client_label(payload)
    companion.begin_request(auth_headers(request), label)
    frozen = False
    try:
        try:
            await companion.ensure_running()
        except VramFailed as exc:
            return json_error(503, str(exc))
        except (LaunchFailed, OSError) as exc:
            return json_error(502, str(exc))
        frozen = gate.freeze_engine()
        try:
            return await relay_chat(request, payload, companion.settings.upstream)
        except (aiohttp.ClientError, TimeoutError) as exc:
            return json_error(502, f"PolyStrata недоступна: {type(exc).__name__}")
    finally:
        if frozen:
            gate.unfreeze_engine()
        companion.end_request(label)
        gate.leave_priority()


async def run_normal_attempt(
    request: web.Request,
    payload: dict[str, object],
    upstream: str,
    switch: StrataSwitch,
    gate: PriorityGate,
    attempt: NormalAttempt,
) -> web.StreamResponse:
    label = client_label(payload)
    try:
        await switch.acquire(str(payload.get("model")), client=label)
    except SwitchBusy as exc:
        return json_error(503, str(exc))
    except LaunchFailed as exc:
        return json_error(502, str(exc))
    try:
        return await relay_chat(request, payload, upstream, gate, attempt)
    finally:
        switch.release(label)


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


def elapsed_since(start: float | None, now: float) -> float | None:
    if start is None:
        return None
    return round(now - start, 1)


def switch_status(
    switch: StrataSwitch, gate: PriorityGate, companion: Companion | None
) -> dict[str, object]:
    now = switch.clock()
    launching: dict[str, object] | None = None
    if switch.launching is not None:
        launching = {"model": switch.launching, "for_s": elapsed_since(switch.launch_started, now)}
    companion_status: dict[str, object] | None = None
    if companion is not None:
        companion_status = {
            "name": companion.settings.name,
            "running": companion.running,
            "active": companion.active,
            "serving": list(companion.serving),
            "idle_for_s": elapsed_since(companion.last_request, now),
            "idle_stop_s": companion.settings.idle_stop_s,
        }
    return {
        "current": switch.current,
        "launching": launching,
        "active": switch.active,
        "serving": list(switch.serving),
        "idle_for_s": elapsed_since(switch.last_request_end, now),
        "idle_s": switch.settings.idle_s,
        "waiting": [
            {
                "model": waiter.model,
                "for_s": elapsed_since(waiter.since, now),
                "priority": waiter.priority,
                "client": waiter.client,
            }
            for waiter in switch.waiting
        ],
        "priority_active": gate.priority_active,
        "engine_frozen": bool(gate.freezer is not None and gate.freezer.frozen),
        "companion": companion_status,
    }


def build_app(
    switch: StrataSwitch, upstream: str, freezer: EngineFreezer | None = None
) -> web.Application:
    gate = PriorityGate(
        switch.clock, switch.settings.poll_s, switch.settings.priority_grace_s, freezer
    )
    companion = build_companion(switch, upstream)

    async def models(request: web.Request) -> web.Response:
        names = list(switch.settings.launchers) + [
            name for name in switch.settings.aliases if name not in switch.settings.launchers
        ]
        if companion is not None:
            names.append(companion.settings.name)
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
        if companion is not None and requested == companion.settings.name:
            payload["model"] = companion.settings.name
            return await relay_companion(request, payload, companion, gate)
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

    async def status(request: web.Request) -> web.Response:
        return web.json_response(switch_status(switch, gate, companion))

    async def release(request: web.Request) -> web.Response:
        if companion is None:
            return json_error(404, "компаньон не настроен")
        headers = auth_headers(request)
        if headers:
            companion.headers = headers
        result = await companion.release()
        return web.json_response({"result": result})

    app = web.Application()
    app.router.add_get(MODELS_PATH, models)
    app.router.add_get(STATUS_PATH, status)
    app.router.add_post(CHAT_PATH, chat)
    app.router.add_post(RELEASE_PATH, release)
    app[COMPANION_KEY] = companion
    if companion is not None:

        async def start_companion_guard(app: web.Application) -> None:
            companion.start_idle_guard()

        async def stop_companion_guard(app: web.Application) -> None:
            await companion.stop_idle_guard()

        app.on_startup.append(start_companion_guard)
        app.on_cleanup.append(stop_companion_guard)
    if freezer is not None:
        attach_freezer(app, freezer, gate, switch.settings)
    return app


FREEZE_GUARD_KEY = web.AppKey("freeze_guard", asyncio.Task)
HOLDS_KEY = web.AppKey("holds", asyncio.Task)


def attach_freezer(
    app: web.Application, freezer: EngineFreezer, gate: PriorityGate, settings: SwitchSettings
) -> None:
    holds = NeighborGate(
        freeze=gate.enter_hold, thaw=gate.leave_hold, freeze_max_s=settings.freeze_max_s
    )

    async def start(app: web.Application) -> None:
        app[FREEZE_GUARD_KEY] = asyncio.create_task(freezer.guard())
        if settings.llm_socket:
            app[HOLDS_KEY] = start_following(holds, settings.llm_socket)

    async def stop(app: web.Application) -> None:
        if HOLDS_KEY in app:
            await stop_following(app[HOLDS_KEY], holds)
        await cancel_task(app[FREEZE_GUARD_KEY])
        freezer.thaw()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)


def load_companion(data: dict[str, object]) -> CompanionSettings | None:
    raw = data.get("companion")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise UnknownModel("секция companion: ожидается таблица")
    table = raw
    name = str(table.get("name") or "")
    if not name:
        raise UnknownModel("companion: нет поля name")
    upstream = str(table.get("upstream") or "")
    if not upstream:
        raise UnknownModel(f"companion {name!r}: нет поля upstream")
    launcher = _argv(table.get("launcher"), "launcher", name)
    stopper = _argv(table.get("stopper"), "stopper", name)
    return CompanionSettings(
        name=name,
        upstream=upstream,
        launcher=launcher,
        stopper=stopper,
        idle_stop_s=float(table.get("idle_stop_s", DEFAULT_COMPANION_IDLE_STOP_S)),
        main_reserve_active_mib=int(table.get("main_reserve_active_mib", 0)),
        main_reserve_idle_mib=int(table.get("main_reserve_idle_mib", 0)),
    )


def _argv(value: object, field_name: str, companion_name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise UnknownModel(f"companion {companion_name!r}: {field_name} должен быть списком argv")
    return [str(item) for item in value]


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
    companion = load_companion(data)
    if companion is not None:
        if companion.name in launchers:
            raise UnknownModel(f"имя компаньона {companion.name!r} занято launchers")
        if companion.name in aliases:
            raise UnknownModel(f"имя компаньона {companion.name!r} занято aliases")
    return SwitchSettings(
        launchers=launchers,
        default=default,
        idle_s=float(data.get("idle_s", DEFAULT_IDLE_S)),
        max_wait_s=float(data.get("max_wait_s", DEFAULT_MAX_WAIT_S)),
        poll_s=float(data.get("poll_s", DEFAULT_POLL_S)),
        aliases=aliases,
        priority=priority,
        priority_grace_s=float(data.get("priority_grace_s", DEFAULT_PRIORITY_GRACE_S)),
        companion=companion,
        freeze_engine=bool(data.get("freeze_engine", False)),
        freeze_max_s=float(data.get("freeze_max_s", DEFAULT_FREEZE_MAX_S)),
        llm_socket=str(data.get("llm_socket") or ""),
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
    settings = load_settings(args.config)
    switch = StrataSwitch(settings)
    freezer = None
    if settings.freeze_engine:
        freezer = EngineFreezer(
            partial(upstream_engine_pids, upstream_port(args.upstream)),
            switch.clock,
            settings.freeze_max_s,
        )
    app = build_app(switch, args.upstream, freezer)

    async def load_default(app: web.Application) -> None:
        await switch.start()

    app.on_startup.append(load_default)
    web.run_app(app, host=args.host, port=args.port, handler_cancellation=True)


if __name__ == "__main__":
    main()
