from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

JSON_FETCH_ERRORS = (urllib.error.URLError, TimeoutError, OSError, ValueError)


def get_json(url: str, timeout: float, headers: Mapping[str, str] | None = None) -> Any:
    target = urllib.request.Request(url, headers=dict(headers)) if headers else url
    with urllib.request.urlopen(target, timeout=timeout) as response:
        return json.loads(response.read())


async def get_json_async(
    url: str, timeout: float, headers: Mapping[str, str] | None = None
) -> Any:
    return await asyncio.to_thread(get_json, url, timeout, headers)
