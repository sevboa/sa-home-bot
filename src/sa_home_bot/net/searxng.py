from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

SNIPPET_LIMIT = 300


class SearxngError(RuntimeError):
    pass


def _get_json_sync(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read())


def _search_url(base_url: str, query: str, language: str) -> str:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            "language": language,
            "safesearch": "0",
        }
    )
    return f"{base_url.rstrip('/')}/search?{params}"


def _trim(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= SNIPPET_LIMIT:
        return text
    return text[:SNIPPET_LIMIT].rstrip() + "…"


async def search(
    base_url: str, query: str, *, limit: int, timeout: float, language: str = "ru"
) -> list[dict[str, str]]:
    url = _search_url(base_url, query, language)
    try:
        payload = await asyncio.to_thread(_get_json_sync, url, timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SearxngError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise SearxngError("неожиданный ответ SearXNG (не объект JSON)")

    results = []
    for item in payload.get("results", [])[:limit]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": _trim(str(item.get("title") or "")),
                "url": str(item.get("url") or ""),
                "snippet": _trim(str(item.get("content") or "")),
            }
        )
    return results
