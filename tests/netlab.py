"""Управляемая сеть для тестов живучести (IMPLEMENTATION_PLAN, этап 25 п. 1).

Повод: за ночь 2026-07-28 вышло пять релизов, два из которых чинили
регрессии предыдущих — правки живучести линков проверялись по логам прода,
потому что полумёртвый TCP негде было воспроизвести. Этот прокси даёт
настоящий TCP-путь клиент → прокси → сервер, отказы которого управляются
из теста детерминированно и за миллисекунды.

Режимы:
- ``freeze()`` — перестать пересылать в обе стороны, НЕ закрывая сокеты:
  настоящий half-open (соединение «живо», ответы не приходят) — то, что не
  видит ни TCP, ни ``wait_closed()``;
- ``unfreeze()`` — возобновить пересылку (застрявшее доезжает);
- ``drop_silently()`` — «машину выдернули»: пересылка замирает навсегда,
  FIN никому не отправляется, новые подключения отвергаются;
- ``restore()`` — «машина вернулась»: слушатель поднимается на том же порту;
- ``delay_s`` — задержка пересылки (медленная сеть).
"""

from __future__ import annotations

import asyncio
import contextlib


class NetLabProxy:
    """TCP-прокси 127.0.0.1:<порт> → target с управляемыми отказами."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self._server: asyncio.Server | None = None
        # Событие «пересылка разрешена»: freeze снимает его, и насосы обеих
        # направлений замирают ПЕРЕД чтением — данные копятся в буферах ядра,
        # как при настоящем зависшем собеседнике.
        self._resume = asyncio.Event()
        self._resume.set()
        self._pumps: set[asyncio.Task] = set()
        self._writers: list[asyncio.StreamWriter] = []
        self.delay_s: float = 0.0
        self.port: int = 0

    @property
    def endpoint(self) -> str:
        return f"tcp://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, host="127.0.0.1", port=self.port or 0
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        await self._close_listener()
        for task in list(self._pumps):
            task.cancel()
        if self._pumps:
            await asyncio.gather(*self._pumps, return_exceptions=True)
        self._pumps.clear()
        for writer in self._writers:
            with contextlib.suppress(Exception):
                writer.close()
        self._writers.clear()

    def freeze(self) -> None:
        self._resume.clear()

    def unfreeze(self) -> None:
        self._resume.set()

    async def drop_silently(self) -> None:
        """Изобразить внезапную смерть машины: без FIN/RST, порт закрыт."""
        self.freeze()
        await self._close_listener()

    async def restore(self) -> None:
        """Машина «включилась обратно»: тот же порт, пересылка работает."""
        self.unfreeze()
        if self._server is None:
            await self.start()

    async def _close_listener(self) -> None:
        if self._server is not None:
            # Без wait_closed(): с Python 3.12 он ждёт закрытия ВСЕХ принятых
            # соединений, а наши нарочно полумёртвые — повис бы сам стенд.
            # close() убирает слушающий сокет сразу, порт освобождается.
            self._server.close()
            self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(*self._target)
        except OSError:
            writer.close()
            return
        self._writers += [writer, up_writer]
        for src, dst in ((reader, up_writer), (up_reader, writer)):
            task = asyncio.create_task(self._pump(src, dst))
            self._pumps.add(task)
            task.add_done_callback(self._pumps.discard)

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                await self._resume.wait()
                data = await reader.read(65536)
                if not data:
                    break
                # freeze мог случиться во время чтения — прочитанное держим
                # у себя, а не досылаем «последний кусок».
                await self._resume.wait()
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            # EOF/обрыв одной стороны честно доносится до другой (FIN);
            # молчаливая смерть — это freeze, сюда она не доходит.
            with contextlib.suppress(Exception):
                writer.close()
