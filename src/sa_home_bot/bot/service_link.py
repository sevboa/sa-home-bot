"""ServiceLink — подключение бота к локальной службе (monitor, node) по протоколу.

Одно подключение на службу. Обрыв не валит бота: фоновая задача
переподключается с паузой, а запросы в этот момент получают
ServiceUnavailableError — хендлеры отвечают человеку «служба недоступна».
Pending-алерты монитор досылает сам (у него handled=False, пока не было ни
одного клиента). После каждого подключения кэшируется describe — из него
фронтенд строит кнопки действий, даже если служба сейчас недоступна.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from sa_home_bot.proto.client import EventCallback, ProtoClient
from sa_home_bot.proto.messages import ActionSpec, Address, ProtoError, ServiceDescription

log = logging.getLogger(__name__)

RECONNECT_DELAY_S = 5.0


class ServiceUnavailableError(RuntimeError):
    """Нет живого соединения со службой."""


class ServiceLink:
    def __init__(
        self,
        socket_path: str | Path,
        *,
        display_name: str = "служба",
        on_event: EventCallback | None = None,
        reconnect_delay: float = RECONNECT_DELAY_S,
    ) -> None:
        self._path = Path(socket_path)
        self.display_name = display_name
        self._on_event = on_event
        self._delay = reconnect_delay
        self._client: ProtoClient | None = None
        self._task: asyncio.Task | None = None
        self._description: ServiceDescription | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def cached_description(self) -> ServiceDescription | None:
        """describe с последнего подключения (None, если ещё не подключались)."""
        return self._description

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._run(), name=f"service-link-{self.display_name}"
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # --- Запросы к службе ---

    async def get_state(self) -> dict[str, Any]:
        client = self._require_client()
        try:
            return await client.get_state()
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise ServiceUnavailableError(str(exc)) from exc

    async def command(self, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        client = self._require_client()
        try:
            return await client.command(action, args)
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise ServiceUnavailableError(str(exc)) from exc

    async def actions(self) -> tuple[ActionSpec, ...]:
        """Действия службы: живой describe, при недоступности — кэш или пусто."""
        client = self._client
        if client is not None:
            try:
                self._description = await client.describe()
            except (ConnectionError, OSError, TimeoutError, ProtoError):
                pass
        return self._description.actions if self._description is not None else ()

    def _require_client(self) -> ProtoClient:
        client = self._client
        if client is None:
            raise ServiceUnavailableError(
                f"нет соединения: {self.display_name} ({self._path})"
            )
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
                with contextlib.suppress(ProtoError):
                    self._description = await client.describe()
                log.info(
                    "Связь со службой установлена: %s/%s v%s",
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
                log.warning(
                    "Связь со службой %s потеряна, переподключение...", self.display_name
                )
            except (ConnectionError, OSError, TimeoutError, ProtoError) as exc:
                if not logged_down:
                    log.warning(
                        "Служба %s недоступна (%s) — переподключение каждые %.0f с",
                        self.display_name,
                        exc,
                        self._delay,
                    )
                    logged_down = True
                else:
                    log.debug("Служба %s всё ещё недоступна: %s", self.display_name, exc)
            finally:
                self._client = None
                await client.close()
            await asyncio.sleep(self._delay)
