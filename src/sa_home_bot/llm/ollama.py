"""Тонкий клиент Ollama (loopback, только localhost) + прогрев/усыпление
контейнера внутри WSL2 на этой же машине (см. LLM_INTEGRATION_PLAN.md §0-1).

Никакого httpx/aiohttp — проект сознательно не тянет HTTP-библиотеку ради
одной локальной службы (urllib + asyncio.to_thread хватает для loopback,
см. pyproject.toml).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from sa_home_bot.config import LlmConfig
from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError

log = logging.getLogger(__name__)

_VERSION_PROBE_TIMEOUT_S = 3.0
_WARMUP_POLL_INTERVAL_S = 2.0
# Живая находка 2026-07-23: WSL на этой конкретной машине (старая LTSC-
# инсталляция) автоматически "засыпает" между вызовами wsl.exe и холодно
# стартует по новой на каждый прогрев — иногда заметно дольше 90с (первый
# прогон в проде занял ~95с и не уложился, curl сразу после — уже отвечал).
# Это ожидаемый режим работы (по требованию пользователя 2026-07-23: модель
# не должна быть постоянно поднята — машина используется и для другого),
# не баг, который нужно скрывать держа WSL всегда тёплым.
_WARMUP_TIMEOUT_S = 150.0
# Живая находка 2026-07-23 (позже в тот же день): даже когда /api/version
# уже отвечает, самый первый /api/generate|chat сразу после холодного старта
# контейнера иногда обрывается ("Remote end closed connection without
# response") — GPU/модельный бэкенд Ollama ещё не полностью готов принимать
# реальные запросы, хотя HTTP-сервер уже слушает. Один короткий ретрай.
_POST_RETRY_ATTEMPTS = 3
_POST_RETRY_DELAY_S = 3.0


def _post_json_sync(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — только localhost
        return json.loads(resp.read())


def _get_json_sync(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — только localhost
        return json.loads(resp.read())


async def ollama_version(
    cfg: LlmConfig, timeout: float = _VERSION_PROBE_TIMEOUT_S
) -> dict[str, Any] | None:
    """None — Ollama сейчас не отвечает (контейнер спит/WSL не поднята)."""
    try:
        return await asyncio.to_thread(_get_json_sync, f"{cfg.ollama_url}/api/version", timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


class WslKeepalive:
    """Держит WSL2-VM живой фоновым `wsl -d ... -- sleep N` процессом:
    короткие `wsl -d ... -- <cmd>` вызовы (прогрев, ретраи) сами по себе
    не мешают VM погаснуть сразу после выхода — держит только ЖИВОЙ
    присоединённый процесс внутри неё.

    Живая находка 2026-07-23: это не одноразовая обвязка вокруг ОДНОГО
    запроса (так было раньше — и WSL гасла уже через секунды ПОСЛЕ ответа,
    задолго до 30-минутного idle-таймера, живая находка того же дня чуть
    позже) — владелец жизненного цикла теперь llm/service.py: старт при
    выходе из простоя, стоп — вместе с реальным сном по idle_sleep_after_s.
    """

    def __init__(self, cfg: LlmConfig, duration_s: float) -> None:
        self._cfg = cfg
        self._duration_s = duration_s
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "wsl",
                "-d",
                self._cfg.wsl_distro,
                "-u",
                "root",
                "--",
                "sleep",
                str(int(self._duration_s)),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("llm: не удалось запустить keepalive-процесс WSL: %s", exc)
            self._proc = None

    async def stop(self) -> None:
        if self._proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            self._proc.terminate()
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        self._proc = None


async def _wsl_exec(cfg: LlmConfig, *args: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "wsl",
            "-d",
            cfg.wsl_distro,
            "-u",
            "root",
            "--",
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_raw = await proc.communicate()
    except OSError as exc:
        log.warning("llm: не удалось выполнить wsl %s: %s", args, exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "llm: wsl %s завершился кодом %s: %s",
            args,
            proc.returncode,
            stderr_raw.decode(errors="replace").strip(),
        )
        return False
    return True


async def ensure_running(cfg: LlmConfig) -> None:
    """Прогреть WSL/контейнер, если Ollama сейчас не отвечает.

    Дёшево, если уже поднята: один быстрый `/api/version` и выход. Ждёт
    здесь же, синхронно с вызовом `ask`/`chat` — таймаут на это отдельный
    (`_WARMUP_TIMEOUT_S`, чисто внутренний, локальный), протокольный таймаут
    роя (`Envelope.timeout_s`) считается от начала запроса бота и покрывает
    в том числе и этот прогрев.
    """
    if await ollama_version(cfg) is not None:
        return
    log.info("llm: Ollama не отвечает — прогрев WSL/контейнера %s", cfg.ollama_container)
    await _wsl_exec(cfg, "true")  # поднять сам дистрибутив WSL
    await _wsl_exec(cfg, "docker", "start", cfg.ollama_container)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WARMUP_TIMEOUT_S
    while loop.time() < deadline:
        if await ollama_version(cfg) is not None:
            return
        await asyncio.sleep(_WARMUP_POLL_INTERVAL_S)
    raise ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева WSL/контейнера")


async def stop(cfg: LlmConfig) -> None:
    await _wsl_exec(cfg, "docker", "stop", cfg.ollama_container)


def _is_transient_connection_error(exc: Exception) -> bool:
    # HTTPError — реальный ответ сервера (в т.ч. с ошибкой) — не транзиентная
    # проблема соединения, ретраить бессмысленно (тот же результат снова).
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


async def _post_with_retry(cfg: LlmConfig, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """``ensure_running`` вызывается на КАЖДУЮ попытку, не один раз до цикла:
    без него ретрай просто бился бы в ту же отвалившуюся WSL/контейнер.
    ``ensure_running`` быстрый no-op, если Ollama и так уже отвечает.

    WSL-keepalive здесь сознательно не заводим — эта функция вызывается на
    КАЖДЫЙ запрос, а держать VM живой нужно на весь «тёплый» idle-период
    (до 30 минут), не только на один запрос (живая находка 2026-07-23: раньше
    keepalive был здесь и WSL гасла уже через секунды после ответа) —
    владеет им llm/service.py::LlmService (WslKeepalive.start()/stop()
    вокруг всего простоя, не вокруг одного запроса)."""
    last_exc: Exception | None = None
    for attempt in range(1, _POST_RETRY_ATTEMPTS + 1):
        await ensure_running(cfg)
        try:
            return await asyncio.to_thread(_post_json_sync, url, payload, cfg.request_timeout_s)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_exc = exc
            if attempt >= _POST_RETRY_ATTEMPTS or not _is_transient_connection_error(exc):
                break
            log.warning(
                "llm: %s оборвался сразу после прогрева (%s) — повтор через %.0fс "
                "(попытка %d/%d)",
                url,
                exc,
                _POST_RETRY_DELAY_S,
                attempt,
                _POST_RETRY_ATTEMPTS,
            )
            await asyncio.sleep(_POST_RETRY_DELAY_S)
    raise ProtoError(ERR_INTERNAL, f"{url}: {last_exc}")


async def generate(cfg: LlmConfig, prompt: str, system: str) -> dict[str, Any]:
    payload = {
        "model": cfg.model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "think": False,
    }
    return await _post_with_retry(cfg, f"{cfg.ollama_url}/api/generate", payload)


async def chat(
    cfg: LlmConfig,
    messages: list[dict[str, Any]],
    system: str,
    tools: list[dict[str, Any]] | None = None,
    think: bool | None = None,
) -> dict[str, Any]:
    full_messages = [{"role": "system", "content": system}, *messages]
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": full_messages,
        "stream": False,
        # think=None — вызывающий не уточнил, использовать дефолт службы
        # (cfg.think_chat). Живая находка 2026-07-24 (вариативное
        # рассуждение): bot/ai_flow.py теперь всегда передаёт think явно
        # (False на быстром проходе, True на проходе-эскалации) — этот
        # дефолт остаётся только подстраховкой для прочих вызовов chat().
        "think": cfg.think_chat if think is None else think,
    }
    if tools:
        payload["tools"] = tools
    return await _post_with_retry(cfg, f"{cfg.ollama_url}/api/chat", payload)
