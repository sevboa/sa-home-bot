from __future__ import annotations

import logging
import socket
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.config import Settings
from sa_home_bot.net import searxng
from sa_home_bot.net.protocol import ACTION_SEARCH, SERVICE_NAME
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)

log = logging.getLogger(__name__)


class NetService:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings.net
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(ACTION_SEARCH,),
            actions=(
                ActionSpec(
                    id=ACTION_SEARCH,
                    title="🔎 Поиск в интернете",
                    params=(
                        ActionParam(name="query", type="string", title="Что искать"),
                    ),
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        try:
            await searxng.search(
                self._cfg.searxng_url,
                "ping",
                limit=1,
                timeout=self._cfg.probe_timeout_s,
            )
        except searxng.SearxngError as exc:
            available, detail = False, str(exc)
        else:
            available, detail = True, ""
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "searxng_url": self._cfg.searxng_url,
            "available": available,
            "error": detail,
        }

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action != ACTION_SEARCH:
            raise ValueError(f"необъявленное действие: {action}")
        query = str(args.get("query") or "").strip()
        if not query:
            raise ProtoError(ERR_BAD_REQUEST, "не указан поисковый запрос (query)")
        limit = self._cfg.max_results
        try:
            results = await searxng.search(
                self._cfg.searxng_url,
                query,
                limit=limit,
                timeout=self._cfg.request_timeout_s,
                language=self._cfg.language,
            )
        except searxng.SearxngError as exc:
            raise ProtoError(ERR_INTERNAL, f"поиск не удался: {exc}") from exc
        return {"query": query, "results": results, "count": len(results)}
