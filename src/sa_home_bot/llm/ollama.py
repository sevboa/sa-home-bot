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
from collections.abc import Callable
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — только localhost
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # Живая находка 2026-08-04: str(exc) по умолчанию — голое "HTTP Error
        # 400: Bad Request", тело ответа Ollama (с точной причиной валидации
        # запроса) терялось молча. Разбор сбоя без него сводился к чтению
        # логов трёх машин (см. bot/node_events.py::ALBERT_TASK_MISSED) —
        # дописываем тело в exc.msg, оно же уходит дальше в ProtoError.
        detail = exc.read().decode("utf-8", "replace").strip()
        with contextlib.suppress(ValueError):
            parsed = json.loads(detail)
            if isinstance(parsed, dict) and parsed.get("error"):
                detail = str(parsed["error"])
        if detail:
            exc.msg = f"{exc.msg}: {detail}"
        raise


def _get_json_sync(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — только localhost
        return json.loads(resp.read())


def _post_json_stream_sync(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    on_chunk: Callable[[str], None],
) -> dict[str, Any]:
    """Как _post_json_sync, но для запросов со `"stream": True` — Ollama
    отдаёт NDJSON (по строке на чанк вместо одного JSON-документа),
    ``on_chunk`` зовётся на каждую строку с уже НАКОПЛЕННЫМ (не дельта)
    текстом — тот же контракт, что ждёт LlmService.run_command (этап 34,
    Фаза 2). Возвращает dict в том же формате, что и chat() без стрима:
    финальный чанк Ollama (``done: true``) несёт tool_calls/метаданные, сюда
    же подставляется полный склеенный текст в message.content."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    content_parts: list[str] = []
    final: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — только localhost
            for raw_line in resp:
                line = raw_line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                piece = (chunk.get("message") or {}).get("content", "")
                if piece:
                    content_parts.append(piece)
                    on_chunk("".join(content_parts))
                if chunk.get("done"):
                    final = chunk
    except urllib.error.HTTPError as exc:
        # См. _post_json_sync — то же дописывание тела ответа в exc.msg.
        detail = exc.read().decode("utf-8", "replace").strip()
        with contextlib.suppress(ValueError):
            parsed = json.loads(detail)
            if isinstance(parsed, dict) and parsed.get("error"):
                detail = str(parsed["error"])
        if detail:
            exc.msg = f"{exc.msg}: {detail}"
        raise
    final_message = dict(final.get("message") or {})
    final_message["content"] = "".join(content_parts)
    final["message"] = final_message
    return final


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

    ``container_backend == "native"`` (Linux, systemd) — своего "подъёма"
    нет: Ollama держит себя живой сама (Restart=on-failure), здесь только
    ждём, пока она ответит (например, после рестарта systemd-юнита) — без
    wsl.exe/docker, которых на этой платформе просто нет.
    """
    if await ollama_version(cfg) is not None:
        return
    if cfg.container_backend == "native":
        log.info("llm: Ollama (native) не отвечает — жду поднятия systemd-сервиса")
    else:
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
    """``native`` — процесс Ollama не трогаем (systemd always-on, отдельный
    выделенный сервер под LLM — гасить его нечем и незачем), только просим
    Ollama немедленно выгрузить модель из RAM явным `keep_alive: 0` (не
    дожидаясь её собственного 5-минутного таймера, живая находка
    2026-07-24 в LlmConfig). ``wsl-docker`` — прежнее поведение: гасим
    контейнер, освобождая VRAM.
    """
    if cfg.container_backend == "native":
        payload = {"model": cfg.model, "prompt": "", "stream": False, "keep_alive": 0}
        try:
            await _post_with_retry(cfg, f"{cfg.ollama_url}/api/generate", payload)
        except ProtoError:
            log.warning("llm: не удалось явно выгрузить модель (keep_alive=0)")
        return
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
    вокруг всего простоя, не вокруг одного запроса).

    Живая находка 2026-07-27: ретраи считались без оглядки на общий бюджет —
    3 попытки по (до 150с прогрева + до request_timeout_s на сам POST)
    складывались в ~20 минут, тогда как вызывающий ждёт ровно
    request_timeout_s (Envelope.timeout_s). Клиент отваливался задолго до
    того, как служба сдавалась, и вместо внятной ошибки наверх приходил
    голый таймаут. Теперь дедлайн общий: новая попытка не начинается, если
    время вызывающего всё равно вышло.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.request_timeout_s
    last_exc: Exception | None = None
    for attempt in range(1, _POST_RETRY_ATTEMPTS + 1):
        await ensure_running(cfg)
        left = deadline - loop.time()
        if left <= 0:
            break
        try:
            return await asyncio.to_thread(_post_json_sync, url, payload, left)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_exc = exc
            if attempt >= _POST_RETRY_ATTEMPTS or not _is_transient_connection_error(exc):
                break
            if deadline - loop.time() <= _POST_RETRY_DELAY_S:
                break
            log.warning(
                "llm: %s оборвался сразу после прогрева (%s) — повтор через %.0fс (попытка %d/%d)",
                url,
                exc,
                _POST_RETRY_DELAY_S,
                attempt,
                _POST_RETRY_ATTEMPTS,
            )
            await asyncio.sleep(_POST_RETRY_DELAY_S)
    if last_exc is None:
        raise ProtoError(
            ERR_INTERNAL, f"{url}: бюджет {cfg.request_timeout_s:.0f}с истёк на прогреве"
        )
    raise ProtoError(ERR_INTERNAL, f"{url}: {last_exc}")


async def _post_stream_with_retry(
    cfg: LlmConfig,
    url: str,
    payload: dict[str, Any],
    on_chunk: Callable[[str], None],
) -> dict[str, Any]:
    """Как _post_with_retry, но для потокового /api/chat (этап 34, Фаза 2).

    Ретраится только попытка, в которой не пришло НИ ОДНОГО чанка — если
    стрим уже начался и оборвался, накопленный текст уже ушёл в on_chunk
    (буфер частичной генерации, llm/service.py) и виден пользователю в
    Rich-черновике; повторять запрос с нуля означало бы задвоение текста в
    буфере на следующий заход, не только трату времени."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.request_timeout_s
    last_exc: Exception | None = None
    for attempt in range(1, _POST_RETRY_ATTEMPTS + 1):
        await ensure_running(cfg)
        left = deadline - loop.time()
        if left <= 0:
            break
        sent_any = False

        def _on_chunk(text: str) -> None:
            nonlocal sent_any
            sent_any = True
            on_chunk(text)

        try:
            return await asyncio.to_thread(_post_json_stream_sync, url, payload, left, _on_chunk)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_exc = exc
            if sent_any or attempt >= _POST_RETRY_ATTEMPTS or not _is_transient_connection_error(
                exc
            ):
                break
            if deadline - loop.time() <= _POST_RETRY_DELAY_S:
                break
            log.warning(
                "llm: %s (стрим) оборвался сразу после прогрева (%s) — повтор через %.0fс "
                "(попытка %d/%d)",
                url,
                exc,
                _POST_RETRY_DELAY_S,
                attempt,
                _POST_RETRY_ATTEMPTS,
            )
            await asyncio.sleep(_POST_RETRY_DELAY_S)
    if last_exc is None:
        raise ProtoError(
            ERR_INTERNAL, f"{url}: бюджет {cfg.request_timeout_s:.0f}с истёк на прогреве"
        )
    raise ProtoError(ERR_INTERNAL, f"{url}: {last_exc}")


# Живая находка 2026-07-24: без явного "keep_alive" в запросе Ollama сама
# выгружает модель из памяти через 5 минут после последнего ответа — это
# ПОЛНОСТЬЮ отдельный от cfg.idle_sleep_after_s таймер (тот управляет только
# жизнью WSL2-VM/контейнера, не самой моделью внутри Ollama). Служба об
# этом не узнаёт (_asleep не выставляется, llm_idle_sleep не эмитится) —
# пользователь видел только "модель зачем-то выгружается за несколько
# минут" без единой реплики в характере. Передаём то же idle_sleep_after_s
# как keep_alive (в секундах, Ollama принимает число) — модель и служба
# теперь согласны, когда наступает сон, и num_ctx (см. LlmConfig.num_ctx) —
# фиксирован явно на каждый вызов одинаково, иначе Ollama не переиспользует
# KV-кэш между запросами, считая их разной конфигурацией.
def _keep_alive_options(cfg: LlmConfig) -> dict[str, Any]:
    return {"keep_alive": cfg.idle_sleep_after_s, "options": {"num_ctx": cfg.num_ctx}}


async def preload(cfg: LlmConfig) -> None:
    """Загрузить модель в память, ничего не генерируя.

    Живая находка 2026-07-27: `ensure_running` поднимает только WSL/контейнер
    и ждёт /api/version — модели в памяти при этом нет, и первый же реальный
    запрос платит десятки секунд за её загрузку. Для отложенных задач это
    значит «прогрето» ≠ «ответит в срок». Ollama на пустом prompt именно
    загружает модель и сразу отвечает (это её штатный способ preload), а
    keep_alive держит её до конца тёплого окна — как и у обычных запросов.
    """
    payload = {"model": cfg.model, "prompt": "", "stream": False, **_keep_alive_options(cfg)}
    await _post_with_retry(cfg, f"{cfg.ollama_url}/api/generate", payload)


async def generate(cfg: LlmConfig, prompt: str, system: str) -> dict[str, Any]:
    payload = {
        "model": cfg.model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "think": False,
        **_keep_alive_options(cfg),
    }
    return await _post_with_retry(cfg, f"{cfg.ollama_url}/api/generate", payload)


async def chat(
    cfg: LlmConfig,
    messages: list[dict[str, Any]],
    system: str,
    tools: list[dict[str, Any]] | None = None,
    think: bool | str | None = None,
) -> dict[str, Any]:
    full_messages = [{"role": "system", "content": system}, *messages]
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": full_messages,
        "stream": False,
        **_keep_alive_options(cfg),
    }
    # think: bool | str | None. None — ключ не отправлять вовсе (не то же, что
    # False: у моделей вроде gemma-4 явный False глушит скрытое рассуждение, а
    # отсутствие флага — нет). Строка ("low"/"medium"/"high") — уровень
    # reasoning для effort-моделей (gpt-oss и т.п.). Чем выразить намерение
    # для конкретной модели решает её профиль (llm/model_profiles.py), сюда
    # приходит уже готовое значение.
    if think is not None:
        payload["think"] = think
    if tools:
        payload["tools"] = tools
    return await _post_with_retry(cfg, f"{cfg.ollama_url}/api/chat", payload)


async def chat_stream(
    cfg: LlmConfig,
    messages: list[dict[str, Any]],
    system: str,
    on_chunk: Callable[[str], None],
    tools: list[dict[str, Any]] | None = None,
    think: bool | str | None = None,
) -> dict[str, Any]:
    """Как chat(), но потоково (этап 34, Фаза 2 — Rich-стрим ответов).

    ``on_chunk`` зовётся синхронно (функция сама выполняется в
    ``asyncio.to_thread``, как и обычный ``chat()``) на каждый кусок текста
    от Ollama, с уже НАКОПЛЕННЫМ содержимым — вызывающий (LlmService,
    llm/service.py) просто кладёт его в буфер частичной генерации как есть,
    без собственной конкатенации дельт. Возвращает тот же формат dict, что
    и ``chat()`` — вызывающему без разницы, каким путём получен ответ."""
    full_messages = [{"role": "system", "content": system}, *messages]
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": full_messages,
        "stream": True,
        **_keep_alive_options(cfg),
    }
    if think is not None:
        payload["think"] = think
    if tools:
        payload["tools"] = tools
    return await _post_stream_with_retry(cfg, f"{cfg.ollama_url}/api/chat", payload, on_chunk)
