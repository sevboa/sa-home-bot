from __future__ import annotations

import asyncio
import contextlib


class NetLabProxy:
    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self._server: asyncio.Server | None = None
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
        self.freeze()
        await self._close_listener()

    async def restore(self) -> None:
        self.unfreeze()
        if self._server is None:
            await self.start()

    async def _close_listener(self) -> None:
        if self._server is not None:
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
                await self._resume.wait()
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
