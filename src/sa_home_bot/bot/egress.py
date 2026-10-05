"""Этап 52: автовыбор маршрута до Telegram Bot API (direct ↔ SOCKS5 нод роя).

Здоровье и пинг прокси берутся из уже собранных ``vpn_check`` замеров
(действие ``telegram_egress`` службы ``vpn``) — отдельных проб прокси нет.
Здоровье direct — это успех обычных запросов бота (детектор-middleware);
пока сидим на прокси, раз в тик делается один прямой ``getMe``.

Маршрут — строка: ``"direct"`` либо URL прокси ``"socks5://host:port"``.
Модуль сам ничего не оповещает: события ``on_switched``/``on_recovered``
привязывает главная сессия.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import certifi
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe
from aiohttp import TCPConnector

from sa_home_bot.config import TelegramConfig

log = logging.getLogger(__name__)

try:  # aiohttp_socks — опциональная зависимость aiogram
    from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

    _PROXY_ERRORS: tuple[type[BaseException], ...] = (
        ProxyError,
        ProxyTimeoutError,
        ProxyConnectionError,
    )
except ImportError:  # pragma: no cover
    _PROXY_ERRORS = ()

DIRECT = "direct"
TICK_S = 300.0  # в такт vpn_check
BAN_S = 15 * 60.0  # бан прокси, на котором детектор поймал ошибки
DIRECT_RETURN_STREAK = 2  # успешных прямых проверок подряд для возврата
SWITCH_GAIN_FACTOR = 2.0  # между прокси: выигрыш > 2× …
SWITCH_GAIN_MS = 300  # … и > 300 мс
TROUBLE_CONSECUTIVE = 3
TROUBLE_NO_SUCCESS_S = 60.0
DIRECT_PROBE_TIMEOUT_S = 8.0
EGRESS_ACTION = "telegram_egress"


try:  # константу добавляет vpn/protocol.py (Этап 52, A1)
    from sa_home_bot.vpn.protocol import ACTION_TELEGRAM_EGRESS as EGRESS_ACTION  # type: ignore
except ImportError:  # pragma: no cover
    pass


# ---------------------------------------------------------------- выбор маршрута


@dataclass(frozen=True)
class Candidate:
    route: str  # URL прокси
    label: str
    ms: int | None  # None — не замерен (extra_proxies)
    valid: bool


@dataclass(frozen=True)
class Decision:
    route: str
    reason: str
    changed: bool


def socks_url(socks: str) -> str:
    """``host:port`` → ``socks5://host:port``; готовый URL не трогаем."""
    return socks if "://" in socks else f"socks5://{socks}"


def normalize_candidates(raw: list[dict], extra_proxies: list[str]) -> list[Candidate]:
    """Ответы ``telegram_egress`` + ручные прокси → кандидаты.

    Валиден: есть ``socks``, ``check`` не null, ``ok`` и не ``stale``. Замеренные
    отсортированы по ``ms``, невалидные и ручные (без замера) — следом."""
    out: list[Candidate] = []
    seen: set[str] = set()
    for item in raw:
        socks = item.get("socks")
        if not socks:
            continue
        route = socks_url(str(socks))
        check = item.get("check") or None
        ms = None
        valid = False
        if check:
            ms_raw = check.get("ms")
            ms = int(ms_raw) if isinstance(ms_raw, (int, float)) else None
            valid = bool(check.get("ok")) and not check.get("stale") and ms is not None
        label = str(item.get("label") or item.get("node") or route)
        seen.add(route)
        out.append(Candidate(route, label, ms, valid))
    for extra in extra_proxies:
        route = socks_url(extra)
        if route not in seen and extra:
            seen.add(route)
            out.append(Candidate(route, route, None, True))
    out.sort(key=lambda c: (not c.valid, c.ms is None, c.ms if c.ms is not None else 0))
    return out


def choose_route(
    *,
    current: str,
    direct_ok: bool,
    direct_streak: int,
    candidates: list[dict],
    bans: dict[str, float],
    now: float,
    prefer_direct: bool = True,
    extra_proxies: list[str] | None = None,
) -> Decision:
    """Чистый выбор маршрута (без I/O).

    ``direct_ok`` — direct сейчас считается живым (для текущего direct: нет
    беды у детектора); ``direct_streak`` — подряд успешных прямых проверок
    (нужно ≥ 2, чтобы вернуться с прокси). ``bans`` — {route: время конца бана}.
    """
    cands = normalize_candidates(candidates, extra_proxies or [])
    usable = [c for c in cands if c.valid and bans.get(c.route, 0.0) <= now]
    by_route = {c.route: c for c in usable}

    if current == DIRECT:
        direct_usable = direct_ok
    else:
        direct_usable = direct_ok and direct_streak >= DIRECT_RETURN_STREAK

    if direct_usable and (prefer_direct or current == DIRECT):
        if current == DIRECT:
            return Decision(DIRECT, "direct жив", False)
        return Decision(DIRECT, "direct снова стабильно жив", True)

    cur = by_route.get(current)
    best = usable[0] if usable else None
    if current != DIRECT and cur is not None:
        # Текущий прокси годен — меняем только при заметном выигрыше.
        if (
            best is not None
            and best.route != cur.route
            and best.ms is not None
            and cur.ms is not None
            and cur.ms > best.ms * SWITCH_GAIN_FACTOR
            and cur.ms - best.ms > SWITCH_GAIN_MS
        ):
            return Decision(best.route, f"выигрыш {cur.ms}→{best.ms} мс", True)
        return Decision(current, "текущий прокси годен", False)

    if best is not None:
        why = "direct недоступен" if current == DIRECT else "текущий прокси негоден"
        if current == DIRECT and direct_ok and not prefer_direct:
            return Decision(DIRECT, "direct жив", False)
        return Decision(best.route, why, True)
    if direct_usable:  # prefer_direct=False, но кроме direct ничего нет
        if current == DIRECT:
            return Decision(DIRECT, "direct жив", False)
        return Decision(DIRECT, "кандидатов нет, direct жив", True)
    return Decision(current, "кандидатов нет", False)


# ------------------------------------------------------ переключение сессии


def apply_route(session: AiohttpSession, route: str) -> None:
    """Переключить живой ``AiohttpSession`` на маршрут (коннектор сменится на
    ближайшем запросе; long-poll aiogram переподнимет сам)."""
    if route == DIRECT:
        # Setter ``proxy`` к direct не возвращает — повторяем __init__ aiogram.
        session._connector_type = TCPConnector
        session._connector_init = {
            "ssl": ssl.create_default_context(cafile=certifi.where()),
            "limit": 100,
            "ttl_dns_cache": 3600,
        }
        session._proxy = None
        session._should_reset_connector = True
    else:
        session.proxy = route


def current_route(session: AiohttpSession) -> str:
    proxy = session.proxy
    return DIRECT if not proxy else str(proxy)


# ------------------------------------------------------------------ детектор


_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    TelegramNetworkError,
    aiohttp.ClientError,
    asyncio.TimeoutError,
    *_PROXY_ERRORS,
)


class TroubleDetector(BaseRequestMiddleware):
    """Считает подряд сетевые ошибки запросов к Bot API.

    Триггер: ``TROUBLE_CONSECUTIVE`` подряд либо нет успеха ``TROUBLE_NO_SUCCESS_S``
    при наличии неудачных попыток. Таймаут ``getUpdates`` — тоже сбой: штатный
    long-poll возвращает пустой список, а не таймаут, и при блокировке DROP'ом
    (как в KZ) ошибки приходят именно таймаутами — без них молчащий бот сбоя не
    заметил бы. Успех сбрасывает счётчик; первый успех после сработавшего
    триггера зовёт ``on_recovered`` (даже если между ними сменился маршрут)."""

    def __init__(
        self,
        on_trouble: Callable[[], Any] | None = None,
        on_recovered: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.on_trouble = on_trouble
        self.on_recovered = on_recovered
        self._clock = clock
        self.failures = 0
        self.last_success = clock()
        self.troubled = False
        # Был сбой, а успеха после него ещё не было — переживает reset(),
        # иначе on_recovered (флаш outbox) потерялся бы при смене маршрута.
        self._awaiting_recovery = False

    def reset(self) -> None:
        """Начать счёт заново (после смены маршрута). Снимает ``troubled`` —
        иначе сбой на НОВОМ маршруте уже не дал бы триггера."""
        self.failures = 0
        self.last_success = self._clock()
        self.troubled = False

    def record_success(self) -> None:
        self.failures = 0
        self.last_success = self._clock()
        self.troubled = False
        if self._awaiting_recovery:
            self._awaiting_recovery = False
            _call_cb(self.on_recovered)

    def record_failure(self) -> None:
        self.failures += 1
        if self.troubled:
            return
        now = self._clock()
        if (
            self.failures >= TROUBLE_CONSECUTIVE
            or now - self.last_success >= TROUBLE_NO_SUCCESS_S
        ):
            self.troubled = True
            self._awaiting_recovery = True
            _call_cb(self.on_trouble)

    async def __call__(self, make_request, bot, method):  # type: ignore[override]
        try:
            result = await make_request(bot, method)
        except _NETWORK_ERRORS:
            self.record_failure()
            raise
        self.record_success()
        return result


def _call_cb(cb: Callable[[], Any] | None) -> None:
    """Синхронный вызов колбэка; корутину запускаем задачей."""
    if cb is None:
        return
    try:
        res = cb()
        if inspect.isawaitable(res):
            asyncio.ensure_future(res)
    except Exception:
        log.exception("egress: колбэк детектора упал")


# ------------------------------------------------------------ сбор кандидатов


def make_fetch_candidates(node_link: Any, observer: str) -> Callable[[], Awaitable[list[dict]]]:
    """Фабрика ``fetch_candidates``: ответы ``telegram_egress`` всех живых vpn-нод."""
    from sa_home_bot.bot import vpn_nodes

    async def _fetch() -> list[dict]:
        return await vpn_nodes.fanout(node_link, EGRESS_ACTION, {"observer": observer})

    return _fetch


# ---------------------------------------------------------------- состояние


def load_saved_state(path: Path) -> dict | None:
    """Последний рабочий маршрут; битый/отсутствующий файл = нет состояния."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("route"), str):
        return None
    return data


def save_state(path: Path, data: dict) -> None:
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.warning("egress: не удалось сохранить состояние в %s", path, exc_info=True)


# ------------------------------------------------------------------ менеджер


class EgressManager:
    """Фоновый менеджер маршрута до Bot API (режим ``proxy_mode="auto"``)."""

    def __init__(
        self,
        bot: Bot,
        cfg: TelegramConfig,
        fetch_candidates: Callable[[], Awaitable[list[dict]]],
        node_id: str,
        state_path: Path = Path("./data/egress.json"),
        clock: Callable[[], float] = time.time,
        probe_direct: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.bot = bot
        self.cfg = cfg
        self.node_id = node_id
        self._fetch = fetch_candidates
        self._state_path = Path(state_path)
        self._clock = clock
        self._probe_direct = probe_direct or self._default_probe_direct
        self.on_switched: Callable[..., Any] | None = None
        self.on_recovered: Callable[[], Any] | None = None

        self.route = current_route(bot.session)
        self.label = DIRECT if self.route == DIRECT else self.route
        self.ms: int | None = None
        self.since = clock()
        self.direct_ok = True
        self.direct_streak = 0
        self.bans: dict[str, float] = {}
        self.saved = load_saved_state(self._state_path)

        self._trouble = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.detector = TroubleDetector(
            on_trouble=self._trouble.set,
            on_recovered=lambda: self._fire(self.on_recovered),
            clock=clock,
        )

    # --- жизненный цикл

    def start(self) -> None:
        self.bot.session.middleware(self.detector)
        self._task = asyncio.create_task(self._loop(), name="egress-manager")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        trouble_wait: asyncio.Task | None = None
        while True:
            trouble_wait = asyncio.ensure_future(self._trouble.wait())
            try:
                await asyncio.wait_for(asyncio.shield(trouble_wait), timeout=TICK_S)
                self._trouble.clear()
                await self._guard(self.handle_trouble)
            except TimeoutError:
                trouble_wait.cancel()
                await self._guard(self.tick)
            except asyncio.CancelledError:
                trouble_wait.cancel()
                raise

    async def _guard(self, fn: Callable[[], Awaitable[None]]) -> None:
        try:
            await fn()
        except Exception:
            log.exception("egress: сбой в %s", getattr(fn, "__name__", fn))

    # --- реакции

    async def handle_trouble(self) -> None:
        """Детектор поймал ошибки на текущем маршруте."""
        async with self._lock:
            if self.route == DIRECT:
                self.direct_ok = False
                self.direct_streak = 0
            else:
                self.bans[self.route] = self._clock() + BAN_S
            await self._evaluate("сбои запросов")
            self.detector.reset()

    async def tick(self) -> None:
        async with self._lock:
            if self.route != DIRECT:
                if await self._probe_direct():
                    self.direct_streak += 1
                    self.direct_ok = True
                else:
                    self.direct_streak = 0
                    self.direct_ok = False
            else:
                # Тихий бот (нет успешных запросов дольше тика) — проверяем
                # direct сами, а не верим отсутствию ошибок.
                idle = self._clock() - self.detector.last_success >= TICK_S
                self.direct_ok = not self.detector.troubled and (
                    not idle or await self._probe_direct()
                )
                self.direct_streak = DIRECT_RETURN_STREAK if self.direct_ok else 0
            await self._evaluate("плановая проверка")

    async def _evaluate(self, why: str) -> None:
        try:
            raw = await self._fetch()
        except Exception:
            log.warning("egress: не удалось получить кандидатов", exc_info=True)
            raw = []
        now = self._clock()
        dec = choose_route(
            current=self.route,
            direct_ok=self.direct_ok,
            direct_streak=self.direct_streak,
            candidates=raw,
            bans=self.bans,
            now=now,
            prefer_direct=self.cfg.prefer_direct,
            extra_proxies=self.cfg.extra_proxies,
        )
        cands = normalize_candidates(raw, self.cfg.extra_proxies)
        if dec.changed:
            await self._switch(dec, cands, why)
        else:
            self._refresh_meta(cands)

    def _refresh_meta(self, cands: list[Candidate]) -> None:
        for c in cands:
            if c.route == self.route:
                self.label, self.ms = c.label, c.ms

    async def _switch(self, dec: Decision, cands: list[Candidate], why: str) -> None:
        old = self.route
        apply_route(self.bot.session, dec.route)
        self.route = dec.route
        self.since = self._clock()
        if dec.route == DIRECT:
            self.label, self.ms = DIRECT, None
        else:
            self.label, self.ms = dec.route, None
            self._refresh_meta(cands)
        self.detector.reset()
        self._persist()
        log.warning("egress: %s → %s (%s; %s)", old, dec.route, why, dec.reason)
        details = [
            {"route": DIRECT, "label": DIRECT, "ok": self.direct_ok, "ms": None},
            *[
                {
                    "route": c.route,
                    "label": c.label,
                    "ok": c.valid and self.bans.get(c.route, 0.0) <= self._clock(),
                    "ms": c.ms,
                }
                for c in cands
            ],
        ]
        await self._fire_async(self.on_switched, old, dec.route, f"{why}: {dec.reason}", details)

    # --- вспомогательное

    def _persist(self) -> None:
        save_state(
            self._state_path,
            {"route": self.route, "label": self.label, "ms": self.ms, "since": self.since},
        )

    def state(self) -> dict:
        """Для карточки ноды: ``route``, ``label``, ``ms``, ``since``."""
        return {"route": self.route, "label": self.label, "ms": self.ms, "since": self.since}

    def _fire(self, cb: Callable[..., Any] | None, *args: Any) -> None:
        if cb is None:
            return
        try:
            res = cb(*args)
            if inspect.isawaitable(res):
                asyncio.ensure_future(res)
        except Exception:
            log.exception("egress: колбэк упал")

    async def _fire_async(self, cb: Callable[..., Any] | None, *args: Any) -> None:
        if cb is None:
            return
        try:
            res = cb(*args)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("egress: колбэк упал")

    async def _default_probe_direct(self) -> bool:
        """Один прямой ``getMe`` через отдельную короткоживущую direct-сессию."""
        session = AiohttpSession()
        try:
            await asyncio.wait_for(
                session.make_request(self.bot, GetMe(), timeout=int(DIRECT_PROBE_TIMEOUT_S)),
                timeout=DIRECT_PROBE_TIMEOUT_S + 2,
            )
            return True
        except Exception:
            return False
        finally:
            try:
                await session.close()
            except Exception:  # pragma: no cover
                pass
