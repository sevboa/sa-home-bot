from __future__ import annotations

import abc
import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from sa_home_bot.config import DevlabModelConfig, DevlabModelsConfig
from sa_home_bot.devlab import protocol
from sa_home_bot.utils.process import ProcResult, run

log = logging.getLogger(__name__)

UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300)
BEARER = "Bearer "
FALLBACK_FIELDS = frozenset({"models", "route"})
LOCAL_HOST = "127.0.0.1"
LLAMA_HEALTH_PATH = "/health"
LLAMA_CHAT_PATH = "/v1/chat/completions"
LLAMA_HEALTH_TIMEOUT_S = 900.0
LLAMA_HEALTH_POLL_S = 1.0
LLAMA_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=5)
LLAMA_IDLE_CHECK_S = 15.0
LLAMA_CRASH_WINDOW_S = 600.0
LLAMA_MAX_CRASHES = 3
UNIT_SUFFIX = ".service"
UNIT_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")

Runner = Callable[[list[str]], Awaitable[ProcResult]]


class ModelUnavailableError(Exception):
    pass


class ModelBackend(abc.ABC):
    def __init__(self, name: str, spec: DevlabModelConfig) -> None:
        self.name = name
        self.spec = spec

    @abc.abstractmethod
    async def chat(self, payload: dict[str, Any]) -> aiohttp.ClientResponse: ...

    def loaded(self) -> bool | None:
        return None

    def finished(self) -> None:
        return None

    async def close(self) -> None:
        return None


class HttpBackend(ModelBackend):
    def __init__(self, name: str, spec: DevlabModelConfig) -> None:
        super().__init__(name, spec)
        self._session = aiohttp.ClientSession(timeout=UPSTREAM_TIMEOUT)

    async def _post(
        self, url: str, payload: dict[str, Any], headers: dict[str, str], proxy: str | None = None
    ) -> aiohttp.ClientResponse:
        return await self._session.post(url, json=payload, headers=headers, proxy=proxy)

    async def close(self) -> None:
        await self._session.close()


class OpenAiBackend(HttpBackend):
    async def chat(self, payload: dict[str, Any]) -> aiohttp.ClientResponse:
        headers = {"Content-Type": "application/json"}
        if self.spec.api_key:
            headers["Authorization"] = BEARER + self.spec.api_key
        url = self.spec.base_url.rstrip("/") + "/chat/completions"
        body = {k: v for k, v in payload.items() if k not in FALLBACK_FIELDS}
        return await self._post(
            url, {**body, "model": self.spec.model}, headers, self.spec.proxy or None
        )


def model_unit(name: str) -> str:
    return protocol.MODEL_UNIT_PREFIX + UNIT_NAME_UNSAFE.sub("-", name)


class LlamaCppBackend(HttpBackend):
    def __init__(
        self,
        name: str,
        spec: DevlabModelConfig,
        *,
        binary: str,
        port: int,
        runner: Runner = run,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(name, spec)
        self._binary = binary
        self._port = port
        self._runner = runner
        self._clock = clock
        self._sleep = sleep
        self._unit = model_unit(name) + UNIT_SUFFIX
        self._base = f"http://{LOCAL_HOST}:{port}"
        self._lock = asyncio.Lock()
        self._up = False
        self._active = 0
        self._last_used = clock()
        self._crashes: deque[float] = deque()
        self._watch: asyncio.Task[None] | None = None

    def loaded(self) -> bool:
        return self._up

    def argv(self) -> list[str]:
        spec = self.spec
        command = [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            f"--unit={model_unit(self.name)}",
            f"--slice={protocol.DEVLAB_SLICE}",
            f"--property=CPUWeight={protocol.DEVLAB_SLICE_CPU_WEIGHT}",
        ]
        if spec.cpus:
            command += ["taskset", "-c", spec.cpus]
        command += [
            self._binary,
            "--model",
            spec.gguf,
            "--host",
            LOCAL_HOST,
            "--port",
            str(self._port),
            "--n-gpu-layers",
            "0",
        ]
        if spec.context:
            command += ["--ctx-size", str(spec.context)]
        if spec.threads:
            command += ["--threads", str(spec.threads)]
        return command + spec.extra_args

    async def chat(self, payload: dict[str, Any]) -> aiohttp.ClientResponse:
        self._active += 1
        try:
            await self._ensure_up()
            return await self._post(
                self._base + LLAMA_CHAT_PATH,
                {**payload, "model": self.spec.model or self.name},
                {"Content-Type": "application/json"},
            )
        except BaseException:
            self.finished()
            raise

    def finished(self) -> None:
        self._active -= 1
        self._last_used = self._clock()

    async def reap(self) -> None:
        async with self._lock:
            if not self._up:
                return
            if not await self._is_active():
                self._mark_down()
                return
            idle = self._clock() - self._last_used
            if self.spec.idle_unload_s and self._active == 0 and idle >= self.spec.idle_unload_s:
                log.info("Модель %s простаивает %.0f с, выгружаю", self.name, idle)
                await self._stop()
                self._up = False

    async def close(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
            await asyncio.gather(self._watch, return_exceptions=True)
        async with self._lock:
            if self._up:
                await self._stop()
                self._up = False
        await super().close()

    async def _ensure_up(self) -> None:
        async with self._lock:
            if self._up and not await self._is_active():
                self._mark_down()
            if self._up:
                return
            self._check_crash_budget()
            await self._start()

    def _mark_down(self) -> None:
        log.warning("Модель %s: юнит %s упал", self.name, self._unit)
        self._up = False
        self._crashes.append(self._clock())

    def _check_crash_budget(self) -> None:
        now = self._clock()
        while self._crashes and now - self._crashes[0] >= LLAMA_CRASH_WINDOW_S:
            self._crashes.popleft()
        if len(self._crashes) >= LLAMA_MAX_CRASHES:
            wait_min = max(1, round((self._crashes[0] + LLAMA_CRASH_WINDOW_S - now) / 60))
            raise ModelUnavailableError(
                f"модель {self.name!r} падала {len(self._crashes)} раз за "
                f"{LLAMA_CRASH_WINDOW_S / 60:.0f} мин, следующая попытка запуска примерно "
                f"через {wait_min} мин"
            )

    async def _start(self) -> None:
        await self._stop()
        started = await self._runner(self.argv())
        if started.returncode != 0:
            log.error("systemd-run для %s: %s", self.name, started.stderr_text)
            self._crashes.append(self._clock())
            raise ModelUnavailableError(f"модель {self.name!r} не удалось запустить")
        deadline = self._clock() + LLAMA_HEALTH_TIMEOUT_S
        while True:
            if not await self._is_active():
                self._crashes.append(self._clock())
                raise ModelUnavailableError(f"модель {self.name!r} упала при загрузке")
            if await self._healthy():
                break
            if self._clock() >= deadline:
                await self._stop()
                self._crashes.append(self._clock())
                raise ModelUnavailableError(
                    f"модель {self.name!r} не загрузилась за {LLAMA_HEALTH_TIMEOUT_S:.0f} с"
                )
            await self._sleep(LLAMA_HEALTH_POLL_S)
        self._up = True
        self._last_used = self._clock()
        if self._watch is None or self._watch.done():
            self._watch = asyncio.create_task(self._watch_idle())

    async def _healthy(self) -> bool:
        try:
            async with self._session.get(
                self._base + LLAMA_HEALTH_PATH, timeout=LLAMA_PROBE_TIMEOUT
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, TimeoutError):
            return False

    async def _is_active(self) -> bool:
        result = await self._runner(["systemctl", "--user", "is-active", "--quiet", self._unit])
        return result.returncode == 0

    async def _stop(self) -> None:
        await self._runner(["systemctl", "--user", "stop", self._unit])

    async def _watch_idle(self) -> None:
        while self._up:
            await asyncio.sleep(LLAMA_IDLE_CHECK_S)
            await self.reap()


BACKENDS: dict[str, type[ModelBackend]] = {protocol.MODEL_KIND_OPENAI: OpenAiBackend}


def build_backend(
    name: str, spec: DevlabModelConfig, cfg: DevlabModelsConfig | None = None
) -> ModelBackend:
    if spec.kind != protocol.MODEL_KIND_LLAMACPP:
        return BACKENDS[spec.kind](name, spec)
    cfg = cfg or DevlabModelsConfig()
    local = [n for n, s in cfg.entries.items() if s.kind == protocol.MODEL_KIND_LLAMACPP]
    port = cfg.llamacpp_port_min + local.index(name)
    if port > cfg.llamacpp_port_max:
        raise RuntimeError(
            f"devlab_models: локальных моделей {len(local)}, а портов "
            f"{cfg.llamacpp_port_min}-{cfg.llamacpp_port_max} не хватает"
        )
    return LlamaCppBackend(name, spec, binary=cfg.llamacpp_bin, port=port)
