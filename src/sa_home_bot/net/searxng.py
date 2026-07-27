"""Клиент локального SearXNG — сетевой слой службы net.

Только ``urllib`` + ``asyncio.to_thread``, как в llm/ollama.py: httpx/aiohttp
в зависимостях проекта нет и заводить их ради одного GET незачем.

Ходим исключительно на свой SearXNG (по умолчанию 127.0.0.1:8888) — сами
страницы по найденным ссылкам НЕ скачиваем. Это осознанное ограничение
(LLM_INTEGRATION_PLAN.md §9): модели отдаётся ровно то, что вернул поисковый
движок, без похода бота на произвольные внешние сайты.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

# Сколько символов сниппета оставлять: полный текст SearXNG бывает в несколько
# абзацев, а результат целиком уезжает в контекст модели.
SNIPPET_LIMIT = 300


class SearxngError(RuntimeError):
    """SearXNG не ответил или ответил не тем."""


def _get_json_sync(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — только localhost
        return json.loads(resp.read())


def _search_url(base_url: str, query: str, language: str) -> str:
    params = urllib.parse.urlencode(
        {
            "q": query,
            # По умолчанию в settings.yml формат json выключен — если его не
            # включить явно, сюда прилетит HTML и json.loads упадёт на ValueError.
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
    """Top-N результатов ``{title, url, snippet}``.

    Пустой список — запрос отработал, но ничего не нашлось (это НЕ ошибка).
    SearxngError — SearXNG недоступен/ответил не JSON (например, format=json
    так и не включили в settings.yml).
    """
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
