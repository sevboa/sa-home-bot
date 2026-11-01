from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import aiohttp

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.queue import Job, Message
from sa_home_bot.devlab.hubhttp import HubHttp

UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300)
ERROR_SNIPPET_CHARS = 300


class HubRequestError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def permanent(self) -> bool:
        return self.status is not None and 400 <= self.status < 500


class ExecutorHub(HubHttp):
    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._session.post(
                self._url + path, json=body, headers=self._headers
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise HubRequestError(
                        f"{path}: HTTP {resp.status} {text[:ERROR_SNIPPET_CHARS]}", resp.status
                    )
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise HubRequestError(f"{path}: {type(exc).__name__}") from exc

    async def heartbeat_queued(self, body: dict[str, Any]) -> int:
        data = await self._post(protocol.HTTP_HEARTBEAT_PATH, body)
        return int(data.get(protocol.FIELD_QUEUED, 0))

    async def claim(self, kinds: list[str], models: Sequence[str] = ()) -> Job | None:
        data = await self._post(
            protocol.HTTP_CLAIM_PATH,
            {protocol.FIELD_MODELS: list(models), protocol.FIELD_KINDS: kinds},
        )
        job = data.get(protocol.FIELD_JOB)
        return Job(**job) if job else None

    async def catalog(self) -> list[dict[str, Any]]:
        data = await self._post(protocol.HTTP_CATALOG_PATH, {})
        return data[protocol.FIELD_MODELS]

    async def post(self, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        await self._post(
            protocol.HTTP_POST_PATH,
            {
                protocol.FIELD_JOB_ID: job_id,
                protocol.FIELD_KIND: kind,
                protocol.FIELD_PAYLOAD: payload,
            },
        )

    async def poll(self, since: int) -> tuple[list[Message], int]:
        data = await self._post(protocol.HTTP_POLL_PATH, {protocol.FIELD_SINCE: since})
        messages = [Message(**message) for message in data[protocol.FIELD_MESSAGES]]
        return messages, data[protocol.FIELD_CURSOR]

    async def upload_log(self, job_id: str, name: str, data: bytes) -> None:
        try:
            async with self._session.put(
                self._url + protocol.HTTP_LOG_PATH,
                params={protocol.FIELD_NAME: name},
                data=data,
                headers={**self._headers, protocol.HEADER_JOB: job_id},
                timeout=UPLOAD_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise HubRequestError(
                        f"лог {name}: HTTP {resp.status} {text[:ERROR_SNIPPET_CHARS]}"
                    )
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise HubRequestError(f"лог {name}: {type(exc).__name__}") from exc
