"""Этап 52: сборка автовыбора маршрута до Bot API в процессе бота.

``bot/egress.py`` — сама логика (выбор, детектор, менеджер) и ничего не
оповещает; здесь — то, что связывает её с остальным ботом: стартовый маршрут,
оповещения админов о переключении (с антиспамом) и флаш outbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from collections.abc import Callable
from pathlib import Path

from aiogram import Bot

from sa_home_bot.bot.egress import DIRECT, apply_route, load_saved_state, socks_url
from sa_home_bot.bot.notifier import Notifier, notify_admins
from sa_home_bot.bot.outbox import flush_outbox
from sa_home_bot.bot.telegram_retry import REQUEST_TIMEOUT_S
from sa_home_bot.config import TelegramConfig
from sa_home_bot.db.store import Store

log = logging.getLogger(__name__)

EGRESS_STATE_PATH = Path("./data/egress.json")
NOTICE_MIN_INTERVAL_S = 10 * 60.0
OUTBOX_FLUSH_INTERVAL_S = 60.0


def startup_routes(cfg: TelegramConfig, state_path: Path = EGRESS_STATE_PATH) -> list[str]:
    """Порядок проб на старте в режиме ``auto``: последний рабочий маршрут,
    затем ``proxy`` из конфига, direct, ``extra_proxies``. Рой на старте ещё
    не опрошен (node_link поднимается позже), поэтому только то, что известно
    локально — ``extra_proxies`` стоит держать с SOCKS обеих VPS."""
    saved = load_saved_state(state_path)
    order = [
        saved["route"] if saved else "",
        socks_url(cfg.proxy) if cfg.proxy else DIRECT,
        DIRECT,
        *(socks_url(p) for p in cfg.extra_proxies if p),
    ]
    out: list[str] = []
    for route in order:
        if route and route not in out:
            out.append(route)
    return out


async def pick_startup_route(bot: Bot, routes: list[str]) -> str | None:
    """Первый маршрут, на котором прошёл ``getMe`` (одна попытка на маршрут).
    ``None`` — не прошёл ни один; сессия остаётся на первом, дальше вызывающий
    делает обычный ``call_with_network_retry``."""
    for route in routes:
        apply_route(bot.session, route)
        try:
            await bot.get_me(request_timeout=REQUEST_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — любой сбой = маршрут не годен
            log.warning("egress: на старте %s не отвечает: %s", route, exc)
            continue
        if route != routes[0]:
            log.warning("egress: на старте выбран %s", route)
        return route
    if routes:
        apply_route(bot.session, routes[0])
    return None


def _route_name(route: str, label: str | None = None) -> str:
    if route == DIRECT:
        return "напрямую"
    return f"через {html.escape(label or route)}"


def render_switch(old: str, new: str, reason: str, candidates: list[dict]) -> str:
    label = {c["route"]: c.get("label") for c in candidates}
    if new == DIRECT:
        head = "📡 Telegram снова доступен напрямую — вернулся с прокси."
    elif old == DIRECT:
        head = f"📡 Telegram недоступен напрямую → переключился {_route_name(new, label.get(new))}."
    else:
        head = f"📡 Сменил прокси до Telegram → {_route_name(new, label.get(new))}."
    lines = [head, f"Причина: {html.escape(reason)}"]
    rows = []
    for c in candidates:
        if c["route"] == DIRECT:
            name = "direct"
        else:
            name = html.escape(str(c.get("label") or c["route"]))
        mark = "✅" if c.get("ok") else "✗"
        ms = f" {c['ms']} мс" if c.get("ms") is not None else ""
        rows.append(f"  {mark} {name}{ms}")
    if rows:
        lines += ["Кандидаты:", *rows]
    return "\n".join(lines)


class SwitchNotices:
    """Оповещения админов о переключении: не чаще ``NOTICE_MIN_INTERVAL_S``;
    подавленные переключения сворачиваются в строку следующего оповещения."""

    def __init__(
        self, book, notifier: Notifier, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._book = book
        self._notifier = notifier
        self._clock = clock
        self._last_sent: float | None = None
        self._suppressed = 0

    async def on_switched(self, old: str, new: str, reason: str, candidates: list[dict]) -> None:
        now = self._clock()
        if self._last_sent is not None and now - self._last_sent < NOTICE_MIN_INTERVAL_S:
            self._suppressed += 1
            log.info("egress: оповещение о переключении подавлено (антиспам)")
            return
        text = render_switch(old, new, reason, candidates)
        if self._suppressed:
            text += f"\n\nМаршрут менялся ещё {self._suppressed} раз(а) с прошлого оповещения."
        self._last_sent = now
        self._suppressed = 0
        await notify_admins(self._book, self._notifier, text)


class OutboxFlusher:
    """Флаш outbox раз в минуту и сразу по восстановлению связи."""

    def __init__(self, notifier: Notifier, store: Store) -> None:
        self._notifier = notifier
        self._store = store
        self._task: asyncio.Task | None = None

    async def flush(self) -> None:
        try:
            sent = await flush_outbox(self._notifier, self._store)
        except Exception:  # noqa: BLE001 — флаш не должен ронять бота
            log.warning("outbox: флаш упал", exc_info=True)
            return
        if sent:
            log.info("outbox: доставлено из очереди %d", sent)

    def kick(self) -> None:
        asyncio.ensure_future(self.flush())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(OUTBOX_FLUSH_INTERVAL_S)
            await self.flush()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="outbox-flush")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
