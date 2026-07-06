"""MonitorLink — подключение бота к монитору с автопереподключением.

Бот держит ровно одно подключение к своей локальной ноде (пока — напрямую к
монитору). Обрыв не валит бота: фоновая задача переподключается с паузой, а
запросы в этот момент получают MonitorUnavailableError — хендлеры отвечают
человеку «монитор недоступен». Pending-алерты монитор досылает сам (у него
handled=False, пока не было ни одного клиента).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from sa_home_bot.proto.client import EventCallback, ProtoClient
from sa_home_bot.proto.messages import Address, ProtoError

log = logging.getLogger(__name__)

RECONNECT_DELAY_S = 5.0


class MonitorUnavailableError(RuntimeError):
    """Нет живого соединения с монитором."""


class MonitorLink:
    def __init__(
        self,
        socket_path: str | Path,
        *,
        on_event: EventCallback | None = None,
        reconnect_delay: float = RECONNECT_DELAY_S,
    ) -> None:
        self._path = Path(socket_path)
        self._on_event = on_event
        self._delay = reconnect_delay
        self._client: ProtoClient | None = None
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="monitor-link")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # --- Запросы к монитору ---

    async def get_state(self) -> dict[str, Any]:
        return await self._request_state()

    async def command(self, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        client = self._require_client()
        try:
            return await client.command(action, args)
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise MonitorUnavailableError(str(exc)) from exc

    async def _request_state(self) -> dict[str, Any]:
        client = self._require_client()
        try:
            return await client.get_state()
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise MonitorUnavailableError(str(exc)) from exc

    def _require_client(self) -> ProtoClient:
        client = self._client
        if client is None:
            raise MonitorUnavailableError(f"нет соединения с монитором ({self._path})")
        return client

    # --- Фоновое переподключение ---

    async def _run(self) -> None:
        logged_down = False
        while True:
            client = ProtoClient(
                self._path,
                src=Address(service="telegram-bot"),
                on_event=self._on_event,
            )
            try:
                await client.connect()
                info = await client.hello()
                log.info(
                    "Связь с монитором установлена: %s/%s v%s",
                    info.node,
                    info.service,
                    info.version,
                )
                logged_down = False
                self._client = client
                try:
                    await client.join()
                finally:
                    self._client = None
                log.warning("Связь с монитором потеряна, переподключение...")
            except (ConnectionError, OSError, TimeoutError, ProtoError) as exc:
                if not logged_down:
                    log.warning(
                        "Монитор недоступен (%s) — переподключение каждые %.0f с",
                        exc,
                        self._delay,
                    )
                    logged_down = True
                else:
                    log.debug("Монитор всё ещё недоступен: %s", exc)
            finally:
                self._client = None
                await client.close()
            await asyncio.sleep(self._delay)
