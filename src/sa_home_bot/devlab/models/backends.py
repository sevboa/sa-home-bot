from __future__ import annotations

import abc
from typing import Any

import aiohttp

from sa_home_bot.config import DevlabModelConfig
from sa_home_bot.devlab import protocol

UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300)
BEARER = "Bearer "


class ModelBackend(abc.ABC):
    def __init__(self, name: str, spec: DevlabModelConfig) -> None:
        self.name = name
        self.spec = spec

    @abc.abstractmethod
    async def chat(self, payload: dict[str, Any]) -> aiohttp.ClientResponse: ...

    async def close(self) -> None:
        return None


class OpenAiBackend(ModelBackend):
    def __init__(self, name: str, spec: DevlabModelConfig) -> None:
        super().__init__(name, spec)
        self._session = aiohttp.ClientSession(timeout=UPSTREAM_TIMEOUT)

    async def chat(self, payload: dict[str, Any]) -> aiohttp.ClientResponse:
        headers = {"Content-Type": "application/json"}
        if self.spec.api_key:
            headers["Authorization"] = BEARER + self.spec.api_key
        url = self.spec.base_url.rstrip("/") + "/chat/completions"
        return await self._session.post(
            url,
            json={**payload, "model": self.spec.model},
            headers=headers,
            proxy=self.spec.proxy or None,
        )

    async def close(self) -> None:
        await self._session.close()


BACKENDS: dict[str, type[ModelBackend]] = {protocol.MODEL_KIND_OPENAI: OpenAiBackend}


def build_backend(name: str, spec: DevlabModelConfig) -> ModelBackend:
    return BACKENDS[spec.kind](name, spec)
